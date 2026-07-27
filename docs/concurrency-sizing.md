# Should the pacer run more than one task at a time?

`PACER_CONCURRENCY` defaults to 1. The proposal: when consumption is well under the planned
rate, let several tasks run at once instead of holding the board to a single turn.

This is the evidence for and against, measured on `gogo` on 2026-07-27 rather than argued
from intuition. Reproduce with `./sim_concurrency.py`, `./sim_concurrency.py --levers`, and
`./sim_concurrency.py --measure ~/.local/state/kandev-pacer/pacer.jsonl`.

## What parallelism can and cannot buy

Over a window, work is quota-capped. With `r` = one stream's burn rate as a multiple of the
target slope, headroom moves as `dh/de = α − N·r`. So:

- **`r ≥ α`** — a single stream already keeps up. Raising `N` hits the line sooner and pauses
  longer for the same area under the curve. It changes granularity, not throughput.
- **`r < α`** — the serial stream cannot burn quota fast enough, windows reset with quota
  unspent, and the steady-state right answer is `N* = α / r`.

Only the second case pays. Everything below is about establishing which one is real, because
nothing else in the decision matters until that is settled.

## Measured

**Turn shape** (349 completed turns over 96.1 h, from `task_session_turns`): median **11 s**,
mean **124 s**, p90 **316 s**, max **75 min**. Heavily skewed — most turns are trivially short,
a few are very long.

**The board is mostly idle.** Sweeping the same turn history: **90.6 %** of wall-clock has zero
turns running, 7.1 % has one, 2.3 % has two or more. Duty cycle **12.5 %** (12.0 busy hours of
96.1). Max observed concurrency was 5 — unpaced work (Routines, UI, interactive sessions) already
runs in parallel outside the gate.

**Burn per stream ≈ 1× steady, order of magnitude only.** From a 10-second usage probe correlated
against turn-seconds actually in flight: consumption `0.33 → 0.37` while ~700 turn-seconds ran, so
one continuously-running stream would spend the 5-hour window in roughly 5 hours. Dividing by
turn-seconds rather than wall-clock matters: at a 12.5 % duty cycle the wall-clock figure
understates one stream's rate eightfold and would make concurrency look far safer than it is.

That estimate is not stable. Across the probe it read 1.22, then 1.07, then 0.89 as busy time
accumulated with no further consumption step — the single step in the sample means the answer
depends on where the sample is cut. `--measure` now warns below three steps. Treat the value as
"about 1", not as 1.1.

**Usage feedback is coarse and stepped.** Over 5.6 minutes the 5-hour number moved exactly once,
by 4 points at +204 s. It is quantized to whole percent and reported in lumps, so there is no
sub-tick feedback: any concurrency rule must assume at least a full tick of blindness.

**The queue is empty.** Right now, using the pacer's own `is_launchable`: **0 launchable tasks**,
1 running, 8 of 10 paced tasks sitting in Done and 1 parked in Review.

## Simulated

`sim_concurrency.py` runs the real gate (`worst_headroom`, `gate_open`, `ceiling_breached`) over a
5-hour window, resampling turn durations from the 349 real turns, with an unlimited backlog.
Turns completed / final consumption, at `dead_time=300 s`:

| burn | N=1 | N=2 | N=4 | N=8 |
|-----:|:----|:----|:----|:----|
| 0.5  | 84 · c=0.30 | 131 · c=0.46 | 150 · c=0.52 | 150 · c=0.52 |
| 1.0  | 84 · c=0.59 | 124 · c=0.89 | 132 · c=0.92 | 132 · c=0.92 |
| 2.0  | 74 · c=1.00 ❗ | 75 · c=1.01 ❗ | 76 · c=1.03 ❗ | 76 · c=1.03 ❗ |

❗ = peak consumption passed 1.0, i.e. a hard lockout mid-turn.

At the measured burn (~1) a second slot is worth **+47 % turns** and lands the window at 0.89
instead of 0.59 — spending quota that was otherwise wasted, without overshoot. A fourth slot adds
6 % more; an eighth adds nothing. At burn ≥ 2 the gate binds and every cap performs identically,
so parallelism buys literally nothing while still widening the overshoot.

**The saturation at 150 is not a concurrency limit — it is the tick rate.** One launch per tick
caps a 5-hour window at `WINDOW/INTERVAL` = 150 turns. That makes tick rate a competing lever:

