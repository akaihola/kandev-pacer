# Review: post-review fixes (228cf81, 97763de)

Date: 2026-07-25. Independent fresh-context two-axis pass (Standards + Spec) over the
four post-review fixes plus the Writing-Level wording follow-up. An earlier orchestrated
review already passed; this confirms it.

Scope reviewed:

- `228cf81` — fix: close duplicate-start, stale-step, lint-scope, and provider fail-open gaps
- `97763de` — review: anchor 'goroutine' behind its observable effect (Writing Level)

Diff command used: `git diff ffb1c1b..97763de -- pacer.py README.md test_pace.py`
(`ffb1c1b` is the parent of `228cf81`). Files: `pacer.py`, `README.md`, `test_pace.py`.

## Verdict

- **Spec: 0 findings.** All four fixes present, correct, and non-creeping. 24 tests pass.
- **Standards: 2 findings**, both judgement-call smells, zero hard violations.
- No code fixes made or committed — the only actionable item is an optional refactor.

## Spec axis (pass)

The four required fixes all land where asked:

- **W1 (TTL bookings):** `_BOOKED` is a dict `key -> booked_at` (`pacer.py:300`);
  `BOOKING_TTL = 3*INTERVAL` (`pacer.py:39`); `prune_booked` (`pacer.py:227-253`)
  holds a `sid is None` admission booking while `now-booked_at < ttl`, and a `sid`-set
  booking only while `WAITING`. Hold/drop traced self-consistent: admission is keyed
  `(task, queue_step, None)`; during the invisible-session window the task still reads
  `(queue_step, no-session)` so the key matches and `is_launchable` (`pacer.py:206`) stays
  guarded against a double start; once a real session surfaces `sid` is non-None so the key
  mismatches and drops naturally; TTL expiry frees a silently-failed launch.
- **P1 (re-read step before launch):** `fresh = next(... paced_tasks() ...)` re-reads
  immediately before `session.launch`, skips on step change, and `launch_target(fresh, ...)`
  uses the fresh step (`pacer.py:343-350`). Replaces the earlier false "never cached" comment.
- **P2 (lint scope):** the `auto_start_agent` and profile+prompt checks moved out of
  `if s["id"] in works:` and now run on every step (`pacer.py:169-172`). Does **not** flag
  the CLEAN fixture; `lint_workflow(CLEAN) == []` still holds.
- **Provider fail-open guard:** `provider_missing` + fail-closed `return False` when
  `not windows and provider_missing` (`pacer.py:119-128`, `310-313`); README documents it.
  Fires only on a non-empty payload missing our provider; empty payload → no withhold;
  provider-present-but-no-windows (fresh account) → paces normally. All three cases correct.

`97763de` is comment-wording only (`pacer.py:37`), no behaviour change — confirmed.

Awareness note (within spec, not a defect): P1 re-reads the *step* but not the session
*state*; a session that flips `WAITING -> RUNNING` between selection and launch is caught
only by `_BOOKED`, not by the re-read. The spec asked only for the step re-read.

## Standards axis (2 judgement calls, no hard violations)

Documented standard is AGENTS.md → Writing Level. No violations: "goroutine" (`pacer.py:37`),
"fails closed"/"fail-open" (README + `provider_missing` docstring), and "TTL" (`prune_booked`)
are each anchored to an observable effect on first use.

Baseline smells (Fowler ch.3), both judgement calls:

- **Duplicated Code / Primitive Obsession** — the booking key 3-tuple
  `(task_id, step_id, session_id)` is built at two sites: the write in `tick`
  (`pacer.py:352`) and the derive in `prune_booked` (`pacer.py:245`). A future key change
  needs edits in both. Optional fix: a one-line `booking_key(task, sid)` helper. Low urgency
  — both sites are in one file and currently consistent; not adopted to avoid adding an
  abstraction against the repo's minimal style.
- **Divergent Change (minor)** — `prune_booked` now carries two retention rules branching on
  `sid is None`. Reads clearly and is well-tested (`test_prune_booked_never_run_admission`);
  left as-is.

## Out of scope

The untracked `test_make_paced_db.py` belongs to the earlier `make_paced.py` work
(`ffb1c1b`), not these two commits; left untouched.
