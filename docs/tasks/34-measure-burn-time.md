---
depends_on: []
---

# Measure how fast agents can empty a weekly allowance

## Original request

2026-10-10, after the [reset pacing review]: "Write it up and add the tasks."

## Why

The review's redemption rules need a **burn time** per provider: the shortest
wall-clock time the agents take to use one full weekly allowance. The five-hour limits
mostly set it. [Task 35] needs it to decide whether a Codex reset is worth more now or
next week. [Task 36] needs it to decide whether enough of the Claude week is left to use
a refill. The pacer has no such number today.

## What to build

- Estimate the burn time per provider and account from quota readings. The tick records
  in `PACER_LOG` already store each window's consumed fraction `c`, duration `d` and
  `resets_at` per provider, so there is history to start from.
- Check real records before choosing a method. Two candidates:
    - How much the weekly window's `c` rises while one five-hour window goes from empty
      to full. The burn time is then about 5 hours divided by that rise.
    - The fastest weekly rise observed over a trailing period.
- Write the estimate into the tick log so an operator can check it against the provider's
  usage page.
- Define what happens without enough history, for example on a new install. Tasks 35
  and 36 must keep today's behavior in that case rather than guess.

## Acceptance

- Offline tests feed recorded tick sequences for both providers and check the estimate.
- The estimate survives a pacer restart.
- [reset-aware-pacing.md] explains the estimate and how to read it in the log.

[reset pacing review]: ../reset-pacing-optimality.md
[Task 35]: 35-redeem-codex-resets-early.md
[Task 36]: 36-keep-claude-resets-near-rollover.md
[reset-aware-pacing.md]: ../reset-aware-pacing.md
