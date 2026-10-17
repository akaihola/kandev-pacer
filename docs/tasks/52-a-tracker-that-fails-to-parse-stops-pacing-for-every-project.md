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
