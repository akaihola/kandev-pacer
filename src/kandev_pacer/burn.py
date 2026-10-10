"""How fast the agents can empty a weekly allowance, measured from quota readings.

The reset rules in `docs/reset-pacing-optimality.md` need a **burn time** per provider:
the shortest wall-clock time the agents take to use one full weekly allowance. The
providers report percentages, not tokens, so the only way to learn it is to watch the
weekly bar move. This module keeps the readings the pacer already makes every tick and
remembers the fastest sustained rise it has seen.

What the real log (`PACER_LOG`, September to October 2026) showed, and why the method
is what it is:

- The five-hour window is rarely full. Claude's five-hour reading peaked around 30% on
  most days, and its `resets_at` moves with the first request of each session. "How
  much the weekly bar moves while one five-hour window fills" would almost never get a
  sample. Codex reports no five-hour window at all. So the estimate is the fastest
  weekly rise over any span of at least one five-hour window, not a per-session ratio.
- Readings are whole percent, so a rise is only trusted once it is several points.
- A reading can be wrong. The weekly bar jumped from 0% to 100% in one tick after a
  credential expiry and sat there for a day, then jumped back. A rate measured across
  that would say the tank empties in an hour. A jump bigger than `MAX_STEP` between two
  consecutive readings is not agents working, so it ends the run being measured and a
  new one starts. A drop does the same: that is a rollover or a redemption.
- A gap in readings longer than `MAX_GAP` also ends the run, because nobody saw what
  happened in between. A pacer that was down for a day cannot claim the rise over it.

The number is a lower bound on how fast the agents *can* go: the pacer itself slows
them down, and the backlog may run dry. It is the fastest the operator has seen, which
is what the redemption rules need. Without enough history -- a fresh install, or a
quiet month -- `estimate()` returns None and callers keep their old behaviour.
"""

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .quota import format_wait

log = logging.getLogger("kandev-pacer")

WEEK = 7 * 86400
# A rise has to span at least one five-hour window: that limit is what sets the pace, and
# a shorter span measures one burst of launches, not a sustained rate.
MIN_SPAN = 5 * 3600
# Readings are whole percent. Five points is clearly movement, one point is rounding.
MIN_RISE = 0.05
# The weekly bar cannot really move this much between two readings 30 seconds apart.
MAX_STEP = 0.05
# Unseen time. Beyond this the run restarts rather than claiming the rise across it.
MAX_GAP = 3600
# How long an observed rise stays the answer. Four weeks covers a slow month.
TRAILING = 28 * 86400
# Unchanged readings are thinned to one per this many seconds, so a week of readings
# every 30 seconds stays a few hundred points instead of twenty thousand.
KEEP_EVERY = 600


def _empty():
    return {"last": None, "run": [], "run_best": None, "rises": []}


class BurnMeter:
    """Fastest weekly rise per provider, kept across restarts in one JSON file."""

    def __init__(self, path=None):
        self.path = Path(path).expanduser() if path else None
        self.state = {}
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                self.state = {
                    name: {**_empty(), **entry} for name, entry in loaded.items()
                }
            except (OSError, ValueError, AttributeError, TypeError) as error:
                log.warning("burn: ignoring unreadable %s: %s", self.path, error)
                self.state = {}

    def observe(self, provider, windows, now):
        """Record this tick's shared weekly reading. True when the estimate changed."""
        weekly = [w for w in windows if w["duration_s"] == WEEK and not w.get("scope")]
        if not weekly:
            return False
        reading = min(1.0, max(0.0, float(weekly[0]["consumed_frac"])))
        # Kept points are stored to the millisecond; measure from the same precision, or a
        # span of exactly MIN_SPAN can read as a hair short of it.
        now = round(now, 3)
        before = self.estimate(provider, now)
        entry = self.state.setdefault(provider, _empty())
        last = entry["last"]
        # Rounded so 0.75 - 0.70 does not land a hair above MAX_STEP in floating point.
        continues = (
            last is not None
            and now - last[0] <= MAX_GAP
            and -0.001 <= round(reading - last[1], 6) <= MAX_STEP
        )
        if continues:
            best = entry["run_best"]
            for t0, c0 in entry["run"]:
                span = now - t0
                if span < MIN_SPAN:
                    break
                rise = reading - c0
                if rise >= MIN_RISE and (best is None or rise / span > best["rate"]):
                    best = {
                        "rate": rise / span,
                        "rise": round(rise, 4),
                        "span": round(span),
                        "from": round(t0),
                        "to": round(now),
                    }
            entry["run_best"] = best
            kept = entry["run"][-1] if entry["run"] else None
            if kept is None or reading != kept[1] or now - kept[0] >= KEEP_EVERY:
                entry["run"].append([round(now, 3), reading])
        else:
            if entry["run_best"]:
                entry["rises"].append(entry["run_best"])
            entry["run"], entry["run_best"] = [[round(now, 3), reading]], None
        entry["last"] = [round(now, 3), reading]
        floor = now - TRAILING
        entry["rises"] = [r for r in entry["rises"] if r["to"] >= floor]
        entry["run"] = [p for p in entry["run"] if p[0] >= floor]
        self._save()
        return self.estimate(provider, now) != before

    def estimate(self, provider, now):
        """`{"s", "rise", "span", "from", "to"}` for the fastest rise seen, or None.

        `s` is the burn time in seconds: how long one full weekly allowance lasts at the
        fastest rate observed. `rise` over `span` seconds, ending at `to`, is the reading
        that produced it, so an operator can check it against the provider's usage page.
        """
        entry = self.state.get(provider)
        if not entry:
            return None
        candidates = [
            r
            for r in [*entry["rises"], entry["run_best"]]
            if r and r["to"] >= now - TRAILING
        ]
        if not candidates:
            return None
        best = max(candidates, key=lambda r: r["rate"])
        return {
            "s": round(best["span"] / best["rise"]),
            "rise": best["rise"],
            "span": best["span"],
            "from": best["from"],
            "to": best["to"],
        }

    def seconds(self, provider, now):
        """The burn time in seconds, or None: the shape `reset_plan` takes."""
        estimate = self.estimate(provider, now)
        return None if estimate is None else estimate["s"]

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as output:
                    json.dump(self.state, output)
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        except OSError as error:
            log.warning("burn: could not write %s: %s", self.path, error)


def burn_note(estimate):
    """`~8h 02m per weekly allowance (rose 63% in 5h 02m to 04 15:03)`, or nothing."""
    if estimate is None:
        return ""
    ended = datetime.fromtimestamp(estimate["to"]).strftime("%d %H:%M")
    return (
        f"~{format_wait(estimate['s'])} per weekly allowance "
        f"(rose {estimate['rise'] * 100:.0f}% in {format_wait(estimate['span'])} "
        f"to {ended})"
    )
