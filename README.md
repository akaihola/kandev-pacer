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

uv run make-paced                         # list the workflows kandev has
uv run make-paced 'Paced Kanban'          # dry run: show the edits pacing needs
uv run make-paced 'Paced Kanban' --apply  # write them through kandev's step API
uv run make-paced 'Paced Kanban' --unpace --apply   # stop pacing that board

uv run kandev-pacer --dump-order          # the queue launches will follow, spending nothing
uv run kandev-pacer                       # start the pacing loop (lints on startup)
```

A workflow has to be **enrolled** before it is paced, which is what `make-paced` does: it
strips the `auto_start_agent` action that would bypass the gate, tags each step that
routes away on turn completion `stage_type=work`, and appends a `step_complete_kandev`
instruction to the prompt of any signal-gated work step that never names that tool —
kandev only advances such a step when the agent calls it, so a silent prompt leaves the
task idling and relaunching until the stall detector parks it. The edits go through
kandev's step update endpoint, `PUT /api/v1/workflow/steps/{id}`, which takes every step
field including `stage_type` and needs no login — so kandev has to be running (`KANDEV_URL`).
`stage_type` still has no UI or import surface: a workflow re-import silently resets it,
so re-run `make-paced` after one.

Enrollment is the *only* opt-in: the pacer paces every enrolled workflow and nothing else.
`--unpace` is how a board stops being paced — it clears the `work` tag, leaving the board
manual rather than self-starting, since `auto_start_agent` is not put back. The pacer prints
the workflows it is pacing and the ones it is not, with the reason, at startup and in
`--dump-order`.

## Configuration

Configuration is read from `~/.config/kandev-pacer/config.toml`, environment variables,
and command-line options, in that precedence order (later sources win). All values are
validated at startup — a bad value stops the pacer with its name in the message.

The TOML file uses a `[kandev-pacer]` table and field names such as `interval` and
`concurrency`. `--config PATH` selects another file; every setting also has a matching
long option such as `--interval` and `--concurrency`.

| variable | default | what it does |
| --- | --- | --- |
| `KANDEV_URL` | `http://127.0.0.1:38429` | where kandev listens |
| `KANDEV_WS_URL` | derived from `KANDEV_URL` | the `/ws` endpoint, if it differs |
| `PACER_PROVIDER` | `anthropic` | which usage agent to gate on. A name matching nothing **fails closed** rather than pacing blind |
| `PACER_PROVIDERS` | `$PACER_PROVIDER` | the subscriptions to pace, comma- or space-separated (e.g. `anthropic,openai`). Naming more than one turns on per-provider gating; see [Two subscriptions](#two-subscriptions) |
| `PACER_QUOTA_SOURCE` | `direct` | `direct` reads provider endpoints; `pebble` reads the collector snapshot |
| `PACER_PEBBLE_SNAPSHOT` | `~/.local/state/pebble-quota/latest.json` | collector snapshot path |
| `PACER_PEBBLE_MAX_AGE` | `900` | maximum snapshot age in seconds |
| `PACER_CONCURRENCY` | `1` | how many paced turns may run at once **per provider** |
| `PACER_CONCURRENCY_TOTAL` | unset | ceiling on the sum across providers — the machine running the worktrees. Unset means each provider is bound only by its own cap |
| `PACER_APIKEY_POLICY` | `cap` | what to do with an api-key task (no subscription window, so nothing to pace it against): `cap` launches it gated on the concurrency cap alone, `withhold` never launches it (opt in when metered spend must stay off) |
| `PACER_AGENT_PROVIDERS` | unset | override which subscription an agent burns, when kandev's `billing_type` is wrong for pacing. `agent=provider` pairs, e.g. `hermes-acp=openai`. Wins over `billing_type`, so an agent kandev labels `api_key` but which really spends a subscription is paced against it — see [Two subscriptions](#two-subscriptions) |
| `PACER_CODEX_CREDENTIALS` | `~/.codex/auth.json` | the ChatGPT token the Codex quota is read with. **Read, never written** — same rule as the Claude credential |
| `PACER_ALPHA` | `1.0` | target burn line. Below 1 aims under steady, finishing each window with quota spare |
| `PACER_ALPHA_5H` | `1.0` | target burn line for 5-hour windows only |
| `PACER_INTERACTIVE_HOURS` | unset | hour range such as `08-23`; outside it, skip the 5-hour burn-line rule while weekly windows and the backstop still apply |
| `PACER_BACKSTOP` | `0.97` | consumed fraction at which everything is withheld regardless. `1.0` disables |
| `PACER_INTERVAL` | `30` | seconds between ticks while headroom is at or above the burn line. Below it the tick paces itself on the wait, in 5–120s — see below |
| `PACER_REFUSAL_WINDOW` | `10` | how long a launch waits. Only long enough to catch a refusal — an accepted launch is answered minutes later, and waiting froze the loop |
| `PACER_LAUNCH_LATENCY` | `90` | how long kandev may take to make an accepted launch visible. Bookings last three ticks at `PACER_INTERVAL` plus this |
| `PACER_FAIL_BACKOFF` | 3 × `PACER_INTERVAL` | seconds before a refused launch is retried; doubles per consecutive failure, up to 8× |
| `PACER_STALL_AFTER` | `5` | consecutive unchanged turns before giving up; `0` disables stall detection |
| `PACER_STALL_ACTION` | `block` | `block` flags stalled tasks; `signal` tries to advance an idle primary session on a step that requires a completion signal |
| `PACER_BLOCK_AFTER` | `2` | consecutive failures before the card is flagged `BLOCKED`. `0` keeps the pacer read-only |
| `PACER_CREDENTIALS` | `~/.claude/.credentials.json` | the subscription token the quota is read with. **Read, never written** — Claude Code owns this file, and a second refresher racing it would rotate the token out from under the user's login |
| `PACER_LOG` | `~/.local/state/kandev-pacer/pacer.jsonl` | JSONL event log. Empty disables it |
| `PACER_HEARTBEAT_WEBHOOK_URL` | unset | the kandev automation webhook to call when a provider runs out of work. Empty turns the feature off — see [Refilling the board](#refilling-the-board) |
| `PACER_HEARTBEAT_WEBHOOK_SECRET` | unset | sent as `X-Webhook-Secret`. Required when the URL is set; keep it out of the TOML and the nix store, load it from an env file |
| `PACER_HEARTBEAT_MIN_LAUNCHABLE` | `2` | call the webhook when an open provider's launchable count is below this |
| `PACER_HEARTBEAT_MIN_INTERVAL` | `1800` | seconds between attempts that led to no launch, counted from the last attempt whatever it answered |
| `PACER_SECRETS_FILE` | empty | optional `KEY=VALUE` file, loaded after TOML and before the process environment; it must not be group- or world-readable |

The secrets file uses one `KEY=VALUE` entry per line. Blank lines and comments are ignored,
and only settings listed in this table are loaded. Process environment variables always win
over the file. Keep the file outside the TOML and protect it with mode `0600` or stricter.
| `PACER_FALLBACK_AFTER` | `0` | seconds a provider gate must stay closed before a configured profile fallback may be used; `0` disables it |
| `PACER_FALLBACK_PROFILES` | unset | profile-name mappings such as `Luna low=Sonnet low`; applies only to `intent:start` |

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

## Refilling the board

The `[Auto] Heartbeat` automation in kandev refills the paced boards from each repository's
`TASKS.md`. On a 15-minute cron it spent a quarter of the Codex subscription finding nothing
to do, so it now has a webhook trigger and no schedule, and the pacer calls it — the one
process that knows when the board is running dry. Set `PACER_HEARTBEAT_WEBHOOK_URL` and
`PACER_HEARTBEAT_WEBHOOK_SECRET` to turn this on; a URL without a secret fails at startup.

Once per tick, after the providers have been paced, the pacer POSTs
`{"source": "kandev-pacer", "provider": ..., "launchable": ...}` when a provider's gate is
**open** and fewer than `PACER_HEARTBEAT_MIN_LAUNCHABLE` of its tasks could be launched:
quota is available and the board is about to idle. A paused gate with no work needs no new
work, so it never calls. It waits `PACER_HEARTBEAT_MIN_INTERVAL` between *attempts* — a
`409` (a run is already active on kandev's side) does not retry every tick — unless the
pacer has launched a task since the last one: the cooldown throttles attempts that produced
nothing, and an attempt whose task has already been launched and finished may be repeated
as soon as the board runs dry again. It skips while a task titled `[Auto] Heartbeat` on a
paced board has a session running. Each attempt writes one `heartbeat` record to the log
with the HTTP status or the error; a failed call is a warning, and the board keeps pacing
without it.

## Logging

The stdout lines say what the pacer is doing now. The JSONL log at `PACER_LOG` says what it
did over the last week, which is what any change to `PACER_CONCURRENCY` or `PACER_ALPHA` has
to be argued from. The record kinds, append-only, ~1.2 MB/day at the default tick;
rotation keeps one 50 MB file and four weekly or size-based backups:

- **`tick`** — one per tick, in every branch. Carries `gate` (`open`/`paused`/`ceiling`/`blind`)
  and `action` (`launched`/`drained`/`full`/`nothing`/`skipped`/`empty`/`exhausted`/`withheld`)
  separately, plus `running`, `launchable`, `cap`, `booked`, `interval` (how long until the
  next tick, so a varying cadence does not read as a stall), and a per-window breakdown.
  The question worth asking of it is `gate=open, action=nothing, launchable>0`: quota going
  unspent while work waits.
- **`failure`** — a launch or drain kandev refused, with the reason and the consecutive count.
- **`start` / `error`** — run configuration, so analysis can segment by cap/alpha/order, and
  loop failures.
- **`heartbeat`** — one per webhook attempt, with the provider, its launchable count and the
  HTTP status or error. See [Refilling the board](#refilling-the-board).

## Troubleshooting

With `PACER_STALL_ACTION=signal`, a stalled primary session waiting for input can
advance through `mcp.step_complete` when its current step requires that signal.
The pacer waits up to `PACER_LAUNCH_LATENCY` seconds, checking on later ticks.
If the step stays put, the signal is rejected, or the session is ineligible, it
falls back to `BLOCKED`. The default `block` action keeps the existing behavior.
`stalled` log records distinguish `signalled`, `advanced`, `signal_uncertain`,
and `blocked` or `block_failed` outcomes and give the fallback reason.
Pending signals and stall counts are in memory and reset on restart. An idle
primary can have a real blocker; review such cards before enabling this option.

**Nothing ever launches, every tick logs `nothing to launch`.** The workflow is probably not
enrolled, or its steps are tagged but do not route on turn completion. `--dump-order` names
what is out of scope and why; `make-paced '<workflow>'` shows what enrollment would change.

When the board is empty, heartbeat handling follows the provider's `nothing to launch`
report. A configured webhook is called when an open provider has fewer than 2 launchable
tasks by default, counted across all its model classes. Setting
`PACER_HEARTBEAT_MIN_LAUNCHABLE=0` prevents triggering. If refill is due but skipped,
the next heartbeat line explains why: the webhook is disabled, the default 1800-second
cooldown has not elapsed since an attempt with no launch after it, or a heartbeat is
already running. Enable it with
`PACER_HEARTBEAT_WEBHOOK_URL` and `PACER_HEARTBEAT_WEBHOOK_SECRET`; see the
[deployment instructions](docs/tasks/2-heartbeat-webhook.md#deploy-on-gogo).

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
`cap`: launched on the concurrency cap alone, since the pacer never withholds work it does
not pace; `withhold` opts back into fail-closed for metered spend). A task the pacer cannot
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

A single provider (the default) paces every subscription task on its window; api-key tasks
still bypass it onto the concurrency cap alone, so a metered-or-free model is never held
behind a subscription window it never touches. The full derivation, and why one process rather
than two, is in [`docs/two-subscriptions.md`].

## The Fable weekly window

The Claude subscription carries a third window besides the 5-hour and 7-day pair: a weekly
quota that only the Fable 5 model spends, capped at half the weekly budget. The pacer
already reads it — any `weekly_scoped` limit in the usage payload becomes a `7-day (Fable)`
window — and it gates each task on exactly the windows its launch can spend. A task whose
agent profile's model (or enabled fallback) contains the scoped window's model name —
`claude-fable-5[1m]` contains `fable` — gates on all three windows; any other Claude task
gates on the shared pair alone, so a tight Fable window no longer withholds work on models
that cannot spend it. A task whose model the pacer cannot see gates on all windows:
stricter, never looser. The two classes still share the provider's concurrency cap and its
one launch per tick, and queue order picks among the tasks whose class is open.
Text logs name the scoped gate `[anthropic (Fable)]`, even when it is the only class.
`open, nothing to launch` means quota allows work but no task is eligible to launch or
drain a queued prompt. Check `kandev-pacer --dump-order` for workflow enrollment, WIP
exclusions and steps that do not auto-launch. Waiting for input at such a step does not
make a task launchable; move it to a paced work step or explicitly queue a prompt on an
idle session. Step configuration determines eligibility, not the step's name.
Restart a running pacer after enrollment changes; it scans workflows only at startup.
For other exclusions, check session state and withheld/failed/stalled log records.
[`docs/fable-window.md`] documents the verified quota rules and the design.

## Pausing and the prompt cache

A paused task's prompt cache expires while it waits — after an hour on a Claude
subscription, after about five minutes on Codex — and the turn that finally runs re-reads
its whole context at the cache-write rate instead of the cache-read rate. The pacer's own
log shows how often its waits crossed that line and what it cost; [`docs/cache-aware-pacing.md`]
measures it and designs a cache-aware release the pacer does not have yet.

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
scripts/        read-only log analyses behind the docs (`cache_waste.py`)
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

Offline tests ignore inherited `PACER_*`/`KANDEV_*` variables and the operator's
default `~/.config/kandev-pacer/config.toml`. Tests still exercise explicit config
files and environment overrides. The separate `PACER_LIVE_KANDEV=1` invocation
keeps operator settings and credentials; its live contract tests otherwise skip.

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
[`docs/fable-window.md`]: docs/fable-window.md
[`docs/cache-aware-pacing.md`]: docs/cache-aware-pacing.md
[`docs/isolation-and-federation.md`]: docs/isolation-and-federation.md
[kdlbs/kandev#1669]: https://github.com/kdlbs/kandev/issues/1669
