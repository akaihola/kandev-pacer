"""Quota math, usage parsing, and structured observation records."""

from dataclasses import dataclass
from datetime import datetime

from .config import MAX_INTERVAL, MIN_INTERVAL, settings, window_duration


def elapsed_frac(now, resets_at, duration_s):
    """How far through its window we are, 0..1."""
    fraction = 1 - (resets_at - now) / duration_s
    return min(max(fraction, 0.0), 1.0)


def headroom(consumed_frac, elapsed_frac_, alpha=1.0):
    """How far ahead of the burn line this window is. Negative means overspending."""
    return alpha * elapsed_frac_ - consumed_frac


def worst_headroom(windows, now, alpha=1.0):
    """The tightest window's headroom -- the one that decides the gate."""
    return min(
        (
            headroom(
                window["consumed_frac"],
                elapsed_frac(now, window["resets_at"], window["duration_s"]),
                alpha,
            )
            for window in windows
        ),
        default=1.0,
    )


def window_resume_at(window, now, alpha=1.0):
    """When this window's headroom reaches 0, assuming nothing more is consumed.

    While withheld, `c` is frozen and `e` keeps rising, so the release is the moment
    `alpha*e == c` -- i.e. `e == c/alpha`, which inverts to a wall-clock instant through
    `e = 1 - (resets_at - t)/d`. A window needing `e > 1` cannot heal before it rolls, so
    its answer is the roll itself, where `c` drops to 0 and `h` with it.
    """
    needed_e = window["consumed_frac"] / alpha
    duration, resets_at = window["duration_s"], window["resets_at"]
    if needed_e > 1:
        return resets_at
    return max(now, resets_at - duration * (1 - needed_e))


def resume_at(windows, now, alpha=1.0):
    """When the *gate* reopens: the last window to heal is the one holding it shut."""
    return max((window_resume_at(w, now, alpha) for w in windows), default=None)


def format_wait(seconds):
    """`4h 07m 12s` -- hours dropped when zero, so short waits stay readable."""
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}h {minutes:02d}m {seconds:02d}s" if hours else f"{minutes}m {seconds:02d}s"


def resume_note(eta, now):
    """The clause a withholding tick ends on, or nothing when there is no estimate."""
    if eta is None:
        return ""
    return (
        f", resumes in ~{format_wait(eta - now)} "
        f"(~{datetime.fromtimestamp(eta):%H:%M})"
    )


def poll_interval(verdict, now, config):
    """How long to sleep before the next tick.

    Negative headroom is the only thing worth converging on, and there the sleep is half
    the estimated wait, clipped to `MIN_INTERVAL`..`MAX_INTERVAL`. Halving converges from
    above: a two-hour wait costs the ceiling's worth of polling, every tick from there
    halves what is left, and the gate is met at most `MIN_INTERVAL` late.

    That band is fixed rather than bounded by `interval`, because the two answer different
    questions. `interval` is how eagerly to look for work that could start *now*, and is
    worth setting to seconds; how often to re-check a wait whose end is already computed
    is not, and polling a two-hour pause at that rate is pure waste. So a low
    PACER_INTERVAL buys a responsive gate without buying a busy one, and the wait paces
    itself either way -- slower than the configured tick, or faster than it.

    At `h >= 0` the configured interval always stands: nothing to climb back to, and it is
    the pacing rate rather than a poll ceiling. That covers an open gate, a blind one, and
    the rare ceiling that binds while `h` is already back above the line.
    """
    if verdict.headroom >= 0 or verdict.resumes_at is None:
        return config.interval
    return min(max(0.5 * (verdict.resumes_at - now), MIN_INTERVAL), MAX_INTERVAL)


