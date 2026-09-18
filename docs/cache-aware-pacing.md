# Pausing and the prompt cache: what it cost, and a cache-aware release

> **Status: measured, designed, not implemented.** Investigated 2026-09-06 over the pacer's
> own log (2026-07-27 → 2026-09-06, 5.9 weeks), the Claude Code and Codex session logs on
> the same machine, and kandev's database. Every number below is printed by
> [`scripts/cache_waste.py`]; the [appendix](#appendix-how-this-was-checked) says how to
> rerun it and what each source contributes.

## The problem in one paragraph

The pacer withholds a task until its headroom `h = α·e − c` climbs back to zero, and then
launches it ([`design.md`]). The wait it inserts runs exactly where the prompt cache's idle
clock runs: between turns. A Claude Code session on a subscription keeps its cached prefix
for **one hour** of idleness, a Codex session for about **five minutes**. When the wait is
longer than that, the released turn re-reads its entire context at the cache-*write* rate
instead of the cache-*read* rate. On Claude that is `2.0×` base input against `0.1×`
(`0.025×` on Fable 5.1), so a prefix of `P` tokens costs an extra `1.9·P`. The pacer's gate
reads the very usage number that miss inflates, so a miss does not make it overspend the
window — it just means fewer useful turns per window. [`pacing-designs.md`] predicted this
and asked for a measurement once the pacer had run. It has now run for six weeks.

## The cache rules, with sources

"Stated" is quoted from a vendor page; "inferred" follows from a verified mechanism but no
page says it outright.

| rule | value | basis |
| --- | --- | --- |
| Claude cache TTL | 5 min default; 1 h option | stated, [prompt-caching] |
| TTL is sliding, refresh is free | "refreshed for no additional cost each time the cached content is used" | stated, [prompt-caching] |
| TTL clock starts at request *start*, not response end | "if a response takes 4 minutes to stream, a follow-up … must start within about 1 minute" | stated, [prompt-caching] |
| Claude write / read multipliers | 5 min write `1.25×`, 1 h write `2×`, read `0.1×`; read `0.025×` on Fable 5.1 / Mythos 5.1 | stated, [prompt-caching] |
| Claude Code on a subscription, main conversation | 1 h TTL, within plan usage; 5 min once on usage credits | stated, [cc-prompt-caching] |
| Claude Code subagents, compaction, teammates | 5 min | stated, [cc-prompt-caching] |
| What invalidates the cache regardless of the clock | model switch, effort change (except Fable 5.1), fast mode, tool set, compaction, upgrade | stated, [cc-prompt-caching] |
| Cache scope | one machine and one working directory; worktrees of one repo do not share | stated, [cc-prompt-caching] |
| Cache misses draw plan usage | "your first message after a break longer than the cache lifetime misses the cache and reprocesses your full context" listed under *why usage climbs* | stated, [cc-costs] |
| Cache reads are cheaper *inside the subscription windows* | the cost page says a cached session "still draws usage for the whole conversation" at the cached rate | **inferred**: the page describes dollar pricing; the plan-limit weighting is not published |
| Codex cache TTL | "around 5 to 10 minutes of inactivity, up to one hour" | stated, [openai-caching] |
| Codex cached-input price | `0.1×` on GPT-5.6 and later | stated, [openai-caching] |
| Codex cached input counts less against a ChatGPT plan | cached input is `0.1×` in the credits rate table | **inferred** for the plan windows, stated for credits, [codex-pricing] |

The one assumption that moves the numbers is the inferred weighting of a cache read inside
the subscription window. Every result below carries it at `0.1` and repeats the headline at
`0.5` and `1.0`.

The Fable 5.1 read rate of `0.025×` cuts both ways: a warm Fable turn is four times cheaper
than a warm Opus turn, so a Fable miss (`1.975·P`) is relatively dearer still.

## What the logs say

### Every gap, whoever caused it

Table A1 of the script, over the Claude Code and Codex sessions kandev ran in task
worktrees. Requests are deduplicated first: Claude Code writes one `assistant` record per
streamed content block, all with the same message and request id, and without the dedup
every cache-write total roughly doubles.

| source | sessions | requests | gaps 5 min–1 h | 1–2 h | 2–4 h | > 4 h | stale (> TTL) | tokens rewritten after them | extra paid, input-equivalents |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Claude Code | 138 | 13,647 | 210 | 30 | 32 | 123 | 169 | 24.9 M | 48.0 M |
| Codex | 198 | 7,151 | 134 | 30 | 6 | 41 | 211 | 7.9 M | 6.7 M |

"Extra paid" is what the request after the gap paid beyond a warm hit: the rewritten
prefix at the write rate minus the read it would have been. Sixteen Claude gaps with a
model switch across them are excluded — those misses were unavoidable.

This is the ceiling. Most of these gaps are not the pacer's: a card sitting on a board
overnight, a human coming back to a session the next morning. Which brings us to
attribution.

### The gaps the pacer caused

The pacer's log records every launch with the task and session it acted on. Joining that
to kandev's `task_session_turns` gives, for each launch that *continued* an existing
session, the end of that session's previous turn and so the wait. A launch is
**pacer-caused stale** when the wait exceeded the TTL and the pacer logged at least one
`withheld` tick for that provider during it. The session's own log — matched through the
kandev workspace path, which is also the Claude project directory name — says what the
next request actually rewrote (table A2).

| provider | launches continuing a session | median wait | stale | stale and withheld | … with the next request found | extra paid, input-eq | median prefix rewritten |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| anthropic | 927 | 1.8 min | 217 | 199 | 94 | 33.6 M | 165 k |
| openai | 81 | 13.7 min | 65 | 23 | 13 | 0.7 M | 54 k |
| api-key | 9 | 0.1 min | 0 | 0 | 0 | 0 | – |

Two things to hold on to:

- **Most launches were fine.** The median anthropic wait was under two minutes; 927 launches
  and 217 crossed the hour. The waste is concentrated in a fifth of the launches, and it is
  large there: the median stale launch rewrote 165 k tokens and paid about 300 k
  input-equivalents for it, roughly fifteen ordinary requests' worth.
- **The measured 33.6 M is a floor, not the total.** Only 94 of the 199 pacer-caused stale
  launches could be matched to a next request: 58 sessions have no workspace path in
  kandev, 44 more have a path with no Claude project directory under it (chats, or agents
  that log elsewhere). The unmatched half spent something too; scaling by 199/94 puts the
  pacer-caused total near 70 M input-equivalents over the six weeks.

### How late was the gate, really

The interesting question for a design is not "was the wait long" but "how far past the
cache deadline did the gate reopen" — because a release *before* the deadline is only
worth considering when the gate would have reopened *shortly after* it. Table A3 recomputes
the gate's own reopening estimate (`quota.resume_at`, the real function) from the windows
logged at the moment each session's cache expired:

| provider | pacer-caused stale | reopened ≤ 5 min after expiry | ≤ 10 min | ≤ 20 min | median | max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| anthropic | 197 | 76 | 85 | 104 | 15.7 min | 11.6 h |
| openai | 23 | 13 | 13 | 13 | 0 | 19.4 h |

Half of the anthropic stale launches had a gate that reopened within twenty minutes of the
cache expiring. Those are the recoverable ones: the wait was almost over, and finishing it
cost a full rewrite.

The other striking number is the **59 anthropic launches (28 of them matched) with an
overshoot of zero**: the gate was already *open* when the cache expired, and the launch
still came hours later — quartiles of 3, 6 and 12 hours late. Nothing about the burn line
held them; the concurrency cap or the queue order did, one launch per tick going to
whichever card the board ranked first. Those 28 alone account for 9.5 M of the matched
waste. A cache-aware pacer needs an *ordering* rule as much as a *release* rule.

For Codex the picture is different and simpler. All thirteen matched stale launches had an
overshoot of zero, and the median openai wait — 13.7 minutes even when nothing was
withheld — is already past the five-minute TTL. The pacer reads each provider's usage
through a five-minute cache and ticks every up-to-120 s while paused, so its own reaction
time is longer than the Codex TTL. An early-release rule cannot help a Codex task; only a
faster hand-off could, and that is kandev's latency more than the pacer's.

### Converting tokens to quota

The subscription windows are read as percentages, not tokens. Table A4 fits the two: for
each window, every rise in the logged `c` against everything every session on this machine
spent between the previous rise and this one, weighted at the API price ratios (input `1`,
1 h cache write `2`, cache read `0.1` or `0.025`, output `5`). The provider read is cached
for five minutes, so `c` moves in steps and the fit accumulates spend per step.

| provider | window | input-equivalents per 1 % | per full window |
| --- | --- | ---: | ---: |
| anthropic | 5-hour | 69 k | 6.9 M |
| anthropic | 7-day | 914 k | 91 M |
| anthropic | 7-day (Fable) | 546 k | 55 M |
| openai | 5-hour | 111 k | 11 M |
| openai | 7-day | 319 k | 32 M |

Read these as ranges, not constants: sessions on other devices and claude.ai are invisible
here and would push the true tokens-per-percent *down* (the same percent, more tokens than
counted), so the quota shares below are, if anything, understated. The Fable figure is
noisier still: that window only moves while a Fable model runs, and the machine's spend
between two Fable steps includes non-Fable work.

Against those constants, the **matched pacer-caused waste of 33.6 M input-equivalents is
37 % of one weekly window** over 5.9 weeks — about **6 % of every week's Claude quota**,
and about 12 % once the unmatched half is scaled in. On the 5-hour window it is 4.9 windows'
worth: nearly one five-hour session per week spent on nothing but rewriting prefixes.

## The cache-aware pacer

Three rules, in decreasing order of value. None of them changes the pacing rule itself:
`h ≥ 0` still means *launch*, `h < 0` still means *wait*. They change *which* task launches
when the gate is open, and allow one specific exception to the wait.

### Rule 1 — order by cache deadline while the gate is open

Each task carries `cache_deadline = last_turn_end + TTL(provider)`, with `TTL` = 3600 s for
`anthropic`, 300 s for `openai`, and `last_turn_end` the `completed_at` of the newest turn of
the session the launch would continue. A task with no prior turn has no prefix to lose and
no deadline.

When the gate is open and more than one task is launchable, the launch goes to the task
with the **earliest still-future deadline**, ahead of the board's queue order — a task whose
cache is about to expire is dearer to delay than a task whose cache is already gone or
that has none. Once a deadline has passed the task falls back to its normal queue rank; the
miss is sunk. The board's order still decides everything else, so this is a tiebreak that
only bites when a warm task and a cold one compete for a slot, which is exactly the case
the 59 zero-overshoot launches lost.

This rule alone would have recovered 9.5 M of the matched 33.6 M, and it costs nothing:
it moves a launch that would have happened anyway to a moment when it is cheaper.

### Rule 2 — release early when the reopening is close behind the deadline

While the gate is paused with reopening estimate `R` (`Verdict.resumes_at`), and a
launchable task's `cache_deadline` `D` satisfies

```
now ≥ D − lead          and          0 < R − D ≤ margin
```

launch that task now, marking the tick `cache_release`. `lead` is the pacer's own reaction
time — `PACER_INTERVAL` plus `PACER_LAUNCH_LATENCY`, the ~90 s kandev takes to make a launch
visible — so the request lands before the prefix expires, not after.

`margin` is the wait the rule is allowed to skip. It has a principled size: skipping a wait
of `M` seconds on a window of duration `Dw` costs at most `M/Dw` of that window's headroom
(the turn spends now what it would have spent `M` seconds later), while the miss it avoids
costs `1.9·P` tokens out of a window worth `Ww` tokens. Releasing early pays when

```
M / Dw  ≤  1.9·P / Ww          ⇒          M  ≤  Dw · 1.9·P / Ww
```

With the median prefix of 165 k and the fitted window sizes: **13.5 minutes on the 5-hour
window, 35 minutes on the 7-day**, 60 minutes on the Fable window. The rule takes the
minimum over the windows that are binding, so it is the 5-hour window that decides in
practice. A fixed `PACER_CACHE_MARGIN=600` (ten minutes) sits under all three; `0` keeps
today's behaviour and is the default until this is shipped and watched.

Guards, each a case in the log:

- never while `consumed > PACER_BACKSTOP` — the ceiling is a ceiling;
- never when the cap is full; an early release takes a free slot, it does not preempt;
- never when `R − D > margin` — hours-deep negative headroom means the prefix is
  unsalvageable and the miss is accepted, exactly as [`pacing-designs.md`] said;
- never for a task whose session changed model since its last turn (kandev's
  `agent_profile_snapshot` versus the profile now) — the prefix is already gone;
- at most one early release per provider per tick, like every other launch.

The overshoot is bounded and self-repairing: the tick after an early release sees `h`
slightly more negative and waits it out, and `M` seconds of elapsed time repays it. The
gate's arithmetic does not change; a release is logged with its reason so the JSONL shows
how often and how far it fired.

### Rule 3 — do nothing for Codex, and say so

A five-minute TTL is shorter than the pacer's five-minute provider-read cache. The
earliest the pacer can *notice* a reopening is later than the cache lives, before kandev's
own hand-off is counted. The rules above are correct for `openai` and will almost never
fire; the honest recommendation is to leave the margin at zero there and treat Codex
continuations as cold. The only lever that would help is reaction time — a shorter
provider cache for `openai` and launching on kandev's turn-completed event instead of a
tick, which [`concurrency-sizing.md`] already wanted for other reasons.

### What the pacer would need

- `provider.prompt_cache_ttl(provider)` supplies the documented provider TTL, and the
  launch selector applies the resulting deadline to the newest session's completed turns;
- `Settings.cache_margin` (`PACER_CACHE_MARGIN`, seconds, default `0`);
- in `selection.select_action`, the deadline leads only among launchable tasks whose deadline
  is still ahead; the original ordered list remains the stable tie-break;
- in `Pacer.run_provider`, before `next_action`, the early-release check against
  `Verdict.resumes_at`, and a `cache_release` reason on the tick record;
- prefix size `P` for the margin derivation is a constant (the fitted 165 k), not a live
  read: reading each session's JSONL from the pacer would couple it to Claude Code's log
  format for a second-order refinement.

A keepalive — one cheap request against the same prefix every fifty minutes — would beat
all of this: `0.1·P` to keep against `1.9·P` to rebuild. Nothing outside the agent harness
can send one, so it stays where [`claude-code-hook-feasibility.md`] left it: an argument for
pacing living inside Claude Code.

## Modelled effect per subscription

Table B replays the matched pacer-caused stale launches under rules 1 and 2. A launch is
"kept warm" when its gate would have reopened within `margin` of the deadline (rule 2), or
was already open at the deadline (rule 1 alone). The saving is that launch's measured
extra; the window share divides by the fitted window sizes.

### Claude subscription

| margin | kept warm | … by rule 1 alone | saved, input-eq | … by rule 1 alone | 5-hour windows | 7-day windows | Fable windows |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 5 min | 37 | 28 | 14.0 M | 9.5 M | 2.03 | 0.15 | 0.04 |
| **10 min** | **44** | **28** | **16.1 M** | **9.5 M** | **2.33** | **0.18** | **0.04** |
| 20 min | 53 | 28 | 19.0 M | 9.5 M | 2.75 | 0.21 | 0.07 |

At the ten-minute margin, over 5.9 weeks:

| what the saving buys | measured (matched only) | scaled for the unmatched half |
| --- | ---: | ---: |
| input-equivalents per week | 2.7 M | 5.8 M |
| share of the weekly window | 3 % | 6.4 % |
| five-hour windows per week | 0.4 | 0.8 |
| extra requests at the median 20.7 k input-eq | 782 total, 130 / week | ~280 / week |
| as raw context tokens read from cache (Opus `0.1×` … Fable `0.025×`) | 27–110 M / week | 58–230 M / week |

Six weeks of kandev task sessions made 13.6 k requests, so 130–280 extra requests a week is
**6–12 % more paced turns from the same subscription**. The Fable window barely moves
(0.04 windows): only 24 matched stale launches were Fable, and the Fable window's own
fitted size is the noisiest constant here.

Sensitivity to the one inferred rule (table B2, ten-minute margin):

| cache read weighs, inside the plan window | saved, input-eq |
| --- | ---: |
| `0.1` (API price, assumed above) | 16.1 M |
| `0.5` | 12.7 M |
| `1.0` (a read costs as much as fresh input) | 8.5 M |

Even if a cache read counted at full price inside the plan — the least favourable reading
the cost page allows — half of the saving remains, because the 1 h *write* rate is `2×`
and the rewrite is the larger term.

### Codex subscription

| margin | kept warm | saved, input-eq | 5-hour windows | 7-day windows |
| --- | ---: | ---: | ---: | ---: |
| any | 6 | 0.31 M | 0.03 | 0.01 |

Six launches and one percent of a weekly window over six weeks: 28 extra requests total.
The Codex quota is not where the money is, and rule 3 says why the release rule cannot
change that.

## Recommendation

1. Implement **rule 1** (deadline-first ordering) first. It has no knob, no overshoot and
   no failure mode, and it carries 60 % of the modelled saving.
2. Implement **rule 2** behind `PACER_CACHE_MARGIN`, default `0`, and run it at `600` on
   this machine. Its log line is the acceptance test: every `cache_release` should be
   followed by a next request whose `cache_read_input_tokens` is large and
   `cache_creation_input_tokens` small, in the session's own JSONL.
3. Re-run `scripts/cache_waste.py` after a month. The A2 "stale and withheld" count for
   `anthropic` should fall from a fifth of launches toward the unavoidable cases — gates
   that reopen hours after expiry — and nothing else in the log should move.
4. Leave `openai` alone until the pacer launches on turn-completed events.

## Appendix: how this was checked

```bash
uv run python scripts/cache_waste.py           # ~2 min; streams every source, writes nothing
uv run python scripts/cache_waste.py --quick   # skips the token-per-percent fit
```

Sources and the join between them:

- **Claude Code**: `~/.claude/projects/<slug>/*.jsonl`, `slug` being the working directory
  with `/`, `.` and `_` replaced by `-`. Only directories under `~/.kandev/tasks/` count
  in the gap tables; every directory counts in the token-per-percent fit, because the quota
  is shared. `assistant` records carry `message.usage` with `cache_creation_input_tokens`,
  `cache_read_input_tokens` and `cache_creation.ephemeral_{1h,5m}_input_tokens`; the
  latter says which TTL the request had. Deduplicated on `(message.id, requestId)`.
- **Codex**: `~/.codex/sessions/**/*.jsonl`; `session_meta.payload.cwd` for the join,
  `event_msg` / `token_count` / `last_token_usage` for `input_tokens` and
  `cached_input_tokens`. The uncached input after a stale gap is compared with the session's
  median uncached input on warm turns, so ordinary new content is not counted as waste.
- **Pacer log**: `~/.local/state/kandev-pacer/pacer.jsonl`, 386 MB, 1.08 M `tick` records.
  Each carries `windows[]` with `c`, `e`, `d`, `resets_at`; `withheld` ticks carry no
  reopening estimate, so the script recomputes it with `kandev_pacer.quota.resume_at` from
  the windows logged nearest the cache deadline. `launched.{task,session}` names the
  session a launch acted on.
- **kandev**: `~/.kandev/data/kandev.db` opened with `?mode=ro`. `task_session_turns`
  gives each turn's `completed_at`; `task_sessions.workspace_path` the join to both logs;
  `agent_profile_snapshot.model` whether the session ran Fable.

Sanity checks that held: all 94 matched stale launches continued the *same* Claude session
file as the turn before the wait, so a prefix existed to lose, and 73 of their next requests
read fewer than 5 k tokens from cache — a cold rewrite, not a fresh session's first
request; the raw Claude counts before the dedup were 8,210 assistant
records over the first forty files against 4,011 distinct requests; the total weighted
Claude spend in the log span (463 M input-equivalents over 5.87 weeks) divided by the total
rise of the 7-day window (504 %) gives 919 k per percent, within 1 % of the step-wise fit;
kandev's `task_usage_events` sums a whole turn, so it matches the JSONL request only on a
single-request turn — where it does exactly (105,477 written, 10,007 read, both sources).

[`scripts/cache_waste.py`]: ../scripts/cache_waste.py
[`design.md`]: design.md
[`pacing-designs.md`]: history/pacing-designs.md
[`claude-code-hook-feasibility.md`]: history/claude-code-hook-feasibility.md
[`concurrency-sizing.md`]: concurrency-sizing.md
[prompt-caching]: https://platform.claude.com/docs/en/docs/build-with-claude/prompt-caching
[cc-prompt-caching]: https://code.claude.com/docs/en/prompt-caching
[cc-costs]: https://code.claude.com/docs/en/costs
[openai-caching]: https://developers.openai.com/api/docs/guides/prompt-caching
[codex-pricing]: https://learn.chatgpt.com/docs/pricing
