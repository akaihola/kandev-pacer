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
| `PACER_CONCURRENCY` | `1` | how many paced turns may run at once |
| `PACER_ALPHA` | `1.0` | target burn line. Below 1 aims under steady, finishing each window with quota spare |
| `PACER_BACKSTOP` | `0.97` | consumed fraction at which everything is withheld regardless. `1.0` disables |
| `PACER_INTERVAL` | `120` | seconds between ticks |
| `PACER_REFUSAL_WINDOW` | `10` | how long a launch waits. Only long enough to catch a refusal — an accepted launch is answered minutes later, and waiting froze the loop |
| `PACER_LAUNCH_LATENCY` | `90` | how long kandev may take to make an accepted launch visible. Bookings last three ticks plus this |
| `PACER_FAIL_BACKOFF` | 3 ticks | seconds before a refused launch is retried; doubles per consecutive failure, up to 8× |
| `PACER_BLOCK_AFTER` | `2` | consecutive failures before the card is flagged `BLOCKED`. `0` keeps the pacer read-only |
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
  separately, plus `running`, `launchable`, `cap`, `booked`, and a per-window breakdown.
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

**The gate never closes even though work is running.** Check `PACER_BACKSTOP` and that the
usage payload actually reports windows; a genuinely fresh account has none yet, and paces
normally.

## Layout

```
src/kandev_pacer/
  config.py     env-backed Settings, plus kandev's fixed vocabulary
  quota.py      the pacing math and the tick's quota verdict (pure)
  workflow.py   which workflows are paceable, and the startup lint (pure)
  selection.py  which task goes next, whether we may, what we already spent (pure)
  client.py     every kandev HTTP and WebSocket call
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

[kandev]: https://github.com/kdlbs/kandev
[docs/design.md]: docs/design.md
[userscript/kandev-queue-button.user.js]: userscript/kandev-queue-button.user.js
[docs/queueing-user-prompts.md]: docs/queueing-user-prompts.md