@dataclass(frozen=True)
class Verdict:
    """What the quota says about launching right now.

    `gate` is the reason, not just a yes/no, because the log and the operator both
    need to tell "we are ahead of the curve" apart from "we cannot see the quota at
    all" -- the second is a misconfiguration that looks identical if you only record
    that nothing launched.
    """

    gate: str        # open | paused | ceiling | blind
    headroom: float
    message: str     # the line to print; empty when the gate is open
    # When this gate is expected to reopen, or None when there is nothing to wait for:
    # an open gate, or a blind one, whose reopening no arithmetic can predict. The
    # instant rather than the ready-made clause, because the loop paces itself by it as
    # well as printing it -- `resume_note` renders it, and the caller tacks that on,
    # since only the caller knows what else belongs on the line first.
    resumes_at: float | None = None

    @property
    def is_open(self):
        return self.gate == "open"


def quota_verdict(usage, windows, now, config):
    """Read the usage payload against the burn line. Pure: no I/O, no state."""
    if not windows and provider_missing(usage, config.provider):
        return Verdict("blind", 1.0, (
            f"provider {config.provider!r} not in usage "
            f"{sorted(usage_providers(usage))} -> WITHHOLDING (check PACER_PROVIDER)"
        ))
    worst = worst_headroom(windows, now, config.alpha)
    # Withholding with no end in sight reads as an outage. It is not one: the release is
    # arithmetic. An estimate all the same -- a turn still running keeps spending `c`.
    healed = resume_at(windows, now, config.alpha)
    # The hard ceiling first: it overrides the burn line, however much headroom says.
    over = [w for w in windows if w["consumed_frac"] > config.backstop] \
        if config.backstop < 1.0 else []
    if over:
        # A ceiling lifts only when the breaching window rolls -- it reads `c`, which does
        # not fall before then, whatever `e` does. The burn line still has to clear
        # afterwards, so the release is the later of the two.
        rolls = max(w["resets_at"] for w in over)
        return Verdict("ceiling", worst, (
            f"headroom={worst:+.3f} CEILING (consumed>{config.backstop:.2f}) "
            "-> withholding"
        ), max(rolls, healed))
    if worst < 0:
        return Verdict("paused", worst, f"headroom={worst:+.3f} PAUSED", healed)
    return Verdict("open", worst, "")


def window_records(windows, now, alpha=1.0):
    records = []
    for window in windows:
        elapsed = elapsed_frac(now, window["resets_at"], window["duration_s"])
        records.append(
            {
                "label": window.get("label"),
                "c": round(window["consumed_frac"], 4),
                "e": round(elapsed, 4),
                "h": round(headroom(window["consumed_frac"], elapsed, alpha), 4),
                "d": window["duration_s"],
                "resets_at": window["resets_at"],
            }
        )
    return records


def binding_label(window_recs):
    return min(window_recs, key=lambda window: window["h"])["label"] if window_recs else None


def tick_record(
    now, gate, action, window_recs, running, launchable, cap, booked, launched=None,
    interval=None,
):
    return {
        "t": round(now, 3),
        "kind": "tick",
        "gate": gate,
        "action": action,
        "wh": min((window["h"] for window in window_recs), default=1.0),
        "bind": binding_label(window_recs),
        "windows": window_recs,
        "running": running,
        "launchable": launchable,
        "cap": cap,
        "booked": booked,
        "launched": launched,
        # How long until the next tick. Without it the gaps between records read as
        # stalls, now that the interval moves with the estimated wait.
        "interval": interval,
    }


def _epoch(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


def parse_windows(usage):
    """The quota windows for the configured provider, as fractions and epoch seconds."""
    provider = settings.provider
    windows = []
    for agent in usage.get("agents", []):
        agent_usage = agent.get("usage") or {}
        if agent_usage.get("provider") != provider:
            continue
        for window in agent_usage.get("windows", []):
            duration = window_duration(window.get("label"))
            reset_at = window.get("reset_at")
            if duration is None or not reset_at:
                continue
            windows.append(
                {
                    "label": window.get("label"),
                    "consumed_frac": window["utilization_pct"] / 100.0,
                    "resets_at": _epoch(reset_at),
                    "duration_s": duration,
                }
            )
    return windows


def usage_providers(usage):
    return {
        (agent.get("usage") or {}).get("provider")
        for agent in usage.get("agents", [])
    } - {None}


def provider_missing(usage, provider):
    providers = usage_providers(usage)
    return bool(providers) and provider not in providers
