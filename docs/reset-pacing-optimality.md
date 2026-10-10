# Does reset-aware pacing get the most tokens?

> **Status: analysed 2026-10-10. The Codex fix ([task 35]) is implemented; the Claude
> fix ([task 36]) is not yet.** This checks the planner from
> [task 32] against the question it was built to answer: does it get the most tokens out
> of a subscription with banked resets? Every number below comes from
> [`scripts/simulate_reset_pacing.py`], which drives the real `reset_plan` and
> `quota_verdict` against a simulated provider. No live reset was read or redeemed.

## Short answer

For Claude the plan does get the most tokens possible, with one gap. It will spend a reset
in the last hours of a week even when that reset is still valid next week.

For Codex it didn't, before [task 35]. Its even-split timing slowed down the very thing
that makes a Codex reset valuable. In a typical week that cost about 2/7 of a weekly
allowance per reset. Its rule for saving resets could also let a reset expire unused.
Since task 35 the pacer runs Codex with no weekly speed cap while a reset is unexpired
and redeems at 100%, which is the `asap` policy below.

Follow-up work is in [task 34], [task 35], [task 36] and [task 37].

## The model

Think of the weekly allowance as a fuel tank.

- The tank refills for free at the weekly rollover. Whatever is left in it then is thrown
  away.
- A reset is a coupon that refills the tank right now.
- A Claude coupon refills the tank and leaves the rollover date alone. The
  [Claude help page] says weekly limits "still reset on their usual day and time".
- A Codex coupon refills the tank and restarts the weekly clock. The next free refill
  comes 7 days after you use it. The [Codex help page] says using one "changes your
  weekly reset date".

Providers report percentages, not token counts, so everything here is measured in
allowances, meaning full tanks. The analysis assumes:

- There is always more queued work than quota, once work starts.
- The **burn time** is the fastest the agents can empty one tank. In practice the
  five-hour limits set it. The simulation uses a fixed burn time and models only the
  weekly window.
- Providers apply coupons the way their help pages say.

Under those assumptions, "most tokens" means getting as many tanks as possible and
emptying every one.

## Rule 1: redeem only when the tank is empty

A refill throws away whatever is still in the tank, so a coupon used at 60% wastes 40% of
a tank. Both providers allow early use. Claude's page says "You don't have to wait until
you reach a limit to use it." The pacer never does this. It redeems only when the shared
weekly window reads 100%, and it re-reads live quota just before sending the request
(`exhausted` in [`resets.py`]). This rule holds for both providers, and the code follows
it.

## Claude

### The plan gets every tank that fits

With rule 1 in place, a week with N usable coupons gives at most N+1 tanks: the tank you
started with plus one per coupon. Each extra tank needs two things. Its coupon has to be
used before it expires, and the tank has to be emptied before the rollover. Every plan
that manages both gets N+1 tanks. Timing inside the week doesn't change the total, so
all of these plans tie for best.

The pacer's plan is one of them. `reset_plan` in [`quota.py`] caps the first tank's speed
so that it runs out at 1/(N+1) of the way from the start of the week to the deadline. The
deadline is the rollover or the earliest coupon expiry. After each refill the plan is
recalculated from the start of the week with one coupon fewer, so later tanks get a much
looser limit. Two cases cover everything:

- **The burn time is shorter than one even share.** Tank 1 is empty by 1/(N+1) of the
  way. Each later tank takes at most one burn time, and its recalculated limit is looser
  than an even share. So tank k is empty by k/(N+1) of the way, and the last one is done
  by the deadline.
- **The burn time is longer than one even share.** The cap is faster than the agents can
  go, so the pacer is already running at full speed. No plan could fit more tanks.

So whenever N+1 tanks fit in the week, the pacer gets them. Scenario A below shows two
coupons and a 1-day burn time. It gets 2.97 tanks. That's the maximum minus the 3% the
pacer always holds back on the last tank (`PACER_BACKSTOP = 0.97`).

