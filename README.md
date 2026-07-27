# kandev-pacer

Paces low-priority [kandev](https://github.com/kdlbs/kandev) task execution against a
Claude subscription quota, so consumption burns down steadily instead of front-loading
and hitting a mid-window lockout. High-priority work (Routines, UI-created tasks) runs
untouched; low-priority backlog work is admitted only while under the steady-burn curve.

## The pacing rule

Per quota window, with `c` = consumed fraction and `e` = elapsed fraction:

- Steady linear burn-down (100% → 0% across the window) traces **`c == e`**.
- **Pause when `c > e`** (consumed a larger fraction of quota than of the window).
  Equivalently: `remaining_fraction + elapsed_fraction < 1`.
- Headroom `h = e − c`. Pause when `h < 0`. Multiple overlapping windows (Anthropic's
  5-hour + 7-day + `limits[]`): take the **worst** window, `min(h)`.
- Reserve knob `alpha` (`< 1`) targets a steeper line `c > alpha·e` to finish each window
  with quota to spare.

**Why not "remaining < elapsed" (`r < e`)?** That's back-loaded — it never fires in the
first half of a window no matter how fast you burn, then fires all through the second half.
It also doesn't self-heal: pausing freezes `r` while `e` rises, so `r < e` only gets *more*
true and never releases. `c > e` self-heals: while paused, `c` is frozen and `e` keeps
rising, so `h` climbs back to 0 on its own and the gate reopens with no external release.

`e` is derived from the reset time, not a start time:
`e = clamp01(1 − (resets_at − now) / duration)`. Window durations are hardcoded
(5h = 18000s, 7d = 604800s) since the payload gives `resets_at`, not duration.

## Enforcement: hold in place, launch on release (Design 13)

Don't kill running turns — gate what's allowed to *start*. The unit is a **turn** (a prompt
that spends quota). This paces **complicated, multi-step, branching/looping** workflows, not
just a flat queue. See [`docs/pacing-designs.md`](docs/pacing-designs.md) §13 for the full
comparison of alternatives.

**Kandev owns routing, the pacer owns timing.** Auto-start is removed from every **work step**,
so a finished turn still runs the step's completion route and lands the task on its correct
next step — through any branch, loop, or agent `move_task` — where it waits `WAITING_FOR_INPUT`.
While under the steady-burn curve, the pacer starts the next turn via the WebSocket
`session.launch` primitive:

- **In place** for a task already sitting at a work step: `intent: workflow_step` with the
  task's current step id and its newest live session id.
- **Admission from the parked queue** (the start step, e.g. Backlog): one call both moves the
  task to the first work step *and* starts it — `intent: workflow_step` with the task's newest
  session, whether it was never run or re-queued. A supplied `workflow_step_id` moves the task
  (`moveTaskToWorkflowStep` / `advanceTaskWorkflowStep`), so no separate move API is needed.
  `intent: start` is reserved for a task with *no* session at all, which in practice doesn't
  happen — kandev prepares a session when the task is created (see below).

**Queued user prompts are released the same way.** A follow-up typed into Kandev's composer
normally fires a turn on the spot. Parked on the session's message queue instead
(`message.queue.add`, e.g. via [`userscript/kandev-queue-button.user.js`](userscript/kandev-queue-button.user.js)),
it waits, and the pacer releases it with `message.queue.drain` when the gate opens — **drain
first, launch second**, since launching a task that has a prompt queued spends the turn on the
step prompt and lets the queued entry leak out un-paced afterwards. A drainable task counts at
*any* step, including parked ones like Review where a launch never applies. Full derivation and
the verified Kandev drain triggers: [`docs/queueing-user-prompts.md`](docs/queueing-user-prompts.md).

**A never-run task already has a session.** Creating a task provisions its workspace, worktree
and branch immediately and parks the session in `CREATED` — prepared, agent never started. So
"never run" does **not** mean "no session", and the pacer treats `CREATED` as launchable
alongside `WAITING_FOR_INPUT`. Getting this wrong is silent and total: tasks sit in the queue
forever while the pacer cheerfully logs `nothing to launch` every tick.

**A launch kandev refuses is one task's problem, not the board's.** Some tasks can't be
launched at all — a `CREATED` session whose `executors_running` row is missing (prepared
without `launch_workspace`, or cleaned up) makes `ensureSessionRunning` fall through to
"session is not resumable: no executor record", and no amount of retrying the same call will
change that. Because candidates are drawn oldest-first, such a task is the top pick *every*
tick, so treating the refusal as a failed tick wedged the pacer completely: nothing else ever
launched and the only symptom was a repeating `tick error` line on stderr. Instead:

- **A refusal spends no quota, so the tick isn't over.** The pacer records the failure, moves
  on to the next candidate, and keeps the gate open — up to three candidates per tick, so a
  board full of broken cards can't turn one tick into a long walk.
- **Failures are tracked apart from bookings.** A booking means "a turn was spent here, don't
  spend another"; a failure means "this didn't work, back off". One map meaning both would make
  every "did we already launch?" question ambiguous. The backoff starts at `PACER_FAIL_BACKOFF`
  (default 3 ticks) and doubles per consecutive failure up to 8×, then flattens — a hopeless
  task still gets an occasional "has the user fixed it yet?" retry. It's forgotten the moment
  anything real changes (the task moves step, a new session appears) or something succeeds.
- **After `PACER_BLOCK_AFTER` failures (default 2) the task is flagged `BLOCKED`** via kandev's
  own `task.state` action — the same state its MCP tools set, rendered as a "Blocked" warning
  on the card. A task the pacer has given up on is something you want to *see*, not something
  you find by reading stderr. Kandev clears it by itself: whatever runs the task next moves it
  to `SCHEDULING`/`IN_PROGRESS`. Set `PACER_BLOCK_AFTER=0` to keep the pacer read-only.

The same treatment covers a rejected `message.queue.drain` and a task whose workflow has no
admission target — both would otherwise keep a task selected forever and starve every launch
behind it.

## Scope: every enrolled workflow, not one hardcoded board

A step is **pace-eligible** when it satisfies *both* halves, and the pacer paces every workflow
that has at least one:

1. **`stage_type: "work"` — the enrollment marker.** Kandev defaults this column to `custom` and
   exposes no API/UI/import surface for it, so the only thing that ever writes `work` is
   `make_paced.py`'s direct SQLite write — which is also what strips the `auto_start_agent` that
   would bypass the gate. "Tagged work" therefore means "deliberately prepared for pacing",
   which is exactly the signal needed to decide, unattended, whether a workflow the pacer merely
   *discovered* is fair game. (`make_paced.py` is the enroll action; a workflow re-import resets
   `stage_type`, so re-run it after one.)
2. **An `on_turn_complete` transition (`move_to_next` | `move_to_previous` | `move_to_step`) —
   the safety property**, and not optional. It buys the guarantee that a task *still sitting* on
   the step has an unfinished turn, so the pacer won't double-launch it. Without it a step that
   never routes away would read as idle after every turn and be relaunched forever — an infinite
   prompt loop billed to the quota this program exists to protect.

Parked steps (Review/Done, the queue) must route via `on_turn_start`/`on_comment`, never
`on_turn_complete`. Step ids are globally unique, so eligible steps across all workflows live in
one flat set; **admission is per workflow**, since a queued task must be admitted into a step of
its own board.

`PACED_WORKFLOW_ID` (comma-separated ids *or* names) narrows the scope; unset — the default —
paces every enrolled workflow. **This changed:** it used to default to one hardcoded workflow id.
Enrollment, not this variable, is now the normal way to opt a workflow in.

On startup, and in `--dump-order`, the pacer prints the workflows it is pacing **and the ones it
is not, with the reason** — a workflow silently out of scope is how a task dragged to the top of
your sidebar goes missing with no explanation. It also lints each scoped workflow for
bypass/ambiguity: leftover `auto_start_agent`, profile-switch + prompt, signal-gated routing,
work-shaped-but-untagged steps (the "why is this never paced" case), `stage_type`/routing
disagreement, and a **start step that is itself pace-eligible** — which leaves the design no
holding area at all, so the backlog executes instead of queueing.

## Which task goes next

When more than one task is launchable and the gate is open, two orderings decide, layered:

1. **What you placed by hand** — the order of your Kandev `TASKS` sidebar list, if you keep one.
2. **Everything else** — the board's own kanban-pull order (below).

An explicit drag outranks the pull heuristic; anything you have *not* dragged keeps pull order.

### The overlay: your sidebar drag order

Put a sidebar view on **sort `custom`, group `none`**, drag the tasks into the order you want,
and the pacer works down that order — no pacer-side priority field to maintain.

- **Placed tasks first**, in your drag order. **Pinned tasks float above them** (pinning is a
  visible re-ordering, so honoring it keeps "the pacer runs the top-most task" true).
- **Subtasks follow their parent**, in their own stored order — the sidebar nests them there.
- **Tasks you never dragged sit behind the placed ones**, in the board's pull order. "Not
  placed" means "not prioritized". Note this is a *deliberate* divergence from the sidebar,
  which floats unplaced tasks to the *top* by newest `created_at`: copying that would let
  merely *creating* a task jump an order you set by hand.
- Re-read **every tick**, so a drag re-prioritizes the queue without restarting the pacer.
- The sidebar list spans **every** workflow while the pacer only sees enrolled ones, so a task
  can sit high in your manual order and still be out of scope. `--dump-order` names those
  explicitly (`placed by hand but not paced`) rather than leaving them to look lost.

`PACER_ORDER` picks the policy:

| value | behavior |
| --- | --- |
| `auto` (default) | Layer the manual order over pull order once **any** saved sidebar view uses sort `custom`; pull order alone otherwise. |
| `custom` | Always layer the manual order, whatever the sidebar's current sort. |
| `board` | Pull order alone — ignores the sidebar and skips the settings fetch entirely. |

**Why "any saved view" and not the active one?** The manual order (`ordered_task_ids`) is
stored **once per user, not per view**. So a saved "Prioritize" view still expresses your
intended order while you have some other view selected to work in — keying on the *active*
view would silently flip the pacer back to FIFO the moment you switched views for an unrelated
reason. The in-flight *draft* counts too: dragging a task flips the sort to `custom` in the
draft before it is ever saved to the view.

**Fails soft, never sideways.** Pull order is the base in every mode and the manual list is
layered on top, so anything missing degenerates cleanly: settings unreadable (logged, pacing
continues — an HTTP hiccup here must not pause the gate), nothing dragged yet, or a task whose
parent chain is cyclic all fall back to the board order rather than to an arbitrary one.

**The trade-off this layering makes.** Pull order deliberately prefers finishing in-flight work
over admitting new work; dragging a queued task to the top overrides that and admits it first.
That is the point — an explicit instruction beats a heuristic — but if you would rather the WIP
preference always win, use `PACER_ORDER=board`.

`./pacer.py --dump-order` prints the queue the pacer would work down, so a drag is checkable
without waiting a tick — or spending quota — to see which task it picks.

**Priority tiering is free**: the gate reads *account-level* usage, which already includes
Routine/UI burn. When high-priority work pushes `c` above `e`, headroom goes negative and the
gate closes automatically. You classify by *which workflow* you create a task in.

`PACER_PROVIDER` picks which usage agent to gate on. If it matches no provider in the payload
(a typo, or a renamed provider), the pacer would otherwise see zero windows and pace *blind* —
fully open, backstop dead. So a mismatch **fails closed**: the pacer withholds and logs
`check PACER_PROVIDER` instead. A genuinely fresh account (provider present, no windows yet)
still paces normally.

**Backstop** (`PACER_BACKSTOP`, default `0.97`; `≥1.0` disables): near an absolute ceiling the
pacer withholds *all* launches (including admissions). A hard mid-turn lockout on an
already-running turn is **not** preventable here — halting a live turn needs a WebSocket
`stop_task`, which is preemption and out of scope for this admission-control pacer.

### The base: the board's pull order

Whatever the overlay does not place, the pacer picks by **column first, then the board's own
pull order**:

```
descending workflow-step position   (rightmost / most downstream column first)
  -> task position ASC              (the manual drag order within that column)
  -> priority     (critical, high, medium, low, then none/unknown)
  -> created_at ASC
  -> id ASC
```

Everything after the first line is kandev's WIP-limit pull ordering copied verbatim
(`task/repository/sqlite/task.go:504`, documented at `docs/public/tasks-and-workflows.md:187`),
so dragging a card up the *board* moves it up the pacer's queue too — `position` outranks both
priority and age, because a drag is an explicit human decision and the other two are defaults.
The `id` tiebreak keeps the order total, so a fully tied pair always resolves the same way
instead of flapping between ticks.

**Why a column ordering comes first.** A task's `position` is scoped to a *workflow step*, not
to the workflow — `MoveTask` never looks at repositories, and two tasks in different columns can
both sit at `position = 0`, so `position` alone is not a total order and comparing it across
columns is meaningless. The pacer therefore ranks columns first and only compares positions
*within* one. It ranks them right-to-left (highest step position first) because that is kanban
pull, and it is what Design 13 needs: a task already at a work step is a turn in flight, and
admitting a fresh one from the parked queue instead would grow WIP without finishing anything.
Draining right-to-left also generalizes past the single work step of the live board — in a
multi-work-step workflow, tasks deepest in the pipeline advance before new ones are pulled in.
A step id that no longer exists in the workflow sorts last, not first.

**Releases outrank launches.** This ordering decides *which* task goes next; the *kind* of turn
is decided first. A task with a queued user prompt is drained before any launch, whatever column
it sits in — see [`docs/queueing-user-prompts.md`](docs/queueing-user-prompts.md) for why the
other order leaks a second, un-gated turn. Within each kind the pull order above applies
unchanged, so dragging a card up its column moves its queued prompt up the release queue too.

`position` reaches the pacer already: `GET /api/v1/workflows/:id/tasks` returns it on every task
(`dto.TaskDTO.Position`, `task/dto/dto.go:146`), as do the WS `task.*` events
(`task/service/service_events.go:352`). A task with a missing or null `position` is read as `0` —
top of its column, not last — so an older payload or an un-dragged board degrades to plain
`created_at` ordering rather than sorting the task to the back.

Concurrency cap (`PACER_CONCURRENCY`, default 1) bounds how many paced turns run at once
(counting sessions in `STARTING`/`RUNNING`); with slow polling a high cap can overshoot the
line between ticks. Duplicate-start protection: the pacer books each `(task, step, session)` it
launches until the turn is observed to start.

## Confirmed against kandev source (`~/repos/ai/kandev`, 2026-07-24)

- **Launch primitive** `session.launch` over `GET /ws` (`gateway/websocket/setup.go:71`),
  handler `wsLaunchSession` (`orchestrator/handlers/handlers.go:38`); request
  `LaunchSessionRequest{task_id, intent, session_id, workflow_step_id}`
  (`orchestrator/session_launch.go`); a supplied `workflow_step_id` moves the task
  (`task_operations.go:819` / `:1304`). Envelope `{id, type:"request", action, payload}`
  (`pkg/websocket/message.go`), response matched on `id`; failures come back as `type:"error"`.
  `/ws` enforces an Origin check (send `Origin = KANDEV_URL`).
- **Sessions are prepared eagerly** (2026-07-25). Creating a task provisions the workspace and
  leaves the session `CREATED`, with an `executors_running` row at `status='prepared'`. So the
  pacer's admission call is `intent: workflow_step`, never `intent: start` — `start` routes to
  `StartTask`, which creates a *second* session, while `workflow_step` reuses the prepared one:
  `StartSessionForWorkflowStep` (`task_operations.go:1244`) moves the task via
  `advanceTaskWorkflowStep` (`:1304`), then `ensureSessionRunning` (`:1328`) sees the `CREATED`
  state plus the `executors_running` row (`:1359`) and calls `startAgentOnPreparedWorkspace`
  rather than a full relaunch. (`intent: start_created` also starts a prepared session, but
  `StartCreatedSession` takes no `workflow_step_id`, so it can't admit — it won't move the task.)
- **Sidebar order** (2026-07-26) `GET /api/v1/user/settings` → `{settings: {…}}` (note the wrapper). The
  manual order lives in `settings.sidebar_task_prefs{pinned_task_ids, ordered_task_ids,
  subtask_order_by_parent_id}` — **per user, not per view** (`user/models/models.go:167`,
  route `user/handlers/handlers.go:39`). Views are `settings.sidebar_views[]{sort{key,
  direction}, group}` plus `sidebar_active_view_id` and an unsaved `sidebar_draft`; the pacer
  merges draft over active view exactly like the frontend's `useEffectiveSidebarView`
  (`web/hooks/domains/sidebar/use-effective-sidebar-view.ts`). Ordering semantics mirror
  `applyView` (`web/lib/sidebar/apply-view.ts`): sort → group → subtask order → pinned float,
  where the pin float applies to root tasks only. `custom` ignores sort `direction` (reversing
  a manual drag has no meaning). Tasks carry `parent_id` (`task/models/models.go:446`).
- **Message queue** (WebSocket only, no `/api/v1` route): `message.queue.add` / `.get` /
  `.drain` / `.update` / `.remove` / `.cancel` (`pkg/websocket/actions.go:114`), handlers in
  `orchestrator/handlers/queue_handlers.go`. `.get` → `{entries, count, max}`; `.drain` →
  `{session_id, drained}` where `drained: false` means the queue was already empty.
  `DrainQueuedMessage` requires a promptable session (`checkSessionPromptable`) — `CREATED` is
  not one. Entries persist in the `queued_messages` table, and `.add` does **not** validate
  `session_id`. Cap 10/session (`KANDEV_QUEUE_MAX_PER_SESSION`), over-cap → `queue_full`.
- **Task state** (2026-07-27) `task.state` over the *same* `/ws` connection as everything above —
  the task handlers register into `gateway.Dispatcher` (`backendapp/helpers.go:881`), the one
  `session.launch` uses too (`backendapp/gateway.go:149`), built once in
  `gateway/websocket/setup.go:29`. Payload `{id, state}`, handler
  `wsUpdateTaskState` (`task/handlers/task_ws_handlers.go:441`) → `service.UpdateTaskState`.
  `BLOCKED` is one of the valid states (`pkg/api/v1/task.go:14`) and the same one kandev's own
  MCP task tools set (`mcp/handlers/config_task_handlers.go:374`). It writes the state column
  and publishes `task.state_changed`, nothing else — its doc comment claims it "moves it to the
  matching column", but the code doesn't (`UPDATE tasks SET state`, `repository/sqlite/task.go:1248`),
  and no automation trigger listens for the event. So the flag can't move a card out from under
  the pacer's own step logic. The web board renders task
  state as a card icon (`web/components/kanban-card-content.tsx` → `getTaskStateIcon`), where
  `BLOCKED` is an amber alert-circle labelled "Blocked" (`web/lib/ui/state-icons.tsx:35`).
  Kandev overwrites the state on the task's next run (`SCHEDULING` in
  `orchestrator/task_operations.go:610`, `IN_PROGRESS` in `scheduler/scheduler.go:303`), so the
  flag clears itself. The pacer never writes `title` or `description`: the description **is**
  the agent's prompt (`task_operations.go:409`), so a note left there would end up in the
  agent's context.
