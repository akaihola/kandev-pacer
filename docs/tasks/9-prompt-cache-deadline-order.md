---
depends_on: []
---

# Order launchable tasks by prompt-cache deadline, preserving queue tie-breaks

## Original request

Order launchable tasks by prompt-cache deadline while the gate is open (rule 1 of
`docs/cache-aware-pacing.md`): the task whose session's last turn ended longest ago
but is still inside its provider's cache TTL goes first. No knob. Keep pull order and
drag order as the tie-break so `--dump-order` still explains the queue. Worth less
since the 2026-09-11 subscription upgrade — a gate that stays open makes fewer
cache-killing waits — but the rule has no knob and no failure mode.