The recalculation also means [reset-aware-pacing.md] describes the schedule wrongly. Only
the first tank follows the N+1 split. With two coupons, the redemptions in scenario A land
on day 2.34 and day 3.51, not day 2.33 and day 4.67. This costs no tokens.

### Gap: spending a long-lived coupon late in the week

The pacer redeems whenever the weekly tank is empty and a coupon is usable, however close
the rollover is. Suppose work only arrives late in the week, for example because the
backlog was empty or the pacer was down. Scenario B uses a 1-day burn time, a coupon valid
for four more weeks, and work starting on day 5.9. The tank runs dry on day 6.9 and the
pacer redeems at once. The free rollover refills the tank 2.4 hours later anyway, so the
coupon bought about a tenth of a tank. Had it waited, the coupon would have bought a whole
tank the next week. Over the two simulated weeks that is 0.91 tanks lost.

The missing check is whether there's still enough of the week left to empty the refilled
tank. A coupon that outlives the next empty point should wait while less than one burn
time remains. [Task 36] adds that check.

## Codex

### What a Codex coupon is worth

A Codex coupon moves the clock as well as filling the tank. Say the week starts on day 0
and the tank runs dry on day d. Without a coupon you wait until day 7. With one, fuel
arrives on day d, and every later free refill also arrives 7 − d days earlier:

| Refill | Without coupon | Coupon used on day d |
|---|---|---|
| 1st | day 0 | day 0 |
| 2nd | day 7 | day d |
| 3rd | day 14 | day d + 7 |
| 4th | day 21 | day d + 14 |

The coupon lets you skip the 7 − d days you would have spent waiting, so it's worth
(7 − d)/7 of a tank. The earlier in the week you empty the tank, the more the coupon is
worth. Slowing down on purpose costs tokens.

### Cost of the even split

Until task 35, `reset_plan` slowed Codex down on purpose. With one coupon that expires at the rollover, it
caps speed so the tank runs out on day 3.5. With a 1.5-day burn time the coupon could
instead be used on day 1.5:

- day 3.5: worth 3.5/7 = 0.50 tank
- day 1.5: worth 5.5/7 = 0.79 tank

Scenario C measures a gap of 0.287 tanks, which matches 2/7 = 0.286. Over any horizon
from 4 to 11 weeks the gap stays the same, because every later refill is shifted by the
same two days.

### Cost of saving coupons for their last week

Until task 35, `reset_plan` saved a Codex coupon until the week whose rollover falls after the coupon's
expiry. If the coupon expires 6 hours into next week, the pacer saves it this week. Next
week the deadline is 6 hours away, but a tank takes 1.5 days to empty, so the coupon
expires unused. Scenario D loses 0.815 tanks. Using the coupon this week on day 1.5 would
have kept them.

### Why saving doesn't pay

The saving rule rests on the claim that "using a Codex reset early would replace a free
upcoming rollover". Compare using a coupon on day d of the first week with using it on day
d of the third week:

| Refill | Use in week 1 | Use in week 3 |
|---|---|---|
| 2nd | day d | day 7 |
| 3rd | day d + 7 | day 14 |
| 4th | day d + 14 | day 14 + d |
| 5th | day d + 21 | day 21 + d |

From the fourth refill onward the two schedules match. Before that, the week-1 plan is
ahead. Using the coupon at the same point in a later week gains nothing. It only delays
the tokens and risks the coupon expiring. The free rollover isn't lost, only moved
earlier. A coupon is worth saving only when this week's tank runs dry close to the
rollover and the coupon will still be valid after the next empty point.
[Task 35] replaced both Codex rules with that single check, which needs the burn time
from [task 34]. Scenario E below shows it: work arrives on day 5.9, the tank is empty
on day 6.9, and a coupon valid for weeks waits for the free rollover instead of buying
two hours.

## Simulation

Run `uv run python scripts/simulate_reset_pacing.py`. `pacer` is the current code,
given the scenario's burn time as its measured estimate. `hold` is the Claude fix from
[task 36]. `asap` is the Codex rule from [task 35] written out by hand. Since that task
landed, `pacer` matches `asap` in scenarios C and D; the `pacer` rows for C and D below
are the pre-task-35 numbers, kept for the record.

