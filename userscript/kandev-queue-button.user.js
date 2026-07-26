// ==UserScript==
// @name         Kandev: Queue instead of Send
// @description  Adds a "Queue" button beside the composer's submit button. Parks the
//               typed prompt on the session's message queue instead of starting a turn,
//               so kandev-pacer decides when the quota gets spent.
// @version      1.0
// @match        *://*:38429/*
// @grant        none
// ==/UserScript==
//
// Why this exists, and what drains the queue, is in docs/queueing-user-prompts.md.
//
// Limitations, all deliberate to keep it small: plain text only (attachments,
// @-mentions and slash commands are not carried over — innerText gives their
// visible label, not their structured form); targets the task's primary session,
// so it queues to the wrong session if you have switched the sessions dropdown;
// and queueing while a turn is running is pointless — Kandev's own submit button
// already queues then, and the entry gets drained at turn end either way.
"use strict";

// The queue lives behind the WebSocket API only (no /api/v1 route). One
// connection per click keeps this stateless — the page's own socket is
// deliberately not reused.
function queueMessage(sessionId, taskId, content) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${location.origin.replace("http", "ws")}/ws`);
    ws.onerror = () => reject(new Error("websocket failed"));
    ws.onopen = () =>
      ws.send(
        JSON.stringify({
          id: "us-queue",
          type: "request",
          action: "message.queue.add",
          payload: { session_id: sessionId, task_id: taskId, content },
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

// The selected session is not in the URL; the task payload names it. This is the
// same default the page itself hydrates with (task.primary_session_id).
async function primarySessionId(taskId) {
  const res = await fetch(`/api/v1/tasks/${taskId}`);
  const body = await res.json();
  return (body.task ?? body).primary_session_id;
}

function taskIdFromUrl() {
  return location.pathname.match(/\/(?:t|tasks)\/([0-9a-f-]{36})/)?.[1];
}

async function onQueue(button) {
  const taskId = taskIdFromUrl();
  const editor = button.closest('[data-testid="chat-input-area"]').querySelector("[contenteditable=true]");
  const content = editor?.innerText.trim();
  if (!taskId || !content) return;

  button.disabled = true;
  try {
    await queueMessage(await primarySessionId(taskId), taskId, content);
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
