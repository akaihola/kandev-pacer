// ==UserScript==
// @name         Kandev: Queue instead of Send
// @description  Adds a "Queue" button beside the composer's submit button. Parks the
//               typed prompt on the session's message queue instead of starting a turn,
//               so kandev-pacer decides when the quota gets spent.
// @version      1.1.4
// @include      /^[^:]*?:\/\/[^\/]*?:(38429|8449)\/.*?$/
// @grant        none
// @run-at       document-idle
// ==/UserScript==
//
// Why this exists, and what drains the queue, is in docs/queueing-user-prompts.md.
//
// A drain replays the stored content verbatim (message.queue.drain → PromptTask);
// nothing re-expands it later. So the entry must carry everything the stock submit
// would have sent, and this mirrors kandev's own composer pipeline: serialization
// from apps/web/components/task/chat/tiptap-helpers.ts (getMarkdownText), context
// blocks from apps/web/hooks/use-message-handler.ts (buildFinalMessage), prompt
// expansion from apps/web/lib/prompts/expand-prompt-references.ts. Tab-completed
// @-mention chips become @label plus their CONTEXT PATHS / CONTEXT PROMPTS /
// REFERENCED TASKS <kandev-system> block, #-reference chips become markdown links
// (and ride along as entity_references), /-command chips become /name.
//
// Limitations, all deliberate to keep it small: no attachments; context that lives
// outside the composer document is not carried over (the context popover's chips,
// the active side-by-side document, plan mode, a model override); targets the
// task's primary session, so it queues to the wrong session if you have switched
// the sessions dropdown; and queueing while a turn is running is pointless —
// Kandev's own submit button already queues then, and the entry gets drained at
// turn end either way.
//
// Queueing to the primary session is safe even though the pacer resumes the newest:
// the pacer reads message.queue.get for *every* idle session and drains whichever
// one came back non-empty. See "The queue is not necessarily on the newest session"
// in the README.
"use strict";

// ── Composer serialization, mirroring tiptap-helpers.ts ─────────────

function slashCommandLabel(attrs) {
  const name = [attrs.label, attrs.commandName]
    .map((v) => (typeof v === "string" ? v.trim().replace(/^\/+/, "") : ""))
    .find(Boolean);
  return name ? `/${name}` : "";
}

function entityReferenceMarkdown(attrs) {
  const label = String(attrs.key || attrs.title || "");
  const url = String(attrs.url || "");
  if (!label || !url) return "";
  const escaped = label.replace(/([\\[\]])/g, "\\$1");
  const encoded = encodeURI(url).replace(/\(/g, "%28").replace(/\)/g, "%29");
  return `[#${escaped}](${encoded})`;
}

function serializeInline(nodes) {
  return (nodes ?? [])
    .map((n) => {
      if (n.type === "hardBreak") return "\n";
      if (n.type === "contextMention") return n.attrs?.label ? `@${n.attrs.label}` : "";
      if (n.type === "entityReference") return entityReferenceMarkdown(n.attrs ?? {});
      if (n.type === "slashCommand") return slashCommandLabel(n.attrs ?? {});
      const text = n.text ?? "";
      return n.marks?.some((m) => m.type === "code") ? "`" + text + "`" : text;
    })
    .join("");
}

function serializeNode(node) {
  switch (node.type) {
    case "paragraph":
      return serializeInline(node.content);
    case "codeBlock":
      return "```" + (node.attrs?.language || "") + "\n" + serializeInline(node.content) + "\n```";
    case "hardBreak":
      return "\n";
    default:
      if (node.content) return node.content.map(serializeNode).join("\n");
      return node.text ?? "";
  }
}

function markdownText(doc) {
  return (doc.content ?? []).map(serializeNode).join("\n");
}

