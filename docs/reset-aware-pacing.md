# Reset-aware pacing

> **Known gaps, 2026-10-10.** The [optimality review] found that the Codex timing
> below gives away tokens and that Claude can spend a long-lived reset just before the
> rollover. Tasks 34 to 36 fix this. Until then, read that review for when a
> redemption pays off.

Both Kandev and T3 read available banked resets and
use them automatically for waiting subscription work. Ordinary five-hour pacing,
model scopes and concurrency caps still constrain launches. The pacer never buys
resets or usage credits.

The providers report allowance percentages rather than remaining token counts.
With N usable resets, the planner divides the weekly burn interval by N + 1.
It shortens that interval to the earliest credit expiry when necessary. The
current allowance can then be exhausted sooner, followed by a reset. Each fresh
quota reading updates the plan with remaining credits and the provider's actual
window timestamps.

For example, Claude halfway through its week with one usable reset can finish
its current allowance now, redeem, and use the refreshed allowance before the
normal weekly rollover. A Codex credit expiring after this week's rollover stays
reserved until its final natural week. A credit expiring within this week instead
accelerates the current allowance so it can be redeemed before expiry.

[Claude keeps its scheduled weekly rollover after a reset](https://support.claude.com/en/articles/17007452-what-is-a-limit-reset).
[Codex starts new five-hour and weekly windows after a full banked reset](https://help.openai.com/en/articles/20001498-how-banked-codex-resets-work).
Using a Codex reset early would replace a free upcoming rollover, so preserving
natural rollovers is part of the plan.

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
