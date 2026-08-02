# Should the pacer run more than one task at a time?

`PACER_CONCURRENCY` defaults to 1. The proposal: when consumption is well under the planned
rate, let several tasks run at once instead of holding the board to a single turn.

Measured on `gogo`, 2026-07-27, rather than argued from intuition, with a one-off simulator
(`sim_concurrency.py`) that has since been deleted — it was a second copy of the pacing loop
that had to be kept in sync with the real one, for a question that is now answered here. To
reproduce or re-run it:

```bash
git show 977d2e1:sim_concurrency.py > /tmp/sim_concurrency.py
```

It offered `--levers`, `--history`, and `--measure <pacer.jsonl>`. It imported the gate
functions from the old top-level `pacer` module, so a re-run needs those imports pointed at
`kandev_pacer.quota` / `kandev_pacer.workflow`.

Three inputs it fed on were removed from the pacer at the same time, for the same reason —
they cost something on every tick and only this analysis read them. To re-run the analysis
on *fresh* data rather than the log already collected, restore them from the same commit:

| dropped | what it recorded | where it lived |
|---|---|---|
| `session` log records | each `{from → to}` session state change, the only way to see when a turn *ended* | `quota.sessions_snapshot` / `diff_sessions` |
| `window_reset` log records | `final_c`, what a window closed at — gone for good once the endpoint rolls over | `quota.detect_resets` |
| `--watch-usage N` | a read-only sampler, run alongside the pacer, timing how long after a launch `utilization_pct` moves | `runtime.watch_usage` |

**Short answer: yes, raise it to 2** — but the strongest reason is not the quota one.

## Read this first: the pacer has never run

