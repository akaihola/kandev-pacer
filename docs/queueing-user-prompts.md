# Queueing a user prompt instead of submitting it

A paced task parks at `WAITING_FOR_INPUT`. Typing into the composer and pressing
the blue submit button starts the next turn *immediately*, which spends quota
outside the pacer's control. What we want is the follow-up equivalent of
`+ Create without starting agent`: park the prompt, let the pacer start the turn.

**It already exists.** Kandev has a per-session FIFO **message queue** with a full
add / get / update / remove / cancel / drain API. No Kandev source change is
needed — only a client that calls `message.queue.add` instead of `message.add`.

Verified against the Kandev source (`~/repos/ai/kandev`, 2026-07-26) and live
against the running server on `gogo`.

## The API

WebSocket `GET /ws`, same envelope and Origin requirement as `session.launch`
(`{id, type:"request", action, payload}`; send `Origin = KANDEV_URL`). Actions
are declared in `apps/backend/pkg/websocket/actions.go:114`:

| Action | Effect |
|---|---|
| `message.queue.add` | Park one entry. `{session_id, task_id, content, model?, plan_mode?, attachments?}` → the created entry incl. its `id`. |
| `message.queue.get` | `{session_id}` → `{entries, count, max}`. |
| `message.queue.update` | Edit a parked entry's content/attachments. |
| `message.queue.remove` | Delete one entry by `entry_id`. |
| `message.queue.cancel` | Clear the whole queue for a session. |
| `message.queue.drain` | **Start a turn now** from the head entry. |

Handlers: `internal/orchestrator/handlers/queue_handlers.go`. There is no
`/api/v1` route for any of them — the queue is WebSocket-only.

The cap is 10 entries per session (`KANDEV_QUEUE_MAX_PER_SESSION`,
`messagequeue.DefaultMaxPerSession`); an over-cap insert fails with
`queue_full`. Entries live in the `queued_messages` table of `kandev.db`
(`repository_sqlite.go:33`), so they survive a server restart.

`message.queue.add` performs **no** session-existence check — a typo'd
`session_id` is accepted and the entry is then unreachable from any UI. Read the
session id from `GET /api/v1/tasks/:id/sessions` rather than constructing it.

## Why the composer submits instead of queueing

`deriveSessionInputMode` (`apps/web/hooks/domains/session/session-input-mode.ts`)
maps session state to composer behaviour, and `use-message-handler.ts` obeys it:

- `CREATED` / `IDLE` / `WAITING_FOR_INPUT` → `direct` → `message.add`, turn starts.
- `STARTING`, or `RUNNING` with foreground activity → `queue` → `message.queue.add`.

So the composer queues **only while the agent is busy**. A parked session is
exactly the case it will not queue for. That is a frontend policy decision, not a
backend restriction: the backend accepts `message.queue.add` in any state.

The UI renders externally-queued entries anyway. `QueueAffordance`
(`components/task/chat/queued-ghost-list.tsx`) shows an "*n* queued" chip above
the composer whenever the queue is non-empty, with edit / remove / clear — and a
**Send now** button gated on `canManuallyDrainQueue`, which is true precisely
when the session is `WAITING_FOR_INPUT` or `IDLE`
(`components/task/chat/chat-input-area.tsx:436`). Queue-while-parked is a
supported state, complete with a manual release control.

## What drains the queue

This is the part that decides whether pacing holds. Drain sites:

1. **Turn end** — `handleAgentReady` drains after `on_turn_complete`, *unless* a
   workflow transition occurred (`event_handlers_agent.go`, the `transitioned`
   early return).
2. **Step entry** — `processOnEnter` drains on the no-`on_enter` and
   no-`auto_start_agent` branches (`event_handlers_workflow.go:901`, `:1008`).
   **This is the paced configuration.**
3. **Agent boot** — `handleAgentBootReady` drains once the agent is back at
   `WAITING_FOR_INPUT` (`event_handlers_agent.go:257`), so a resumed session
   flushes an orphaned entry.
4. **Auto-start merge** — `takeAndMergeHandoffMessage` appends a queued entry to a
   step's auto-start prompt (`event_handlers_workflow.go:1613`). Irrelevant here:
   `make_paced.py` strips `auto_start_agent`.
5. **Explicit** `message.queue.drain`.

Consequence for the pacer:

