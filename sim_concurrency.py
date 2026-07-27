#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Simulate the gate against N-concurrency policies, using the pacer's own pacing math.

Answers the question raising PACER_CONCURRENCY is supposed to answer -- how many extra
turns does a higher cap actually buy, and at what overshoot risk -- instead of arguing it
from intuition. Turn durations are resampled from the real completed turns in kandev.db,
so the duty cycle the launch policy achieves (one launch per tick) falls out of the
simulation rather than being assumed.

`burn` is the one parameter that has to come from measurement: it is one running turn's
consumption rate expressed as a multiple of the steady line, so burn=1.0 means a single
continuously-running stream would consume exactly the whole window. Measure it with
--measure, which correlates the JSONL log's consumption steps against the turn-seconds
that were actually in flight. Everything else here is derived from the repo's own code.

    ./sim_concurrency.py                 # policy sweep
    ./sim_concurrency.py --levers        # concurrency vs tick rate, side by side
    ./sim_concurrency.py --measure LOG   # estimate burn from a pacer.jsonl + kandev.db
"""
import json
import os
import random
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from pacer import ceiling_breached, gate_open, worst_headroom

WINDOW = 18000.0          # 5-hour window, seconds
INTERVAL = 120            # PACER_INTERVAL default
ALPHA = 1.0
BACKSTOP = 0.97
DB = Path(os.environ.get("KANDEV_DB", Path.home() / ".kandev" / "data" / "kandev.db"))

def turn_durations(db=DB):
    """Completed turn durations in seconds, from kandev's own history."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("SELECT (julianday(completed_at) - julianday(started_at)) * 86400"
                       " FROM task_session_turns"
                       " WHERE completed_at IS NOT NULL AND completed_at > started_at").fetchall()
    con.close()
    return [r[0] for r in rows]

def run(cap, burn, dead, exo, durations, interval=INTERVAL, seed=0, alpha=ALPHA):
    """One 5-hour window under one policy. -> (turns, final_c, peak_c, closed_fraction).

    dead: seconds before consumption becomes visible in the usage payload. The pacer is a
    bang-bang controller with dead time, so this is what decides whether a higher cap
    overshoots: N turns burn unobserved for `dead` seconds before the gate can react.
    exo:  quota fraction consumed over the window by work outside the gate (Routines, UI,
    interactive sessions). It is the account-level number the gate reads, so exogenous
    burn closes the gate on paced work without the pacer being able to attribute it.
    """
    rng = random.Random(seed)
    rate, exo_rate = burn / WINDOW, exo / WINDOW
    c, closed, turns, was_open = 0.0, 0, 0, True
    seen = [(0.0, 0.0)]                       # (t, c) history, for the lagged view
    running = []                              # remaining seconds per in-flight turn
    for t in range(int(WINDOW)):
        c += rate * len(running) + exo_rate
        seen.append((t, c))
        running = [d - 1 for d in running if d - 1 > 0]
        if t % interval:
            continue
        observed = next((v for ts, v in reversed(seen) if ts <= t - dead), 0.0)
        w = [{"consumed_frac": observed, "resets_at": WINDOW, "duration_s": WINDOW}]
        is_open = (not ceiling_breached(w, BACKSTOP)
                   and gate_open(worst_headroom(w, t, alpha), was_open))
        was_open = is_open
        if not is_open:
            closed += interval
            continue
        if len(running) < cap:                # one launch per tick, as the pacer does
            running.append(rng.choice(durations))
            turns += 1
    return turns, c, max(v for _, v in seen), closed / WINDOW

def avg(cap, burn, dead, exo, durations, interval=INTERVAL, runs=5):
    """Mean turns and final c over `runs` seeds, plus the worst peak c any seed reached."""
    r = [run(cap, burn, dead, exo, durations, interval, seed=s) for s in range(runs)]
    return (sum(x[0] for x in r) / runs, sum(x[1] for x in r) / runs, max(x[2] for x in r))

def sweep(durations):
    caps, burns = [1, 2, 4, 8], [0.25, 0.5, 1.0, 2.0, 4.0]
    for dead, exo in [(0, 0.0), (300, 0.0), (300, 0.5)]:
        print(f"\n  dead_time={dead}s  exogenous={exo:.0%}")
        print("  burn |" + "".join(f"    N={c}          " for c in caps))
        for b in burns:
            row = f"  {b:4.2f} |"
            for cap in caps:
                t, fc, pk = avg(cap, b, dead, exo, durations)
                row += f" {t:6.1f} turns c={fc:4.2f}{'!' if pk > 1.0 else ' '}"
            print(row)
    print("\n  ! = peak consumption passed 1.0 in some seed (hard lockout mid-turn)")

