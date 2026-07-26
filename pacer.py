#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["websocket-client"]
# ///
"""Kandev quota pacer (Design 13: hold in place, launch on release).

Paces complicated, multi-step, branching/looping workflows against a Claude
subscription quota. Kandev owns routing; the pacer owns timing. Work steps have
auto-start disabled, so a finished turn lands the task on its correct next step
(any branch/loop) and waits there `WAITING_FOR_INPUT`. While under the steady-burn
curve, the pacer starts the next turn via the WebSocket `session.launch` primitive
-- in place for a task already at a work step, or by admitting one from the parked
queue (the start step) with a single call that both moves and launches it.

See docs/pacing-designs.md (Design 13) and README.md for the full derivation and
the confirmed kandev source behavior.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

# --- config (env-overridable; defaults are the live "Paced Kanban" on gogo) ---
URL = os.environ.get("KANDEV_URL", "http://127.0.0.1:38429").rstrip("/")
PACED_WORKFLOW_ID = os.environ.get("PACED_WORKFLOW_ID", "1a47b559-2178-4f81-9ea7-c518e97a2a45")
PROVIDER = os.environ.get("PACER_PROVIDER", "anthropic")     # usage agent to gate on
CONCURRENCY = int(os.environ.get("PACER_CONCURRENCY", "1"))  # max in-flight paced turns
ALPHA = float(os.environ.get("PACER_ALPHA", "1.0"))          # reserve slope; <1 keeps quota to spare
RESUME_MARGIN = float(os.environ.get("PACER_RESUME_MARGIN", "0.0"))  # hysteresis reopen threshold
BACKSTOP = float(os.environ.get("PACER_BACKSTOP", "0.97"))   # absolute cutoff; >=1.0 disables
INTERVAL = int(os.environ.get("PACER_INTERVAL", "120"))      # seconds between ticks
# How long a never-run admission booking (session not yet visible) is held before it may
# retry -- long enough to outlast the lag before a just-started session shows up in the API
# (Kandev starts it in a background goroutine, detached from our launch call), short enough
# that a launch that silently failed still gets another shot instead of wedging forever.
BOOKING_TTL = 3 * INTERVAL
ADMISSION_OVERRIDE = os.environ.get("PACER_ADMISSION_STEP_ID") or None
WS_URL = os.environ.get("KANDEV_WS_URL") or (
    URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws")

# Anthropic subscription window durations (seconds), keyed by the usage label.
WINDOW_DURATIONS = {"5-hour": 5 * 3600, "7-day": 7 * 24 * 3600, "7-day (Fable)": 7 * 24 * 3600}

# Workflow step-event action kinds that move a task off its step.
TRANSITION_KINDS = {"move_to_next", "move_to_previous", "move_to_step"}
# Kandev's task priority ordering, lower first -- copied from the WIP-limit pull query
# (task/repository/sqlite/task.go:504). 'none' and any unknown value tie at the back.
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
# Session states that mean a turn is in flight (counts against concurrency).
RUNNING_STATES = {"STARTING", "RUNNING"}
# Session state that means the turn is done and the next may be launched.
READY_STATE = "WAITING_FOR_INPUT"
# Kandev prepares a session (workspace, worktree, branch) the moment a task is created and
# leaves it here -- provisioned, agent never started. Not running, and not a finished turn.
CREATED_STATE = "CREATED"
# States the pacer may start a turn from: prepared-but-never-run, or waiting after a turn.
IDLE_STATES = {CREATED_STATE, READY_STATE}

# --- pure pacing math (unit-tested in test_pace.py; unchanged) ---

def clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x

def elapsed_frac(now, resets_at, duration_s):
    """Fraction of the window elapsed, derived from reset time (not a start time)."""
    return clamp01(1 - (resets_at - now) / duration_s)

def headroom(consumed_frac, elapsed_frac_, alpha=1.0):
    """Time-ahead-of-spend, window-fraction units. <0 means over the steady line."""
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
    climbs back to 0 on its own -- no external release needed.
    """
    return worst_h >= 0 if was_open else worst_h >= resume_margin

