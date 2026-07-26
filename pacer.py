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
queue (the start step) with a single call that both moves and launches it. Which task
goes next is the user's own drag order in the Kandev TASKS sidebar (see PACER_ORDER).

See docs/pacing-designs.md (Design 13) and README.md for the full derivation and
the confirmed kandev source behavior.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

# --- config (env-overridable) ---
URL = os.environ.get("KANDEV_URL", "http://127.0.0.1:38429").rstrip("/")
# Which workflows to pace: comma-separated ids or names. Unset (the default) paces EVERY
# enrolled workflow -- one whose steps carry the stage_type=work tag, which only
# make_paced.py writes. Enrollment, not this variable, is the normal way to opt a workflow in.
PACED_WORKFLOWS = [w.strip() for w in (os.environ.get("PACED_WORKFLOW_ID") or "").split(",")
                   if w.strip()]
PROVIDER = os.environ.get("PACER_PROVIDER", "anthropic")     # usage agent to gate on
CONCURRENCY = int(os.environ.get("PACER_CONCURRENCY", "1"))  # max in-flight paced turns
ALPHA = float(os.environ.get("PACER_ALPHA", "1.0"))          # reserve slope; <1 keeps quota to spare
RESUME_MARGIN = float(os.environ.get("PACER_RESUME_MARGIN", "0.0"))  # hysteresis reopen threshold
BACKSTOP = float(os.environ.get("PACER_BACKSTOP", "0.97"))   # absolute cutoff; >=1.0 disables
INTERVAL = int(os.environ.get("PACER_INTERVAL", "120"))      # seconds between ticks
# Which order pending tasks are launched in: "auto" follows the sidebar TASKS list when the
# user has it on the custom (manual, drag-ordered) sort and falls back to FIFO otherwise;
# "custom" always follows the manual order; "created" is plain FIFO by task creation time.
ORDER = (os.environ.get("PACER_ORDER") or "auto").lower()
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

def is_paced_step(step):
    """A step the pacer may start a turn on: tagged `stage_type=work` AND routing away on
    turn completion. Both halves are load-bearing, for different reasons.

    The tag is the *enrollment* marker. Kandev defaults the column to 'custom' and has no
    API/UI/import surface for it, so the only thing that ever writes 'work' is
    make_paced.py's direct SQLite write -- which is also what strips the `auto_start_agent`
    that would bypass the gate. So "tagged work" means "deliberately prepared for pacing",
    which is exactly the signal needed to decide, unattended, whether a workflow the pacer
    merely *discovered* is fair game.

    The routing half is the *safety* property, and it is not optional. It buys the guarantee
    that a task still sitting on the step has an unfinished turn -- without it a step that
    never routes away would read as idle after every turn and be relaunched forever, an
    infinite prompt loop billed to the quota this program exists to protect.
    """
    return (step.get("stage_type") or "").lower() == "work" and is_work_step(step)

def paced_step_ids(steps):
    return {s["id"] for s in steps if is_paced_step(s)}

def queue_step_id(steps):
    """The parked queue is the workflow's start step (where new/re-queued tasks wait)."""
    for s in steps:
        if s.get("is_start_step"):
            return s["id"]
    return None

def admission_target(steps, override=None):
    """The step to admit a queued task into: the lowest-position paced step, or an override."""
    if override:
        return override
    works = [s for s in steps if is_paced_step(s)]
    if not works:
        return None
    return min(works, key=lambda s: s.get("position", 0))["id"]

