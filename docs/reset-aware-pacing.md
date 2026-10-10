# Reset-aware pacing

Both Kandev and T3 read available banked resets and
use them automatically for waiting subscription work. Ordinary five-hour pacing,
model scopes and concurrency caps still constrain launches. The pacer never buys
resets or usage credits.

The providers report allowance percentages rather than remaining token counts, and
the two subscriptions apply a reset differently, so the planner has one rule per
provider.

[Claude keeps its scheduled weekly rollover after a reset](https://support.claude.com/en/articles/17007452-what-is-a-limit-reset),
so a Claude reset adds budget to this week. With N usable resets, the planner paces
only the first allowance to run out 1/(N + 1) of the way to the deadline: the rollover
or the earliest credit expiry, whichever is first. After each redemption it
recalculates from the start of the week with one reset fewer, so later allowances get
a looser cap. With two resets, the redemption targets are 1/3 and then 1/2 of the
way to the deadline, not 1/3 and 2/3. For example, Claude halfway through its week
with one usable reset can finish its current allowance now, redeem, and use the
refreshed allowance before the normal weekly rollover. Each fresh quota reading updates the plan with remaining
credits and the provider's actual window timestamps.

When less than one measured [burn time](#burn-time) remains before the weekly
rollover, Claude holds a reset that stays valid past rollover plus one burn time.
The free rollover can then refill the allowance, and the reset is still available
when that allowance runs out. At 100% consumed the gate stays shut until rollover.
A reset that expires at or before that next empty point is redeemed anyway, including
one expiring before rollover. Without a burn time estimate, redemption keeps its
existing timing. The status line logs both times, for example "1 resets held: 2h 10m
to rollover is under the ~7h burn time".

### Codex: as early as possible

[Codex starts new five-hour and weekly windows after a full banked reset](https://help.openai.com/en/articles/20001498-how-banked-codex-resets-work).
A Codex reset is worth the part of the week it lets you skip: used on day d, it
brings every later free refill (7 − d) days earlier, so it is worth (7 − d)/7 of an
allowance. Pacing toward an even split or saving a reset for a later week only
delays those tokens; the [optimality review] measured 2/7 of an allowance lost per
reset by the even split, and a whole reset lost when saving let it expire. So while
an unexpired Codex reset exists, the pacer drops the weekly speed cap: the weekly
burn line sits at 100% and the agents run as fast as the five-hour limits, model
scopes and concurrency caps allow. At 100% weekly it redeems right away, earliest
expiry first, and several resets get used back to back.

The one exception needs the measured [burn time](#burn-time). When less than one
burn time remains before the rollover and the reset would still be valid after the
refilled allowance could run out again (rollover plus one burn time), the pacer holds
the reset and lets the free rollover refill the allowance. Used in those last hours it
would move the clock by almost nothing. A reset that would expire before then is used
anyway. Without a burn time estimate nothing is held. The status line says what is
happening: "N resets usable, no weekly speed cap, redeem at weekly exhaustion" while
the plan is active, and "N resets held: 2h 10m to rollover is under the ~7h burn time"
while one waits.

This uses the Codex allowance faster than the even split did, leaving less for
interactive use until the next refill. There is no setting for that today; if it
matters, it needs one of its own rather than a return to the even split.

Reset-assisted weekly segments target the full allowance, temporarily replacing
weekly `alpha` and `backstop` reserves. At 100% consumed, launches stop until
redemption and fresh quota are confirmed. Once credits run out, ordinary weekly
pacing and reserves resume. A scoped weekly ceiling still blocks its model class;
redemption waits for exhaustion of the shared weekly allowance. Five-hour-only
limits and unidentified credit types do not receive a weekly reset plan.

The target uses all available weekly allowances under sustained runnable work.
Task costs, short-window limits and an empty backlog can prevent a credit from
being exhausted before expiry. Reset targets in the log are pacing targets,
not promises about how quickly a particular task will consume tokens.

## Burn time

The redemption rules in the [optimality review] need to know how fast the agents can
empty one weekly allowance: the **burn time**. The pacer measures it from the quota
readings it already makes every tick, in `burn.py`.

**What it measures.** Every tick records the shared weekly window's consumed
fraction. The estimate is the fastest sustained rise seen in the last four weeks over
any span of at least five hours, one five-hour window, scaled to a full allowance.
Weekly rising 46% in five hours means one allowance lasts about 11 hours at that pace.
It is a lower bound on how fast the agents *can* go: the pacer's own burn-line pacing
and an empty backlog both slow them down, and the fastest-seen number is what the
redemption rules need to avoid redeeming when the refill cannot be used in time.

**Why not the five-hour window.** The log from September and October 2026 showed
that Claude's five-hour bar rarely got past 30%, and that its reset time moves with
the first request of each session, so "how much the weekly bar moves while one
five-hour window fills" almost never gets a sample. Codex reports no five-hour window
at all. A trailing-rise measurement works for both.

**What it ignores.** Readings are whole percent, so a rise under five points is not
trusted. A jump of more than five points between two consecutive readings is not
agents working: the log showed the weekly bar jumping from 0% to 100% after a
credential expiry and sitting there for a day. Such a jump, a drop (a rollover or a
redemption), or more than an hour without readings ends the run being measured, and
a new run starts from the next reading. Readings from a scoped window such as
`7-day (Fable)` are not used; only the shared weekly window counts.

**Reading it in the log.** Each `tick` record in `PACER_LOG` carries `burn`:

```json
"burn": {"s": 26091, "rise": 0.69, "span": 18003, "from": 1791466920, "to": 1791484923}
```

`s` is the burn time in seconds. `rise` over `span` seconds, ending at `to`, is the
reading that produced it, so the number can be checked against the provider's usage
page for that period. The stdout line `[openai] burn time ~7h 14m 51s per weekly
allowance (rose 69% in 5h 00m 03s to 04 17:42)` appears whenever the estimate changes,
in both Kandev and T3 modes. `burn` is `null` until a rise of at least five points
over at least five hours has been seen; a fresh install, or a month with no sustained
work, has no estimate. The redemption rules keep today's behaviour in that case rather
than guess.

**Where it lives.** `PACER_BURN_STATE`, default `~/.local/state/kandev-pacer/burn.json`,
keeps the readings and the best rises seen, so a restart continues the measurement
instead of starting over. Kandev and T3 pacers on the same machine share it, as they
share the reset state; they read the same accounts and so measure the same thing. An
unreadable file is ignored with a warning and rewritten. Setting the path empty keeps
the estimate in memory only.

Replaying the real log from 2026-09-19 to 2026-10-04 through the estimator gave a
burn time of about 52 hours for the Claude subscription, from a 10% rise over 5h 15m
on 2026-09-20, and about 7 hours for Codex, from a 69% rise in 5 hours on 2026-10-04
after a reset. The two faulty 100% readings in that log were skipped.

Both providers use the estimate to decide whether to hold a reset in the last hours
of a week. Without an estimate both providers redeem at weekly exhaustion as before.

## Reading grants

Codex reads `/backend-api/wham/rate-limit-reset-credits` with the local subscription
login. It counts only available `codex_rate_limits` credits with valid future
expiries. A failed credit read retains the normal usage windows and falls back to
ordinary pacing.

Claude requests `cedar_ember=1&skip_spend=1` from its OAuth usage endpoint. It
counts eligible, usable, unpaused grants with remaining resets and valid expiries,
including grants with no expiry. Its advertised next grant must be valid.
`claude_account_path`, default `~/.claude.json`, supplies the organization UUID
for redemption. Custom Claude configuration directories may need this path set
explicitly. Credentials and organization files are read without modification.

Pebble snapshots do not contain grant data. `quota_source = "pebble"` supplements
snapshots with cached live provider reads. While resets are available it uses the
live quota windows too, so a pre-reset collector snapshot cannot spend another
credit. Without resets it keeps snapshot pacing. A failed optional live read keeps
snapshot pacing. Redemption always rechecks live quota before sending a request
and confirms against live quota afterwards.

## Recovery and overrides

`reset_state_dir`, default `~/.local/state/kandev-pacer/resets`, contains private
pending request records and account locks. Kandev and T3 processes using the same
subscription must share it. Keep it across restarts. A request ID saved before
redemption is reused after a lost reply, so retrying asks about the same redemption
instead of spending another credit. Different accounts have separate locks.

A reset is confirmed only when a fresh reading shows both fewer credits and
available shared weekly allowance. An unconfirmed request or failed refresh
withholds that provider. The next tick with waiting work retries or confirms the
saved request. Other subscriptions keep their own gates. Inspect pending records
and provider status before removing recovery state.

API-key overrides keep their existing concurrency-only behavior and exclude the
bypassed work from redemption:

```bash
kandev-pacer --agent-providers-raw 'codex-acp=__api_key__ hermes-acp=__api_key__' --apikey-policy cap
```

In T3, `codex-acp` aliases `codex`; `hermes-acp` is ignored because T3 pacing
supports Codex and Claude instances. `--apikey-policy withhold` still prevents
bypassed launches. Remove the overrides to restore reset-aware subscription pacing.
Inspection reads do not redeem resets.

[optimality review]: reset-pacing-optimality.md
