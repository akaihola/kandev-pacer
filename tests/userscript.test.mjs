// Pins the userscript's mirror of kandev's composer pipeline (tiptap-helpers.ts
// getMarkdownText, use-message-handler.ts buildFinalMessage,
// expand-prompt-references.ts). Run via tests/test_userscript.py or
// `node --test tests/userscript.test.mjs`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const source = readFileSync(
  path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "userscript", "kandev-queue-button.user.js"),
  "utf8",
);

// vm-context objects live in another realm, whose Object.prototype fails
// deepStrictEqual's prototype check; a JSON round-trip re-homes them.
function local(value) {
  return JSON.parse(JSON.stringify(value));
}

// Identity fields from GET /api/v1/tasks/:id/sessions on gogo, 2026-09-21.
const sessionFixture = JSON.parse(readFileSync(new URL("./fixtures/queue/sessions.json", import.meta.url)));
const primary = sessionFixture.sessions[0];

function queueClick({ sessions = sessionFixture, taskStatus = 200, sessionsStatus = 200, queueError } = {}) {
  const sent = [], fetched = [], alerts = [], edits = [];
  let closed = false;
  const editor = { innerText: "keep this prompt", querySelector: () => null, focus() {} };
  const button = { disabled: false, closest: () => ({ querySelector: () => editor }) };
  const ctx = load({
    location: { origin: "https://kandev.example.test:8449", pathname: `/tasks/${primary.task_id}` },
    document: { querySelectorAll: () => [], body: {}, execCommand: (command) => edits.push(command) },
    alert: (message) => alerts.push(message),
    fetch: async (url) => {
      fetched.push(url);
      const isSessions = url.endsWith("/sessions");
      const status = isSessions ? sessionsStatus : taskStatus;
      return { ok: status === 200, status, json: async () => isSessions ? sessions : { id: primary.task_id } };
    },
    WebSocket: class {
      constructor(url) {
        assert.equal(url, "wss://kandev.example.test:8449/ws");
        queueMicrotask(() => this.onopen());
      }
      send(message) {
        sent.push(JSON.parse(message));
        queueMicrotask(() => this.onmessage({ data: JSON.stringify({
          id: "us-queue", type: queueError ? "error" : "response", payload: { message: queueError },
        }) }));
      }
      close() { closed = true; }
    },
  });
  return { ctx, button, sent, fetched, alerts, edits, get closed() { return closed; } };
}

test("Queue click maps the live REST identity and clears only after admission", async () => {
  const click = queueClick();
  await click.ctx.onQueue(click.button);
  assert.deepEqual(click.alerts, []);
  assert.deepEqual(click.fetched.sort(), [
    `/api/v1/tasks/${primary.task_id}`, `/api/v1/tasks/${primary.task_id}/sessions`,
  ]);
  assert.deepEqual(click.sent, [{
    id: "us-queue", type: "request", action: "message.queue.add",
    payload: {
      task_id: primary.task_id, session_id: primary.id,
      session_incarnation_id: primary.queue_incarnation_id, content: "keep this prompt",
    },
  }]);
  assert.deepEqual(click.edits, ["selectAll", "delete"]);
  assert.equal(click.button.disabled, false);
  assert.equal(click.closed, true);
});

test("Queue accepts identity from a task-scoped session response without task_id", async () => {
  const click = queueClick({ sessions: { sessions: [{
    id: primary.id,
    queue_incarnation_id: primary.queue_incarnation_id,
    is_primary: true,
  }] } });
  await click.ctx.onQueue(click.button);
  assert.deepEqual(click.alerts, []);
  assert.equal(click.sent[0].payload.session_id, primary.id);
  assert.equal(click.sent[0].payload.session_incarnation_id, primary.queue_incarnation_id);
  assert.deepEqual(click.edits, ["selectAll", "delete"]);
});

test("Queue rejection keeps the composer and restores the button", async () => {
  const click = queueClick({ queueError: "stale session incarnation" });
  await click.ctx.onQueue(click.button);
  assert.deepEqual(click.alerts, ["Queue failed: stale session incarnation"]);
  assert.deepEqual(click.edits, []);
  assert.equal(click.sent.length, 1);
  assert.equal(click.closed, true);
  assert.equal(click.button.disabled, false);
});

