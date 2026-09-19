---
depends_on: []
---

# Launch the primary session, falling back to the newest only when no primary exists

## Original request

Launch only the primary session. `pick_intent` in `selection.py` takes the newest
session, which on a task with `survey-*` child sessions is a read-only subagent: in the
48 h to 2026-09-06 21 of 60 launches went to such sessions and four cards were flagged
stalled. Prefer the session with `is_primary`, then the newest primary-less session
only when the task has no primary at all. One test per case in `test_selection.py`.
