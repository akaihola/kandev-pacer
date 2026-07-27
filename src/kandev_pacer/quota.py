"""Quota math, usage parsing, and structured observation records."""

from datetime import datetime

from .config import PROVIDER, WINDOW_DURATIONS


def clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def elapsed_frac(now, resets_at, duration_s):
    return clamp01(1 - (resets_at - now) / duration_s)


def headroom(consumed_frac, elapsed_frac_, alpha=1.0):
    return alpha * elapsed_frac_ - consumed_frac


def worst_headroom(windows, now, alpha=1.0):
    if not windows:
        return 1.0
    return min(
        headroom(
            window["consumed_frac"],
            elapsed_frac(now, window["resets_at"], window["duration_s"]),
            alpha,
        )
        for window in windows
    )


def gate_open(worst_h, was_open, resume_margin=0.0):
    return worst_h >= 0 if was_open else worst_h >= resume_margin


def ceiling_breached(windows, backstop):
    return backstop < 1.0 and any(
        window["consumed_frac"] > backstop for window in windows
    )


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


def sessions_snapshot(task_sessions):
    return {
        task_id: {session["id"]: session.get("state") for session in sessions}
        for task_id, sessions in task_sessions.items()
    }


def diff_sessions(prev, cur, now):
    if prev is None:
        return []
    events = []
    for task_id, sessions in sorted(cur.items()):
        old = prev.get(task_id, {})
        for session_id, state in sorted(sessions.items()):
            if old.get(session_id) != state:
                events.append(
                    {
                        "t": round(now, 3),
                        "kind": "session",
                        "task": task_id,
                        "session": session_id,
                        "from": old.get(session_id),
                        "to": state,
                    }
                )
    return events


def detect_resets(prev_windows, cur_windows, now):
    previous = {window.get("label"): window for window in prev_windows}
    events = []
    for window in cur_windows:
        old = previous.get(window.get("label"))
        if old and window["resets_at"] > old["resets_at"]:
            events.append(
                {
                    "t": round(now, 3),
                    "kind": "window_reset",
                    "label": window.get("label"),
                    "final_c": round(old["consumed_frac"], 4),
                    "d": window["duration_s"],
                }
            )
    return events


def _epoch(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


def parse_windows(usage, provider=None):
    provider = PROVIDER if provider is None else provider
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