for (const [name, options, error] of [
  ["task HTTP error", { taskStatus: 404 }, /task lookup failed \(HTTP 404\)/],
  ["sessions HTTP error", { sessionsStatus: 503 }, /session lookup failed \(HTTP 503\)/],
  ["missing sessions", { sessions: {} }, /invalid task sessions response/],
  ["malformed sessions", { sessions: { sessions: {} } }, /invalid task sessions response/],
  ["null session", { sessions: { sessions: [null] } }, /task session identity is unavailable/],
  ["no sessions", { sessions: { sessions: [] } }, /task session identity is unavailable/],
  ...[undefined, "", " ", 7, {}].map((value) => [
    `invalid incarnation ${JSON.stringify(value)}`,
    { sessions: { sessions: [{ ...primary, queue_incarnation_id: value }] } },
    /task session identity is unavailable/,
  ]),
  ["invalid session id", { sessions: { sessions: [{ ...primary, id: {} }] } }, /task session identity is unavailable/],
  ["invalid primary with valid secondary", { sessions: { sessions: [
    { ...primary, queue_incarnation_id: null },
    { ...primary, id: "secondary", is_primary: false },
  ] } }, /task session identity is unavailable/],
]) {
  test(`Queue refuses ${name} without changing queue or composer`, async () => {
    const click = queueClick(options);
    await click.ctx.onQueue(click.button);
    assert.equal(click.alerts.length, 1);
    assert.match(click.alerts[0], error);
    assert.deepEqual(click.sent, []);
    assert.deepEqual(click.edits, []);
    assert.equal(click.button.disabled, false);
  });
}

for (const wrapped of [false, true]) {
  for (const primaryId of ["older", undefined]) {
    test(`session selection preserves task primary and first-session fallback, wrapped=${wrapped}, primary=${primaryId}`, async () => {
      const ctx = load({
        fetch: async (url) => ({
          ok: true,
          json: async () => {
            const task = { primary_session_id: primaryId };
            return url.endsWith("/sessions") ? { sessions: [
              { ...primary, id: "newer", is_primary: false },
              { ...primary, id: "older", is_primary: false },
            ] } : wrapped ? { task } : task;
          },
        }),
      });
      assert.equal((await ctx.queueTarget(primary.task_id)).sessionId, primaryId ?? "newer");
    });
  }
}