def ceiling_breached(windows, backstop):
    """True if any window's consumed fraction is over the absolute cutoff. backstop>=1.0 disables."""
    if backstop >= 1.0:
        return False
    return any(w["consumed_frac"] > backstop for w in windows)

# --- usage parsing ---

def _epoch(ts):
    """RFC3339 -> epoch seconds. Accepts trailing 'Z' and numeric offsets."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()

def parse_windows(usage, provider=None):
    """Map /agents/usage to [{consumed_frac, resets_at, duration_s}] for one provider.

    Real shape: {agents:[{usage:{provider, windows:[{label, utilization_pct, reset_at}]}}]}.
    Windows with an unknown label or no reset time are skipped.
    """
    provider = PROVIDER if provider is None else provider
    out = []
    for agent in usage.get("agents", []):
        u = agent.get("usage") or {}
        if u.get("provider") != provider:
            continue
        for w in u.get("windows", []):
            dur = WINDOW_DURATIONS.get(w.get("label"))
            ra = w.get("reset_at")
            if dur is None or not ra:
                continue
            out.append({"consumed_frac": w["utilization_pct"] / 100.0,
                        "resets_at": _epoch(ra), "duration_s": dur})
    return out

def usage_providers(usage):
    """Provider names present in the /agents/usage payload."""
    return {(a.get("usage") or {}).get("provider") for a in usage.get("agents", [])} - {None}

def provider_missing(usage, provider):
    """True if the payload names some providers but not ours -- a likely PACER_PROVIDER typo.

    Guards a fail-OPEN hole: with no matching windows, worst_headroom is 1.0 (fully open) and
    the ceiling never trips, so a misconfigured provider would silently disable all pacing. An
    empty payload (no providers at all) returns False -- we can't tell misconfig from a fresh
    account there, so we don't withhold on it.
    """
    provs = usage_providers(usage)
    return bool(provs) and provider not in provs

# --- workflow step classification (pure) ---

def is_work_step(step):
    """A work step routes away on turn completion (any transition kind)."""
    ev = step.get("events") or {}
    return any(a.get("type") in TRANSITION_KINDS for a in (ev.get("on_turn_complete") or []))

def work_step_ids(steps):
    return {s["id"] for s in steps if is_work_step(s)}

def queue_step_id(steps):
    """The parked queue is the workflow's start step (where new/re-queued tasks wait)."""
    for s in steps:
        if s.get("is_start_step"):
            return s["id"]
    return None

def admission_target(steps, override=None):
    """The step to admit a queued task into: the lowest-position work step, or an override."""
    if override:
        return override
    works = [s for s in steps if is_work_step(s)]
    if not works:
        return None
    return min(works, key=lambda s: s.get("position", 0))["id"]

def step_ranks(steps):
    """step id -> board position (column order), for ordering tasks across columns."""
    return {s["id"]: s.get("position", 0) for s in steps}

def step_names(steps):
    """id -> display name, for logging. Steps without a name fall back to their id."""
    return {s["id"]: s.get("name") or s["id"] for s in steps}

def named(id_, names):
    """Human-readable label for a step/task id: its name, or the raw id when unknown.

    Falling back to the id matters -- a step that vanished from the workflow, or a task the
    pacer skipped mid-tick, still has to be identifiable in the log.
    """
    return names.get(id_, id_)

def _has_on_enter(step, kind):
    ev = step.get("events") or {}
    return any(a.get("type") == kind for a in (ev.get("on_enter") or []))

def lint_workflow(steps):
    """Return warning strings for config that would bypass the gate or misbehave. No mutation."""
    works = work_step_ids(steps)
    warns = []
    for s in steps:
        name = s.get("name", "?")
        stage = (s.get("stage_type") or "").lower()
        # These two auto-launch a turn on step ENTRY, before the pacer gets to act, so they
        # bypass the gate on ANY step -- a parked Review step leaks just as a work step does.
        if _has_on_enter(s, "auto_start_agent"):
            warns.append(f"step {name!r} still has on_enter:auto_start_agent (bypasses the gate)")
        if s.get("agent_profile_id") and (s.get("prompt") or "").strip():
            warns.append(f"step {name!r} has both an agent-profile override and a prompt (auto-launch bypass)")
        if s["id"] in works:
            if s.get("auto_advance_requires_signal"):
                warns.append(f"work step {name!r} gates routing on the agent step_complete signal")
            if stage in ("review", "approval"):
                warns.append(f"step {name!r} routes on on_turn_complete (classified work) but is tagged stage_type={stage!r}")
        elif stage == "work":
            warns.append(f"step {name!r} is tagged stage_type='work' but has no on_turn_complete transition (parked)")
    if not works:
        warns.append("no work step found (no on_turn_complete transition anywhere) -- nothing to pace")
    if admission_target(steps, ADMISSION_OVERRIDE) is None:
        warns.append("no admission target derivable")
    if queue_step_id(steps) is None:
        warns.append("no start step (parked queue) found")
    return warns

