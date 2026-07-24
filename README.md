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
  task to the first work step *and* starts it — `intent: start` for a never-run task,
  `intent: workflow_step` for a re-queued one. A supplied `workflow_step_id` moves the task
  (`moveTaskToWorkflowStep` / `advanceTaskWorkflowStep`), so no separate move API is needed.

**Work-step classification is read from Kandev, not a local list.** A step counts as a *work
step* when — and only when — its `on_turn_complete` carries a transition action (`move_to_next` |
`move_to_previous` | `move_to_step`) — writable via the normal step-edit API and self-consistent
with the rule that a completed turn always moves the task off the step. That gives us a handy
guarantee: if a task is *still sitting* on a work step, its turn hasn't finished yet, so the
pacer won't double-launch it. `stage_type: "work"` is a corroborating hint only (it is read-only
via the step API; settable only out of band, via workflow import or a direct SQLite write).
Parked steps (Review/Done, the queue)
must route via `on_turn_start`/`on_comment`, never `on_turn_complete`. On startup the pacer
lints the workflow and warns about bypass/ambiguity (leftover `auto_start_agent`, profile-switch
+ prompt, signal-gated routing, stage_type/routing disagreement, missing work step or queue).

**Priority tiering is free**: the gate reads *account-level* usage, which already includes
Routine/UI burn. When high-priority work pushes `c` above `e`, headroom goes negative and the
gate closes automatically. You classify by *which workflow* you create a task in.

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
- **Usage** `GET /api/v1/agents/usage` →
  `agents[].usage{provider, windows[]{label, utilization_pct, reset_at}}`; `utilization_pct`
  is *consumed*; labels `"5-hour"`, `"7-day"`, `"7-day (Fable)"`.
- **Tasks** `GET /api/v1/workflows/:id/tasks` → `{tasks:[…]}`, each with `workflow_step_id`.
  **Sessions** `GET /api/v1/tasks/:id/sessions` → `{sessions:[…]}` with `state`.
  **Steps** `GET /api/v1/workflows/:id/workflow/steps` → `{steps:[…]}` with `stage_type`,
  `events`, `is_start_step`, `position`. `/api/v1` is unauthenticated.

## Run

```bash
./make_paced.py             # list workflows in the kandev SQLite DB
./make_paced.py 'Paced Kanban'          # dry run: show the edits pacing needs
./make_paced.py 'Paced Kanban' --apply  # write them (server can stay up; WAL)
./pacer.py --dump-usage     # inspect live usage payload first
./pacer.py                  # start the pacing loop (lints the workflow on startup)
uv run test_pace.py         # pure pacing + classification tests (no network)
uv run test_make_paced.py   # pure workflow-adaptation planning tests
```

**Workflow setup:** `make_paced.py` adapts an existing workflow in place: it strips every
`on_enter: auto_start_agent` action and normalizes `stage_type` (`work` for steps that route
away on `on_turn_complete` — the same classifier the pacer uses — `review` for parked steps
named Review or listed via `--review`, `custom` otherwise). It edits `~/.kandev/data/kandev.db`
directly (`--db`/`KANDEV_DB` to override) because `stage_type` has no API/UI/import surface;
dry run is read-only, and a workflow re-import silently resets `stage_type`, so re-run it after
any import. The steps it leaves alone still show as `ok` lines, and it prints the same lint
warnings the pacer would.

Config via env: `KANDEV_URL`, `KANDEV_WS_URL` (default derives `ws://host/ws`),
`PACED_WORKFLOW_ID`, `PACER_PROVIDER` (default `anthropic`), `PACER_CONCURRENCY`, `PACER_ALPHA`,
`PACER_RESUME_MARGIN`, `PACER_BACKSTOP`, `PACER_INTERVAL`, and optional `PACER_ADMISSION_STEP_ID`
(else the lowest-position work step). Defaults are the live `gogo` "Paced Kanban" ids.

## Design provenance

Distilled from the kandev integration investigation in the vault:
`pages/IT/kandev - Claude Code integration and auth - 2026-07.md` (the pacer was ideated
under "the quota endpoint you need for the pacer" and "the cron pacer has two viable shapes").
