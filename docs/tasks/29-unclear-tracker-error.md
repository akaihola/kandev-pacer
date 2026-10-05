This message on the console:

    T3 withheld: Tracker must match exactly one T3 project

is a bit vague. It's unclear what it means, whether it prevents normal operation, and what the user should fix.

## Implementation evidence, 2026-10-05

The error now names the tracker, the required project workspace root, and the
number of matching projects. Zero matches explain how to add the project or fix
tracker/server configuration. Multiple matches explain how to resolve duplicate
workspace roots. It states that new launches stop for the current tick, existing
threads continue, and the pacer retries next tick.

Validated with 110 focused T3 tests and the full suite, 735 passed and 20 skipped.
Differential formatting, lint, and type checks passed. Regression cases cover
missing projects, wrong roots, duplicate roots, unchanged tracker/launch records
on failure, and successful retry after correction, with and without a parent.

Merged into the canonical main checkout. The running T3 pacer is a manual process
in a tmux scope, with no pacer user service available. It still has the previous
Python module loaded; restart that process to activate the new wording.