- **Usage** `GET /api/v1/agents/usage` →
  `agents[].usage{provider, windows[]{label, utilization_pct, reset_at}}`; `utilization_pct`
  is *consumed*; labels `"5-hour"`, `"7-day"`, `"7-day (Fable)"`.
- **Tasks** `GET /api/v1/workflows/:id/tasks` → `{tasks:[…]}`, each with `workflow_step_id`,
  and — via `dto.FromTask` — the `position`, `priority` and `created_at` the launch ordering
  above needs. `position` is the board drag order **scoped to a workflow step**, `priority` is
  a free-standing `critical|high|medium|low` attribute (default `medium`); neither is scoped to
  a repository, so one column mixes and orders tasks from every repo in the workspace.
  **Sessions** `GET /api/v1/tasks/:id/sessions` → `{sessions:[…]}` with `state` and
  `started_at` — note there is **no** `created_at` on a session, so ordering by recency uses
  `started_at` (tasks *do* have `created_at`).
  **Steps** `GET /api/v1/workflows/:id/workflow/steps` → `{steps:[…]}` with `stage_type`,
  `events`, `is_start_step`, `position`. `/api/v1` is unauthenticated.

## Run

```bash
uv sync                        # create the environment and install kandev-pacer
./make_paced.py             # list workflows in the kandev SQLite DB
./make_paced.py 'Paced Kanban'          # dry run: show the edits pacing needs
./make_paced.py 'Paced Kanban' --apply  # write them (server can stay up; WAL)
uv run kandev-pacer --dump-usage     # inspect live usage payload first
uv run kandev-pacer --dump-order     # show the queue order launches will follow
uv run kandev-pacer                  # start the pacing loop (lints on startup)
uv run kandev-pacer --watch-usage 30 # read-only usage sampler (run alongside)
./sim_concurrency.py       # is a higher PACER_CONCURRENCY worth it?
                            #   --levers | --history | --measure <pacer.jsonl>
uv run test_pace.py         # pure pacing + classification tests (no network)
uv run test_make_paced.py   # pure workflow-adaptation planning tests
uv run test_make_paced_db.py  # SQLite round-trip tests (temp db, no server)
uv run test_drain_io.py     # queued-prompt release vs a fake kandev (loopback, no quota)
```

