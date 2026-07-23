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

## Enforcement: admission control, not preemption

Don't kill running turns — gate what's allowed to *start*. The unit is a **turn** (a prompt
that spends quota), gated at the task boundary.

- A **"Paced Kanban"** workflow holds low-priority tasks in a **Backlog** step whose events
  are `{}` (no `auto_start_agent`) — tasks sit there inert.
- The pacer moves one Backlog task at a time into the **In Progress** step (which has
  `on_enter: auto_start_agent`) via `POST /api/v1/tasks/:id/move`. Entering that step
  starts the agent.
- Priority tiering is **free**: the gate reads *account-level* usage, which already includes
  Routine/UI burn. When high-priority work pushes `c` above `e`, headroom goes negative and
  the Backlog gate closes automatically. kandev needs no concept of priority — the shared
  quota number is the coordination channel. You classify by *which workflow* you create a
  task in.
- Backstop (not yet implemented): near an absolute ceiling (`c > ~0.97` on any window),
  `stop_task` low-priority in-flight to avoid a hard mid-turn lockout. Never touches
  high-priority.

Concurrency cap (`PACER_CONCURRENCY`, default 1) bounds how many paced tasks run at once;
with slow polling and parallel tasks a high cap can overshoot the line between ticks.

## Confirmed against kandev source (`~/repos/ai/kandev`, 2026-07-23)

- **Move → auto-start works, including for a task that never ran.**
  `handleTaskMovedNoSession` (`orchestrator/event_handlers_workflow.go:397`): moving a task
  with no session into a step where `HasOnEnterAction(OnEnterAutoStartAgent)` creates a
  session and calls `StartTask`. So Backlog → In Progress reliably fires the agent.
- **New tasks land in the workflow's start step** (`ResolveStartStep`,
  `backendapp/services.go:224`; unique index `idx_workflow_steps_single_start`).
- **Endpoints:** usage `GET /api/v1/agents/usage`; list `GET /api/v1/workflows/:id/tasks`
  (bare `GET /api/v1/tasks` 404s); move `POST /api/v1/tasks/:id/move` with body
  `{workflow_id, workflow_step_id, position}` (both ids required). `/api/v1` is unauthenticated.

### ⚠️ The Paced Kanban workflow needs one fix

Its `is_start_step` is on **In Progress**, so newly created tasks land there and
**auto-start immediately, bypassing Backlog**. For pacing to work, move the start flag to
**Backlog**. (Backlog events are correctly `{}`; In Progress correctly has
`on_enter: auto_start_agent`.) Also note the Review/Done steps have `on_turn_start` events
that move a task back into In Progress and auto-start it — a re-engagement path that bypasses
pacing on manual re-prompt. Acceptable (user-initiated), but a known gap.

## Open questions (before this runs for real)

1. **`/api/v1/agents/usage` response shape** — `parse_windows()` is written against the
   documented Anthropic `limits[]` shape (`percent`, `resets_at`, a window label) but is
   unconfirmed. Run `./pacer.py --dump-usage` against a live kandev and pin it. Confirm
   whether `percent` is consumed or remaining.
2. **Task step field** — `_step_of()` assumes `workflow_step_id` on the task JSON from
   `/workflows/:id/tasks`. Confirm the field name.
3. **Backstop** for the absolute ceiling (`stop_task`) — not yet built.

## Run

```bash
./pacer.py --dump-usage     # inspect live usage payload first
./pacer.py                  # start the pacing loop
uv run test_pace.py         # pacing-math tests (no network)
```

Config via env: `KANDEV_URL`, `PACED_WORKFLOW_ID`, `BACKLOG_STEP_ID`, `ACTIVE_STEP_ID`,
`PACER_CONCURRENCY`, `PACER_ALPHA`, `PACER_RESUME_MARGIN`, `PACER_INTERVAL`. Defaults are
the live `gogo` "Paced Kanban" ids.

## Design provenance

Distilled from the kandev integration investigation in the vault:
`pages/IT/kandev - Claude Code integration and auth - 2026-07.md` (the pacer was ideated
under "the quota endpoint you need for the pacer" and "the cron pacer has two viable shapes").