No pacer process, no service unit, and every turn in the history belongs to a
manually-created task. Both workflows *are* fully adapted (`make_paced.py` reports "already
paced" for `Paced Kanban` and `Feature Dev`), so the board is ready and nothing is launching
turns on it.

This governs how every historical number below may be read. The board being idle 92.3 % of
the time is **the absence of a launcher, not a pacer choosing to withhold**. Any claim of the
form "the pacer only achieves X" is unsupported — it has not achieved anything yet. What the
history *can* tell us is what the pacer would have found had it been running: how deep the
queue was, and how often the cap would have blocked it.

## What parallelism can and cannot buy

Over a window, work is quota-capped. With `r` = one stream's burn as a multiple of the target
slope, headroom moves as `dh/de = α − N·r`:

- **`r ≥ α`** — one stream already keeps up. Raising `N` hits the line sooner and pauses longer
  for the same area under the curve: granularity, not throughput.
- **`r < α`** — the serial stream cannot burn quota fast enough, windows reset with quota
  unspent, and the steady-state answer is `N* = α / r`.

## Measured

**Turn shape** (349 completed turns, 96.1 h): median **11 s**, mean **124 s**, p90 **316 s**,
max **75 min**. Heavily skewed — most turns are trivially short, a few very long.

**Whether the cap can bind at all is decided by workflow routing, not by queue depth.** A
finished turn either leaves a task on another work step — where it is launchable again and the
pacer can drive it onward by itself — or parks it until a human moves the card. `--history`
calls these *chained* and *gated*, and the two boards here differ:

| workflow | shape | routing | turns |
|---|---|---|---|
| `Paced Kanban` | **gated** | `In Progress` → `Review` (parked) | 276 (91 %) |
| `Feature Dev` | **chained** | `Spec → Work → Review → QA → PR`, all work steps | 28 |

On the gated board the pacer gets **one turn per human hand-off**, no matter what the cap is.
Two tasks can only run at once there if a human has put two cards on `In Progress`. On the
chained board the pacer can run a task through five steps unattended, so the cap binds as soon
as two tasks are in flight — but only two tasks have ever existed there.

**A previous version of this document claimed the queue was deep 95.6 % of the time. That was
wrong and is withdrawn.** It counted the gap between a task's turns as candidate time, which is
only an upper bound: `is_launchable` also requires the task to be at a work step, and 91 % of
the history is on the gated board, where a finished turn parks the task at `Review` and almost
none of that gap is launchable. The exact figure is unrecoverable — `session_step_history` is
empty, so where a task sat during a gap is not recorded. `--history` now prints the bound as a
bound, with the routing shape beside it.

What can be said: the gaps are parked work rather than conversational pauses (gaps under
5 minutes are 73 % of the *gaps* but 0.2 % of candidate-seconds), and the live board at the time
of writing had **0 launchable tasks** with 8 of 10 in Done.

**Burn per stream ≈ 1× steady, order of magnitude only.** Correlating a usage probe against
turn-seconds in flight gave 1.22, then 1.07, then 0.89 as busy time accrued with no further
consumption step; a later 4-minute probe spanned no step at all. Dividing by turn-seconds
rather than wall-clock matters — at this duty cycle the wall-clock figure understates one
stream's rate several-fold. Treat the value as "about 1". `--measure` warns below three steps
and refuses when consumption did not move.

**Usage feedback is coarse and stepped.** The 5-hour number is whole-percent quantized and
arrives in lumps — one 4-point jump in 5.6 minutes, none at all in a later 4-minute window.
There is no sub-tick feedback; any concurrency rule must assume at least a full tick of
blindness.

## The finding that actually decides it

**The cap counts *any* running session in a paced workflow, including a human's.** It is
`sum(1 for sessions in STARTING/RUNNING)` over paced tasks — it does not care who started
the turn.

Checked live: this very session — an interactive task sitting in `Paced Kanban` at `In
Progress`, state `RUNNING` — occupies the single slot, and the pacer reports **FULL**. With
`PACER_CONCURRENCY=1`, *while a human works on any task in a paced workflow, the backlog gets
nothing*, no matter how much headroom there is.

Historically the cap would have read FULL for 7.7 % of the span. That is modest in aggregate,
but it is not randomly distributed: it is exactly the hours a human is at the keyboard, which
is also when new backlog work appears. And it is a pathology **only a higher cap fixes** —
shortening the tick does not help, because the slot is occupied, not merely unpolled.

This is an argument for `N ≥ 2` that is independent of quota entirely.

## Simulated

The simulator ran the real gate (`worst_headroom` and the burn-line rule now inlined
into `quota_verdict`)
over a 5-hour window, resampling durations from the 349 real turns, with a deep backlog. Read
it as *what a higher cap is worth when there is work to give it* — the section above is exactly
the argument that this precondition is not established here. Turns completed / final consumption, `dead_time=300 s`:

| burn | N=1 | N=2 | N=4 | N=8 |
|-----:|:----|:----|:----|:----|
| 0.5  | 84 · c=0.30 | 131 · c=0.46 | 150 · c=0.52 | 150 · c=0.52 |
| 1.0  | 84 · c=0.59 | 124 · c=0.89 | 132 · c=0.92 | 132 · c=0.92 |
| 2.0  | 74 · c=1.00 ❗ | 75 · c=1.01 ❗ | 76 · c=1.03 ❗ | 76 · c=1.03 ❗ |

❗ = peak consumption passed 1.0, i.e. a hard lockout mid-turn.

At the measured burn a second slot is worth **+47 % turns**, landing the window at 0.89 instead
of 0.59 — spending quota that was otherwise wasted, with no overshoot. A fourth slot adds 6 %;
an eighth adds nothing. At burn ≥ 2 the gate binds and every cap performs identically, so
parallelism buys nothing there while still widening the overshoot.

**The saturation at 150 is the tick rate, not concurrency.** One launch per tick caps a 5-hour
window at `WINDOW/INTERVAL` = 150 turns, which makes tick rate a competing lever:

| burn | N=1/120s | N=2/120s | N=4/120s | N=1/30s | N=1/15s |
|-----:|---------:|---------:|---------:|--------:|--------:|
| 0.5  | 84 | 131 | 150 | 118 | 128 |
| 1.0  | 84 | 124 | 132 | 118 | 128 |

For *throughput alone*, `N=1` at a 15-second tick matches `N=2` at 120 s while staying serial —
which does not multiply dead-time overshoot (`N × burn × dead_time`) and keeps scheduling
depth-first, which `docs/history/pacing-designs.md` wants on prompt-cache grounds, since round-robining
pushes every task's between-turn gap past the cache TTL. (That cache argument has since been
weakened: Claude Code on a subscription uses a **1-hour** TTL, not the 5-minute one assumed
there, so a round-robin gap has far more slack before it costs anything. Depth-first remains
preferable, but it is no longer a reason on its own to prefer `N=1`. The dead-time argument
below is unaffected.) That document already named the fix:
*"react to turn-completed events instead of a slow polling tick."* But the tick lever does
nothing for the occupied-slot problem above, so the two are complements, not alternatives.

