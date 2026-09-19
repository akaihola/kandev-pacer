---
depends_on: []
---

# Tune 5-hour gating and interactive hours while retaining weekly and backstop gates

## Original request

Gate on the weekly windows and make the 5-hour rule soft. When the Claude gate
paused, the 5-hour window bound it six times more often than the 7-day one, while the
general weekly window ended two of the last five weeks at about half. Add
`PACER_ALPHA_5H` (default `1.0`, so nothing changes until set) applied to 5-hour
windows only, and `PACER_INTERACTIVE_HOURS` (e.g. `08-23`, default empty) outside of
which the 5-hour rule is skipped and only the weekly windows and the backstop gate.
Deprioritized 2026-09-11: on Max 20x the weekly window is far from binding. Keep it
for the two knobs, which matter again if usage grows.