def resolve_scope(entries, restrict=(), override=None):
    """Work out which workflows to pace, from [(workflow, steps)] pairs.

    Returns (scope, work_steps, queue_steps, admission, skipped):
      scope       [workflow_id] -- every enrolled workflow, in payload order
      work_steps  {step_id} -- pace-eligible steps, unioned across the scope (kandev step
                  ids are globally unique, so one flat set stays unambiguous)
      queue_steps {step_id} -- each scoped workflow's start step
      admission   {workflow_id: step_id} -- per workflow: a queued task must be admitted
                  into a step of *its own* workflow, so this cannot be one global value
      skipped     [(workflow, reason)] -- reported, never silently dropped: a workflow
                  quietly out of scope is how a task the user dragged to the top goes
                  missing with no explanation.
    """
    want = [r.lower() for r in restrict]
    scope, work, queues, admission, skipped = [], set(), set(), {}, []
    for wf, steps in entries:
        if want and wf["id"].lower() not in want and (wf.get("name") or "").lower() not in want:
            skipped.append((wf, "not named in PACED_WORKFLOW_ID"))
            continue
        eligible = paced_step_ids(steps)
        if not eligible:
            skipped.append((wf, "not enrolled: no step is tagged stage_type=work *and* routes "
                                "on turn complete -- run make_paced.py to enroll it"))
            continue
        scope.append(wf["id"])
        work |= eligible
        queue = queue_step_id(steps)
        if queue:
            queues.add(queue)
        # An override names a step in one specific workflow; applying it to every workflow
        # would admit their tasks into a step they don't own.
        own = override if override in {s["id"] for s in steps} else None
        target = admission_target(steps, own)
        if target:
            admission[wf["id"]] = target
    return scope, work, queues, admission, skipped

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

def lint_workflow(steps, enrolled_only=True):
    """Warning strings for config that would bypass the gate or misbehave. No mutation.

    `enrolled_only` suppresses the "nothing to pace" complaints for a workflow that was
    never enrolled -- with discovery across every workflow, most of them are legitimately
    not paced and saying so each startup would bury the warnings that matter.
    """
    works = paced_step_ids(steps)
    warns = []
    for s in steps:
        name = s.get("name", "?")
        stage = (s.get("stage_type") or "").lower()
        routes = is_work_step(s)
        # These two auto-launch a turn on step ENTRY, before the pacer gets to act, so they
        # bypass the gate on ANY step -- a parked Review step leaks just as a work step does.
        if _has_on_enter(s, "auto_start_agent"):
            warns.append(f"step {name!r} still has on_enter:auto_start_agent (bypasses the gate)")
        if s.get("agent_profile_id") and (s.get("prompt") or "").strip():
            warns.append(f"step {name!r} has both an agent-profile override and a prompt (auto-launch bypass)")
        if s["id"] in works:
            if s.get("auto_advance_requires_signal"):
                warns.append(f"work step {name!r} gates routing on the agent step_complete signal")
        elif routes and stage in ("review", "approval"):
            warns.append(f"step {name!r} routes on on_turn_complete (work-shaped) but is tagged stage_type={stage!r}")
        elif routes and enrolled_only:
            # The one that explains a task sitting there forever: work-shaped but untagged,
            # so the pacer will not touch it.
            warns.append(f"step {name!r} routes on on_turn_complete but is tagged stage_type={stage or 'custom'!r} "
                         f"-> NOT paced (make_paced.py tags it 'work')")
        elif stage == "work":
            warns.append(f"step {name!r} is tagged stage_type='work' but has no on_turn_complete transition (parked)")
    if not enrolled_only:
        return warns
    if not works:
        warns.append("no pace-eligible step (stage_type=work + on_turn_complete transition) -- nothing to pace")
    if admission_target(steps, ADMISSION_OVERRIDE) is None:
        warns.append("no admission target derivable")
    queue = queue_step_id(steps)
    if queue is None:
        warns.append("no start step (parked queue) found")
    elif queue in works:
        # The whole design rests on new work *waiting* somewhere until the gate opens. A start
        # step that is itself pace-eligible has no holding area: every task landing there is
        # immediately launchable, so a backlog executes instead of queueing.
        warns.append(f"start step {named(queue, step_names(steps))!r} is itself pace-eligible -- "
                     f"the parked queue will execute rather than hold (tasks never wait there)")
    return warns

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

def is_launchable(task, sessions, work_steps, queue_steps, booked):
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
    if step in queue_steps:
        return newest is None or state in IDLE_STATES
    return False