function load(overrides = {}) {
  const context = {
    document: { querySelectorAll: () => [], body: {} },
    MutationObserver: class {
      observe() {}
    },
    location: { origin: "http://gogo:38429", pathname: "/" },
    fetch: async () => {
      throw new Error("fetch not stubbed");
    },
    ...overrides,
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  return context;
}

const heartbeatChip = {
  type: "contextMention",
  attrs: { id: "p1", label: "heartbeat", kind: "prompt", path: "prompt:p1" },
};

test("serializes chips, code and breaks like getMarkdownText", () => {
  const ctx = load();
  const doc = {
    type: "doc",
    content: [
      {
        type: "paragraph",
        content: [
          heartbeatChip,
          { type: "text", text: " and pick " },
          { type: "text", text: "070", marks: [{ type: "code" }] },
          { type: "hardBreak" },
          { type: "slashCommand", attrs: { label: "/review", commandName: "review" } },
          { type: "text", text: " " },
          {
            type: "entityReference",
            attrs: {
              version: 1,
              ref: "jira:KAN-7",
              provider: "jira",
              kind: "issue",
              id: "7",
              key: "KAN-7",
              title: "Fix login",
              url: "https://jira/browse/KAN-7 (1)",
              scope: "board",
            },
          },
        ],
      },
      { type: "codeBlock", attrs: { language: "py" }, content: [{ type: "text", text: "x = 1" }] },
    ],
  };
  assert.equal(
    ctx.markdownText(doc),
    "@heartbeat and pick `070`\n/review [#KAN-7](https://jira/browse/KAN-7%20%281%29)\n```py\nx = 1\n```",
  );
});

test("expands a prompt chip into CONTEXT PROMPTS with nested references, cycles cut", () => {
  const ctx = load();
  const prompts = [
    { id: "p1", name: "heartbeat", content: "Do the thing. See @style and @heartbeat." },
    { id: "p2", name: "style", content: "Terse. @heartbeat back-reference." },
  ];
  assert.equal(
    ctx.contextPromptsBlock([heartbeatChip.attrs], prompts),
    "\n\n<kandev-system>\n" +
      "CONTEXT PROMPTS: The user has included the following prompt instructions as context:\n" +
      "### heartbeat\nDo the thing. See @style and @heartbeat.\n\n" +
      "EXPANDED PROMPT REFERENCES: The message above references saved prompts by @name. " +
      "Use these expansions as hidden context while preserving the original @mentions.\n\n" +
      "### @style\nTerse. @heartbeat back-reference.\n" +
      "</kandev-system>",
  );
});

test("buildPayload queues what the stock submit would have sent", async () => {
  const fetched = [];
  const ctx = load({
    fetch: async (url) => {
      fetched.push(url);
      if (url === "/api/v1/prompts")
        return { json: async () => ({ prompts: [{ id: "p1", name: "heartbeat", content: "Do the thing." }] }) };
      if (url === "/api/v1/workflow/steps/ws1") return { json: async () => ({ id: "ws1", name: "Review" }) };
      throw new Error(`unexpected fetch ${url}`);
    },
  });
  const editor = {
    editor: {
      getJSON: () => ({
        type: "doc",
        content: [
          {
            type: "paragraph",
            content: [
              heartbeatChip,
              { type: "text", text: " for " },
              { type: "contextMention", attrs: { label: "README.md", kind: "file", path: "docs/README.md" } },
              { type: "text", text: " and " },
              {
                type: "contextMention",
                attrs: {
                  label: "Ship it",
                  kind: "task",
                  path: "task:t1",
                  taskId: "t1",
                  workflowId: "w1",
                  workflowStepId: "ws1",
                  taskState: "IN_PROGRESS",
                },
              },
            ],
          },
        ],
      }),
    },
  };
  const payload = await ctx.buildPayload(editor);
  assert.equal(
    payload.content,
    "@heartbeat for @README.md and @Ship it" +
      "\n\n<kandev-system>\nCONTEXT PATHS: The user has attached the following file and directory paths " +
      "as context. Inspect these paths to understand what the user is referring to:\n" +
      "- file: docs/README.md\n</kandev-system>" +
      "\n\n<kandev-system>\nCONTEXT PROMPTS: The user has included the following prompt instructions as context:\n" +
      "### heartbeat\nDo the thing.\n</kandev-system>" +
      "\n\n<kandev-system>\nREFERENCED TASKS: The user mentioned the following tasks. Use these IDs with " +
      "the kandev MCP tools (e.g. `get_task_conversation_kandev`, `update_task_kandev`, " +
      "`get_task_plan_kandev`) when the user asks you to act on them.\n" +
      "- Ship it (id: t1, workflow_id: w1, step: Review, state: IN_PROGRESS)\n</kandev-system>",
  );
  assert.deepEqual(local(payload.context_files), [{ path: "docs/README.md", name: "README.md" }]);
  assert.equal(payload.entity_references, undefined);
  assert.deepEqual(fetched.sort(), ["/api/v1/prompts", "/api/v1/workflow/steps/ws1"]);
});

test("plain text needs no document and no fetch", async () => {
  const ctx = load();
  const editor = {
    editor: { getJSON: () => ({ type: "doc", content: [{ type: "paragraph", content: [{ type: "text", text: "just words" }] }] }) },
  };
  assert.deepEqual(local(await ctx.buildPayload(editor)), { content: "just words" });
});

test("queues with the current primary session incarnation", async () => {
  const sent = [];
  const ctx = load({
    WebSocket: class {
      constructor() { this.readyState = 1; queueMicrotask(() => this.onopen()); }
      send(message) {
        sent.push(JSON.parse(message));
        queueMicrotask(() => this.onmessage({ data: JSON.stringify({ id: "us-queue", type: "response" }) }));
      }
      close() {}
    },
    fetch: async (url) => ({
      ok: true,
      json: async () => url.endsWith("/sessions")
        ? { sessions: [{ id: "stale", task_id: "task-7", queue_incarnation_id: "old", is_primary: false }, { id: "current", task_id: "task-7", queue_incarnation_id: "inc-7", is_primary: true }] }
        : { task: { id: "task-7", primary_session_id: "stale" } },
    }),
  });
  assert.deepEqual(local(await ctx.queueTarget("task-7")), {
    sessionId: "current",
    sessionIncarnationId: "inc-7",
  });
  await ctx.queueMessage("task-7", "current", "inc-7", { content: "queued" });
  assert.deepEqual(local(sent[0].payload), {
    task_id: "task-7",
    session_id: "current",
    session_incarnation_id: "inc-7",
    content: "queued",
  });
});

test("valid entity references ride along; duplicates and malformed ones dropped", async () => {
  const ctx = load();
  const ref = {
    version: 1,
    ref: "jira:KAN-7",
    provider: "jira",
    kind: "issue",
    id: "7",
    key: null,
    title: "Fix login",
    url: "https://jira/browse/KAN-7",
    scope: "board",
  };
  const editor = {
    editor: {
      getJSON: () => ({
        type: "doc",
        content: [
          {
            type: "paragraph",
            content: [
              { type: "entityReference", attrs: ref },
              { type: "entityReference", attrs: ref },
              { type: "entityReference", attrs: { version: 1, ref: "broken" } },
            ],
          },
        ],
      }),
    },
  };
  const payload = await ctx.buildPayload(editor);
  const { key: _key, ...expected } = ref;
  assert.deepEqual(local(payload.entity_references), [expected]);
});

test("refuses innerText when the document is unreadable and a chip is present", async () => {
  const ctx = load();
  const chipEditor = { querySelector: () => ({}), innerText: "heartbeat mangled" };
  await assert.rejects(() => ctx.buildPayload(chipEditor), /mention chip would be mangled/);
  const plainEditor = { querySelector: () => null, innerText: "  plain fallback  " };
  assert.deepEqual(local(await ctx.buildPayload(plainEditor)), { content: "plain fallback" });
});

test("missing queue incarnation rejects before admission", async () => {
  const ctx = load({
    fetch: async () => ({ ok: true, json: async () => ({ sessions: [{ id: "s", task_id: "t", is_primary: true }] }) }),
  });
  await assert.rejects(ctx.queueTarget("t"), /task session identity is unavailable/);
});
