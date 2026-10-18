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

## Implementation and validation, 2026-10-18

Tests changed; `src/` did not. [52]'s code behaved as specified throughout.

- New malformation: `break_tracker()` in `tests/test_t3.py` gives `[1]` a second,
  conflicting link definition (`docs/tasks/1.md` and `docs/tasks/2.md`). Hai
  rejects it with "Duplicate description reference [1]". Hai's own tests pin
  that message: `tests/python/test_prettier.py` gained the assertion in the same
  commit `abbb1ac` that made the old fixture readable. The other candidates
  were weaker: an unterminated `hai:` comment is read as description text
  today, and no Hai test asserts the message for duplicate issue IDs. The
  helper also reads the broken tracker itself and fails with "DID NOT RAISE"
  if Hai ever accepts it, so a future tolerance change shows up as a clear
  fixture failure, not as the pacer tests quietly testing nothing.
- `time.sleep`: `test_startup_and_dump_order_skip_unreadable_tracker` patched
  the global function to stop `run()` after one tick. `t3.git()` runs
  `subprocess.check_output(timeout=30)`, and subprocess's wait loop calls
  `time.sleep` too, so a slow git call could raise the stop signal in the
  middle of the tick, before anything launched. The test now replaces only
  `t3`'s `time` module, with a copy whose `sleep` stops the loop.
- `ty`: `_simulation()` in `tests/test_resets.py` asserts that
  `spec_from_file_location` found the script and a loader. That narrows
  both `None` cases, and a moved script now fails with its path.

Evidence, from this worktree with an isolated HOME
(`HOME=$TMPDIR/home XDG_CONFIG_HOME=$TMPDIR/home/.config`, see [52]'s note on
the user config):

- [52]'s 7 tests: 7 failed before the change, 7 passed after, against Hai
  `284213c` (current). Against Hai `abbb1ac^`, exported to `PYTHONPATH`, all
  7 pass, including the `[False]` case that was timing-dependent there.
- Full suite: 942 passed, 20 skipped. `uv run ty check`: all checks passed.
  `darker --check`, `graylint` and `git diff --check`: clean.
- Mutations of `T3Pacer.read_trackers()`, each reverted: catching no
  errors (`except ()`) failed all 7 tests; returning `{}` on the first
  unreadable tracker, so it withholds every tracker as before [52], also
  failed all 7.

Not changed: `test_connection_error_is_logged_and_tick_retries` also patches
the global `time.sleep`. It is not one of [52]'s tests and passed in every
run here.