# --- launch ordering (pure) ---

def priority_rank(task):
    """Kandev's priority ordering, lower first. Anything unrecognized ranks last, with 'none'."""
    return PRIORITY_RANK.get((task.get("priority") or "").strip().lower(), PRIORITY_RANK["none"])

def task_order_key(task, ranks):
    """Sort key deciding which launchable task goes next: downstream column first, then pull order.

    `position` is the manual drag order from the board, but it is scoped to a *workflow step*,
    not to the workflow -- two tasks in different columns can both sit at position 0, so it is
    not a total order on its own. We therefore order by column first and only compare positions
    within one: descending step position, i.e. the rightmost (most downstream) column wins.

    That is kanban pull, and it is what Design 13 needs. A task already at a work step is a turn
    in flight; admitting a fresh one from the parked queue (the leftmost column) instead would
    grow WIP without finishing anything. Draining right-to-left also generalizes past the single
    work step of the live board: a multi-work-step workflow advances tasks already deep in the
    pipeline before pulling new ones in at the start.

    Within a column, this is kandev's own WIP-limit pull ordering (task.go:504) verbatim:
    position -> priority -> created_at -> id. The id tiebreak keeps the order total, so the same
    board state always picks the same task rather than flapping between two equal candidates.
    """
    step = task.get("workflow_step_id", "")
    return (-ranks.get(step, -1),                 # unknown/vanished step sorts last, not first
            task.get("position") or 0,            # missing or null position == top of the column
            priority_rank(task),
            task.get("created_at") or "",
            task.get("id") or "")

# --- launchability (pure) ---

def newest_session(sessions):
    """The most recently started session, or None.

    Sorts on `started_at` -- the sessions payload has no `created_at`. Keying on a field
    that isn't there makes every key equal and quietly returns the first session instead,
    which would hand a stale session_id to session.launch once a step spawns a second one.
    """
    if not sessions:
        return None
    return max(sessions, key=lambda s: s.get("started_at", ""))

def is_launchable(task, sessions, work_steps, queue_step_id_, booked):
    """Can the pacer start a turn for this task now?

    - at a work step: only when its newest session is idle -- WAITING_FOR_INPUT (in-place next
      turn) or CREATED (a fresh task dragged straight onto the step, prepared but never run).
    - at the parked queue (start step): a task with no session at all, or one whose newest
      session is idle -- CREATED for a new task, WAITING_FOR_INPUT for a re-queued one.
    Excludes anything already booked for this exact (task, step, session).

    CREATED counts as launchable because kandev prepares a session up front, so "never run"
    no longer means "no session". Treating it as unlaunchable is what left new tasks parked
    in the queue forever while the pacer reported "nothing to launch".
    """
    step = task.get("workflow_step_id", "")
    newest = newest_session(sessions)
    sid = newest["id"] if newest else None
    if (task["id"], step, sid) in booked:
        return False
    state = newest["state"] if newest else None
    if step in work_steps:
        return state in IDLE_STATES
    if step == queue_step_id_:
        return newest is None or state in IDLE_STATES
    return False

def launch_target(task, work_steps, queue_step_id_, admission_step):
    """Which step id to launch at: the current step in place, or the admission step from the queue."""
    step = task.get("workflow_step_id", "")
    return step if step in work_steps else admission_step