`./pacer.py` remains as a compatibility entry point. The installed code lives in
`src/kandev_pacer/`: `quota.py` and `workflow.py` hold the pure rules, `client.py`
owns Kandev I/O, and `runtime.py` owns mutable loop state and the CLI.

**Workflow setup:** `make_paced.py` adapts an existing workflow in place: it strips every
`on_enter: auto_start_agent` action and normalizes `stage_type` (`work` for steps that route
away on `on_turn_complete` — which is what enrolls them, since the pacer then requires that tag
*and* the routing — `review`/`approval` for
parked steps named via `--review`/`--approval`, with any step named Review defaulting to
`review`). A parked step that already carries a valid tag by hand (`review`, `approval`,
`custom`) keeps it; only a missing tag or a stale `work` tag on a parked step falls back to
`custom`. It edits `~/.kandev/data/kandev.db`
directly (`--db`/`KANDEV_DB` to override) because `stage_type` has no API/UI/import surface;
dry run is read-only, and a workflow re-import silently resets `stage_type`, so re-run it after
any import. The steps it leaves alone still show as `ok` lines, and it prints the same lint
warnings the pacer would.

Config via env: `KANDEV_URL`, `KANDEV_WS_URL` (default derives `ws://host/ws`),
`PACED_WORKFLOW_ID` (comma-separated ids or names; unset = every enrolled workflow),
`PACER_PROVIDER` (default `anthropic`), `PACER_CONCURRENCY`, `PACER_ALPHA`,
`PACER_RESUME_MARGIN`, `PACER_BACKSTOP`, `PACER_INTERVAL`, `PACER_LOG`, `PACER_ORDER`
(`auto` | `custom` | `board`, default `auto`), `PACER_FAIL_BACKOFF` (seconds before a refused
launch is retried, default 3 ticks, doubling per failure), `PACER_BLOCK_AFTER` (consecutive
failures before the card is flagged `BLOCKED`, default 2; `0` = never write to the board), and
optional `PACER_ADMISSION_STEP_ID`
(else the lowest-position pace-eligible step; applies only to the workflow that owns it).