def launch_target(task, work_steps, admission):
    """Which step id to launch at: the current step in place, or the admission step to move to.

    Admission is looked up by the task's OWN workflow_id -- with several workflows paced at
    once, a single global admission step would drag a queued task into another workflow's
    board. Returns None when the task's workflow has no admission target, which the caller
    must treat as "skip", never as "launch in place".
    """
    step = task.get("workflow_step_id", "")
    if step in work_steps:
        return step
    return admission.get(task.get("workflow_id", ""))

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

# --- launch order: the sidebar TASKS list, top to bottom (pure) ---

def effective_sidebar_view(settings):
    """The active sidebar view merged with any in-flight draft, or None.

    Mirrors the frontend's useEffectiveSidebarView. Reading only the saved view would miss
    exactly the case this feature is for: dragging a task flips the sort to 'custom' in the
    *draft* first, and the draft is what the user is looking at.
    """
    views = settings.get("sidebar_views") or []
    active = next((v for v in views if v.get("id") == settings.get("sidebar_active_view_id")), None)
    if active is None:
        active = views[0] if views else None
    if active is None:
        return None
    draft = settings.get("sidebar_draft") or None
    if draft and draft.get("base_view_id") == active.get("id"):
        return {**active, "filters": draft.get("filters"), "sort": draft.get("sort"),
                "group": draft.get("group")}
    return active

def has_custom_sort_view(settings):
    """True when the user keeps a sidebar view on the manual (drag-ordered) sort.

    Deliberately *any* saved view, not just the active one: the manual order itself
    (`ordered_task_ids`) is stored once per user, not per view, so a live "Prioritize" view
    still expresses the user's intended order while they have some other view selected to
    work in. Keying on the active view would silently flip the pacer back to FIFO the
    moment they switch views for an unrelated reason. The draft is checked too -- dragging
    a task flips the sort to custom in the draft first, before it is saved to the view.
    """
    views = list(settings.get("sidebar_views") or [])
    effective = effective_sidebar_view(settings)
    if effective:
        views.append(effective)
    return any((v.get("sort") or {}).get("key") == "custom" for v in views)

def _rank(tasks, ordered_ids):
    """Tasks listed in `ordered_ids` first, in that order; the rest keep their incoming order."""
    index = {tid: i for i, tid in enumerate(ordered_ids)}
    listed = sorted((t for t in tasks if t["id"] in index), key=lambda t: index[t["id"]])
    return listed + [t for t in tasks if t["id"] not in index]

def visible_task_order(tasks, pinned_ids, ordered_ids, subtask_order=None):
    """Tasks in the order the sidebar renders them under sort=custom, group=none.

    Root tasks take the manual order, then pinned ones float to the top (pinning is a
    visible re-ordering, so honoring it keeps "the pacer runs the top-most task" true).
    Each task is followed by its subtasks in their own stored order -- the sidebar nests
    them under the parent, so scheduling them right after it is what the list shows.

    Tasks the user never dragged keep their incoming order -- the caller passes them in
    FIFO, so "not placed" means "not prioritized", and they run oldest-first behind
    everything that was placed. (The sidebar itself floats them to the top by newest
    created_at; copying that here would let merely *creating* a task jump the queue ahead
    of an order the user deliberately set.)
    """
    subtask_order = subtask_order or {}
    ids = {t["id"] for t in tasks}
    roots, subs = [], {}
    for t in tasks:
        parent = t.get("parent_id") or ""
        if parent and parent in ids and parent != t["id"]:
            subs.setdefault(parent, []).append(t)
        else:
            roots.append(t)
    out, seen = [], set()
    def visit(task):
        if task["id"] in seen:
            return
        seen.add(task["id"])
        out.append(task)
        for sub in _rank(subs.get(task["id"], []), subtask_order.get(task["id"]) or []):
            visit(sub)
    for task in _rank(_rank(roots, ordered_ids), pinned_ids):
        visit(task)
    # A parent cycle leaves its tasks unreachable from any root. Append them rather than
    # dropping them -- an unorderable task must still be launchable, just last.
    out.extend(t for t in tasks if t["id"] not in seen)
    return out