def pick_intent(sessions):
    """(intent, session_id): 'start' only with no session at all, else 'workflow_step' + newest.

    A prepared-but-never-run (CREATED) session takes the 'workflow_step' route, not 'start'.
    'start' creates a *new* session, which would leave the task with two; 'workflow_step'
    reuses the prepared one -- kandev's ensureSessionRunning starts the agent on the
    already-prepared workspace -- and moves the task in the same call.
    """
    newest = newest_session(sessions)
    if newest is None:
        return ("start", None)
    return ("workflow_step", newest["id"])

def prune_booked(booked, tasks, task_sessions, now, ttl):
    """Keep a booking (key -> booked_at) only while it still guards against a double start.

    Two retention rules, split by whether the key itself will eventually stop matching:
    - Session exists and reads WAITING (sid set): keep with no deadline. Once the launched
      turn starts (state leaves WAITING) or the task routes away, the key stops matching and
      is dropped -- bounding memory and self-clearing the guard.
    - Nothing in the key will change on its own (sid None, or the session still reads
      CREATED): keep only until `ttl` elapses. A session launched from CREATED doesn't flip
      state instantly, and one launched from no-session-at-all may not have surfaced in the
      API yet (Kandev launches in a detached goroutine) -- in both cases the task reads
      exactly as it did before the launch and would look launchable again. The TTL is what
      lets a launch that silently failed retry instead of wedging the task forever.
    """
    keep = {}
    for t in tasks:
        newest = newest_session(task_sessions.get(t["id"], []))
        sid = newest["id"] if newest else None
        st = newest["state"] if newest else None
        key = (t["id"], t.get("workflow_step_id", ""), sid)
        if key not in booked:
            continue
        if sid is None or st == CREATED_STATE:
            if now - booked[key] < ttl:
                keep[key] = booked[key]
        elif st == READY_STATE:
            keep[key] = booked[key]
    return keep

# --- kandev glue (IO) ---

def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(URL + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=15) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None

def workflow_name():
    return (_req("GET", f"/api/v1/workflows/{PACED_WORKFLOW_ID}") or {}).get("name") or PACED_WORKFLOW_ID

def workflow_steps():
    return (_req("GET", f"/api/v1/workflows/{PACED_WORKFLOW_ID}/workflow/steps") or {}).get("steps", [])

def paced_tasks():
    return (_req("GET", f"/api/v1/workflows/{PACED_WORKFLOW_ID}/tasks") or {}).get("tasks", [])

def sessions_of(task_id):
    return (_req("GET", f"/api/v1/tasks/{task_id}/sessions") or {}).get("sessions", [])

def ws_launch(task_id, intent, session_id, workflow_step_id, req_id="pace-1"):
    """Send one session.launch over the WebSocket and wait for the matching response."""
    import websocket  # lazy: keeps the test env (no network) dependency-free
    payload = {"task_id": task_id, "intent": intent, "workflow_step_id": workflow_step_id}
    if session_id:
        payload["session_id"] = session_id
    msg = {"id": req_id, "type": "request", "action": "session.launch", "payload": payload}
    conn = websocket.create_connection(WS_URL, timeout=30, origin=URL)
    try:
        conn.send(json.dumps(msg))
        while True:
            raw = conn.recv()
            if not raw:
                continue
            m = json.loads(raw)
            if m.get("id") != req_id:
                continue  # skip unrelated pushed events
            p = m.get("payload") or {}
            if m.get("type") == "error" or p.get("success") is False:
                raise RuntimeError(f"session.launch failed: {p.get('message') or m}")
            return p
    finally:
        conn.close()

# --- pacing loop ---

_BOOKED = {}             # (task_id, step_id, session_id) -> booked_at (launch issued)
_WORK_STEPS = set()
_QUEUE_STEP = None
_ADMISSION = None
_STEP_NAMES = {}         # step id -> name, for logging only
_STEP_RANKS = {}         # step id -> board position, for cross-column launch ordering

