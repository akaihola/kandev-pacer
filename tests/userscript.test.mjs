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

function queueClick({ sessions = sessionFixture, taskStatus = 200, sessionsStatus = 200, queueError,
  quickChat = false, pathname = `/tasks/${primary.task_id}`, sessionId = primary.id,
  delayReply = false, delayOpen = false, reply, beforeFetch, missingArea = false, unknownPanel = false,
  missingEditor = false, tiptap = true, task = { id: primary.task_id },
} = {}) {
  const sent = [], fetched = [], alerts = [], edits = [];
  const timers = new Map();
  let closed = false, socket, selection;
  // Real frontend ancestry: session-chat/quick-chat-content > chat-input-area > editor/button.
  function element(testId, parentElement = null) {
    return {
      parentElement, isConnected: true,
      attrs: { "data-testid": testId },
      getAttribute(name) { return this.attrs[name] ?? null; },
      closest(selector) {
        return selector.split(",").some((part) =>
          [...part.matchAll(/\[([^=\]]+)(?:=["']?([^"'\]]+)["']?)?\]/g)]
            .every(([, name, value]) => name in this.attrs && (!value || this.attrs[name] === value)),
        ) ? this : this.parentElement?.closest(selector) ?? null;
      },
    };
  }
  const document = {
    querySelectorAll: () => [], body: {}, activeElement: null,
    createRange: () => ({ selectNodeContents(target) { this.target = target; } }),
    getSelection: () => ({ removeAllRanges() { selection = null; }, addRange(range) { selection = range; } }),
    execCommand(command) {
      edits.push(command);
      if (command === "selectAll") selection = { target: document.activeElement };
      if (command === "delete" && selection?.target) selection.target.innerText = "";
      return true;
    },
  };
  function composer(isQuickChat, text) {
    const panel = element(isQuickChat ? "quick-chat-content" : "session-chat");
    if (!isQuickChat) panel.attrs["data-session-id"] = sessionId;
    const area = element("chat-input-area", panel);
    const editor = Object.assign(element(null, area), {
      innerText: text, querySelector: () => null,
      focus() { document.activeElement = this; },
    });
    if (tiptap) editor.editor = {
      getJSON: () => ({ type: "doc", content: [{ type: "paragraph", content: [{ type: "text", text: editor.innerText }] }] }),
      commands: { clearContent() { edits.push("clearContent"); editor.innerText = ""; return true; } },
    };
    area.querySelector = () => area.editor;
    area.editor = editor;
    return { panel, area, editor, button: Object.assign(element(null, area), { disabled: false }) };
  }
  const taskComposer = composer(false, "task draft");
  const quickComposer = composer(true, "quick chat draft");
  const selected = quickChat ? quickComposer : taskComposer;
  const { button, editor, area, panel } = selected;
  editor.innerText = "keep this prompt";
  if (missingArea) button.parentElement = null;
  if (unknownPanel) panel.attrs["data-testid"] = "unknown";
  if (missingEditor) area.editor = null;
  const ctx = load({
    location: { origin: "https://kandev.example.test:8449", pathname },
    document,
    setTimeout(callback) { const id = {}; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
    alert: (message) => alerts.push(message),
    fetch: async (url) => {
      fetched.push(url);
      await beforeFetch?.();
      const isSessions = url.endsWith("/sessions");
      const status = isSessions ? sessionsStatus : taskStatus;
      return { ok: status === 200, status, json: async () => isSessions ? sessions : task };
    },
    WebSocket: class {
      constructor(url) {
        socket = this;
        assert.equal(url, "wss://kandev.example.test:8449/ws");
        if (!delayOpen) queueMicrotask(() => this.onopen());
      }
      send(message) {
        sent.push(JSON.parse(message));
        if (!delayReply) queueMicrotask(() => this.respond());
      }
      respond(value = reply ?? {
        id: "us-queue", action: "message.queue.add", type: queueError ? "error" : "response",
        payload: queueError ? { message: queueError } : {
          id: "entry-1", task_id: sent[0].payload.task_id, session_id: sent[0].payload.session_id,
        },
      }) {
        this.onmessage({ data: typeof value === "string" ? value : JSON.stringify(value) });
      }
      close() { closed = true; this.onclose?.(); }
    },
  });
  return { ctx, ...selected, taskComposer, quickComposer, sent, fetched, alerts, edits, timers,
    get closed() { return closed; }, get socket() { return socket; } };
}