## Logging

The stdout lines say what the pacer is doing *now*; the JSONL log at `PACER_LOG` (default
`~/.local/state/kandev-pacer/pacer.jsonl`, `PACER_LOG=''` disables) says what it did over the
last week. That history is what any change to the concurrency cap has to be argued from —
every such question is either a time derivative (does headroom trend up while work is queued?
how long after a launch does `utilization_pct` move?) or a per-window breakdown, and neither
survives in a formatted line with no timestamp.

Five record kinds, append-only, ~300 KB/day at the default 120s tick:

- `tick` — one per tick, **in every branch**. Carries `gate` (the quota verdict:
  `open`/`paused`/`ceiling`/`blind`) and `action` (what was done: `launched`/`drained`/`full`/
  `nothing`/`skipped`/`empty`/`exhausted`/`withheld`) as separate fields, plus `running`,
  `launchable`, `cap`, `booked`, and the full per-window breakdown `{label, c, e, h, d,
  resets_at}`. Gate and action are split because the decisive question — is quota going unspent
  while work waits? — is exactly the `gate=open, action=nothing, launchable>0` combination,
  which one merged field hides. Windows are logged individually rather than collapsed to
  `min(h)` because headroom is in *window-fraction* units, which are not comparable across
  windows: `h=0.1` is 30 minutes of slack on the 5-hour window and 16.8 hours on the 7-day.
  `d` is what converts back to seconds. `launchable` counts launch candidates only — counting
  drains too would need a `queued_counts()` WebSocket round trip on every tick, including the
  paused ones the record exists to capture.
