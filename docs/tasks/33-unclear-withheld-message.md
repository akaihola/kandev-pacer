Kandev-pacer says on the console:

    T3 withheld: Launch record does not match endpoint/trackers

The message is unclear. What does withholding mean? What is a launch record? What are endpoints/trackers? How to fix the situation?

## Implementation, 2026-10-10

The mismatch error now names the saved launch record and issue, identifies a changed
T3 server URL, an unconfigured TASKS.md path, or both, and explains that new launches
pause while existing threads continue. It describes why records prevent duplicates
and gives configuration restoration and obsolete-record recovery steps. Raw saved
URLs stay out of logs. See [recovery documentation](../t3-pacing.md#launch-record-mismatch).

Validation: `uv run --frozen --extra t3 --extra heartbeat pytest -q` passed with
805 tests passing and 20 live tests skipped. The 109 T3 tests include URL-only,
tracker-only, and combined mismatches, checking credential omission and no tracker,
record, or dispatch changes on rejection. `uv run --frozen darker --check`,
`uv run --frozen graylint`, and `git diff --check` passed. No live turn was launched.
