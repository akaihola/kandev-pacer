# Pace Hai sources using workflow stages

Moved on 2026-10-15 from the Hai tracker, where it was issue 32; that number
stays reserved there. Builds on Hai issue 30 in `~/prg/hai/TASKS.md`, which
made every Hai backend publish `capabilities.sourceStates` with a `stage` per
native state.

kandev-pacer selected and moved work by comparing `sourceSection` strings in
`t3.py`, `heartbeat.py` and `tasks_md.py`, so it could only pace TASKS.md
trackers. With stages it needs one code path for every backend.

## Requirements

- Eligible issues are those whose state has stage `ready` or `scheduled` and
  whose dependencies are done, read from the catalog instead of section names.
- Scheduling moves an issue to the catalog state with stage `scheduled` when
  the workflow has one, and is otherwise a no-op that keeps the issue in its
  `ready` state.
- The worker prompt and the heartbeat name the `active` and `done` states
  from the catalog instead of hard-coding In Progress and Completed /
  Accepted, and keep the acceptance rule only where the backend supports
  acceptance.
- Pacing `.scratch/` features and `docs/backlog/` repositories works without
  provider-specific branches, including `ready-for-human` staying untouched.
- Tests cover eligibility and scheduling on all three workflows.

## Implementation and validation, 2026-10-15

Implemented in commit `990d23d` on main. Eligibility uses catalog stages, with
scheduled work first and dependencies completed within the same project.
Scheduling leaves ready issues unchanged when no scheduled stage exists. Both
T3 and heartbeat prompts name catalog active and done states and condition the
acceptance rule on `fields.acceptance`. T3 opens sources through Hai and
resolves the Git root independently of the source directory. Heartbeat
discovers TASKS.md, scratch and backlog sources. `docs/t3-pacing.md` and
`docs/heartbeat-scripts.md` describe the selection rules.

Validation: the full offline suite passed 856 tests, with 20 live tests
skipped. The final focused suite passed 158 tests, including real files,
scheduling, T3 launches and heartbeat prompts for all three workflows.
Formatting and Ruff/type checks passed with `darker --check` and `graylint`.
Tests also cover missing, unfinished and other-project dependencies. Scratch
waiting states remain untouched.

The manually started pacer process still needs an operator restart to load the
merged Python code. New CLI invocations use it immediately. Completion does
not record user acceptance.