| burn | N=1/120s | N=2/120s | N=4/120s | N=1/30s | N=1/15s |
|-----:|---------:|---------:|---------:|--------:|--------:|
| 0.5  | 84 | 131 | 150 | 118 | 128 |
| 1.0  | 84 | 124 | 132 | 118 | 128 |

`N=1` at a 15-second tick matches `N=2` at 120 s — while staying strictly serial. That matters
because serial is not merely equivalent here, it is better on two axes the table does not show:
it does not multiply dead-time overshoot (which scales as `N × burn × dead_time`), and it keeps
scheduling depth-first, which `docs/pacing-designs.md` argues for on prompt-cache grounds — round
-robining across tasks pushes every task's between-turn gap past the cache TTL and forces
full-history re-reads. Concurrency is inherently breadth-first. The same document already
identified the real fix: *"react to turn-completed events instead of a slow polling tick, so an
open gate releases the next turn within seconds."*

## Conclusions

1. **The premise is sound but currently unexercised.** With 0 launchable tasks and 8 of 10 in
   Done, raising the cap today would change nothing. The cap only binds when two or more tasks
   are simultaneously launchable, which the board's history suggests is rare. **The binding
   constraint is queue depth, not the cap.**
2. **If the queue were deep, a second slot would pay** at the measured burn: +47 % turns, no
   overshoot, window ending at 0.89 instead of 0.59. Beyond `N=2` returns fall off fast.
3. **Tick rate is the better lever for the same gain.** It reaches the same throughput, does not
   amplify overshoot, and preserves cache locality. Prefer shortening `PACER_INTERVAL` — or
   event-driven launch on turn completion — before raising the cap.
4. **Sizing must use measured `r`, not an assumed one.** The transient form is
   `N ≤ (α + h·D/T) / r` — headroom `h` converted to seconds via the window duration `D`, over a
   reaction time `T`, divided by the measured burn. Assuming `r ≈ α` (as an uncalibrated version
   of this formula does) overstates `N` by exactly the factor `r`. Take the minimum over windows
   of `slots_w`, never `slots(min h)`: `h = 0.1` is 30 minutes of slack on the 5-hour window and
   16.8 hours on the 7-day.
5. **Risk is asymmetric, and the backstop does not cover it.** Under-parallel wastes quota,
   which is recoverable. Over-parallel crosses the line with turns that cannot be stopped —
   preemption is out of scope — and the simulation shows peak consumption passing 1.0 at burn ≥ 2
   *even at N=1*, because running turns keep burning after the gate shuts. Concurrency widens an
   exposure that already exists.
6. **Attribution limits the whole idea.** The gate reads *account-level* usage, so Routines, UI
   work, and interactive sessions all count. At `exogenous=50 %` the simulation locks out at every
   cap for burn ≥ 1. Paced work is a minority of burn on this machine, so the leverage of any
   change to the paced queue is correspondingly smaller than the headroom number suggests.

## If it is implemented anyway

- Compute `slots` from measured `r` per §4, take the min over windows, cap it with
  `PACER_CONCURRENCY` (which stays a hard ceiling — N worktrees and N agents may exhaust the
  machine well below the quota-implied N), and force 1 near `PACER_BACKSTOP`. `c` high and `h`
  positive can coexist.
- Keep one launch per tick. With a dynamic cap it becomes additive-increase / multiplicative-
  decrease: headroom is re-checked between every launch and the cap collapses when the gate shuts.
- Hysteresis on the 0→1 boundary only. Flapping between 3 and 2 is harmless, since the cap gates
  new launches and never kills running turns.
- Setting `PACER_CONCURRENCY=1` must keep reproducing today's behaviour exactly.

## What would change the answer

The JSONL log (see README § Logging) is what settles it. Three questions, three fields:

- **Is quota going unspent?** `window_reset.final_c` per window, and the frequency of
  `gate=open, action=nothing, launchable>0`.
- **Is the queue ever deep?** `tick.launchable` — the distribution matters far more than the mean.
  If it rarely exceeds 1, conclusion 1 stands and nothing else is worth doing.
- **What is `r`, and is it stable?** `--measure` over a longer log. The estimate here rests on a
  single 4-point step over 5.6 minutes and should not be trusted past its first significant digit;
  the gap between the measured ~1 and the 2.0 at which every cap starts locking out is not
  comfortable.