for (const pathname of [`/tasks/${primary.task_id}`, "/"]) {
  test(`Quick Chat refuses Queue and preserves both drafts at ${pathname}`, async () => {
    const click = queueClick({ quickChat: true, pathname });
    await click.ctx.onQueue(click.button);
    assert.deepEqual(click.sent, []);
    assert.deepEqual(click.fetched, []);
    assert.equal(click.editor.innerText, "keep this prompt");
    assert.equal(click.taskComposer.editor.innerText, "task draft");
    assert.match(click.alerts[0], /Quick Chat.*unavailable.*draft.*kept/i);
    assert.equal(click.button.disabled, false);
  });
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
  assert.deepEqual(click.edits, ["clearContent"]);
  assert.equal(click.editor.innerText, "");
  assert.equal(click.quickComposer.editor.innerText, "quick chat draft");
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
  assert.deepEqual(click.edits, ["clearContent"]);
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
  ["unknown panel", { unknownPanel: true }, /composer task session identity is unavailable/],
  ["missing composer", { missingArea: true }, /composer task session identity is unavailable/],
  ["missing editor", { missingEditor: true }, /composer task session identity is unavailable/],
  ["missing session attribute", { sessionId: "" }, /composer task session identity is unavailable/],
  ["missing task URL", { pathname: "/" }, /composer task session identity is unavailable/],
  ["conflicting task", { task: { id: "another-task" } }, /identity conflicts/],
  ["conflicting session task", { sessions: { sessions: [{ ...primary, task_id: "another-task" }] } }, /identity conflicts/],
  ["duplicate session", { sessions: { sessions: [primary, primary] } }, /identity is unavailable/],
  ["missing selected session", { sessionId: "missing" }, /identity is unavailable/],
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
    assert.equal(click.editor.innerText, "keep this prompt");
    assert.equal(click.button.disabled, false);
  });
}

for (const wrapped of [false, true]) {
  test(`targets the displayed secondary session, wrapped=${wrapped}`, async () => {
    const task = { id: primary.task_id, primary_session_id: primary.id };
    const click = queueClick({
      sessionId: "secondary",
      task: wrapped ? { task } : task,
      sessions: { sessions: [primary, { ...primary, id: "secondary", is_primary: false, queue_incarnation_id: "inc-secondary" }] },
    });
    await click.ctx.onQueue(click.button);
    assert.deepEqual(click.alerts, []);
    assert.equal(click.sent[0].payload.session_id, "secondary");
    assert.equal(click.sent[0].payload.session_incarnation_id, "inc-secondary");
  });
}

test("repeated Quick Chat clicks never create or submit anything", async () => {
  const click = queueClick({ quickChat: true });
  await Promise.all([click.ctx.onQueue(click.button), click.ctx.onQueue(click.button)]);
  assert.deepEqual(click.fetched, []);
  assert.deepEqual(click.sent, []);
  assert.equal(click.editor.innerText, "keep this prompt");
});

test("repeated clicks and a replacement button share the composer guard until admission", async () => {
  const click = queueClick({ delayReply: true });
  const pending = click.ctx.onQueue(click.button);
  await new Promise(setImmediate);
  await click.ctx.onQueue(click.button);
  await click.ctx.onQueue({ ...click.button, disabled: false });
  assert.equal(click.button.disabled, true);
  assert.equal(click.sent.length, 1);
  assert.equal(click.editor.innerText, "keep this prompt");
  click.socket.respond({ id: "unrelated", type: "event" });
  assert.equal(click.editor.innerText, "keep this prompt");
  click.socket.respond();
  await pending;
  assert.equal(click.editor.innerText, "");
  assert.equal(click.quickComposer.editor.innerText, "quick chat draft");
  assert.equal(click.button.disabled, false);
  assert.equal(click.timers.size, 0);
});

