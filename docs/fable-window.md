# Pacing the Fable 5 weekly window

> **Status: investigation and plan.** Investigated 2026-08-23. Nothing in this document is
> implemented yet. Today the pacer gates every Anthropic task on the *worst* window,
> and that includes the Fable window. This document shows why that loses value, and it
> plans the change that fixes it. Evidence sources are in the
> [appendix](#appendix-how-this-was-checked).

## Background for new readers

The Claude subscription does not have one quota. It has three windows:

| window | pacer label | duration | who spends it |
| --- | --- | --- | --- |
| session | `5-hour` | 5 h | every Claude model |
| weekly, all models | `7-day` | 7 d | every Claude model |
| weekly, Fable only | `7-day (Fable)` | 7 d | **only the Fable 5 model** |

The pacer reads all three from `GET https://api.anthropic.com/api/oauth/usage`. Each
window reports its own consumed fraction. The pacer compares that fraction with the
elapsed fraction of the window. The difference is the **headroom** `h`. The gate is open
while `h ≥ 0`. [`docs/design.md`] derives this rule.

## The facts, verified

Each fact below has a source: the official articles, a wire capture, or this pacer's own
JSONL log (2026-07-27 → 2026-08-23, ~800k ticks).

1. **Every Claude turn spends the 5-hour and the 7-day window.** Session and weekly
   limits are shared across all models; a model switch does not restore them
   ([Claude Code errors doc][cc-errors]).
2. **Only Fable turns spend the Fable window.** Our log confirms it: between 08-16 and
   08-17 the general 7-day fraction rose by +0.33 while the Fable fraction stayed frozen
   at 0.83 across every sample.
3. **A Fable turn spends all three windows at once.** "Fable 5 draws from your plan's
   regular weekly usage limits" ([Fable 5 on your plan][fable-plan]). In our log the two
   weekly fractions always rise together during Fable work.
4. **The Fable cap is half the weekly budget.** The plan allows "up to 50% of your
   weekly usage limits on Fable 5" ([Fable 5 on your plan][fable-plan]). Our log agrees:
   one unit of Fable spend moves the Fable fraction ~1.7–2.5× as much as the general
   fraction.
5. **A full Fable window blocks only Fable.** A captured rejection shows
   `5h: allowed, 7d: allowed, 7d_oi: rejected` on the same response
   ([CLIProxyAPI PR 5102][pr5102]). Other models keep working.
6. **Fable burns the shared windows faster than other models.** Stated officially, with
   no published number ([Fable 5 on your plan][fable-plan]).
7. **An unspent window is absent from the payload.** A `weekly_scoped` limit with no
   spend returns `resets_at: null`, and `claude_windows` drops it. Whole weeks of our
   log have no Fable window at all; the 5-hour window vanishes the same way.
8. **The vocabulary.** In `limits[]` the Fable window is `kind: weekly_scoped` with
   `scope.model.display_name: "Fable"`. On the wire its claim is
   `seven_day_overage_included` (header slug `7d_oi`, label "Fable 5 limit" in the
   Claude Code bundle). There is **no** top-level `seven_day_fable` key. The pacer's
   `limit_label` already turns any scoped entry into `7-day (<display name>)`, so the
   window already reaches the gate — that part needs no change.

## What the pacer does today, and what it costs

`quota_verdict` takes the **worst** headroom across all of a provider's windows
(`worst_headroom` in [`quota.py`]). One verdict gates every Anthropic task. So when the
Fable window is the tightest, the pacer withholds *all* Claude work — also the tasks on
models that cannot spend the Fable window at all.

This is not hypothetical. The log shows ~44,000 ticks bound on `7-day (Fable)`. From
08-14 to 08-16 the Fable fraction sat at 0.73–0.83 and paced work was withheld for
about 2.5 days, while the general 7-day window held 0.2–0.4 of headroom the whole time.
That headroom is paid-for quota that non-Fable tasks could have spent.

The opposite loss exists too. Nothing today makes sure the Fable window gets *filled*.
A week that ends with the Fable window at 0.20 gave away 80% of the right to run the
best model at no extra cost.

## The rule that gets the most value

**Gate each task on exactly the windows its launch can spend.**

- A Fable task: worst headroom over `5-hour`, `7-day`, and `7-day (Fable)`.
- Any other Claude task: worst headroom over `5-hour` and `7-day` only.

Start from the task's own assumption: the backlog is larger than any window, for Fable
work and for other work alike. Then this rule is sufficient, and no cross-window
arithmetic is needed:

- Each window self-reports its consumed fraction. The general window already contains
  Fable's contribution (fact 3), so pacing non-Fable work on the general headroom
  automatically accounts for what Fable work spent. There is no multiplier to estimate.
- When the Fable window binds, Fable tasks pause and other tasks keep filling the
  general headroom. The 2.5-day starvation above cannot happen again.
- When a general window binds, both classes pause, because the general windows are in
  both gate sets. Nothing overspends.
- With Fable tasks in the backlog, the Fable window burns down its own steady line and
  ends the week near `PACER_ALPHA` — the full half-budget right, used.

The week then ends with **both** weekly windows near the line. In numbers, with the
observed ~2:1 exchange rate: a full Fable window consumes about half the general
budget; the other half goes to Opus/Sonnet work. That is the maximum the plan rules
allow, which is what "most value for the subscription price" means here — under one
judgment the pacer cannot make for you: that Fable output is worth more than what the
same general-window quota would buy from cheaper models. The pacer's contract is
narrower and mechanical: never idle while some admissible task has headroom, never
lock a window out mid-week, and leave the Fable cap reachable. **Which** class fills an
open slot is still the queue order, which you control (see
[Which task goes next][readme-order]). Keep Fable tasks ranked where their value says
they belong; a Fable backlog that is always outranked will leave the Fable window
unspent, and that is then a statement about your priorities, not a pacer defect.

The 5-hour window needs no new treatment. It is shared (fact 1), it sits in both gate
sets, and it shapes bursts, not the weekly mix.

## Which tasks are Fable tasks

The pacer must map a task to the models its launch can run. The data exists:

- kandev's `GET /api/v1/agents` reports each profile's `model` string —
  `claude-fable-5[1m]`, `opus[1m]`, `gpt-5.6-sol`, or `""` for the agent default. The
  DB confirms it (`agent_profiles.model`, plus `fallback_model` and `auto_fallback`).
  The two-subscriptions work already verified this endpoint and already resolves
  task → profile → agent ([`selection.profile_provider`][`selection.py`]).
- The scoped window names its model: `scope.model.display_name` = `"Fable"`.

The matching rule is a normalized substring test: a profile whose model string contains
the scope's display name (case-insensitive — `claude-fable-5[1m]` contains `fable`)
spends that scoped window. Three cases need care:

- **Unknown wins upward.** An empty model, a missing profile, or an unreadable agents
  payload puts the task in the *all-windows* class — exactly today's behavior. This is
  the safe direction: a wrong guess makes pacing stricter, never looser. Contrast with
  provider attribution, where a wrong guess spends the wrong money and the pacer
  withholds instead; here both candidate gates spend the same subscription, so the
  strict gate is a valid answer, not a refusal.
- **Fallback models widen the gate.** A profile with `auto_fallback` can run either
  model, so it gates on the union of both models' window sets. The union is the
  stricter set, so this is automatic conservatism, not a special case.
- **A profile can lie.** Claude Code can switch models mid-session, and subagents can
  override the model. The windows stay truthful regardless — every tick re-reads real
  consumption, and misattributed spend still lands in the general windows both classes
  gate on. The residual risk is one-sided: a task classed non-Fable that actually runs
  Fable can push the Fable window past `PACER_BACKSTOP` without its class noticing. If
  a real profile ever needs it, a `PACER_AGENT_PROVIDERS`-style override is the known
  shape; do not build it before then.

## Design sketch

The two-subscriptions change already made verdicts, eligibility, and tick records
plural per gate group. This change makes the group key finer: **(provider, window
scope)** instead of provider alone. Generic on purpose — if a `7-day (Sonnet)` scoped
window ever appears in `limits[]` (the claim vocabulary already contains
`seven_day_sonnet`), Sonnet tasks gate on it with no new code, exactly as
`limit_label`'s family matching already anticipates.

Per module:

- [`provider.py`] — keep the scope on the window: `claude_windows` adds
  `scope: display_name | None` next to `label`. ~10 lines.
- [`quota.py`] — a function selects a class's window subset (general windows, plus the
  scoped windows the class matches). `quota_verdict` itself does not change; call it
  once per class over that subset. The backstop then applies per class for free: a
  breached Fable window stops only Fable tasks, because only their subset contains it.
  `tick_record` gains a `scope` field, `None` outside the plural path, so the
  `gate=open, action=nothing, launchable>0` one-line filter keeps working. ~30 lines.
