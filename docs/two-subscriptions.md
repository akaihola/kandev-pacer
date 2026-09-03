# Pacing two subscriptions from one pacer

> **Implemented.** This began as the investigation below and is now the design of record; the
> feature ships. Set `PACER_PROVIDERS=anthropic,openai` and read [Two subscriptions] in the
> README for the operator's view. What landed follows the plan here, with these choices made at
> implementation time:
>
> - **Backward-compatible by construction.** A single provider (the default) paces every
>   subscription task on its window exactly as before; the per-provider machinery engages
>   only once `PACER_PROVIDERS` names more than one — which is why the existing suite stayed
>   green unchanged. The one exception, single or plural, is an api-key task: it has no
>   window, so it bypasses the gate onto the concurrency cap alone (or is withheld under
>   `PACER_APIKEY_POLICY=withhold`).
> - `parse_windows(usage, provider=…)` still returns a flat list for one provider (the old
>   callers are untouched); the new `windows_by_provider(usage)` returns the `{provider: […]}`
>   grouping. `quota_verdict(…, provider=…)` and `tick_record(…, provider=…)` gained the same
>   optional argument rather than changing shape.
> - `provider.usage` stays the Anthropic single source (its tests are unchanged). The Codex
>   source is `provider.codex_usage`; `provider.read(providers, …)` merges them and returns
>   `(envelope, errors)`. The tick raises `QuotaUnreadable` only when *every* configured source
>   fails — so one provider reproduces today's whole-gate-shut behaviour exactly.
> - Codex is reported under the provider name **`openai`** (the string a `codex-acp` agent's
>   burn already appeared under), so the operator sets `PACER_PROVIDERS=anthropic,openai`.
> - `PACER_APIKEY_POLICY` defaults to **`cap`** — recommendation (3) defaulting to (1)
>   below — so the pacer never withholds work it does not pace; `withhold` remains for
>   operators whose metered spend must stay off unless asked for.
> - Drains are attributed by the task's own provider rather than the draining session's, to keep
>   attribution consistent with the running- and launchable-counts that also read it. The
>   session-level override matters only for a profile-switch mid-workflow.
> - **The Codex endpoint is Cloudflare-fronted and 403s a bare `Python-urllib` User-Agent** —
>   and a browser one. It answers only to the Codex CLI's own agent string, so the read sends
>   `User-Agent: codex_cli_rs/<version>`. Verified live 2026-08-08 (no UA and `Mozilla/5.0` both
>   403; `codex_cli_rs/0.147.0` gets 200). The version is cosmetic to the check today; the live
>   contract test is where a future tightening would surface.
>
> [Two subscriptions]: ../README.md#two-subscriptions

---

