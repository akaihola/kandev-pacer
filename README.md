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

**A never-run task already has a session.** Creating a task provisions its workspace, worktree
and branch immediately and parks the session in `CREATED` — prepared, agent never started. So
"never run" does **not** mean "no session", and the pacer treats `CREATED` as launchable
alongside `WAITING_FOR_INPUT`. Getting this wrong is silent and total: tasks sit in the queue
forever while the pacer cheerfully logs `nothing to launch` every tick.

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

## Launch order: you dictate it from the sidebar

Which pending task goes next is **the order of your Kandev `TASKS` sidebar list**. Put a
sidebar view on **sort `custom`, group `none`**, drag the tasks into the order you want, and
the pacer works down that order — no pacer-side priority field to maintain.

- **Placed tasks first**, in your drag order. **Pinned tasks float above them** (pinning is a
  visible re-ordering, so honoring it keeps "the pacer runs the top-most task" true).
- **Subtasks follow their parent**, in their own stored order — the sidebar nests them there.
- **Tasks you never dragged run last, oldest-first.** "Not placed" means "not prioritized", so
  they keep the pacer's original FIFO order behind everything you did place. Note this is a
  *deliberate* divergence from the sidebar, which floats unplaced tasks to the *top* by newest
  `created_at`: copying that would let merely *creating* a task jump an order you set by hand.
- Re-read **every tick**, so a drag re-prioritizes the queue without restarting the pacer.
- The sidebar list spans **every** workflow while the pacer only sees enrolled ones, so a task
  can sit high in your manual order and still be out of scope. `--dump-order` names those
  explicitly (`placed by hand but not paced`) rather than leaving them to look lost.

`PACER_ORDER` picks the policy:

| value | behavior |
| --- | --- |
| `auto` (default) | Follow the manual order once **any** saved sidebar view uses sort `custom`; plain FIFO otherwise. |
| `custom` | Always follow the manual order, whatever the sidebar's current sort. |
| `created` | Plain FIFO by `created_at` — the pre-feature behavior; skips the settings fetch entirely. |

**Why "any saved view" and not the active one?** The manual order (`ordered_task_ids`) is
stored **once per user, not per view**. So a saved "Prioritize" view still expresses your
intended order while you have some other view selected to work in — keying on the *active*
view would silently flip the pacer back to FIFO the moment you switched views for an unrelated
reason. The in-flight *draft* counts too: dragging a task flips the sort to `custom` in the
draft before it is ever saved to the view.

**Fails soft, never sideways.** FIFO is the base order in every mode and the manual list is
layered on top, so anything missing degenerates cleanly: settings unreadable (logged, pacing
continues — an HTTP hiccup here must not pause the gate), nothing dragged yet, or a task whose
parent chain is cyclic all fall back to oldest-first rather than to an arbitrary order.

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
- **Usage** `GET /api/v1/agents/usage` →
  `agents[].usage{provider, windows[]{label, utilization_pct, reset_at}}`; `utilization_pct`
  is *consumed*; labels `"5-hour"`, `"7-day"`, `"7-day (Fable)"`.
- **Tasks** `GET /api/v1/workflows/:id/tasks` → `{tasks:[…]}`, each with `workflow_step_id`.
  **Sessions** `GET /api/v1/tasks/:id/sessions` → `{sessions:[…]}` with `state` and
  `started_at` — note there is **no** `created_at` on a session, so ordering by recency uses
  `started_at` (tasks *do* have `created_at`).
  **Steps** `GET /api/v1/workflows/:id/workflow/steps` → `{steps:[…]}` with `stage_type`,
  `events`, `is_start_step`, `position`. `/api/v1` is unauthenticated.

## Run

```bash
./make_paced.py             # list workflows in the kandev SQLite DB
./make_paced.py 'Paced Kanban'          # dry run: show the edits pacing needs
./make_paced.py 'Paced Kanban' --apply  # write them (server can stay up; WAL)
./pacer.py --dump-usage     # inspect live usage payload first
./pacer.py --dump-order     # show the queue order launches will follow
./pacer.py                  # start the pacing loop (lints the workflow on startup)
uv run test_pace.py         # pure pacing + classification tests (no network)
uv run test_make_paced.py   # pure workflow-adaptation planning tests
uv run test_make_paced_db.py  # SQLite round-trip tests (temp db, no server)
```

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
`PACER_RESUME_MARGIN`, `PACER_BACKSTOP`, `PACER_INTERVAL`, `PACER_ORDER`
(`auto` | `custom` | `created`, default `auto`), and optional `PACER_ADMISSION_STEP_ID`
(else the lowest-position pace-eligible step; applies only to the workflow that owns it).

## Design provenance

Distilled from the kandev integration investigation in the vault:
`pages/IT/kandev - Claude Code integration and auth - 2026-07.md` (the pacer was ideated
under "the quota endpoint you need for the pacer" and "the cron pacer has two viable shapes").