// Mirrors isEntityReference just enough to drop a malformed chip instead of
// queueing an entry the backend would reject.
function entityReference(attrs) {
  if (!attrs || attrs.version !== 1) return null;
  const ref = { ...attrs };
  if (ref.key === null) delete ref.key;
  const fields = ["ref", "provider", "kind", "id", "title", "url", "scope"];
  return fields.every((f) => typeof ref[f] === "string" && ref[f]) ? ref : null;
}

function collectChips(doc) {
  const mentions = [];
  const references = [];
  const seenRefs = new Set();
  const visit = (n) => {
    if (n.type === "contextMention" && n.attrs) mentions.push(n.attrs);
    else if (n.type === "entityReference") {
      const ref = entityReference(n.attrs);
      if (ref && !seenRefs.has(ref.ref)) {
        seenRefs.add(ref.ref);
        references.push(ref);
      }
    }
    (n.content ?? []).forEach(visit);
  };
  visit(doc);
  return { mentions, references };
}

// ── Prompt reference expansion, mirroring expand-prompt-references.ts ──

function mentionNames(prompts) {
  return [...new Set(prompts.map((p) => p.name).filter(Boolean))].sort(
    (a, b) => b.length - a.length || a.localeCompare(b),
  );
}

function matchMention(content, index, names) {
  if (content[index] !== "@") return null;
  if (index !== 0 && !" \n\t\r".includes(content[index - 1])) return null;
  const start = index + 1;
  for (const name of names) {
    if (!content.startsWith(name, start)) continue;
    const end = start + name.length;
    if (end < content.length && /[A-Za-z0-9_-]/.test(content[end])) continue;
    return { name, end };
  }
  return null;
}

function collectExpansions(content, state, depth) {
  for (let i = 0; i < content.length; ) {
    const match = matchMention(content, i, state.names);
    if (!match) {
      i += 1;
      continue;
    }
    const prompt = state.byName.get(match.name);
    if (prompt && !state.stack.has(prompt.name) && depth < 8 && !state.seen.has(prompt.name)) {
      state.seen.add(prompt.name);
      state.expansions.push(prompt);
      collectExpansions(
        prompt.content,
        { ...state, stack: new Set([...state.stack, prompt.name]) },
        depth + 1,
      );
    }
    i = match.end;
  }
}

// ── <kandev-system> context blocks, mirroring use-message-handler.ts ──

function sanitize(value) {
  return String(value ?? "").replace(/[\r\n<>]/g, " ");
}

function stripTagEnd(value) {
  return String(value).replaceAll("</kandev-system>", "");
}

// "file:" even for directories: inline mentions never carry is_directory
// (getMentions omits it), so this is what the stock submit emits too.
function contextPathsBlock(files) {
  if (!files.length) return "";
  const list = files.map((f) => `- file: ${sanitize(f.path)}`).join("\n");
  return `\n\n<kandev-system>\nCONTEXT PATHS: The user has attached the following file and directory paths as context. Inspect these paths to understand what the user is referring to:\n${list}\n</kandev-system>`;
}

function contextPromptsBlock(promptMentions, prompts) {
  const byId = new Map(prompts.map((p) => [p.id, p]));
  const selected = promptMentions
    .map((m) => byId.get(String(m.path ?? "").replace("prompt:", "")))
    .filter(Boolean);
  if (!selected.length) return "";
  const selectedNames = new Set(selected.map((p) => p.name));
  const byName = new Map(prompts.map((p) => [p.name, p]));
  const names = mentionNames(prompts);
  const expanded = new Set();
  const expansions = [];
  for (const prompt of selected) {
    const state = { byName, names, stack: new Set([prompt.name]), seen: new Set(selectedNames), expansions: [] };
    collectExpansions(prompt.content, state, 0);
    for (const e of state.expansions) {
      if (!expanded.has(e.name)) {
        expanded.add(e.name);
        expansions.push(e);
      }
    }
  }
  const resolved = selected.map((p) => `### ${p.name}\n${p.content}`);
  const expansionContext = expansions.length
    ? [
        "EXPANDED PROMPT REFERENCES: The message above references saved prompts by @name. Use these expansions as hidden context while preserving the original @mentions.",
        ...expansions.map((e) => `### @${stripTagEnd(e.name)}\n${stripTagEnd(e.content)}`),
      ].join("\n\n")
    : "";
  return `\n\n<kandev-system>\nCONTEXT PROMPTS: The user has included the following prompt instructions as context:\n${resolved.join("\n\n")}${expansionContext ? "\n\n" + expansionContext : ""}\n</kandev-system>`;
}

