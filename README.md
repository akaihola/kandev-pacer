# kandev-pacer

Paces low-priority [kandev] task execution against a
Claude subscription quota, so consumption burns down steadily instead of front-loading
and hitting a mid-window lockout. High-priority work (Routines, UI-created tasks) runs
untouched; low-priority backlog work is admitted only while under the steady-burn curve.

The rule in one line: **pause while you have consumed a larger fraction of your quota than
of the window**. Headroom `h = elapsed − consumed`; launch while `h ≥ 0`. Pausing lets `h`
climb back on its own, so the gate reopens without anything having to release it.

Why that rule and not another, how enforcement avoids killing live turns, and the kandev
source behind every claim here: [`docs/design.md`].

## When the gate reopens

While the gate is shut nothing is spent, so `consumed` is frozen and only `elapsed` moves:
the moment `h` climbs back to zero is arithmetic, not a guess. The paused line says it, and
the loop paces itself by it — each tick sleeps **half the remaining wait**, clipped to
**5–120 s**:

```
headroom=-0.300 PAUSED (0 running), resumes in ~1h 30m 00s (~14:05), next tick in 120s
headroom=-0.004 PAUSED (0 running), resumes in ~0m 34s (~14:05), next tick in 17s
```

Halving what is left converges on the reopening from above: an hour out it costs two ticks
an hour, and the gate is still noticed within 5 seconds of opening rather than up to two
minutes late. It is only an estimate — a turn still running keeps spending, which pushes
the reopening out, and the next tick simply re-estimates.

The trigger is the headroom itself, not which gate is shut: **`h < 0` adapts, `h ≥ 0`
always waits `PACER_INTERVAL`**. At or above the line there is nothing to climb back to,
and the interval is the pacing rate rather than a poll ceiling — shortening it there would
start turns sooner, which is a different decision from noticing something sooner. That
covers an open gate, a blind one, and the rare `PACER_BACKSTOP` breach that binds while
`h` is already back above the line. A breach *below* the line adapts like any other wait,
converging on the later of the window's roll and the burn line clearing — the ceiling
reads `consumed`, which time does not lower.

The 5–120 s band is fixed, deliberately **not** bounded by `PACER_INTERVAL`, because the
two answer different questions. `PACER_INTERVAL` is how eagerly to look for work that
could start *now*, and is worth setting to seconds; how often to re-check a wait whose end
is already computed is not. So `PACER_INTERVAL=5` gives a gate that reacts within seconds
while a two-hour pause still costs one tick every two minutes — the wait paces itself
slower than the configured tick there, and faster than it at the 120 s default.

## Quick start

```bash
uv sync                                   # create the environment, install the commands

uv run make-paced                         # list workflows in the kandev SQLite DB
uv run make-paced 'Paced Kanban'          # dry run: show the edits pacing needs
uv run make-paced 'Paced Kanban' --apply  # write them (the server can stay up; WAL)
uv run make-paced 'Paced Kanban' --unpace --apply   # stop pacing that board

uv run kandev-pacer --dump-order          # the queue launches will follow, spending nothing
uv run kandev-pacer                       # start the pacing loop (lints on startup)
```

A workflow has to be **enrolled** before it is paced, which is what `make-paced` does: it
strips the `auto_start_agent` action that would bypass the gate, and tags each step that
routes away on turn completion `stage_type=work`. Both edits go straight to
`~/.kandev/data/kandev.db`, because `stage_type` has no API, UI or import surface. A
workflow re-import silently resets it — re-run `make-paced` after one.

Enrollment is the *only* opt-in: the pacer paces every enrolled workflow and nothing else.
`--unpace` is how a board stops being paced — it clears the `work` tag, leaving the board
manual rather than self-starting, since `auto_start_agent` is not put back. The pacer prints
the workflows it is pacing and the ones it is not, with the reason, at startup and in
`--dump-order`.

## Configuration

All configuration is environment variables, validated at startup — a bad value stops the
pacer with the variable's name in the message rather than failing hours later.

