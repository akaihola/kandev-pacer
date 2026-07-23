#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Kandev quota pacer: admit low-priority backlog work only while under the steady-burn curve.

Enforcement = admission control. A "Paced" workflow holds new/low-priority tasks in a
Backlog step (no auto_start_agent). This pacer moves one task at a time into the active
step (which auto-starts the agent) only while quota consumption stays on/under pace.
Routine/UI work runs in other workflows and is never touched — it just raises `consumed`,
which the account-level gate reads, so low-priority work yields to it automatically.

See README.md for the derivation and the confirmed kandev source behavior.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

# --- config (env-overridable; defaults are the live "Paced Kanban" ids on gogo) ---
URL = os.environ.get("KANDEV_URL", "http://127.0.0.1:38429").rstrip("/")
PACED_WORKFLOW_ID = os.environ.get("PACED_WORKFLOW_ID", "1a47b559-2178-4f81-9ea7-c518e97a2a45")
BACKLOG_STEP_ID = os.environ.get("BACKLOG_STEP_ID", "71597d9d-89cc-436c-9cf0-33cc19e14d02")
ACTIVE_STEP_ID = os.environ.get("ACTIVE_STEP_ID", "33c75cb3-2b55-4feb-b9fb-84f8d48b2af8")
CONCURRENCY = int(os.environ.get("PACER_CONCURRENCY", "1"))   # max tasks in the active step
ALPHA = float(os.environ.get("PACER_ALPHA", "1.0"))          # reserve slope; <1 finishes windows with quota to spare
RESUME_MARGIN = float(os.environ.get("PACER_RESUME_MARGIN", "0.0"))  # hysteresis; reopen only when headroom >= this
INTERVAL = int(os.environ.get("PACER_INTERVAL", "120"))      # seconds between ticks

# Known Anthropic subscription window durations, seconds. Keyed by the label the usage
# endpoint reports. TODO: confirm keys against a live `--dump-usage`.
WINDOW_DURATIONS = {"5h": 5 * 3600, "7d": 7 * 24 * 3600}

# --- pure pacing math (unit-tested in test_pace.py) ---

def clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x

def elapsed_frac(now, resets_at, duration_s):
    """Fraction of the window elapsed, derived from reset time (not a start time)."""
    return clamp01(1 - (resets_at - now) / duration_s)

def headroom(consumed_frac, elapsed_frac_, alpha=1.0):
    """Time-ahead-of-spend, window-fraction units. <0 means over the steady line.

    Steady burn-down traces consumed==elapsed. alpha<1 targets a steeper line to keep a reserve.
    """
    return alpha * elapsed_frac_ - consumed_frac

def worst_headroom(windows, now, alpha=1.0):
    """min headroom across all active windows. windows: [{consumed_frac, resets_at, duration_s}]."""
    if not windows:
        return 1.0  # no active window -> nothing consumed -> fully open
    return min(headroom(w["consumed_frac"], elapsed_frac(now, w["resets_at"], w["duration_s"]), alpha)
               for w in windows)

def gate_open(worst_h, was_open, resume_margin=0.0):
    """Hysteresis: close the moment we cross the line; reopen only past the margin.

    Self-heals: while paused, consumed is frozen and elapsed keeps rising, so headroom
    climbs back to 0 on its own — no external release needed.
    """
    return worst_h >= 0 if was_open else worst_h >= resume_margin

# --- kandev glue ---

def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(URL + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=15) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None

def _epoch(ts):
    """RFC3339 -> epoch seconds. Accepts trailing 'Z'."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()

def parse_windows(usage, now):
    """Map the /agents/usage payload to [{consumed_frac, resets_at, duration_s}].

    TODO: shape unconfirmed. Written against the documented Anthropic `limits[]` shape
    (each entry: percent utilized + resets_at). Run `pacer.py --dump-usage` to capture a
    live response, then pin this. Entries without a known duration or resets_at are skipped.
    """
    out = []
    for lim in usage.get("limits", []):
        dur = WINDOW_DURATIONS.get(lim.get("window") or lim.get("name"))
        ra = lim.get("resets_at")
        if dur is None or not ra:
            continue
        out.append({"consumed_frac": lim["percent"] / 100.0,
                    "resets_at": _epoch(ra), "duration_s": dur})
    return out

def paced_tasks():
    """Tasks in the paced workflow. Returns the raw list; caller filters by step."""
    return _req("GET", f"/api/v1/workflows/{PACED_WORKFLOW_ID}/tasks") or []

def _step_of(task):
    return task.get("workflow_step_id", "")  # TODO: confirm field name via --dump-usage sibling

def move_to_active(task_id):
    _req("POST", f"/api/v1/tasks/{task_id}/move",
         {"workflow_id": PACED_WORKFLOW_ID, "workflow_step_id": ACTIVE_STEP_ID, "position": 0})

def tick(was_open):
    usage = _req("GET", "/api/v1/agents/usage")
    now = time.time()
    wh = worst_headroom(parse_windows(usage, now), now, ALPHA)
    is_open = gate_open(wh, was_open, RESUME_MARGIN)
    if is_open:
        tasks = paced_tasks()
        active = sum(1 for t in tasks if _step_of(t) == ACTIVE_STEP_ID)
        backlog = sorted((t for t in tasks if _step_of(t) == BACKLOG_STEP_ID),
                         key=lambda t: t.get("created_at", ""))
        if active < CONCURRENCY and backlog:
            move_to_active(backlog[0]["id"])
            print(f"headroom={wh:+.3f} open -> started {backlog[0]['id']} "
                  f"(active {active+1}/{CONCURRENCY}, {len(backlog)-1} queued)", flush=True)
            return is_open
    print(f"headroom={wh:+.3f} {'open' if is_open else 'PAUSED'}", flush=True)
    return is_open

def main():
    if "--dump-usage" in sys.argv:
        print(json.dumps(_req("GET", "/api/v1/agents/usage"), indent=2))
        return
    print(f"pacing {URL} workflow={PACED_WORKFLOW_ID} every {INTERVAL}s "
          f"(alpha={ALPHA}, concurrency={CONCURRENCY})", flush=True)
    was_open = True
    while True:
        try:
            was_open = tick(was_open)
        except Exception as e:  # keep pacing across transient API errors; fail closed for this tick
            print(f"tick error: {e} -> pausing this tick", file=sys.stderr, flush=True)
            was_open = False
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