async function referencedTasksBlock(taskMentions) {
  const seen = new Set();
  const tasks = taskMentions.filter((m) => {
    if (!m.taskId || !m.workflowId || !m.workflowStepId || seen.has(m.taskId)) return false;
    seen.add(m.taskId);
    return true;
  });
  if (!tasks.length) return "";
  const titles = new Map();
  await Promise.all(
    [...new Set(tasks.map((t) => t.workflowStepId))].map(async (stepId) => {
      try {
        const body = await (await fetch(`/api/v1/workflow/steps/${stepId}`)).json();
        const name = (body.step ?? body).name;
        if (name) titles.set(stepId, name);
      } catch {
        // fall through to "Step", the frontend's own unknown-step fallback
      }
    }),
  );
  const lines = tasks.map((t) => {
    const stateSuffix = t.taskState ? `, state: ${sanitize(t.taskState)}` : "";
    return `- ${sanitize(t.label ?? t.taskId)} (id: ${sanitize(t.taskId)}, workflow_id: ${sanitize(t.workflowId)}, step: ${sanitize(titles.get(t.workflowStepId) ?? "Step")}${stateSuffix})`;
  });
  return `\n\n<kandev-system>\nREFERENCED TASKS: The user mentioned the following tasks. Use these IDs with the kandev MCP tools (e.g. \`get_task_conversation_kandev\`, \`update_task_kandev\`, \`get_task_plan_kandev\`) when the user asks you to act on them.\n${lines.join("\n")}\n</kandev-system>`;
}

// ── Reading the composer and building the queue payload ─────────────

// TipTap exposes no public handle on the page; the document hangs off the
// contenteditable via .editor when TipTap attached one, else via pmViewDesc
// (a prosemirror-view internal, but one its rendering has always relied on).
function composerDoc(editor) {
  return editor.editor?.getJSON?.() ?? editor.pmViewDesc?.node?.toJSON?.() ?? null;
}

async function buildPayload(editor) {
  const doc = composerDoc(editor);
  if (!doc) {
    // Without the document, a chip would degrade to its visible label — the
    // very bug this serialization exists to fix — so refuse loudly instead.
    if (editor.querySelector("[data-node-view-wrapper]"))
      throw new Error("cannot read the composer document; a mention chip would be mangled");
    const content = editor.innerText.trim();
    return content ? { content } : null;
  }
  const { mentions, references } = collectChips(doc);
  const files = mentions.filter((m) => m.kind === "file" && m.path);
  const promptMentions = mentions.filter((m) => m.kind === "prompt");
  let promptsBlock = "";
  if (promptMentions.length) {
    const body = await (await fetch("/api/v1/prompts")).json();
    promptsBlock = contextPromptsBlock(promptMentions, body.prompts ?? []);
  }
  const tasksBlock = await referencedTasksBlock(mentions.filter((m) => m.kind === "task"));
  const content = markdownText(doc).trim() + contextPathsBlock(files) + promptsBlock + tasksBlock;
  if (!content) return null;
  const payload = { content };
  if (files.length) payload.context_files = files.map((f) => ({ path: f.path, name: f.label ?? f.path }));
  if (references.length) payload.entity_references = references;
  return payload;
}