| variable | default | what it does |
| --- | --- | --- |
| `KANDEV_URL` | `http://127.0.0.1:38429` | where kandev listens |
| `KANDEV_WS_URL` | derived from `KANDEV_URL` | the `/ws` endpoint, if it differs |
| `PACER_PROVIDER` | `anthropic` | which usage agent to gate on. A name matching nothing **fails closed** rather than pacing blind |
| `PACER_PROVIDERS` | `$PACER_PROVIDER` | the subscriptions to pace, comma- or space-separated (e.g. `anthropic,openai`). Naming more than one turns on per-provider gating; see [Two subscriptions](#two-subscriptions) |
| `PACER_CONCURRENCY` | `1` | how many paced turns may run at once **per provider** |
| `PACER_CONCURRENCY_TOTAL` | unset | ceiling on the sum across providers — the machine running the worktrees. Unset means each provider is bound only by its own cap |
| `PACER_APIKEY_POLICY` | `withhold` | what to do with an api-key task (no subscription window): `withhold` never launches it, `cap` launches it gated on the concurrency cap alone. Only consulted with two-plus providers |
| `PACER_AGENT_PROVIDERS` | unset | override which subscription an agent burns, when kandev's `billing_type` is wrong for pacing. `agent=provider` pairs, e.g. `hermes-acp=openai`. Wins over `billing_type`, so an agent kandev labels `api_key` but which really spends a subscription is paced against it — see [Two subscriptions](#two-subscriptions) |
| `PACER_CODEX_CREDENTIALS` | `~/.codex/auth.json` | the ChatGPT token the Codex quota is read with. **Read, never written** — same rule as the Claude credential |
| `PACER_ALPHA` | `1.0` | target burn line. Below 1 aims under steady, finishing each window with quota spare |
| `PACER_BACKSTOP` | `0.97` | consumed fraction at which everything is withheld regardless. `1.0` disables |
| `PACER_INTERVAL` | `120` | seconds between ticks while headroom is at or above the burn line. Below it the tick paces itself on the wait, in 5–120s — see below |
| `PACER_REFUSAL_WINDOW` | `10` | how long a launch waits. Only long enough to catch a refusal — an accepted launch is answered minutes later, and waiting froze the loop |
| `PACER_LAUNCH_LATENCY` | `90` | how long kandev may take to make an accepted launch visible. Bookings last three ticks at `PACER_INTERVAL` plus this |
| `PACER_FAIL_BACKOFF` | 3 × `PACER_INTERVAL` | seconds before a refused launch is retried; doubles per consecutive failure, up to 8× |
| `PACER_BLOCK_AFTER` | `2` | consecutive failures before the card is flagged `BLOCKED`. `0` keeps the pacer read-only |
| `PACER_CREDENTIALS` | `~/.claude/.credentials.json` | the subscription token the quota is read with. **Read, never written** — Claude Code owns this file, and a second refresher racing it would rotate the token out from under the user's login |
| `PACER_LOG` | `~/.local/state/kandev-pacer/pacer.jsonl` | JSONL event log. Empty disables it |
| `KANDEV_DB` | `~/.kandev/data/kandev.db` | the database `make-paced` edits |

## Which task goes next

Two orderings, layered: **what you placed by hand** wins, and everything else falls back to
**the board's own kanban-pull order** (rightmost column first, then drag position, priority,
age, id).

To place tasks by hand, put a sidebar view on sort `custom`, group `none`, and drag. The
pacer re-reads that order every tick, so re-prioritizing needs no restart. Pinned tasks
float to the top; subtasks follow their parent; anything you never dragged keeps pull order
behind the placed ones.

The drag order is layered on as soon as **any** saved sidebar view sorts `custom`; with no
such view, pull order decides alone.

Everything degrades to pull order rather than to something arbitrary: unreadable settings,
nothing dragged yet, or a task whose parent chain loops.

Run `uv run kandev-pacer --dump-order` to see the resulting queue without waiting a tick or
spending a turn. The full derivation — including why "any saved view" rather than the active
one, and the WIP trade-off the layering makes — is in [`docs/design.md`].

## Queued user prompts

A follow-up typed into kandev's composer normally fires a turn on the spot. Park it on the
session's message queue instead — the Queue button in
[`userscript/kandev-queue-button.user.js`] — and the
pacer releases it when the gate opens. Releases outrank launches, and a task with a queued
prompt is drained at *any* step, including parked ones like Review where a launch never
applies. See [`docs/queueing-user-prompts.md`].

## Logging

The stdout lines say what the pacer is doing now. The JSONL log at `PACER_LOG` says what it
did over the last week, which is what any change to `PACER_CONCURRENCY` or `PACER_ALPHA` has
to be argued from. Three record kinds, append-only, ~300 KB/day at the default tick:

- **`tick`** — one per tick, in every branch. Carries `gate` (`open`/`paused`/`ceiling`/`blind`)
  and `action` (`launched`/`drained`/`full`/`nothing`/`skipped`/`empty`/`exhausted`/`withheld`)
  separately, plus `running`, `launchable`, `cap`, `booked`, `interval` (how long until the
  next tick, so a varying cadence does not read as a stall), and a per-window breakdown.
  The question worth asking of it is `gate=open, action=nothing, launchable>0`: quota going
  unspent while work waits.