def ordered_tasks(tasks, settings, mode):
    """Candidate tasks in launch-priority order.

    mode 'created' (or no settings) is FIFO by creation time -- the pacer's original order,
    and the fallback whenever the user's list order can't be read. 'auto' follows the manual
    order once the user keeps a sidebar view on the custom sort; 'custom' always does.

    FIFO is the base order in every mode: the manual list is layered on top of it, so tasks
    the user hasn't placed stay in oldest-first order behind the ones they have.
    """
    fifo = sorted(tasks, key=lambda t: t.get("created_at", ""))
    if mode == "created" or not settings:
        return fifo
    if mode != "custom" and not has_custom_sort_view(settings):
        return fifo
    prefs = settings.get("sidebar_task_prefs") or {}
    return visible_task_order(fifo, prefs.get("pinned_task_ids") or [],
                              prefs.get("ordered_task_ids") or [],
                              prefs.get("subtask_order_by_parent_id") or {})

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

def workflows():
    return (_req("GET", "/api/v1/workflows") or {}).get("workflows") or []

def workflow_steps(workflow_id):
    return (_req("GET", f"/api/v1/workflows/{workflow_id}/workflow/steps") or {}).get("steps", [])

def workflow_entries():
    """[(workflow, steps)] for every workflow on the server -- the input to resolve_scope."""
    return [(wf, workflow_steps(wf["id"])) for wf in workflows()]

def tasks_of(workflow_id):
    return (_req("GET", f"/api/v1/workflows/{workflow_id}/tasks") or {}).get("tasks", [])

def paced_tasks(scope=None):
    """Tasks across every paced workflow. Each carries `workflow_id`, which is what routes
    an admission back to a step of its own board."""
    return [t for wf in (scope if scope is not None else _SCOPE) for t in tasks_of(wf)]

def user_settings():
    """The user settings blob (sidebar views + task prefs), or None if unavailable.

    Returns None rather than raising: a failure here only costs the manual order, and
    falling back to FIFO is far better than pausing the whole tick over it.
    """
    if ORDER == "created":
        return None
    try:
        return (_req("GET", "/api/v1/user/settings") or {}).get("settings") or None
    except Exception as e:
        print(f"user settings unavailable ({e}) -> FIFO order this tick", file=sys.stderr, flush=True)
        return None

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
_SCOPE = []              # workflow ids being paced
_WORK_STEPS = set()      # pace-eligible step ids, unioned across the scope
_QUEUE_STEPS = set()     # each scoped workflow's start step (its parked queue)
_ADMISSION = {}          # workflow id -> step id to admit a queued task into
_STEP_NAMES = {}         # step id -> name, for logging only
_WF_NAMES = {}           # workflow id -> name, for logging only

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

    launchable = [t for t in ordered_tasks(tasks, user_settings(), ORDER)
                  if is_launchable(t, task_sessions[t["id"]], _WORK_STEPS, _QUEUE_STEPS, _BOOKED)]
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
    target = launch_target(fresh, _WORK_STEPS, _ADMISSION)
    if target is None:
        print(f"headroom={wh:+.3f} open, {task.get('title') or task['id']!r} has no admission "
              f"target in workflow {named(fresh.get('workflow_id'), _WF_NAMES)!r} -> skip", flush=True)
        return is_open
    sessions = sessions_of(task["id"])
    intent, sid = pick_intent(sessions)
    ws_launch(task["id"], intent, sid, target)
    _BOOKED[(task["id"], step, sid)] = now
    print(f"headroom={wh:+.3f} open -> launched {fresh.get('title') or task['id']!r} "
          f"intent={intent} workflow={named(fresh.get('workflow_id'), _WF_NAMES)!r} "
          f"target={named(target, _STEP_NAMES)!r} ({running + 1}/{CONCURRENCY})", flush=True)
    return is_open

def resolve_globals(entries):
    """Fill the scope globals from [(workflow, steps)]. Returns the skipped list for reporting."""
    global _SCOPE, _WORK_STEPS, _QUEUE_STEPS, _ADMISSION, _STEP_NAMES, _WF_NAMES
    _SCOPE, _WORK_STEPS, _QUEUE_STEPS, _ADMISSION, skipped = resolve_scope(
        entries, PACED_WORKFLOWS, ADMISSION_OVERRIDE)
    _STEP_NAMES = {sid: name for _, steps in entries for sid, name in step_names(steps).items()}
    _WF_NAMES = {wf["id"]: wf.get("name") or wf["id"] for wf, _ in entries}
    return skipped

