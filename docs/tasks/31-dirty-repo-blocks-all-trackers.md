It seems "a dirty `drum-transcribe` checkout is blocking scheduling for every tracker."

Make dirty checkouts only block scheduling tasks for that particular tracker.

## Implementation evidence, 2026-10-06

A dirty checkout, or one not on `t3_base_branch`, now skips only its own tracker.
The pacer logs a warning naming the checkout and tracker, then tries the next
tracker in the same tick. Before, the error escaped the tick and stopped every
tracker listed after the dirty one.

The off-branch check was included because it is the same kind of local checkout
state, and it had the same all-trackers effect. Its message now names the
repository, tracker and expected branch. An unresolved pending launch still
blocks all new admissions on purpose; that is unchanged.

Validated with the full suite, 737 passed and 20 skipped, run with an isolated
HOME. Differential formatting, lint, and type checks passed. Regression cases
cover a dirty first tracker and an off-branch first tracker: the second tracker
launches, the first tracker's TASKS.md revision and HEAD stay unchanged, and one
warning names the blocked checkout.

The running T3 pacer keeps the previous module loaded until it is restarted.