- `session` — an observed `{from → to}` state transition. Turn boundaries can't be derived
  from launch records: kandev starts the agent in a detached goroutine, so the launch call
  returns before `STARTING` appears, and nothing at all is recorded when a turn *ends*.
- `window_reset` — a window rolled over, with `final_c`: what it *closed* at. Answers "did the
  window end with quota unspent?", and is unrecoverable after the fact — once the endpoint
  reports the new window, the old number is gone.
- `failure` — a launch or drain kandev refused, with the reason and the consecutive-failure
  count. Its own kind rather than a tick outcome, for the same reason `_FAILED` is not
  `_BOOKED`: a refused launch spends no quota, and the tick goes on to the next candidate.
- `start` / `error` — run configuration (so analysis can segment by `cap`, `alpha`, `order`, …)
  and loop failures.

To make this uniform the pacer reads tasks and sessions on **every** tick, including paused
ones — it previously returned early when the gate was shut, leaving no record for precisely
the period that matters most, since running turns keep burning after the gate closes. Cost is
one tasks call plus one sessions call per task per tick, against a local server.

`--watch-usage N` is a separate read-only mode that polls usage every `N` seconds and appends
`usage` records to the same log. Run it alongside the pacer to time how long after a turn
starts `utilization_pct` actually moves: that dead time bounds any concurrency rule (N parallel
turns overshoot by N × burn_rate × dead_time), and the 120s control tick is far too coarse to
resolve it. Separate process, so the measurement cadence never perturbs the control loop.

## Packaging as a kandev plugin

Assessed and rejected as a *native* plugin: kandev's plugin Host API has no session-launch
call, no usage/quota reader, no `workflow_step_id` on its `Task`, and no step routing on its
`WorkflowStep` — so the pacing loop would still run on `/api/v1` + `/ws` exactly as it does
today. Viable only as a lifecycle/config/UI wrapper. See
[`docs/kandev-plugin-feasibility.md`](docs/kandev-plugin-feasibility.md).

## Design provenance

Distilled from the kandev integration investigation in the vault:
`pages/IT/kandev - Claude Code integration and auth - 2026-07.md` (the pacer was ideated
under "the quota endpoint you need for the pacer" and "the cron pacer has two viable shapes").
