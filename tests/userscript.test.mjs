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