const contextChanges = [
  ["edited draft", (click) => { click.editor.innerText = "new draft"; }],
  ["changed session", (click) => { click.panel.attrs["data-session-id"] = "other-session"; }],
  ["navigation", (click) => { click.ctx.location.pathname = "/"; }],
  ["removed editor", (click) => { click.editor.isConnected = false; }],
  ["replacement editor", (click) => { click.area.editor = click.quickComposer.editor; }],
];
for (const [name, change] of contextChanges) {
  test(`refuses submission after ${name} while the socket connects`, async () => {
    const click = queueClick({ delayOpen: true });
    const pending = click.ctx.onQueue(click.button);
    await new Promise(setImmediate);
    change(click);
    click.socket.onopen();
    await pending;
    assert.deepEqual(click.sent, []);
    assert.deepEqual(click.edits, []);
    assert.match(click.alerts[0], /composer changed/);
    assert.equal(click.closed, true);
  });
  test(`preserves ${name} while admission is pending`, async () => {
    const click = queueClick({ delayReply: true });
    const pending = click.ctx.onQueue(click.button);
    await new Promise(setImmediate);
    change(click);
    const text = click.editor.innerText;
    click.socket.respond();
    await pending;
    assert.equal(click.editor.innerText, text);
    assert.equal(click.quickComposer.editor.innerText, "quick chat draft");
    assert.deepEqual(click.edits, []);
    assert.equal(click.sent.length, 1);
  });
  test(`refuses submission after ${name} during lookup`, async () => {
    const click = queueClick({ beforeFetch: () => change(click) });
    await click.ctx.onQueue(click.button);
    assert.deepEqual(click.sent, []);
    assert.deepEqual(click.edits, []);
    assert.match(click.alerts[0], /composer changed/);
  });
}

for (const [name, response] of [
  ["invalid JSON", "{"],
  ["wrong action", { action: "message.add" }],
  ["wrong type", { type: "event" }],
  ["missing entry", { payload: {} }],
  ["wrong task", { payload: { id: "q", task_id: "other", session_id: primary.id } }],
  ["wrong session", { payload: { id: "q", task_id: primary.task_id, session_id: "other" } }],
]) {
  test(`unconfirmed admission (${name}) preserves the draft`, async () => {
    const click = queueClick({ reply: typeof response === "string" ? response : {
      id: "us-queue", action: "message.queue.add", type: "response", ...response,
    } });
    await click.ctx.onQueue(click.button);
    assert.match(click.alerts[0], /admission unconfirmed/);
    assert.equal(click.editor.innerText, "keep this prompt");
    assert.deepEqual(click.edits, []);
    assert.equal(click.button.disabled, false);
    assert.equal(click.closed, true);
    assert.equal(click.timers.size, 0);
  });
}

for (const failure of ["error", "close", "timeout"]) {
  test(`socket ${failure} retains text and releases the guard`, async () => {
    const click = queueClick({ delayReply: true });
    const pending = click.ctx.onQueue(click.button);
    await new Promise(setImmediate);
    if (failure === "timeout") [...click.timers.values()][0]();
    else if (failure === "close") click.socket.close();
    else click.socket.onerror();
    await pending;
    assert.match(click.alerts[0], /admission unconfirmed/);
    assert.equal(click.editor.innerText, "keep this prompt");
    assert.equal(click.button.disabled, false);
    assert.equal(click.timers.size, 0);
    assert.equal(click.closed, true);
  });
}

test("plain editor clearing selects only the submitted content", async () => {
  const click = queueClick({ tiptap: false });
  click.quickComposer.editor.focus();
  await click.ctx.onQueue(click.button);
  assert.deepEqual(click.alerts, []);
  assert.deepEqual(click.edits, ["delete"]);
  assert.equal(click.editor.innerText, "");
  assert.equal(click.quickComposer.editor.innerText, "quick chat draft");
});

test("failed focus after admission cannot delete a different composer", async () => {
  const click = queueClick({ tiptap: false });
  click.quickComposer.editor.focus();
  click.editor.focus = () => {};
  await click.ctx.onQueue(click.button);
  assert.match(click.alerts[0], /^Prompt queued, but draft could not be cleared/);
  assert.equal(click.editor.innerText, "keep this prompt");
  assert.equal(click.quickComposer.editor.innerText, "quick chat draft");
  assert.deepEqual(click.edits, []);
});

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
    fetch: async () => ({ ok: true, json: async () => ({ id: "t", sessions: [{ id: "s", task_id: "t", is_primary: true }] }) }),
  });
  await assert.rejects(ctx.queueTarget("t", "s"), /task session identity is unavailable/);
});