Investigated 2026-08-07 against kandev v0.84.1, codex-cli 0.147.0 and this repo at `84fb5bc`.
Every claim marked *verified* was read off the live instance or the live provider endpoint on
that date; the commands are in [Appendix: how this was checked](#appendix-how-this-was-checked).

**The answer: one process.** Two processes would still need most of the work a single process
needs — without per-task provider attribution they would fight over the same tasks — and they
would additionally lose the shared booking state and the single ordering. The three things
two subscriptions actually require are all *inside* one tick, and the pacer's internals
already carry a `provider` field through the middle of them.

## What the pacer assumes today

One subscription, everywhere, implicitly:

- `PACER_PROVIDER` is one string. `quota.parse_windows` filters the usage envelope down to
  that one provider and returns a flat window list.
- `quota_verdict` produces one `Verdict` per tick. One `gate`, one `headroom`, one
  `resumes_at`, and `poll_interval` paces the whole loop by it.
- `Snapshot.running` counts every running session on the board, against one
  `PACER_CONCURRENCY`.
- `select_action` returns the single highest-ranked actionable task, and the tick spends at
  most one turn on it.
- `provider.py` is Anthropic: one `USAGE_URL`, one credentials file, one module-level
  `_cache`, and `QuotaUnreadable` fails the *whole* tick.

None of that is wrong for one subscription. All of it is what has to become plural.

## The three things it takes

### 1. Which subscription a task will burn

*Verified.* The pacer already reads the answer every tick and throws it away for every
purpose but one.

A task carries `metadata.agent_profile_id` — `selection.task_agent_profile` reads it, only to
hand it back to kandev on `intent:start`. A session carries its own `agent_profile_id` at the
top level. And `GET /api/v1/agents` maps a profile id to the agent that runs it:

| profile id | agent | display | `billing_type` | model |
| --- | --- | --- | --- | --- |
| `a830d9de…` | `claude-acp` | Claude | `subscription` | `opus[1m]` |
| `9e76dd2f…` | `codex-acp` | Codex | `subscription` | `gpt-5.6-sol` |
| `62fcea79…` | `hermes-acp` | Hermes | `api_key` | `openai-codex:gpt-5.6-sol` |

So: one new client call, cached, and a `task_provider()` resolver. Three things it must get
right, none of them hard but each of them a real case on this board:

- **Map by agent, not by model.** `hermes-acp` runs an OpenAI model on an API key. Its burn
  never appears in a subscription window, and reading `openai` out of the model string would
  gate it on the ChatGPT quota — pacing it against a number that does not move with it.
- **`billing_type` decides whether a task is paced at all.** `api_key` profiles have no window
  to pace against. See [the one open question](#the-one-open-question-api-key-profiles).
- **Attribution is per *launch*, not per task.** The provider that will burn is the one at the
  step the launch targets. Task metadata is the answer today, but a profile-switch step
  carries its own `agent_profile_id` — `workflow.lint_workflow` already contemplates one, and
  no step on this instance has one yet, so this branch is unverifiable from outside (the same
  gap the [contract test][contract] already documents). Resolve step override first, task
  metadata second. Drains are easier still: the session holding the queue names its own
  profile, so a drain's provider is read directly off the session.

A task with no resolvable profile is the fail-closed case: kandev would apply its own default,
and the pacer cannot tell which. Withhold it and name it at startup rather than guess.

### 2. A second quota source

*Verified live.* `GET https://chatgpt.com/backend-api/codex/usage`, with the ChatGPT OAuth
bearer and `chatgpt-account-id` from `~/.codex/auth.json`, answered 200 with exactly the
numbers the gate needs:

```json
"rate_limit": {
  "allowed": true, "limit_reached": false,
  "primary_window": {
    "used_percent": 54, "limit_window_seconds": 604800,
    "reset_after_seconds": 41699, "reset_at": 1786168839
  },
  "secondary_window": null
}
```

That maps onto the pacer's internal window dict *better* than Anthropic's does, because the
duration arrives in the payload instead of being hardcoded. Four differences to write down:

- **Read `limit_window_seconds`; do not infer duration from the window's name.** `primary` here
  is the *weekly* window — the names are slot names, not durations. `config.window_duration`
  matches a label prefix against `WINDOW_DURATIONS`, so a Codex window of any other length
  would be silently dropped, which is precisely the under-withholding failure that function's
  docstring warns about. The internal envelope should carry `duration_s` and `parse_windows`
  should prefer it over the label lookup. The label stays, for the log and the paused line.
- **Expiry is a JWT claim, not a field.** Anthropic's credential has `expiresAt` in
  milliseconds; the Codex `access_token` is a JWT whose payload carries `exp` (192 h out when
  checked). Decode-without-verify, apply the same `EXPIRY_SKEW`.
- **The same read-only rule, for the same reason.** Codex CLI owns and refreshes
  `~/.codex/auth.json`. Two refreshers rotating one refresh token is the lost update
  `provider.py`'s docstring already refuses to risk. `auth_mode` must also be checked:
  `apikey` means there is no subscription window, which is the Codex equivalent of "an
  API-key login has no quota to pace".
- **`credits` and `spend_control` are a second ceiling.** A plan with credits can run *past*
  the rate limit and be billed for it. Out of scope, but it is the natural place for a
  Codex-side `PACER_BACKSTOP` analogue later.

Structurally the neat move is to keep `provider.usage_envelope`'s shape as the internal lingua
franca. It already tags each agent's usage with a `provider` string, and `parse_windows`
already filters on it. So each source becomes an adapter emitting that envelope, one tick
merges the `agents` lists, and `quota.py` barely moves.

### 3. A gate per provider, not a gate per tick

This is the actual work. Verdicts, caps, intervals and logs all stop being scalar:

- `parse_windows` returns `{provider: [window, …]}`.
- `quota_verdict` is **unchanged** and called once per provider. Its fail-closed `blind`
  branch keeps working per provider: a `PACER_PROVIDERS` entry matching nothing in the merged
  envelope withholds *its* tasks and leaves the other's alone.
- `select_action` takes the set of open providers and returns the first actionable candidate
  whose provider is in it — instead of the first actionable candidate full stop. A shut Claude
  gate must not hide Codex work behind it in the order.
- `running` and the cap go per provider. `PACER_CONCURRENCY` becomes the cap *per provider*
  (one provider configured reproduces today exactly), plus a new optional
  `PACER_CONCURRENCY_TOTAL` for the thing that is genuinely shared: the machine running the
  worktrees.
- `poll_interval` takes the **minimum** across providers. A two-hour Claude pause must not
  stop the loop noticing Codex work it could start now.
- **One launch per tick becomes one launch per provider per tick.** This is a deliberate
  departure from [`concurrency-sizing.md`]'s "keep one launch per tick", and the reason it is
  safe is the reason the whole change exists: the two providers are not competing for one
  resource, so serialising them buys no protection and only starves whichever one loses the
  global order. Within a provider, one launch per tick still stands, and headroom is still
  re-read every tick.
- **A tick writes one JSONL record per provider,** each gaining a `provider` field. Every
  other field in `tick_record` — `gate`, `wh`, `bind`, `windows`, `running`, `launchable`,
  `cap`, `launched` — is already per-provider-meaningful; only `interval` is global. Splitting
  the record rather than nesting a map inside it keeps `gate=open, action=nothing,
  launchable>0` answerable by the same one-line filter it is answerable by today. Roughly
  doubles the log to ~600 KB/day at the default tick.
- **A tick error must degrade per provider.** Today `QuotaUnreadable` aborts the tick. With
  two subscriptions an unreachable ChatGPT endpoint must not shut the Claude gate. That means
  a per-provider read, a per-provider `_cache` (it is a module-level singleton today), and
  `note_tick_error` keyed by provider so a standing outage on one is reported once rather
  than suppressing the other's recovery line.

What you lose, stated plainly: **a single global order across providers.** Today the queue is
one list and the pacer works down it. With two gates it works down one list *projected* onto
each provider, so "task X runs before task Y" is only guaranteed when X and Y share a
subscription. That is not a regression so much as the truth arriving — X and Y on different
subscriptions were never contending for the same quota, and making one wait for the other was
always an accident of the implementation.

## Why not two processes

Two processes look cheaper — set `PACER_PROVIDER` twice and be done — and are not, because
scope is per *workflow*, not per provider. `Board.scan` enrols whole workflows, and both
processes would read the same enrolled workflows and the same task list. Neither would have
any reason to skip the other's tasks.

So work item 1 — per-task provider attribution and filtering — is needed **either way**. Two
processes save only item 3's plumbing, and pay for it with:

- **Double launches.** Two processes select from one list; both pick the same top task.
  `booked`, `failed` and `stalled` are in-memory per process and invisible to the other, so
  the reservation that exists precisely to stop a double launch does not span them.
- **Cross-counted concurrency.** Each process's `running` count includes the other's sessions,
  so each throttles on the other's work while neither can enforce a real shared machine cap.
- **A task that changes provider mid-workflow** — the profile-switch step — falls between two
  sets of state that cannot hand off to each other.
- **Two interleaved logs** for one board, and two credential configurations to keep straight.

None of that is exotic; it is the same reason [`isolation-and-federation.md`] concludes that
independent pacers buy approximate *rate* safety and never global *ordering*. There, the two
pacers at least read one account-level number and see each other's burn. Here they would read
two different numbers and see none of each other at all.

The one case for two processes is two different *machines*. That is the federation problem,
not this one.

## The one open question: API-key profiles

`hermes-acp` and `pi-acp` are `billing_type: api_key`. They have no subscription window, so
there is nothing to pace them against — pacing one against the Claude gate withholds it
behind a window it never touches.

Three defensible answers, and this is a spend decision rather than a technical one:

1. **Gate them on the concurrency cap only.** Most correct, and it *increases* metered spend
   relative to today, silently, the day the change lands.
2. **Do not launch them at all**, and say so at startup. Most conservative, and it strands
   whatever is on the board under those profiles.
3. **A knob**, defaulting to (2), so the change is opt-in.

Recommendation: (3) defaulting to (1), because the pacer should never withhold work it does
not pace — gating an api-key task on a subscription window it never touches strands
whatever is on the board under those profiles. `withhold` stays for the operator whose
metered spend must stay off unless asked for. Worth a decision before
implementation rather than during it.

## What it costs

| module | change | rough size |
| --- | --- | --- |
| `client.py` | `agents()` — profile id → agent, `billing_type` | ~15 |
| `provider.py` | split into per-source adapters over the shared envelope; per-provider cache; JWT expiry | ~150 |
| `config.py` | `PACER_PROVIDERS`, `PACER_CODEX_CREDENTIALS`, `PACER_CONCURRENCY_TOTAL`, api-key policy | ~40 |
| `quota.py` | `parse_windows` grouped; `duration_s` preferred over label; `provider` on the tick record | ~50 |
| `selection.py` | `task_provider()` resolver; `select_action` takes the open-provider set | ~60 |
| `workflow.py` | step-level profile override surfaced on `Board` | ~20 |
| `runtime.py` | per-provider snapshot, verdict loop, caps, `min` interval, per-provider error state, startup report | ~130 |
| `tests/` | two-provider fake, agents endpoint, Codex payload, per-provider gate cases | ~300 |
| `docs/`, `README.md` | the env table, the design rationale, this doc superseded | ~80 |

Call it 600–800 lines net across the tree, over half of it tests and prose, and a day or two
of focused work. `quota_verdict` — the part anyone would argue about — does not change at all.

Two things belong in the same change rather than after it: a `PACER_PROVIDERS` naming a
provider the merged envelope lacks must still fail closed per provider, and
[`test_contract_live.py`][contract] must grow the two new shapes it now depends on — the
`/api/v1/agents` profile mapping, and the Codex `rate_limit` payload. Both are undocumented
endpoints on someone else's roadmap; the Anthropic one has already been moved once.

## Appendix: how this was checked

```bash
# 1. profile -> agent -> billing_type
curl -s http://127.0.0.1:38429/api/v1/agents | jq '.agents[] | {name, profiles: [.profiles[] | {id, billing_type, model}]}'

# 2. the task and session fields that carry the profile
curl -s http://127.0.0.1:38429/api/v1/workflows/<id>/tasks | jq '.tasks[0] | {primary_agent_name, metadata}'
curl -s http://127.0.0.1:38429/api/v1/tasks/<id>/sessions | jq '.sessions[0].agent_profile_id'

# 3. the Codex quota, read-only, spending nothing
python3 -c 'import json,pathlib,urllib.request as u; t=json.loads((pathlib.Path.home()/".codex/auth.json").read_text())["tokens"]; r=u.urlopen(u.Request("https://chatgpt.com/backend-api/codex/usage", headers={"Authorization":"Bearer "+t["access_token"],"chatgpt-account-id":t["account_id"]})); print(json.dumps(json.load(r)["rate_limit"],indent=2))'
```

[`concurrency-sizing.md`]: concurrency-sizing.md
[`isolation-and-federation.md`]: isolation-and-federation.md
[contract]: ../tests/test_contract_live.py