// The queue lives behind the WebSocket API only (no /api/v1 route). One
// connection per click keeps this stateless — the page's own socket is
// deliberately not reused.
function queueMessage(taskId, sessionId, sessionIncarnationId, payload) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${location.origin.replace("http", "ws")}/ws`);
    ws.onerror = () => reject(new Error("websocket failed"));
    ws.onopen = () =>
      ws.send(
        JSON.stringify({
          id: "us-queue",
          type: "request",
          action: "message.queue.add",
          payload: {
            task_id: taskId,
            session_id: sessionId,
            session_incarnation_id: sessionIncarnationId,
            ...payload,
          },
        }),
      );
    ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.id !== "us-queue") return; // skip pushed events on the shared /ws stream
      ws.close();
      m.type === "error" ? reject(new Error(m.payload?.message || "queue failed")) : resolve();
    };
  });
}

// The queue API now validates all three identities. Read the current session
// incarnation instead of relying on the task's cached primary_session_id.
// REST calls it queue_incarnation_id; only the WS payload uses session_incarnation_id.
async function queueTarget(taskId) {
  const [taskRes, sessionsRes] = await Promise.all([
    fetch(`/api/v1/tasks/${taskId}`),
    fetch(`/api/v1/tasks/${taskId}/sessions`),
  ]);
  if (!taskRes.ok) throw new Error(`task lookup failed (HTTP ${taskRes.status})`);
  if (!sessionsRes.ok) throw new Error(`session lookup failed (HTTP ${sessionsRes.status})`);
  const task = await taskRes.json();
  const sessions = (await sessionsRes.json())?.sessions;
  if (!Array.isArray(sessions)) throw new Error("invalid task sessions response");
  const primaryId = (task?.task ?? task)?.primary_session_id;
  const session = sessions.find((candidate) => candidate?.is_primary === true) ??
    sessions.find((candidate) => candidate?.id === primaryId) ?? sessions[0];
  if (![session?.id, session?.queue_incarnation_id].every((id) => typeof id === "string" && id.trim()))
    throw new Error("task session identity is unavailable: expected id and queue_incarnation_id from the sessions API");
  return { sessionId: session.id, sessionIncarnationId: session.queue_incarnation_id };
}

function taskIdFromUrl() {
  return location.pathname.match(/\/(?:t|tasks)\/([0-9a-f-]{36})/)?.[1];
}

async function onQueue(button) {
  const taskId = taskIdFromUrl();
  const editor = button.closest('[data-testid="chat-input-area"]').querySelector("[contenteditable=true]");
  if (!taskId || !editor) return;

  button.disabled = true;
  try {
    const payload = await buildPayload(editor);
    if (!payload) return;
    const target = await queueTarget(taskId);
    await queueMessage(taskId, target.sessionId, target.sessionIncarnationId, payload);
    editor.focus();
    document.execCommand("selectAll");
    document.execCommand("delete");
  } catch (err) {
    alert(`Queue failed: ${err.message}`);
  } finally {
    button.disabled = false;
  }
}

// The submit button sits in its own tooltip <span>; the injection target is that
// span's parent (the toolbar's flex row, which supplies the gap).
function inject() {
  for (const submit of document.querySelectorAll('[data-testid="submit-message-button"]')) {
    const span = submit.parentElement;
    const row = span.parentElement;
    if (row.querySelector(".us-queue-btn")) continue;
    const button = document.createElement("button");
    button.className = "us-queue-btn";
    button.textContent = "Queue";
    button.title = "Park this prompt on the queue — the pacer starts the turn";
    button.style.cssText = "padding:2px 8px;border:1px solid currentColor;border-radius:9999px;font-size:12px";
    button.onclick = () => onQueue(button);
    row.insertBefore(button, span);
  }
}

// Inject now *and* on every DOM change: the composer may already be mounted when
// this runs (@run-at document-idle), and it remounts on task/session switch,
// where React can drop a foreign child.
inject();
new MutationObserver(inject).observe(document.body, { childList: true, subtree: true });