## Conclusions

1. **Nothing here has been exercised.** The pacer is not running. Before tuning its cap, run
   it — the JSONL log will answer in days what history can only approximate.
2. **Whether the cap binds is a board-usage question, and history cannot answer it.** On the
   gated board that carries 91 % of the history, the pacer gets one turn per hand-off and the
   cap is irrelevant unless a human parks two cards on a work step at once. On the chained
   board it binds as soon as two tasks are in flight. Both are choices about how the boards get
   used — and since the pacer has never run, usage has never had reason to adapt to it. The
   earlier statistical case for a deep queue does not survive scrutiny and is withdrawn.
3. **Raise it to 2 anyway, for the interlock.** That argument is structural and needs no
   queue-depth estimate: at cap 1 a human working in a paced workflow blocks the backlog
   entirely, because their own session fills the slot. Two slots decouple those. The simulation
   independently puts the throughput at the second slot (+47 %) should the queue ever be deep;
   the third and fourth are nearly free but nearly worthless.
4. **Prefer the tick rate for further throughput.** Same gain, no overshoot amplification,
   keeps cache locality — though with a 1-hour TTL that last point carries less weight than
   when this was written. Event-driven launch on turn completion is the endpoint.
5. **Size any dynamic cap from measured `r`.** `N ≤ (α + h·D/T) / r`: headroom converted to
   seconds via the window duration `D`, over reaction time `T`, divided by measured burn.
   Assuming `r ≈ α` overstates `N` by exactly the factor `r`. Take the minimum over windows of
   `slots_w`, never `slots(min h)` — `h = 0.1` is 30 minutes of slack on the 5-hour window and
   16.8 hours on the 7-day.
6. **Risk is asymmetric and the backstop does not cover it.** Peak consumption passes 1.0 at
   burn ≥ 2 *even at N=1*, because running turns keep burning after the gate shuts and
   preemption is out of scope. Concurrency widens an exposure that already exists.
7. **Attribution caps the upside.** The gate reads account-level usage, so Routines, UI work and
   interactive sessions all count against it. At `exogenous=50 %` the simulation locks out at
   every cap for burn ≥ 1. Paced work is a minority of burn here, so any change to the paced
   queue has correspondingly less leverage than the headroom number suggests.

## If it is implemented

- Start at a static `PACER_CONCURRENCY=2` and read the log before going further. A dynamic cap
  is premature while `r` is known only to one significant figure and the gap between the
  measured ~1 and the 2.0 where every cap starts locking out is this narrow.
- A dynamic cap, when it comes: compute `slots` per §5, take the min over windows, keep
  `PACER_CONCURRENCY` as a hard ceiling (N worktrees and N agents may exhaust the machine well
  below the quota-implied N), and force 1 near `PACER_BACKSTOP` — `c` high and `h` positive can
  coexist.
- Keep one launch per tick. With a dynamic cap that becomes additive-increase /
  multiplicative-decrease: headroom is re-checked between launches and the cap collapses when
  the gate shuts.
- Hysteresis on the 0→1 boundary only. Flapping between 3 and 2 is harmless, since the cap
  gates new launches and never kills running turns.
- `PACER_CONCURRENCY=1` must keep reproducing today's behaviour exactly.

## Watch for

Raising the cap removes an accidental protection. A task parked at a work step with an idle
session is launchable — including one a human is mid-conversation with, between prompts. At
cap 1 that is largely masked, because the human's own turns keep the slot busy. Those gaps are
0.2 % of candidate-seconds, so the exposure is small and it exists at cap 1 already, but it is
worth a look once the pacer is actually running: an unsolicited turn injected into a live
conversation is a much more visible failure than a slow backlog.