- **`failure`** — a launch or drain kandev refused, with the reason and the consecutive count.
- **`start` / `error`** — run configuration, so analysis can segment by cap/alpha/order, and
  loop failures.

## Troubleshooting

**Nothing ever launches, every tick logs `nothing to launch`.** The workflow is probably not
enrolled, or its steps are tagged but do not route on turn completion. `--dump-order` names
what is out of scope and why; `make-paced '<workflow>'` shows what enrollment would change.

**A prompt you queued never runs.** Check it is on a session the pacer can see: it reads
every *idle* session, so a prompt parked on a `RUNNING` one waits for that turn to finish.

**A card is flagged `BLOCKED`.** The pacer gave up on it after `PACER_BLOCK_AFTER` refused
launches — usually a `CREATED` session with no executor record, which no retry will fix.
kandev clears the flag itself the next time anything runs the task.

**`provider ... not in usage ... check PACER_PROVIDER`.** The name matches no provider in the
usage payload. The pacer withholds rather than pacing blind with the backstop dead.

**`quota credentials expired -- re-auth Claude Code`.** The access token in
`PACER_CREDENTIALS` has run out and the pacer will not refresh it: Claude Code refreshes the
same file, and two refreshers rotating one token is how a login gets broken. Run `claude` and
`/login`; the next tick picks the new token up. Nothing is lost by withholding meanwhile —
the turns the pacer releases run against that same credential.

**The gate never closes even though work is running.** Check `PACER_BACKSTOP` and that the
usage payload actually reports windows; a genuinely fresh account has none yet, and paces
normally.

## Two subscriptions

One pacer can gate more than one subscription. Set `PACER_PROVIDERS=anthropic,openai` and each
paced task is held behind **its own** provider's window, not behind whichever one happens to be
configured — so a board mixing Claude and Codex work no longer withholds Codex behind a Claude
window it never touches.

Everything that was scalar becomes per provider: a verdict, a running count, a `PACER_CONCURRENCY`
cap, and a JSONL `tick` record each. One launch per provider per tick — the two are not competing
for one resource, so serialising them would only starve whichever lost the global order. The loop
paces itself on the **earliest** reopening across providers, so a two-hour Claude pause never
stops it noticing Codex work it could start now. What you give up is a single global order *across*
providers: two tasks on different subscriptions were never contending for the same quota, so "X
before Y" holds only when X and Y share one.

Which subscription a task burns is read from its agent profile — `GET /api/v1/agents` maps the
profile to its agent (`claude-acp` → `anthropic`, `codex-acp` → `openai`) and its billing type.
Mapping is **by agent, not by model**: an agent running an OpenAI model on an API key has no
subscription window, and `PACER_APIKEY_POLICY` — not a gate — decides its fate (default
`withhold`, so an upgrade never quietly starts spending metered money). A task the pacer cannot
attribute is withheld, never paced against a guess.

Reads degrade per provider: an unreachable Codex endpoint blinds *its* gate and leaves Claude's
exactly as open as its own quota says. `PACER_CONCURRENCY_TOTAL` caps the sum across providers —
the one genuinely shared thing is the machine running the worktrees.

**When kandev's `billing_type` is wrong.** An agent can run a subscription-backed model while
kandev still labels the profile `api_key` — Hermes runs `gpt-5.6-sol` on the Codex *OAuth*
subscription (the same `~/.codex/auth.json` the openai source reads), yet its profile reports
`api_key`. Left alone the pacer would withhold it, or under `cap` launch it **ungated against a
window it actually spends**. `PACER_AGENT_PROVIDERS=hermes-acp=openai` states the truth: it wins
over `billing_type`, so Hermes is paced on the Codex window it really burns. The pacer will not
infer this from the model string (an agent *can* run an OpenAI model on a real API key); only the
operator knows, so it is stated, not guessed.

A single provider (the default) takes none of this path: every task burns it, one `tick` record,
byte-identical to before. The full derivation, and why one process rather than two, is in
[`docs/two-subscriptions.md`].

## Multiple kandev instances

The pacer drives one kandev instance. Splitting sensitive projects across several instances —
one trust domain each — costs the single global priority order, since a kandev instance can
read everything inside it and nothing outside it.
[`docs/isolation-and-federation.md`] works through what a coordinator over several instances
would look like: a redacted central queue holding rank ordinals and opaque ids, short-lived
leases that authorize a *start* (never a kill — there is no preemption here), and prompts,
logs and repositories that never leave their domain.

Two facts shape it. The quota is read from Anthropic directly and is **account-level**, so
independent pacers on separate instances already see each other's burn and hold the aggregate
rate roughly right — what they cannot do is order across boards. And kandev's `/api/v1` is
**unauthenticated** while `/ws` checks only `Origin`, so reaching an instance's port is full
access to it: the pacing decision can be centralized, but the launching cannot. Nothing there
is implemented.