| Scenario | Policy | Tanks used | Redeemed on |
|---|---|---|---|
| A. Claude, 2 coupons expire at rollover, 1-day burn, 1 week | pacer | 2.972 | day 2.34, 3.51 |
| B. Claude, coupon valid 4 weeks, 1-day burn, work from day 5.9, 2 weeks | pacer | 2.062 | day 6.91 |
| | hold | 2.972 | day 10.51 |
| C. Codex, coupon expires at rollover, 1.5-day burn, 6 weeks | pacer | 6.361 | day 3.49 |
| | asap | 6.648 | day 1.50 |
| D. Codex, coupon expires 6 h into next week, 1.5-day burn, 6 weeks | pacer | 5.833 | never, expired |
| | asap | 6.648 | day 1.50 |
| E. Codex, coupon valid 4 weeks, 1-day burn, work from day 5.9, 3 weeks | asap | 2.958 | day 6.91 |
| | pacer (task 35) | 3.806 | day 8.01 |

After the coupon is gone, each policy falls back to the pacer's ordinary weekly pacing.
That's why full weeks show 0.97 tanks.

## When to redeem

First find your burn time: how long it takes to go from 0% to 100% on the weekly bar with
agents running flat out. One fully used five-hour session gives a rough estimate. If a full
session moves the weekly bar 10%, a tank takes 10 sessions, about 50 hours back to back.
Since [task 34] the pacer measures this from its own quota readings and logs it; see
[burn time] in reset-aware-pacing.md for how to read it.

Then:

1. **Never redeem below 100% weekly.** The leftover is thrown away.
2. **Claude, at 100%:** redeem if at least one burn time is left before the weekly
   rollover. Also redeem if the coupon expires before you'd reach 100% again. Otherwise
   let the free rollover refill the tank and keep the coupon.
3. **Codex, at 100%:** redeem right away. The coupon is worth the fraction of the week
   still left. On day 2 that's 5/7 of a tank. On day 6.5 it's 1/14, so wait if the coupon
   is still valid next week.
4. **Codex coupon expiring soon:** make sure you hit 100% at least once before it expires.
   If it expires early next week, use it this week.
5. **Several coupons:** use the one that expires first. The Codex reader already sorts
   coupons by expiry. For Claude the pacer uses the provider's advertised next grant.

The pacer's log shows its intent. "N resets planned, redeem at weekly exhaustion, target
~date" means Claude is working toward a redemption. "N resets usable, no weekly speed
cap, redeem at weekly exhaustion" is the Codex rule running. "N resets held: 2h to
rollover is under the ~7h burn time" means rule 3's wait is in effect. Claude shows
coupon expiry in Settings > Usage. Codex shows it in the usage settings of the app, CLI
or web.

## Open questions

The redemption requests in [`resets.py`] haven't been exercised against the live
services. [Task 37] researches how each subscription redeems resets and how to verify
the pacer's requests without wasting a coupon.

[task 32]: tasks/32-add-reset-aware-pacing.md
[task 34]: tasks/34-measure-burn-time.md
[task 35]: tasks/35-redeem-codex-resets-early.md
[task 36]: tasks/36-keep-claude-resets-near-rollover.md
[task 37]: tasks/37-research-reset-redemption.md
[`scripts/simulate_reset_pacing.py`]: ../scripts/simulate_reset_pacing.py
[`resets.py`]: ../src/kandev_pacer/resets.py
[`quota.py`]: ../src/kandev_pacer/quota.py
[reset-aware-pacing.md]: reset-aware-pacing.md
[burn time]: reset-aware-pacing.md#burn-time
[Claude help page]: https://support.claude.com/en/articles/17007452-what-is-a-limit-reset
[Codex help page]: https://help.openai.com/en/articles/20001498-how-banked-codex-resets-work
