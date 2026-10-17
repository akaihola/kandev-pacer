# Hold unresolved V1 launch records before quota and capacity checks

Found by the steward's pre-cutover review of [49], [51] and [52] at `1a444a5`
(vault issue [14], T3 thread `287f13fe`, 2026-10-17). Fix this before gogo's T3
moves to V2: until then a closed gate can hide a saved V1 command from the
migration hold that [49] promises.

## Defect

In `src/kandev_pacer/t3.py` at `1a444a5`, `admitted()` (lines 869–879) checks
pending records and returns early when its gate is closed. The check for a
legacy `bootstrap` command comes later, in `dispatch()` (lines 973–974).

Reproduction: save a V1 `thread.turn.start` command with `bootstrap` in
`prepared`, `scheduled` or `submitted`, and leave its thread out of the imported
snapshot. Give it a disabled provider, and configure a second healthy tracker
with an enabled provider. After two ticks nothing has launched, the old record
has no inspection hold, and the migration warning never appears.

- With open gates the same record holds only its own tracker, and the healthy
  tracker launches (six phase and gate probes passed).
- By inspection, closed quota and a full per-provider slot take the same early
  return; the review did not exercise them.
- Paced prompt admissions run before the return and still proceed.
- `test_saved_v1_launch_is_held_before_v2_dispatch` uses an open gate and a V2
  envelope with an added `bootstrap`, so it misses this case.

## Fix

During launch-record reconciliation, after checking whether the record's thread
exists, classify an absent-thread V1 command as a permanent tracker hold before
any quota or capacity check. Keep its command ID and duplicate protection,
report the inspection action, and go on with the other trackers. For a
`prepared` record, do this before changing the tracker or committing its
schedule. Never translate or replay a V1 command on V2.

## Acceptance

- Regression tests with real V1 envelopes in all three phases, each under closed
  quota, a disabled provider and a full provider slot, show a named migration
  hold and a launch from another eligible tracker.
- Imported threads that still exist reconcile without relaunching.
- The hold and the record survive a restart.

## Review context

The same review found the rest of [49], [51] and [52] ready: the V2 wire
contract, reconciliation of imported records, the paced-only caps and the
isolation of an unreadable tracker. `uv run pytest tests/test_t3.py
tests/test_t3_client.py -q` passed 213 tests. Failed V2 workspace-setup cleanup
remains unverified, as [49] says. The full review note is in the vault,
`git -C /home/agent/my-knowledge show 606b679:"agents/steward/inbox/2026-10-17 kandev-pacer V2 cutover review.md"`.

## Completion evidence, 2026-10-17

Implemented in `036b2bf`. Reconciliation now gives absent V1 launches a permanent
tracker inspection hold before scheduling or admission. Saved commands and IDs
remain intact. Imported active and archived threads reconcile without relaunching.

- Real V1 envelopes cover prepared, scheduled and submitted records under closed
  quota, disabled providers, full provider slots and open gates. Every case also
  checks another tracker's launch, unchanged tracker contents and Git revision,
  and hold persistence after restarting the pacer.
- Six further cases cover active and archived imported threads in all three phases.
- Removing the fix makes all 12 absent-thread cases fail.
- `uv run --extra t3 pytest -q`: 942 passed, 20 skipped.
- `uv run darker --check`, `uv run graylint` and `git diff --check` passed.

Completion awaits user acceptance. The live V2 cutover and failed workspace-setup
cleanup remain outside this issue's validation.