def levers(durations):
    """Raising the cap goes breadth-first; shortening the tick keeps depth-first.

    Both lift the same ceiling -- one launch per tick caps a 5-hour window at
    WINDOW/INTERVAL turns -- but only the cap multiplies dead-time overshoot, and only
    the cap breaks the prompt-cache locality argument in docs/pacing-designs.md.
    """
    combos = [(1, 120), (2, 120), (4, 120), (1, 30), (1, 15), (4, 30)]
    print("\n  raise the cap (breadth-first) vs shorten the tick (keeps depth-first)")
    print("  burn |" + "".join(f"  N={c}/{i}s" for c, i in combos))
    for b in (0.25, 0.5, 1.0, 2.0, 4.0):
        row = f"  {b:4.2f} |"
        for cap, iv in combos:
            t, _, pk = avg(cap, b, 300, 0.0, durations, interval=iv)
            row += f" {t:7.0f}{'!' if pk > 1.0 else ' '}"
        print(row)
    print(f"\n  turns per 5h window; the ceiling is {WINDOW / INTERVAL:.0f} at 120s, "
          f"{WINDOW / 30:.0f} at 30s. ! = peak c > 1.0")

def measure(log_path, db=DB):
    """Estimate `burn` from a real log: consumption per turn-second actually in flight.

    Needs the turn-seconds, not the wall-clock: the board is idle most of the time, so
    dividing a window's consumption by its elapsed time understates one stream's rate by
    the duty cycle and makes concurrency look far safer than it is.
    """
    samples = []
    for line in Path(log_path).read_text().splitlines():
        rec = json.loads(line)
        w = [x for x in rec.get("windows", []) if x.get("label") == "5-hour"]
        if w:
            samples.append((rec["t"], w[0]["c"], w[0]["d"]))
    if len(samples) < 2:
        sys.exit(f"{log_path}: need at least two 5-hour samples, found {len(samples)}")
    t0, t1 = samples[0][0], samples[-1][0]
    dc = samples[-1][1] - samples[0][1]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    busy = 0.0
    for a, b in con.execute("SELECT started_at, completed_at FROM task_session_turns"
                            " WHERE started_at IS NOT NULL"):
        f = lambda s: datetime.fromisoformat(s.replace(" ", "T")).timestamp()  # noqa: E731
        s, e = f(a), (f(b) if b else t1)
        if e > t0 and s < t1:
            busy += min(e, t1) - max(s, t0)
    con.close()
    steps = [(t, c) for i, (t, c, _) in enumerate(samples)
             if i and c != samples[i - 1][1] for t, c in [(t, c)]]
    print(f"span      {(t1 - t0) / 60:.1f} min, {len(samples)} samples")
    print(f"consumed  {samples[0][1]:.2f} -> {samples[-1][1]:.2f}  (delta {dc:+.2f})")
    print(f"in flight {busy:.0f} turn-seconds ({100 * busy / (t1 - t0):.0f}% of wall clock)")
    print(f"steps     {len(steps)} change(s): "
          + ", ".join(f"+{t - t0:.0f}s->{c:.2f}" for t, c in steps[:8]))
    if busy <= 0:
        sys.exit("no turns ran during the sample; burn is unmeasurable from this log")
    burn = dc / (busy / WINDOW)
    print(f"\nburn = {burn:.2f} x steady   "
          f"(one stream would spend the window in {WINDOW / 60 / burn:.0f} min)")
    if len(steps) < 3:
        # utilization_pct is whole-percent and arrives in lumps, so a sample spanning one
        # step is dominated by where it was cut: busy time keeps accruing while c sits
        # still, and the estimate slides down until the next step lands. Seen live drifting
        # 1.22 -> 1.07 -> 0.89 over four minutes on one step.
        print(f"  WARNING: only {len(steps)} consumption step(s) in this sample. The estimate "
              "is\n  quantization-dominated and drifts with sample length -- widen the log "
              "until\n  it spans several steps before sizing anything on it.")

if __name__ == "__main__":
    if "--measure" in sys.argv:
        measure(sys.argv[sys.argv.index("--measure") + 1])
    else:
        d = turn_durations()
        d.sort()
        print(f"{len(d)} real turns: median {d[len(d) // 2]:.0f}s, "
              f"mean {sum(d) / len(d):.0f}s, p90 {d[int(0.9 * len(d))]:.0f}s")
        levers(d) if "--levers" in sys.argv else sweep(d)
