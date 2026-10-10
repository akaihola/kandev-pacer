---
depends_on: [34]
---

# Redeem Codex resets as early in the week as possible

## Original request

2026-10-10, after the [reset pacing review]: "Write it up and add the tasks."

## Problem

A Codex reset restarts the weekly clock. It's worth the part of the week it lets you
skip, (7 − d)/7 of an allowance when used on day d. Two rules in `reset_plan` push d
later than it needs to be:

- **The even split.** The plan caps weekly speed so the allowance runs out at 1/(N+1) of
  the way to the deadline. With one reset expiring at the rollover and a 1.5-day burn
  time, the pacer redeems on day 3.5 instead of day 1.5. Scenario C in
  [`scripts/simulate_reset_pacing.py`] loses 0.287 allowances.
- **Saving resets for their last week.** Resets that expire after the current rollover
  wait for the week their expiry falls in. A reset that expires 6 hours into a week can't
  be used then, so it expires unused. Scenario D loses 0.815 allowances.

The review shows saving gains nothing. A reset used at the same point in a later week is
worth the same, so waiting only delays the tokens and risks expiry.

## Change

- While an unexpired Codex reset exists, drop the weekly speed cap below 100%. Keep the
  100% ceiling, five-hour pacing, model scope gates and concurrency caps.
- Redeem at 100% weekly. Wait only when the rollover is less than one burn time away
  (from [task 34]) and the reset stays valid past the next point where the allowance
  would run out again. Without a burn time estimate, redeem at 100%.
- Several resets get used back to back, earliest expiry first. The credit reader already
  sorts by expiry.
- Remove the "reserved to preserve natural rollover" rule and its log note. Update
  [reset-aware-pacing.md], which still explains the old rule as protecting a free
  rollover.

This uses quota faster than today, leaving less for interactive use until the next
refill. If that matters, it needs its own setting. Don't bring back the even split for
it.

## Acceptance

- Scenarios C and D become offline tests in `tests/test_resets.py`.
- After the change, the script's `pacer` and `asap` rows for C and D match.
- The full Python suite passes.

[reset pacing review]: ../reset-pacing-optimality.md
[`scripts/simulate_reset_pacing.py`]: ../../scripts/simulate_reset_pacing.py
[task 34]: 34-measure-burn-time.md
[reset-aware-pacing.md]: ../reset-aware-pacing.md
