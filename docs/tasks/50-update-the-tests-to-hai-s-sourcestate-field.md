# Update the tests to Hai's `sourceState` field

On `main`, `uv run pytest tests/test_t3.py tests/test_t3_client.py -q` fails 7 tests
(Antti, 2026-10-17):

```
FAILED tests/test_t3.py::test_lost_reply_restart_preserves_one_thread_and_settings - KeyError: 'sourceSection'
FAILED tests/test_t3.py::test_finished_turn_never_relaunches_or_accepts[completed] - KeyError: 'sourceSection'
FAILED tests/test_t3.py::test_finished_turn_never_relaunches_or_accepts[error] - KeyError: 'sourceSection'
FAILED tests/test_t3.py::test_finished_turn_never_relaunches_or_accepts[interrupted] - KeyError: 'sourceSection'
FAILED tests/test_t3.py::test_dependency_order_and_excluded_sections - hai_tasks.workflow.TasksError: Unknown or read-only issue field.
FAILED tests/test_t3.py::test_scheduled_before_backlog - hai_tasks.workflow.TasksError: Unknown or read-only issue field.
FAILED tests/test_t3.py::test_submitted_record_for_moved_issue_holds_only_that_issue - hai_tasks.workflow.TasksError: Unknown or read-only issue field.
```

Cause: Hai removed its `sourceSection` aliases in `fc6979d` ("refactor: remove
sourceSection aliases", 2026-10-15 11:55), four minutes after this repository's
`990d23d` moved `src/` to `sourceState`. The tests still read and write
`sourceSection` (`tests/test_t3.py` lines 258, 281, 447, 798 and 978, and
`tests/test_heartbeat.py` line 182). `src/` no longer uses it.

- Replace `sourceSection` with `sourceState` in the tests; change no production
  code unless a test shows a real defect.
- Acceptance: `uv run pytest -q` passes in full, and
  `grep -rn sourceSection src tests` finds nothing.

This blocks verifying [31] and [21], which are awaiting acceptance, and the test
work in [49].
