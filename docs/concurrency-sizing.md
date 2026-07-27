# Should the pacer run more than one task at a time?

`PACER_CONCURRENCY` defaults to 1. The proposal: when consumption is well under the planned
rate, let several tasks run at once instead of holding the board to a single turn.

Measured on `gogo`, 2026-07-27, rather than argued from intuition. Reproduce with
`./sim_concurrency.py`, `--levers`, `--history`, and `--measure <pacer.jsonl>`.

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

**The queue was deep.** Replaying the 302 turns across 18 tasks in the two paced workflows
(`--history`), counting a task as a candidate in the gap between two of its own turns — a later
turn exists, so work was genuinely pending, which is what `is_launchable` tests:

| candidates waiting at once | 0 | 1 | 2 | 3 | 4 | 6 | 8 | 10 |
|---|---|---|---|---|---|---|---|---|
| share of span | 0.6 % | 3.7 % | 4.8 % | 25.5 % | 27.8 % | 8.0 % | 12.2 % | 11.1 % |

**Two or more candidates were waiting 95.6 % of the span**, with the mode at 3–4. These are not
conversational pauses: gaps under 5 minutes are 73 % of the *gaps* but only **0.2 % of
candidate-seconds**, while 95.5 % of candidate-time comes from gaps over two hours — genuinely
parked work.

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

`sim_concurrency.py` runs the real gate (`worst_headroom`, `gate_open`, `ceiling_breached`)
over a 5-hour window, resampling durations from the 349 real turns, with a deep backlog — an
assumption the history above supports. Turns completed / final consumption, `dead_time=300 s`:

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
depth-first, which `docs/pacing-designs.md` wants on prompt-cache grounds, since round-robining
pushes every task's between-turn gap past the cache TTL. That document already named the fix:
*"react to turn-completed events instead of a slow polling tick."* But the tick lever does
nothing for the occupied-slot problem above, so the two are complements, not alternatives.

## Conclusions

1. **Nothing here has been exercised.** The pacer is not running. Before tuning its cap, run
   it — the JSONL log will answer in days what history can only approximate.
2. **The premise is correct: the cap would bind.** Two or more tasks were launchable at once
   95.6 % of the span. A single live snapshot showing zero launchable tasks was taken after the
   board had drained (8 of 10 in Done) and is not representative.
3. **Raise it to 2, for the interlock more than the quota.** At cap 1 a human working in a
   paced workflow blocks the backlog entirely. Two slots decouple those, and the simulation
   says the second slot is also where the throughput is (+47 %); the third and fourth are
   nearly free but nearly worthless.
4. **Prefer the tick rate for further throughput.** Same gain, no overshoot amplification,
   keeps cache locality. Event-driven launch on turn completion is the endpoint.
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
