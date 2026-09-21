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
| `message.queue.get` | `{task_id, session_id, session_incarnation_id}` → `{entries, count, max}`. |
| `message.queue.update` | Edit a parked entry's content/attachments. |
| `message.queue.remove` | Delete one entry by `entry_id`. |
| `message.queue.cancel` | Clear the whole queue for a session. |
| `message.queue.drain` | `{task_id, session_id, session_incarnation_id}`: **start a turn now** from the head entry. |

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
- **A server restart does not flush it — the next agent boot does.**
  `reconcileSessionsOnStartup` (`internal/orchestrator/service.go:1504`) "does NOT
  launch any agent processes — sessions are recovered lazily"; it flips active
  sessions to `WAITING_FOR_INPUT` and abandons open turns without prompting, and
  the entries survive in SQLite. But the recovery it defers is exactly drain site
  3: the first thing that boots that session's agent — the UI opening the task,
  `RecoverSession`, a peer message, the pacer's own next launch — fires
  `agent.boot_ready`, and `handleAgentBootReady` drains the queue
  *unconditionally* (`event_handlers_agent.go:295`), un-paced. A restart therefore
  arms every parked prompt in the board; whoever next wakes each agent pulls the
  trigger. See [the 2026-07-31 incident](#incident-2026-07-31-a-restart-armed-three-parked-prompts).
- **`RecoverSession`** is a user-triggered WS action (the UI's Recover button), not
  an automatic path — but per the bullet above, triggering it releases the parked
  prompt.

### Incident 2026-07-31: a restart armed three parked prompts

kandev was restarted at 12:48:40 local. At 12:50–12:51 five sessions were booted
back (`is_resuming: true` in the turn metadata); two of them drained a parked
prompt on boot — `"merge"` at 12:51:57 on session `6e144a9f`, a review reply at
12:51:56 on `22ccdd91` — each starting a turn. The pacer was `paused` the whole
time and launched nothing, and its tick at 12:52:03 reported
`headroom=-0.136 PAUSED (3 running)` with `cap=2`: three un-paced turns, one over
the concurrency cap, none of them the pacer's.

Nothing in the pacer can prevent this: the drain is kandev's, it needs no client,
and by the time the pacer's next tick reads the board the turns are already
running. What the pacer does do is report it honestly — the paused line carries the
running count precisely so an un-paced turn is visible rather than silent. The
mitigation is operational: after a kandev restart, expect every parked prompt to
fire as its task is opened.

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

   A drain replays the stored content verbatim (`message.queue.drain` →
   `PromptTask`), so fidelity is the queueing client's problem: the stock
   composer serializes its TipTap document (@-mention chips → `@label`,
   `#`-reference chips → markdown links, `/`-command chips → `/name`) and
   appends the CONTEXT PATHS / CONTEXT PROMPTS / REFERENCED TASKS
   `<kandev-system>` blocks *before* `message.queue.add` — nothing expands
   later. The userscript's first version queued `innerText`, which collapses a
   chip to its visible label: a tab-completed `@heartbeat` arrived as plain
   `heartbeat` with no prompt expansion (task `ad31370d`, 2026-08-08). v1.1
   reads the document off the contenteditable (`.editor`, falling back to
   ProseMirror's `pmViewDesc`) and mirrors kandev's own serialization,
   context-block building and prompt-reference expansion;
   `tests/userscript.test.mjs` pins the mirrored pipeline.

   Verified in headless Chromium against a fixture mirroring the composer markup
   (placement in the toolbar row, `message.queue.add` payload, editor cleared,
   idempotent, re-injected if React drops the node) and, on 2026-08-08, against
   the live UI: both document handles exist on the real composer, tab-completing
   `@heart` in it and running the userscript's payload builder produced
   `@heartbeat …` plus the same CONTEXT PROMPTS block the stock submit stores.
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


## Queue identity after 0.94-dev

Session REST responses expose `queue_incarnation_id`. Send that value under
`session_incarnation_id` in queue WebSocket requests, alongside `task_id` and
`session_id`. Status reads, explicit drains, and userscript admission all require
this mapping. A missing identity or malformed count is unknown, not an empty queue.

A failed idle-session queue read withholds that task's automatic workflow launch;
other tasks and known queued prompts remain eligible. The final launch check reads
queues again, including fallback launches that bypass the normal open-gate scan.
A pending prompt also prevents that automatic launch. Before draining, pacer compares
the fresh queue incarnation with the one selected from the tick snapshot. If it
changed, pacer waits for another tick rather than draining the replacement queue.

Deploy both the Python repair and the updated installed userscript. Restart the
actual pacer process with its existing configuration and credentials, then verify
queue reads from the replacement process. A merge alone does not update a running
Python process. Test prompt admission and release only on a designated test task.

For the Queue button, replace the installed script in the browser's userscript
manager with `userscript/kandev-queue-button.user.js`, confirm version `1.1.4`, save,
and reload the Kandev page. Updating this repository does not update the browser's
installed copy. Version `1.1.2` reads the wrong REST field and reports
`task session identity is unavailable` even when the API returns a valid identity.
Version `1.1.3` fixed that mapping; `1.1.4` also rejects failed HTTP lookups and
invalid identities before queue admission, preserving the composer text.
