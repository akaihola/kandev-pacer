"""Quota math, usage parsing, and structured observation records."""

from dataclasses import dataclass
from datetime import datetime

from .config import WINDOW_DURATIONS, settings


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
    # The hard ceiling first: it overrides the burn line, however much headroom says.
    if config.backstop < 1.0 and any(w["consumed_frac"] > config.backstop for w in windows):
        return Verdict("ceiling", worst, (
            f"headroom={worst:+.3f} CEILING (consumed>{config.backstop:.2f}) "
            "-> withholding"
        ))
    if worst < 0:
        return Verdict("paused", worst, f"headroom={worst:+.3f} PAUSED")
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
    now, gate, action, window_recs, running, launchable, cap, booked, launched=None
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
            duration = WINDOW_DURATIONS.get(window.get("label"))
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