def tick(was_open):
    global _BOOKED
    usage = _req("GET", "/api/v1/agents/usage")
    now = time.time()
    windows = parse_windows(usage)
    if not windows and provider_missing(usage, PROVIDER):
        print(f"provider {PROVIDER!r} not in usage {sorted(usage_providers(usage))} "
              f"-> WITHHOLDING (check PACER_PROVIDER)", flush=True)
        return False  # fail closed: never pace freely against a quota we can't see
    wh = worst_headroom(windows, now, ALPHA)
    if ceiling_breached(windows, BACKSTOP):
        print(f"headroom={wh:+.3f} CEILING (consumed>{BACKSTOP:.2f}) -> withholding", flush=True)
        return False
    is_open = gate_open(wh, was_open, RESUME_MARGIN)
    if not is_open:
        print(f"headroom={wh:+.3f} PAUSED", flush=True)
        return is_open

    tasks = paced_tasks()
    task_sessions = {t["id"]: sessions_of(t["id"]) for t in tasks}
    _BOOKED = prune_booked(_BOOKED, tasks, task_sessions, now, BOOKING_TTL)
    running = sum(1 for t in tasks for s in task_sessions[t["id"]] if s.get("state") in RUNNING_STATES)
    if running >= CONCURRENCY:
        print(f"headroom={wh:+.3f} open, full ({running}/{CONCURRENCY})", flush=True)
        return is_open

    launchable = [t for t in sorted(tasks, key=lambda t: task_order_key(t, _STEP_RANKS))
                  if is_launchable(t, task_sessions[t["id"]], _WORK_STEPS, _QUEUE_STEP, _BOOKED)]
    if not launchable:
        print(f"headroom={wh:+.3f} open, nothing to launch ({running}/{CONCURRENCY} running)", flush=True)
        return is_open

    task = launchable[0]
    # Re-read the task's current step immediately before launching. The workflow_step_id we
    # pass is an unvalidated move, so acting on the top-of-tick snapshot could drag a task
    # that was moved in between (human board edit, another agent's move_task) back to a stale
    # step. If it moved since selection, skip and re-evaluate next tick. Resolve the newest
    # live session now too -- a profile-switch step spawns a fresh one on entry.
    fresh = next((t for t in paced_tasks() if t["id"] == task["id"]), None)
    if fresh is None or fresh.get("workflow_step_id", "") != task.get("workflow_step_id", ""):
        print(f"headroom={wh:+.3f} open, {task.get('title') or task['id']!r} moved since selection "
              f"-> skip", flush=True)
        return is_open
    step = fresh.get("workflow_step_id", "")
    sessions = sessions_of(task["id"])
    intent, sid = pick_intent(sessions)
    target = launch_target(fresh, _WORK_STEPS, _QUEUE_STEP, _ADMISSION)
    ws_launch(task["id"], intent, sid, target)
    _BOOKED[(task["id"], step, sid)] = now
    print(f"headroom={wh:+.3f} open -> launched {fresh.get('title') or task['id']!r} "
          f"intent={intent} target={named(target, _STEP_NAMES)!r} "
          f"({running + 1}/{CONCURRENCY})", flush=True)
    return is_open

def main():
    global _WORK_STEPS, _QUEUE_STEP, _ADMISSION, _STEP_NAMES, _STEP_RANKS
    if "--dump-usage" in sys.argv:
        print(json.dumps(_req("GET", "/api/v1/agents/usage"), indent=2))
        return

    steps = workflow_steps()
    _WORK_STEPS = work_step_ids(steps)
    _QUEUE_STEP = queue_step_id(steps)
    _ADMISSION = admission_target(steps, ADMISSION_OVERRIDE)
    _STEP_NAMES = step_names(steps)
    _STEP_RANKS = step_ranks(steps)
    for w in lint_workflow(steps):
        print(f"lint: {w}", file=sys.stderr, flush=True)
    works = sorted(named(s, _STEP_NAMES) for s in _WORK_STEPS)
    print(f"pacing {URL} workflow={workflow_name()!r} every {INTERVAL}s "
          f"(alpha={ALPHA}, concurrency={CONCURRENCY}, provider={PROVIDER})\n"
          f"  work_steps={works} queue={named(_QUEUE_STEP, _STEP_NAMES)!r} "
          f"admission={named(_ADMISSION, _STEP_NAMES)!r}", flush=True)

    was_open = True
    while True:
        try:
            was_open = tick(was_open)
        except Exception as e:  # keep pacing across transient errors; fail closed for this tick
            print(f"tick error: {e} -> pausing this tick", file=sys.stderr, flush=True)
            was_open = False
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