Upstream, [kdlbs/kandev#1669] tracks first-class pull-based pacing; kandev today offers
per-workflow WIP limits, and "office mode" plans model tiers and provider routing.

## Layout

```
src/kandev_pacer/
  config.py     env-backed Settings, plus kandev's fixed vocabulary
  quota.py      the pacing math and the tick's quota verdict (pure)
  workflow.py   which workflows are paceable, and the startup lint (pure)
  selection.py  which task goes next, whether we may, what we already spent (pure)
  client.py     every kandev HTTP and WebSocket call
  provider.py   the quota, read from each provider directly (Anthropic, Codex) — kandev stopped answering for it
  runtime.py    the Pacer object, the loop, the CLI
  make_paced.py the enrollment tool (`make-paced`)
tests/          one file per module, over conftest.py's fake kandev and builders
docs/           how the system works today, and the investigations behind it
  history/      designs that were considered and rejected — background, not current
```

The three pure modules import nothing but `config`, so everything worth arguing about can
be tested with plain dictionaries. One tick runs as **gather → decide → act**:
`Pacer.read()` makes the calls and returns a `Snapshot`, `quota_verdict()` and
`select_action()` decide over it, and `Pacer.drain()`/`Pacer.launch()` make the single call
that spends a turn.

```bash
uv run pytest                              # the whole suite (no network, no quota, no server)
uv run pytest tests/test_quota.py -k alpha # one file, or one test by name
PACER_LIVE_KANDEV=1 uv run pytest tests/test_contract_live.py   # after every kandev upgrade
uv run ruff check src tests                # style and dead code, whole tree
uv run ty check src tests                  # types, whole tree
uv run darker                              # reformat what this branch changed
uv run graylint                            # ruff + ty findings this branch introduced
```

`darker` and `graylint` rather than formatting and checking everything: both compare
against the branch point and act only on what the branch touched, so style converges as
code is edited instead of arriving as one sweep nobody can review, and a pre-existing
warning in a file you edited does not become your problem. All of it is configured in
`pyproject.toml` — the formatter aims for 88 columns, the linter only errors past 100,
and graylint runs `ruff` and `ty` together.

Coverage is one flag away and worth running when adding tests:
`uv run pytest --cov=kandev_pacer --cov-report=term-missing`.

### Run the contract test after every kandev upgrade

The default suite runs entirely against `conftest.py`'s fake board, which means it stays
green no matter what kandev does to its API. 0.82.0 → 0.84.1 removed
`GET /api/v1/agents/usage`; every tick died on a 404, nothing launched, and the only
evidence was a log full of anonymous errors — with the whole suite passing throughout.

`tests/test_contract_live.py` is the one test that would have caught it at upgrade time.
It checks the *shape* of everything the pacer reads from a running kandev — the endpoints,
their envelopes, the keys, the session-state and step-event vocabulary — plus the
Anthropic usage read that quota now comes from, and whether kandev still recognizes the
WebSocket actions the pacer sends. It is skipped unless `PACER_LIVE_KANDEV=1`, so
`uv run pytest` is unchanged, and it starts no turn and spends no quota.

```bash
PACER_LIVE_KANDEV=1 uv run pytest tests/test_contract_live.py -v
KANDEV_URL=http://host:38429 PACER_LIVE_KANDEV=1 uv run pytest tests/test_contract_live.py
```

It asserts nothing about the board's contents, so it passes against any instance with at
least one workflow; rows needing a task or a session skip themselves on an emptier one. A
server that is not there is a skip, not a failure — only kandev answering differently is a
failure, and the message names the endpoint and the key that moved.

Two things it cannot cover, both worth knowing before trusting a green run. It never sends
`session.launch` or `message.queue.drain`, because sending them is the one thing that could
spend a turn — so a rename of those two action names would still reach production. And
kandev omits empty fields, so `prompt` and `agent_profile_id` on a step can only be checked
where some step actually carries one; no step of any workflow carries an `agent_profile_id`
at 0.84.1, which leaves that branch of the startup lint unverifiable from outside.

[kandev]: https://github.com/kdlbs/kandev
[`docs/design.md`]: docs/design.md
[`userscript/kandev-queue-button.user.js`]: userscript/kandev-queue-button.user.js
[`docs/queueing-user-prompts.md`]: docs/queueing-user-prompts.md
[`docs/two-subscriptions.md`]: docs/two-subscriptions.md
[`docs/isolation-and-federation.md`]: docs/isolation-and-federation.md
[kdlbs/kandev#1669]: https://github.com/kdlbs/kandev/issues/1669