def _dump_order(entries, skipped):
    """Print the queue the pacer would work down, so the effect of a drag is checkable without
    waiting a tick (or spending quota) to see which task it picks."""
    settings = user_settings()
    manual = bool(settings) and (ORDER == "custom" or has_custom_sort_view(settings))
    placed = ((settings or {}).get("sidebar_task_prefs") or {}).get("ordered_task_ids") or []
    print(f"order={ORDER} -> {'manual sidebar order' if manual else 'FIFO by created_at'} "
          f"({len(placed)} task(s) placed by hand)")
    print(f"scope: {[named(w, _WF_NAMES) for w in _SCOPE]}")
    tasks = paced_tasks()
    for i, t in enumerate(ordered_tasks(tasks, settings, ORDER)):
        print(f"{i + 1:3}. {t.get('title') or t['id']}")
    # The sidebar list spans every workflow, so a task can be placed high by hand and still be
    # out of scope. Saying nothing about it is what made a top-dragged task look lost.
    if not skipped:
        return
    in_scope = {t["id"] for t in tasks}
    elsewhere = {t["id"]: (t, wf) for wf, _ in skipped for t in tasks_of(wf["id"])}
    missing = [tid for tid in placed if tid not in in_scope and tid in elsewhere]
    print(f"\nnot paced: {len(skipped)} workflow(s) out of scope")
    for wf, reason in skipped:
        print(f"  - {wf.get('name') or wf['id']}: {reason}")
    for tid in missing:
        task, wf = elsewhere[tid]
        print(f"  ! placed by hand but not paced: {task.get('title') or tid!r} "
              f"(in {wf.get('name') or wf['id']!r})")

def main():
    if "--dump-usage" in sys.argv:
        print(json.dumps(_req("GET", "/api/v1/agents/usage"), indent=2))
        return
    if ORDER not in ("auto", "custom", "created"):
        print(f"lint: PACER_ORDER={ORDER!r} unknown -> treating it as 'auto'",
              file=sys.stderr, flush=True)

    entries = workflow_entries()
    skipped = resolve_globals(entries)
    if "--dump-order" in sys.argv:
        _dump_order(entries, skipped)
        return
    if not _SCOPE:
        print("no workflow is enrolled for pacing (no step tagged stage_type=work that routes "
              "on turn complete). Run: ./make_paced.py '<workflow>' --apply", file=sys.stderr, flush=True)
        return 1
    for wf, steps in entries:
        if wf["id"] not in _SCOPE:
            continue
        for w in lint_workflow(steps):
            print(f"lint: [{wf.get('name') or wf['id']}] {w}", file=sys.stderr, flush=True)
    print(f"pacing {URL} every {INTERVAL}s "
          f"(alpha={ALPHA}, concurrency={CONCURRENCY}, provider={PROVIDER}, order={ORDER})", flush=True)
    steps_by_wf = {wf["id"]: steps for wf, steps in entries}
    for wf_id in _SCOPE:
        steps = steps_by_wf[wf_id]
        works = sorted(named(s["id"], _STEP_NAMES) for s in steps if s["id"] in _WORK_STEPS)
        print(f"  {named(wf_id, _WF_NAMES)!r}: work_steps={works} "
              f"queue={named(queue_step_id(steps), _STEP_NAMES)!r} "
              f"admission={named(_ADMISSION.get(wf_id), _STEP_NAMES)!r}", flush=True)
    for wf, reason in skipped:
        print(f"  not paced: {wf.get('name') or wf['id']!r} -- {reason}", flush=True)

    was_open = True
    while True:
        try:
            was_open = tick(was_open)
        except Exception as e:  # keep pacing across transient errors; fail closed for this tick
            print(f"tick error: {e} -> pausing this tick", file=sys.stderr, flush=True)
            was_open = False
        time.sleep(INTERVAL)

if __name__ == "__main__":
    sys.exit(main() or 0)