- [`selection.py`] — a `task_models` resolver beside `task_provider`, reading the
  profile's `model` and `fallback_model`; the `allowed`/`provider_of` projection in
  `select_action` generalizes from provider names to group keys. ~40 lines.
- [`runtime.py`] — `gate_groups` splits a provider into its scope classes when its
  window list carries a scoped window; verdicts, `poll_interval`'s min, withheld notes
  and the startup report iterate groups as they already do. The agents payload is
  needed as soon as a scoped window appears, also on a single-provider run — one
  cached call. ~80 lines.
- [`config.py`] — nothing. No new setting is required; that is a feature of the design.
- Tests — a payload with the scoped window, the attribution cases above, and a
  regression for the 08-14 starvation shape: Fable window bound, general open, a
  non-Fable task must launch. ~150 lines.

Deliberately **unchanged**: one launch per provider per tick. Unlike two providers, the
two classes *do* share a resource — the general windows and the machine — so the
concurrency-sizing argument for one launch per re-read still applies. The projection
alone fixes visibility: a shut Fable gate no longer hides general work, and queue order
picks among tasks whose class is open. `PACER_CONCURRENCY` also stays per provider,
shared by both classes, for the same reason.

Backward compatibility falls out rather than being built: no scoped window in the
payload → one class, today's path, byte-identical. Scoped window present but no model
information → every task in the all-windows class, today's verdicts. Behavior moves
only for a task whose profile model provably cannot spend the scope.

