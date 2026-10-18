---
depends-on: [58]
---

Steward issue, 2026-10-18. Goal: agentic-leverage. `main` is red, and the
cutover restart (decision D29) runs `main`. The steward's evidence check
for the 2026-10-18 digest found:

- 7 of [52]'s regression tests fail:
  `uv run --extra t3 pytest -q tests/test_t3.py -k 'unreadable_tracker_skips_only_itself_and_retains_records or startup_and_dump_order_skip_unreadable_tracker'`.
  Their fixtures break a tracker with a malformation that Hai's reader
  accepts since Hai `abbb1ac` (hai [36], "Read TASKS.md unchanged after a
  Prettier pass with Zed's settings", 2026-10-17 17:17). With Hai from
  `abbb1ac^` on `PYTHONPATH`, 6 pass and 1 fails. [52]'s code still
  works (its live probe in docs/tasks/55-…), so the tests no longer test
  anything.
- `uv run ty check` reports 3 errors, all in `tests/test_resets.py`
  around lines 202–203 (`_simulation()`'s `spec_from_file_location`).

The full `tests/test_t3.py tests/test_t3_client.py` run gives 223 passed,
7 failed, all [52]'s.

## Work

1. Point [52]'s fixtures at a malformation that current Hai rejects, and
   one that keeps failing if Hai grows more tolerant again (for example
   an unterminated `hai:` metadata comment or a duplicate issue ID; check
   with Hai's own tests which errors are stable). Stop patching the
   global `time.sleep` if the tests do; patch the module under test.
2. Fix the three `ty` errors without silencing the checker for the file.
3. Run the full suite and `uv run ty check`; both must be clean.

## Ground rules

- Change tests only, unless a test shows [52]'s code itself is wrong;
  then stop and describe it instead of changing `src/`.
- Never touch the running pacer, its config or its state. Don't push.

## Acceptance

- The full test suite passes and `uv run ty check` is clean on `main`.
- Breaking the tracker parse in [52]'s code (for example skipping the
  unreadable-tracker branch) makes its tests fail again.
