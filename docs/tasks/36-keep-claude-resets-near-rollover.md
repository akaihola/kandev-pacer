---
depends_on: [34]
---

# Keep long-lived Claude resets when the weekly rollover is close

## Original request

2026-10-10, after the [reset pacing review]: "Write it up and add the tasks."

## Problem

A Claude reset refills the weekly allowance and keeps the scheduled rollover, so the
refill only lasts until that rollover. The pacer redeems whenever the weekly allowance
is used up and a reset is usable, however close the rollover is. In scenario B of
[`scripts/simulate_reset_pacing.py`], work arrives on day 5.9 and the allowance runs out
on day 6.9. The pacer redeems with 2.4 hours left, and the reset buys about a tenth of an
allowance instead of a whole one the next week. That loses 0.91 allowances over two
weeks.

## Change

- Skip a Claude redemption when less than one burn time (from [task 34]) remains before
  the weekly rollover and the reset stays valid past the next point where the allowance
  would run out again. The 100% ceiling keeps the gate shut until the rollover.
- Still redeem a reset that would otherwise expire unused, even close to the rollover.
- Without a burn time estimate, keep today's behavior.
- Log why a reset is being held, with the time to rollover and the burn time.
- Correct [reset-aware-pacing.md]. Only the first allowance follows the N+1 split. Each
  redemption recalculates from the start of the week, so with two resets the targets are
  1/3 and 1/2 of the way, not 1/3 and 2/3.

## Acceptance

- Scenario B becomes an offline test in `tests/test_resets.py`, along with a reset that
  expires before the next rollover and must still be redeemed.
- After the change, the script's `pacer` and `hold` rows for B match.
- The full Python suite passes.

[reset pacing review]: ../reset-pacing-optimality.md
[`scripts/simulate_reset_pacing.py`]: ../../scripts/simulate_reset_pacing.py
[task 34]: 34-measure-burn-time.md
[reset-aware-pacing.md]: ../reset-aware-pacing.md