Call it ~300–350 lines net, half of it tests — smaller than two-subscriptions, because
the plural machinery this rides on is that change's work.

## Out of scope, and why

- **Usage credits (overage).** The endpoint reports `extra_usage` and the plan can bill
  past the included quota. The pacer paces *included* quota; `PACER_BACKSTOP` already
  stops short of the edge. A credits-aware ceiling is the Codex `credits` note from
  [`two-subscriptions.md`] again — same later, same place.
- **A per-scope alpha** (aim Fable under its line to reserve general budget for cheaper
  models). Real lever, no demonstrated need. The queue order already expresses the
  preference; add the knob when a log shows order alone cannot.
- **Estimating the burn multiplier.** Never needed: every window self-reports. Any
  number we computed would be a guess about Anthropic's private weighting (fact 6).

## Appendix: how this was checked

Log analysis (the pacer's own JSONL at `~/.local/state/kandev-pacer/pacer.jsonl`,
158 MB, 2026-07-27 09:00 UTC → 2026-08-23):

```bash
# the vocabulary: every window label ever logged, with counts
grep -o '"label":"[^"]*"' pacer.jsonl | sort | uniq -c
#  216432 "5-hour"   466202 "7-day"   129375 "7-day (Fable)"   — nothing else, ever

# how often each window was the binding one
grep -o '"bind":"[^"]*"' pacer.jsonl | sort | uniq -c
#   64243 "5-hour"   357400 "7-day"    44248 "7-day (Fable)"

# co-movement: sample anthropic ticks ~6 h apart, difference each window's `c`
# non-Fable weeks: general 0.05→0.92 with no Fable window present at all
# Fable bursts: ΔG +0.08/+0.14/+0.15 against ΔF +0.14/+0.26/+0.25 — the ~2:1 rate
# freeze test: 08-16→08-17 ΔG +0.33, ΔF +0.00 across five consecutive samples
```

Payload and vocabulary: `tests/test_provider.py` carries a real recorded response
(`limits[]` with `session` / `weekly_all` / `weekly_scoped`, percent 0–100, RFC3339
resets); the Claude Code 2.1.241 bundle's schema strings list the top-level keys and
map slug `7d_oi` → `seven_day_overage_included` → "Fable 5 limit". kandev model
attribution read from `~/.kandev/data/kandev.db` (`agent_profiles.model`) read-only;
the `/api/v1/agents` shape was verified live in [`two-subscriptions.md`].

Official articles and captures:

- [Claude Fable 5 on your plan][fable-plan] — the 50% weekly cap, "draws from your
  plan's regular weekly usage limits", "uses them faster".
- [Claude Code errors reference][cc-errors] — shared session/weekly limits; the
  model-scoped limit frees other models.
- [Models, usage and limits in Claude Code][cc-models] — Opus "uses meaningfully more
  of your quota"; the same shape one tier down.
- [Manage usage credits][credits] — the overage mechanics behind `7d_oi`'s name.
- [CLIProxyAPI PR 5102][pr5102] — the Fable-only rejection on the wire.
- [claude-code issue 88137][issue88137] — no Fable key in statusline `rate_limits`;
  the `limits[]` scoped entry is the delivery path.

[`docs/design.md`]: design.md
[`two-subscriptions.md`]: two-subscriptions.md
[`provider.py`]: ../src/kandev_pacer/provider.py
[`quota.py`]: ../src/kandev_pacer/quota.py
[`selection.py`]: ../src/kandev_pacer/selection.py
[`runtime.py`]: ../src/kandev_pacer/runtime.py
[`config.py`]: ../src/kandev_pacer/config.py
[readme-order]: ../README.md#which-task-goes-next
[fable-plan]: https://support.claude.com/en/articles/15424964-claude-fable-5-on-your-plan
[cc-errors]: https://code.claude.com/docs/en/errors
[cc-models]: https://support.claude.com/en/articles/14552983-models-usage-and-limits-in-claude-code
[credits]: https://support.claude.com/en/articles/12429409-manage-usage-credits-for-paid-claude-plans
[pr5102]: https://github.com/router-for-me/CLIProxyAPI/pull/5102
[issue88137]: https://github.com/anthropics/claude-code/issues/88137