> **Queue only while the task is parked and no turn is running.** An entry added
> mid-turn is drained by site 1 or 2 the moment the turn ends and the task lands
> on its next step — immediately, un-paced.

A parked task with no in-flight turn has no drain trigger at all: nothing fires
until the pacer says so. Confirmed live — `message.queue.add` against a
`WAITING_FOR_INPUT` session left the state `WAITING_FOR_INPUT` and started
nothing; the entry sat in `message.queue.get` until removed.

Nothing *else* will pick it up either, which is what makes the queue a safe
parking place:

- **No background reconciler.** Every drain site above is event-driven. The only
  `time.NewTicker`s in the orchestrator are inside `waitForAgentPromptReady` and
  the session-ready wait loop — no sweeper visits idle sessions.
- **A server restart does not flush it.** `reconcileSessionsOnStartup`
  (`internal/orchestrator/service.go:1414`) "does NOT launch any agent processes —
  sessions are recovered lazily"; it flips active sessions to `WAITING_FOR_INPUT`
  and abandons open turns without prompting. Entries are in SQLite, so they
  survive the restart untouched.
- **`RecoverSession`** is a user-triggered WS action (the UI's Recover button), not
  an automatic path.

## How the pacer releases it

Queueing is inert unless the pacer drains, because **nothing else will**. Before
this was wired up, `pacer.py` had no reference to the queue API at all, and a
queued prompt was either never delivered or delivered un-paced:

- **Parked at Review or Done → never released.** `is_launchable` returns `False`
  for any step that is neither a work step nor the queue step, so those tasks were
  invisible to the pacer. Review is precisely where a human wants to queue
  feedback, and no Kandev event fires there either (its `on_enter` already ran;
  there is no next transition without a turn).
- **Parked at a work step → a bonus turn at the wrong step.** `ws_launch` prompts
  with `buildWorkflowPrompt` and never touches the queue, so the entry survived
  that turn (site 1 skips the drain when a transition occurred) and was flushed by
  site 2 on arrival at the next parked step — a second turn that never passed the
  gate, carrying text written for the *previous* step.

What the pacer does now, per tick:

1. `queued_counts()` (in `runtime.py`) asks `message.queue.get` about every session
   in `DRAINABLE_STATES` — running or never-run sessions are not asked about at all.
2. `select_action()` returns a `Candidate('drain', task, session)` or
   `Candidate('launch', task)`, oldest task first within each kind, **drain outranking launch**. A drainable task is
   eligible at *any* step, which is what makes a prompt parked at Review
   releasable.
3. A drain re-checks that the session is still the counted one and still idle,
   issues `message.queue.drain`, and books `(task, step, session)` against the
   same concurrency budget — it spends a turn exactly like a launch does.

Drain-before-launch is the required order, not a preference: launching first
spends the turn on the step prompt and leaves the user's prompt to leak later.

Edge cases handled explicitly:

- **`drained: false`** — the queue emptied between the count and the take (the
  UI's **Send now**, or another client). No turn was spent, so nothing is booked
  and the task stays eligible.
- **Drain rejected** (`session_busy`, `session_not_promptable`) — a benign race
  where a turn started since the count. Logged and skipped rather than allowed to
  reach the loop's catch-all, which would pause the gate for a tick. Caveat: since
  drain outranks launch, an entry that failed *every* attempt would keep its task
  selected and starve launches; the log names the task and Kandev's reason, and
  removing the entry from the UI's queue panel clears it.
- **Queue unreadable** — degrades to "nothing queued" and keeps launching, i.e.
  exactly the pre-queue behaviour. Halting all pacing over an unreadable queue
  would be the worse trade.
- **`CREATED` sessions are never drained.** `checkSessionPromptable` rejects them,
  so a never-run task's queued prompt waits for that task's admission turn.

Independent of the pacer, Kandev's own queue chip offers **Send now** whenever the
session is `WAITING_FOR_INPUT`/`IDLE`, so a parked prompt can always be released by
hand. Manual gating, not pacing, but it means nothing is ever stuck.

Verified by `tests/test_workflow.py` (selection and eligibility, pure),
`tests/test_client.py` (the WebSocket layer) and `tests/test_runtime.py` (`tick()`'s
drain branch against a fake Kandev on loopback: drain at Review, drain outranking
launch, launch unchanged when the queue is empty, `drained: false` spending no turn,
`CREATED` never drained, and the concurrency cap short-circuiting before any queue read).

### Live round trip (2026-07-26, real Kandev, task parked at Done)

```
0. before            : WAITING_FOR_INPUT, step Done
1. message.queue.add : entry d241146d…
2. message.queue.get : count = 1
   state while parked: WAITING_FOR_INPUT   <- parked, no turn fired
3. message.queue.drain: {"drained": true}
   t+3s state        : STARTING, step Backlog
4. queue after       : count = 0
```

The agent then answered the *queued* text (`pacer-drain-probe-ok`), confirming the
parked content became the turn's prompt, and returned to `WAITING_FOR_INPUT`. So the
whole path is verified against real Kandev: park without firing, release on demand.

**Draining at a parked step also re-queues the task.** Note step `Done` → `Backlog`
above. In this workflow both `Review` and `Done` carry
`on_turn_start: move_to_step → Backlog`, so releasing a prompt there runs the turn
*at that step* and then hands the task back to the admission queue, where the pacer
gates the next work turn separately. That is Kandev owning routing and the pacer
owning timing, applied to drains — one gated turn for the human's feedback, another
gated turn for the work it triggers. Worth knowing before wondering why a drained
task reappears in Backlog.

## The release primitive

`message.queue.drain` → `Service.DrainQueuedMessage` (`task_operations.go:3121`)
→ `PromptTask` with the queued content. That is the same turn the submit button
would have produced: a plain follow-up on the current session, no step move.

It requires a *promptable* session — `checkSessionPromptable`
(`task_operations.go:2944`) accepts only `WAITING_FOR_INPUT`, `COMPLETED`, `IDLE`,
or `RUNNING` with an idle foreground turn. Error codes: `session_busy`
(`ErrAgentPromptInProgress`), `session_not_promptable`. Notably **`CREATED` is not
promptable**, so a never-run task's queued prompt cannot be drained; admission
must still go through `session.launch`.

`StartSessionForWorkflowStep` (`task_operations.go:1387`) does **not** consult the
queue — it prompts with `buildWorkflowPrompt` only. So `session.launch` and
`message.queue.drain` are two distinct release primitives; see the section above
for which one the pacer must pick, and why the order matters.

## Getting a queue button in front of the user

The backend needs nothing; only the typing surface does. Options, cheapest first:

1. **CLI** — one `pacer.py`-style subcommand (`queue <task> "text"`) reusing the
   existing WS helper. Good for scripting and for agents; not for typing into the
   chat panel.
2. **Browser userscript** — implemented:
   [`userscript/kandev-queue-button.user.js`](../userscript/kandev-queue-button.user.js).
   Adds a "Queue" button beside submit that calls `message.queue.add` over its own
   WebSocket. From the page, `Origin` is Kandev's own and passes the `/ws` check —
   which is why a userscript works where an external tool must set the header. The
   entry then appears in Kandev's own queue chip, editable and removable, with
   **Send now** available. No Kandev source touched, no rebuild.

   Verified in headless Chromium against a fixture mirroring the composer markup:
   correct placement in the toolbar row, correct `message.queue.add` payload,
   editor cleared, idempotent, and re-injected if React drops the node. Not yet
   run against the live UI, so the real page's markup matching the fixture rests
   on the source read (`chat-input-toolbar-desktop.tsx`, `-primitives.tsx`).
3. **Type into the composer while the agent is still running.** Free, and Kandev
   already queues it — but drain sites 1 and 2 release it at turn end, so it is
   not paced. Do not rely on it.

## Dead ends checked

- **`message_task_kandev` (MCP) with `delivery_mode: "queued"`** does not park a
  prompt on an idle session. `dispatchTaskMessage`
  (`internal/mcp/handlers/handlers.go:2342`) queues only for `RUNNING` /
  `STARTING`; otherwise it prompts or starts the session immediately (status
  `sent` / `started`). `delivery_mode` picks queue-vs-interrupt *while busy*, and
  `"interrupt"` is authorized only for a direct parent task.
- **Pending clarification** also forces the composer into queue mode
  (`use-message-handler.ts`), but the clarification-answer path dispatches
  immediately when it can (`dispatchClarificationResumeLocked`), so it is not a
  parking mechanism.
- **Faking the session state in SQLite** to trick `deriveSessionInputMode` into
  queue mode: races the orchestrator's own state writes. Not viable.
