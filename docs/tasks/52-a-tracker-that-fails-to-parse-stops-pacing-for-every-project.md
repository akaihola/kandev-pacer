# A tracker that fails to parse stops pacing for every project

On 2026-10-17, commit `d9bbfb6` in hai wrapped one reference-style link definition
in hai's `TASKS.md` across two lines. Hai's parser then raised `TasksError` ("Task
[23] has no description reference"), and every T3 tick logged `T3 tick withheld
(TasksError); inspect tracker and launch records` from 09:14 until the line was
rejoined (`fe02617`, about 10:00). Restarting the pacer in that state would have
failed at startup ("T3 startup failed (TasksError)"), as `--dump-order` did. This is
the same class of fault as [39], [43] and [44].

- An unreadable tracker skips only itself, with a warning naming the tracker and
  Hai's message, as a dirty checkout does since [31]. Its launch records stay in
  place, preventing duplicates.
- Startup and `--dump-order` report the unreadable tracker and continue.
- Tests: one tracker with a malformed link definition among valid ones.

Acceptance: with one malformed tracker, the other trackers keep launching, and the
status line names the skipped tracker.

## Implementation and validation, 2026-10-17

T3 reads each tracker independently before scheduling. Hai read errors and file
access errors produce a warning naming the source and error; healthy trackers
continue. Records belonging to an unreadable tracker remain unchanged, including
pending submissions. Running threads still count against capacity. Each tick
retries the read, and startup and `--dump-order` use the same handling.

Regression fixtures split a real reference-style link definition across two lines
among valid definitions. They cover a running pacer, restart, normal startup,
`--dump-order`, repair, and prepared, submitted, accepted and observed records.
They check that healthy projects launch and saved command IDs survive recovery.

Validation used the canonical checkout's Python environment with `PYTHONPATH=src`
to load this worktree's code. The full offline suite passed with 924 tests and
20 opt-in live tests skipped. After adding an explicit normal-startup case, all
seven unreadable-tracker cases passed. `darker --check`, `graylint` and
`git diff --check` passed. No live launches were made; live T3 deployment remains
part of the separate V2 rollout described in task [49].
