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
from pathlib import Path

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
# Absolute deadline for one WebSocket request after it has been sent. The socket's own
# timeout is not enough: every unrelated pushed event makes recv() return and starts another
# socket timeout, so a busy event stream can otherwise keep one request alive forever.
WS_REQUEST_TIMEOUT = float(os.environ.get("PACER_WS_TIMEOUT", "30"))
# Which order pending tasks are launched in. "auto" layers the sidebar TASKS list drag order
# over the board's kanban-pull order once the user keeps a view on the custom sort; "custom"
# always layers it; "board" is pull order alone (most downstream column first, then
# position -> priority -> created_at -> id).
ORDER = (os.environ.get("PACER_ORDER") or "auto").lower()
# How long a never-run admission booking (session not yet visible) is held before it may
# retry -- long enough to outlast the lag before a just-started session shows up in the API
# (Kandev starts it in a background goroutine, detached from our launch call), short enough
# that a launch that silently failed still gets another shot instead of wedging forever.
BOOKING_TTL = 3 * INTERVAL
# How long a task whose launch was *rejected* is left alone before the pacer tries it again,
# doubling with each consecutive failure up to FAIL_BACKOFF_MAX_MULT x the base. Failures are
# tracked separately from bookings on purpose: a booking means "we spent a turn here, don't
# spend another", a failure means "this one didn't work, back off" -- one map meaning both
# would make every "did we already launch?" question ambiguous.
FAIL_BACKOFF = int(os.environ.get("PACER_FAIL_BACKOFF", str(3 * INTERVAL)))
FAIL_BACKOFF_MAX_MULT = 8
# Consecutive failures before the task is flagged BLOCKED on the board. Not the first one: a
# single WebSocket hiccup should not paint a card red. 0 disables the flagging entirely.
BLOCK_AFTER = int(os.environ.get("PACER_BLOCK_AFTER", "2"))
# Kandev task state used for that flag -- it renders as a "Blocked" warning icon on the card.
BLOCKED_STATE = "BLOCKED"
# How many candidates one tick may try before giving up. A rejected launch spends no quota, so
# moving on to the next task is what keeps one bad card from starving the whole board; the cap
# stops a board full of broken tasks from turning one tick into a long run of round trips.
MAX_ATTEMPTS = 3
ADMISSION_OVERRIDE = os.environ.get("PACER_ADMISSION_STEP_ID") or None
# Structured tick/event log (JSONL, append-only). Set PACER_LOG='' to disable.
LOG_PATH = os.environ.get(
    "PACER_LOG", str(Path.home() / ".local" / "state" / "kandev-pacer" / "pacer.jsonl"))
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
# States kandev will accept a *queued* prompt for. checkSessionPromptable also allows
# COMPLETED and IDLE, but the pacer never selects those. It does NOT allow CREATED, so a
# never-run task's queued prompt has to wait for that task's admission turn -- draining it
# would come straight back as `session_not_promptable`.
DRAINABLE_STATES = {READY_STATE}

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

# --- structured log records (pure; unit-tested in test_pace.py) ---
#
# The human-readable stdout lines answer "what is the pacer doing right now"; these
# records answer "what did it do over the last week", which is the only way to size
# concurrency. Every question we need to settle is a time derivative (does headroom
# trend up while work is queued? how long after a launch does utilization_pct move?)
# or a per-window breakdown (which window binds, and how much absolute slack is that),
# and neither survives in a formatted line with no timestamp.

def window_records(windows, now, alpha=1.0):
    """Per-window detail for one observation: consumed, elapsed, headroom, duration.

    Logged per window rather than collapsed to min() because headroom is in
    window-fraction units, which are NOT comparable across windows for sizing:
    h=0.1 is 30 minutes of slack on the 5-hour window and 16.8 hours on the 7-day.
    Recovering the absolute slack later needs the duration alongside the fraction.
    """
    out = []
    for w in windows:
        e = elapsed_frac(now, w["resets_at"], w["duration_s"])
        out.append({"label": w.get("label"), "c": round(w["consumed_frac"], 4),
                    "e": round(e, 4), "h": round(headroom(w["consumed_frac"], e, alpha), 4),
                    "d": w["duration_s"], "resets_at": w["resets_at"]})
    return out

def binding_label(window_recs):
    """Label of the least-headroom window -- the one the gate is actually reacting to."""
    return min(window_recs, key=lambda w: w["h"])["label"] if window_recs else None

def tick_record(now, gate, action, window_recs, running, launchable, cap, booked, launched=None):
    """One record per tick, written in every branch.

    `gate` is the quota verdict (open/paused/ceiling/blind); `action` is what the pacer
    did about it (launched/full/nothing/skipped/withheld). They are separate because the
    decisive question -- is quota going unspent while work waits? -- is exactly the
    gate=open + action=nothing + launchable>0 combination, which one merged field hides.
    """
    return {"t": round(now, 3), "kind": "tick", "gate": gate, "action": action,
            "wh": min((w["h"] for w in window_recs), default=1.0),
            "bind": binding_label(window_recs), "windows": window_recs,
            "running": running, "launchable": launchable, "cap": cap, "booked": booked,
            "launched": launched}

def sessions_snapshot(task_sessions):
    """{task_id: {session_id: state}} -- the shape diff_sessions compares."""
    return {tid: {s["id"]: s.get("state") for s in sess} for tid, sess in task_sessions.items()}

def diff_sessions(prev, cur, now):
    """Session state transitions between two snapshots. prev=None (first tick) -> no events.

    Turn boundaries can't be derived from launch records alone: kandev starts the agent in
    a detached goroutine, so the launch call returns well before STARTING/RUNNING appears,
    and nothing at all is recorded when a turn *ends*. Both timestamps are needed to measure
    how stale utilization_pct is, which sets the safety margin any concurrency rule needs.
    """
    if prev is None:
        return []
    events = []
    for tid, sess in sorted(cur.items()):
        old = prev.get(tid, {})
        for sid, state in sorted(sess.items()):
            if old.get(sid) != state:
                events.append({"t": round(now, 3), "kind": "session", "task": tid,
                               "session": sid, "from": old.get(sid), "to": state})
    return events

def detect_resets(prev_windows, cur_windows, now):
    """Windows whose reset time moved forward have rolled over; record what they closed at.

    `final_c` -- the last consumed fraction observed before the roll -- answers "did the
    window end with quota unspent?". That is the single datum that decides whether more
    concurrency is worth anything at all, and no other record preserves it: after the roll
    the endpoint reports the new window and the old number is gone for good.
    """
    prev = {w.get("label"): w for w in prev_windows}
    events = []
    for w in cur_windows:
        p = prev.get(w.get("label"))
        if p and w["resets_at"] > p["resets_at"]:
            events.append({"t": round(now, 3), "kind": "window_reset", "label": w.get("label"),
                           "final_c": round(p["consumed_frac"], 4), "d": w["duration_s"]})
    return events

# --- log writer (IO) ---

_log_failed = False

def log_event(rec):
    """Append one JSONL record. Never raises -- a broken log must not stop the pacing loop."""
    global _log_failed
    if not LOG_PATH:
        return
    try:
        path = Path(LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except OSError as e:
        if not _log_failed:  # warn once; a full disk shouldn't spam every tick
            print(f"log: {e} (continuing unlogged)", file=sys.stderr, flush=True)
            _log_failed = True

# --- usage parsing ---

def _epoch(ts):
    """RFC3339 -> epoch seconds. Accepts trailing 'Z' and numeric offsets."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()

def parse_windows(usage, provider=None):
    """Map /agents/usage to [{consumed_frac, resets_at, duration_s}] for one provider.

    Real shape: {agents:[{usage:{provider, windows:[{label, utilization_pct, reset_at}]}}]}.
    Windows with an unknown label or no reset time are skipped.

    The label is carried through (the pacing math ignores it) so the log can name which
    window bound a decision and match windows across ticks to spot a rollover.
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
            out.append({"label": w.get("label"), "consumed_frac": w["utilization_pct"] / 100.0,
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

def is_launchable(task, sessions, work_steps, queue_steps, skip):
    """Can the pacer start a turn for this task now?

    - at a work step: only when its newest session is idle -- WAITING_FOR_INPUT (in-place next
      turn) or CREATED (a fresh task dragged straight onto the step, prepared but never run).
    - at the parked queue (start step): a task with no session at all, or one whose newest
      session is idle -- CREATED for a new task, WAITING_FOR_INPUT for a re-queued one.

    `skip` is the set of (task, step, session) keys this tick may not act on: live bookings
    (we already spent a turn there) plus tasks inside a post-failure backoff. Both answer the
    same question -- "is this a candidate right now?" -- which is why they share one argument,
    while the state behind them stays in two maps that mean two different things.

    CREATED counts as launchable because kandev prepares a session up front, so "never run"
    no longer means "no session". Treating it as unlaunchable is what left new tasks parked
    in the queue forever while the pacer reported "nothing to launch".
    """
    step = task.get("workflow_step_id", "")
    newest = newest_session(sessions)
    sid = newest["id"] if newest else None
    if (task["id"], step, sid) in skip:
        return False
    state = newest["state"] if newest else None
    if step in work_steps:
        return state in IDLE_STATES
    if step in queue_steps:
        return newest is None or state in IDLE_STATES
    return False

def is_drainable(task, sessions, queued, skip):
    """Is a user-queued prompt parked on this task, releasable as its next turn?

    Deliberately ignores the workflow step. A prompt queued while the task sits at Review or
    Done is exactly the case to serve, and those steps are never launchable -- without this,
    such a prompt is never released at all, because kandev fires no event on a parked step
    either (its on_enter already ran, and there is no next transition without a turn).

    What it does require is a session kandev will take a prompt for (DRAINABLE_STATES), and a
    non-empty queue as counted this tick.
    """
    newest = newest_session(sessions)
    if newest is None or newest.get("state") not in DRAINABLE_STATES:
        return False
    if queued.get(newest["id"], 0) <= 0:
        return False
    return (task["id"], task.get("workflow_step_id", ""), newest["id"]) not in skip

def select_action(ordered, task_sessions, queued, work_steps, queue_steps, skip):
    """('drain'|'launch', task) for the one turn this tick may spend, else (None, None).

    `ordered` is the caller's launch-priority order (ordered_tasks), applied to *both* kinds --
    so dragging a card up the sidebar or up its column moves its queued prompt up too, and a
    prompt parked in a downstream column is released before one further left. The ordering is
    the caller's business because only it knows the user's sidebar settings; this stays pure.

    Drain outranks launch for a reason: launching a task that has a prompt queued spends the
    turn on the *step* prompt and leaves the queued entry behind, where kandev's own
    on-step-entry drain flushes it after the turn completes -- a second turn that never passed
    the gate, carrying text written for the previous step. Releasing the queued prompt first
    keeps it at one turn per admitted decision.
    """
    for t in ordered:
        if is_drainable(t, task_sessions.get(t["id"], []), queued, skip):
            return ("drain", t)
    for t in ordered:
        if is_launchable(t, task_sessions.get(t["id"], []), work_steps, queue_steps, skip):
            return ("launch", t)
    return (None, None)

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

def ordered_tasks(tasks, settings, mode, ranks=None):
    """Candidate tasks in launch-priority order.

    The base order is always the board's kanban-pull order (task_order_key): drain the most
    downstream column first, then position -> priority -> created_at -> id within it. mode
    'board' stops there -- and so does every mode when the sidebar order can't be read.

    On top of that base, 'auto' (once the user keeps a sidebar view on the custom sort) and
    'custom' (always) apply the sidebar's manual drag order. The two are layered rather than
    alternatives: tasks the user placed by hand go first in the order they chose, and
    everything they did *not* place keeps the board's pull order behind them.

    Layering this way does let an explicit drag override the WIP-pull preference for
    continuing in-flight work over admitting new work -- dragging a queued task to the top
    admits it first. That is the point of the feature: an explicit instruction outranks a
    heuristic. PACER_ORDER=board restores strict pull order.
    """
    base = sorted(tasks, key=lambda t: task_order_key(t, ranks or {}))
    if mode == "board" or not settings:
        return base
    if mode != "custom" and not has_custom_sort_view(settings):
        return base
    prefs = settings.get("sidebar_task_prefs") or {}
    return visible_task_order(base, prefs.get("pinned_task_ids") or [],
                              prefs.get("ordered_task_ids") or [],
                              prefs.get("subtask_order_by_parent_id") or {})

def action_key(task, sessions):
    """The (task, step, session) triple every guard is keyed on -- selection, bookings, failures.

    Keeping one shape for all three is what lets a failure suppress the *same* candidate
    selection would pick next, rather than a near-miss that never matches.
    """
    newest = newest_session(sessions)
    return (task["id"], task.get("workflow_step_id", ""), newest["id"] if newest else None)

def live_keys(tasks, task_sessions):
    """{action key: newest session state (None when there is no session)} for the board as it
    reads right now. Both prune passes work off this: a key that no longer describes any task
    can never match again, so holding it would just leak memory."""
    out = {}
    for t in tasks:
        sessions = task_sessions.get(t["id"], [])
        out[action_key(t, sessions)] = (newest_session(sessions) or {}).get("state")
    return out

def fail_backoff(attempts, base=None):
    """Seconds to leave a task alone after `attempts` consecutive failed launches.

    Doubles per failure and then flattens. A task that is broken for good (a session kandev
    refuses to resume, say) otherwise costs a WebSocket round trip every few ticks forever;
    with the doubling it settles into an occasional retry, which is all a "did the user fix
    it yet?" check needs to be.
    """
    base = FAIL_BACKOFF if base is None else base
    return base * min(2 ** max(attempts - 1, 0), FAIL_BACKOFF_MAX_MULT)

def backed_off(failures, now, base=None):
    """Keys still inside their retry backoff -- not candidates this tick.

    Handed to select_action together with the live bookings. This is the half of the fix that
    lets the pacer move past a task it cannot launch: without it the same failing task is
    re-selected every tick, because it is genuinely the top candidate every tick.
    """
    return {k for k, f in failures.items()
            if now - f["at"] < fail_backoff(f["attempts"], base)}

def prune_failures(failures, tasks, task_sessions):
    """Drop failures whose key no longer describes the board.

    Kept while the key matches, with no deadline, because the attempt count is the whole point:
    expiring it would reset the backoff to its shortest value and retry forever at that pace.
    The key stops matching as soon as anything real changes -- the task moves step, a new
    session appears, the task is deleted -- and any of those is a new situation that deserves a
    fresh attempt. A launch that *succeeds* is cleared separately, by the caller: success does
    not change the key (same task, same step, same session), so nothing here would notice it.
    """
    live = live_keys(tasks, task_sessions)
    return {k: v for k, v in failures.items() if k in live}

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

    Most values are plain timestamps. A request whose response was lost is stored as
    {"at": timestamp, "uncertain": True}; unlike a confirmed launch from WAITING, that guard
    also expires after `ttl`, because the server may never have acted on the request.
    """
    keep = {}
    for key, st in live_keys(tasks, task_sessions).items():
        if key not in booked:
            continue
        booking = booked[key]
        booked_at = booking["at"] if isinstance(booking, dict) else booking
        uncertain = isinstance(booking, dict) and booking.get("uncertain")
        if key[2] is None or st == CREATED_STATE or uncertain:
            if (st in IDLE_STATES or st is None) and now - booked_at < ttl:
                keep[key] = booking
        elif st == READY_STATE:
            keep[key] = booking
    return keep

class RequestOutcomeUnknown(RuntimeError):
    """A sent WebSocket request got no matching response before disconnect/deadline."""

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
    if ORDER == "board":
        return None
    try:
        return (_req("GET", "/api/v1/user/settings") or {}).get("settings") or None
    except Exception as e:
        print(f"user settings unavailable ({e}) -> FIFO order this tick", file=sys.stderr, flush=True)
        return None

def sessions_of(task_id):
    return (_req("GET", f"/api/v1/tasks/{task_id}/sessions") or {}).get("sessions", [])

def _ws_connect():
    import websocket  # lazy: keeps the test env (no network) dependency-free
    # /ws enforces an Origin check, so present the server's own URL as the origin.
    return websocket.create_connection(WS_URL, timeout=30, origin=URL)

def _ws_call(conn, action, payload, req_id, timeout=None):
    """One request/response round trip, matched on `id`, with an absolute deadline.

    Once send() is attempted, a transport error is ambiguous: kandev may have accepted the
    operation and only its response was lost. Callers distinguish that from an explicit
    type:error response so they do not spend the same concurrency slot twice.
    """
    timeout = WS_REQUEST_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + timeout
    try:
        conn.settimeout(max(deadline - time.monotonic(), 0))
        conn.send(json.dumps(
            {"id": req_id, "type": "request", "action": action, "payload": payload}))
    except Exception as e:
        raise RequestOutcomeUnknown(
            f"{action} outcome unknown: connection failed while sending") from e
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RequestOutcomeUnknown(
                f"{action} outcome unknown: no response within {timeout:g}s")
        try:
            # Recompute on every loop. Unrelated pushed events must consume the same request
            # budget rather than refreshing a per-recv socket timeout.
            conn.settimeout(remaining)
            raw = conn.recv()
        except Exception as e:
            raise RequestOutcomeUnknown(
                f"{action} outcome unknown: no response within {timeout:g}s") from e
        if not raw:
            continue
        m = json.loads(raw)
        if m.get("id") != req_id:
            continue  # skip unrelated pushed events
        p = m.get("payload") or {}
        if m.get("type") == "error" or p.get("success") is False:
            raise RuntimeError(f"{action} failed: {p.get('message') or m}")
        return p

def ws_request(action, payload, req_id="pace-1"):
    """One WebSocket request on its own short-lived connection."""
    conn = _ws_connect()
    try:
        return _ws_call(conn, action, payload, req_id)
    finally:
        conn.close()

def ws_launch(task_id, intent, session_id, workflow_step_id, req_id="pace-1"):
    payload = {"task_id": task_id, "intent": intent, "workflow_step_id": workflow_step_id}
    if session_id:
        payload["session_id"] = session_id
    return ws_request("session.launch", payload, req_id)

def ws_set_state(task_id, state, req_id="state-1"):
    """Set the task's state (kandev's own `task.state` action, the one its MCP tools use)."""
    return ws_request("task.state", {"id": task_id, "state": state}, req_id)

def ws_drain(session_id, req_id="drain-1"):
    """Release the head queued prompt as this session's next turn.

    Returns kandev's payload; `drained` is False when the queue turned out to be empty (the
    UI's own "Send now" got there first), which spends no turn.
    """
    return ws_request("message.queue.drain", {"session_id": session_id}, req_id)

def queued_counts(task_sessions):
    """{session_id: pending queue entries} for every session a drain could target.

    One connection for the whole tick, and only sessions kandev would accept a prompt for are
    asked about -- a board of running or never-run tasks costs nothing. A failure degrades to
    "nothing queued" rather than killing the tick: the pacer then behaves exactly as it did
    before queue support (launch only), which loses pacing on a queued prompt but keeps the
    gate working. Halting all pacing over an unreadable queue would be the worse trade.
    """
    targets = []
    for sessions in task_sessions.values():
        newest = newest_session(sessions)
        if newest is not None and newest.get("state") in DRAINABLE_STATES:
            targets.append(newest["id"])
    if not targets:
        return {}
    try:
        conn = _ws_connect()
    except Exception as e:
        print(f"queue status unavailable ({e}) -> treating every queue as empty",
              file=sys.stderr, flush=True)
        return {}
    counts = {}
    try:
        for i, sid in enumerate(targets):
            try:
                p = _ws_call(conn, "message.queue.get", {"session_id": sid}, f"q{i}") or {}
                counts[sid] = int(p.get("count") or 0)
            except Exception as e:
                print(f"queue status failed for session {sid} ({e}) -> treating as empty",
                      file=sys.stderr, flush=True)
    finally:
        conn.close()
    return counts

# --- pacing loop ---

_BOOKED = {}             # key -> booked_at, or uncertain booking metadata (slot reserved)
_FAILED = {}             # (task_id, step_id, session_id) -> {at, attempts, reason, flagged}
_SCOPE = []              # workflow ids being paced
_WORK_STEPS = set()      # pace-eligible step ids, unioned across the scope
_QUEUE_STEPS = set()     # each scoped workflow's start step (its parked queue)
_ADMISSION = {}          # workflow id -> step id to admit a queued task into
_STEP_NAMES = {}         # step id -> name, for logging only
_WF_NAMES = {}           # workflow id -> name, for logging only
_STEP_RANKS = {}         # step id -> board position, for cross-column launch ordering
_PREV_SESSIONS = None    # {task_id: {session_id: state}} last tick; None = no baseline
_PREV_WINDOWS = []       # parse_windows() output last tick, for rollover detection

def label(task):
    """What to call a task in the log: its title, or its id when it has none."""
    return task.get("title") or task["id"]

def flag_blocked(task, reason):
    """Flag the task BLOCKED on the kandev board, so a stuck card is visible without stderr.

    Kandev renders that state as a "Blocked" warning icon on the card, and it is the same
    state its own MCP tools set, so nothing here is a private convention. It also clears
    itself: the next launch that works moves the task to SCHEDULING/IN_PROGRESS, whether the
    pacer or the user starts it.

    Returns whether the flag stuck. A failure to flag is logged and otherwise ignored --
    reporting a problem must never become a second problem that stops pacing.
    """
    try:
        ws_set_state(task["id"], BLOCKED_STATE, "pace-block")
    except Exception as e:
        print(f"could not flag {label(task)!r} {BLOCKED_STATE} ({e})", file=sys.stderr, flush=True)
        return False
    print(f"flagged {label(task)!r} {BLOCKED_STATE} on the board: {reason}",
          file=sys.stderr, flush=True)
    return True

def note_failure(key, task, reason):
    """Record a rejected launch/drain, back the task off, and flag it once it looks permanent.

    Deliberately not a booking. A booking says a turn was spent and the quota went with it; a
    rejected launch spent nothing. Keeping them apart is what lets the booking stay a plain
    "already launched, don't launch again" while failures carry their own retry schedule.
    """
    prev = _FAILED.get(key) or {}
    attempts = prev.get("attempts", 0) + 1
    observed_at = time.time()
    _FAILED[key] = {
        "at": observed_at, "attempts": attempts, "flagged": prev.get("flagged", False)}
    print(f"kandev refused to start {label(task)!r} ({reason}) -- failure {attempts}, "
          f"holding it for {fail_backoff(attempts)}s", file=sys.stderr, flush=True)
    log_event({"t": round(observed_at, 3), "kind": "failure", "task": task["id"],
               "reason": reason, "attempts": attempts})
    if BLOCK_AFTER and attempts >= BLOCK_AFTER and not _FAILED[key]["flagged"]:
        _FAILED[key]["flagged"] = flag_blocked(task, reason)

def note_uncertain(key, task, reason):
    """Reserve one slot when kandev may have acted but its response did not arrive."""
    observed_at = time.time()
    _BOOKED[key] = {"at": observed_at, "uncertain": True}
    _clear_failures(task["id"])
    print(f"kandev did not confirm start of {label(task)!r} ({reason}) -- "
          f"reserving its slot for {BOOKING_TTL}s pending board re-check",
          file=sys.stderr, flush=True)
    log_event({"t": round(observed_at, 3), "kind": "uncertain", "task": task["id"],
               "reason": reason})

def attempt(kind, task, key, wh, occupied, emit):
    """Carry out one selected action. True when the tick is finished, False to try the next task.

    False means only one thing: this candidate was rejected, no quota was spent, and the tick
    still has a turn to give someone else. It is never returned for a race (the task moved, the
    session started a turn) -- those mean the board changed under us, and re-reading everything
    next tick is the honest response.
    """
    # Re-read the task's current step immediately before launching. The workflow_step_id we
    # pass is an unvalidated move, so acting on the top-of-tick snapshot could drag a task
    # that was moved in between (human board edit, another agent's move_task) back to a stale
    # step. If it moved since selection, skip and re-evaluate next tick. Resolve the newest
    # live session now too -- a profile-switch step spawns a fresh one on entry.
    fresh = next((t for t in paced_tasks() if t["id"] == task["id"]), None)
    if fresh is None or fresh.get("workflow_step_id", "") != task.get("workflow_step_id", ""):
        print(f"headroom={wh:+.3f} open, {label(task)!r} moved since selection -> skip", flush=True)
        emit("open", "skipped")
        return True
    step = fresh.get("workflow_step_id", "")
    sessions = sessions_of(task["id"])

    if kind == "drain":
        # The re-read above only re-checked the step. The session must still be the one whose
        # queue we counted and still idle, or the drain would target a stale session or race a
        # turn that started since selection (kandev would reject it, but skipping is quieter).
        newest = newest_session(sessions)
        if newest is None or newest.get("state") not in DRAINABLE_STATES:
            print(f"headroom={wh:+.3f} open, {label(fresh)!r} no longer idle -> skip drain",
                  flush=True)
            emit("open", "skipped")
            return True
        # A rejected drain is handled here rather than by the loop's catch-all, which would
        # pause the gate for a tick. session_busy / session_not_promptable is usually a benign
        # race (a turn started since we counted). But drain outranks launch, so an entry that
        # fails *every* time would keep this task selected and starve every launch on the
        # board -- so it is recorded as a failure, backed off, and eventually flagged, exactly
        # like a failed launch.
        try:
            drained = bool((ws_drain(newest["id"]) or {}).get("drained"))
        except RequestOutcomeUnknown as e:
            note_uncertain((task["id"], step, newest["id"]), fresh, f"drain: {e}")
            emit("open", "uncertain",
                 {"task": task["id"], "session": newest["id"], "step": step})
            return True
        except Exception as e:
            note_failure(key, fresh, f"drain rejected: {e}")
            return False
        if not drained:
            # Queue emptied between the count and the take (the UI's "Send now", or another
            # client). No turn was spent, so don't book -- the task stays eligible.
            print(f"headroom={wh:+.3f} open, queue for {label(fresh)!r} already empty "
                  f"-> no turn spent", flush=True)
            emit("open", "empty")
            return True
        _BOOKED[(task["id"], step, newest["id"])] = time.time()
        _clear_failures(task["id"])
        print(f"headroom={wh:+.3f} open -> drained queued prompt for {label(fresh)!r} "
              f"at {named(step, _STEP_NAMES)!r} ({occupied + 1}/{CONCURRENCY})", flush=True)
        emit("open", "drained", {"task": task["id"], "session": newest["id"], "step": step})
        return True

    # Resolved here, not before the drain branch: a drain needs no launch target, so a workflow
    # without one must not block draining a prompt parked in it. A missing target is a failure
    # rather than a plain skip because it does not fix itself: the task is at the head of the
    # queue, stays launchable, and would be re-picked every tick while nothing else ran.
    target = launch_target(fresh, _WORK_STEPS, _ADMISSION)
    if target is None:
        note_failure(key, fresh, "no admission target in workflow "
                     f"{named(fresh.get('workflow_id'), _WF_NAMES)!r}")
        return False
    intent, sid = pick_intent(sessions)
    try:
        ws_launch(task["id"], intent, sid, target)
    except RequestOutcomeUnknown as e:
        note_uncertain((task["id"], step, sid), fresh, str(e))
        emit("open", "uncertain",
             {"task": task["id"], "intent": intent, "session": sid, "target": target})
        return True
    except Exception as e:
        # The case this exists for: kandev refusing the launch outright (a CREATED session with
        # no executor record answers "session is not resumable"). Before, that exception reached
        # main(), closed the gate, and left the same task at the head of the queue next tick --
        # one bad card and the whole board stopped.
        note_failure(key, fresh, str(e))
        return False
    _BOOKED[(task["id"], step, sid)] = time.time()
    _clear_failures(task["id"])
    print(f"headroom={wh:+.3f} open -> launched {label(fresh)!r} "
          f"intent={intent} workflow={named(fresh.get('workflow_id'), _WF_NAMES)!r} "
          f"target={named(target, _STEP_NAMES)!r} ({occupied + 1}/{CONCURRENCY})", flush=True)
    emit("open", "launched",
         {"task": task["id"], "intent": intent, "session": sid, "target": target})
    return True

def _clear_failures(task_id):
    """Forget a task's failure history once something worked for it.

    Has to be explicit: a successful launch leaves the key (task, step, session) untouched, so
    prune_failures cannot see the difference, and a stale backoff would then suppress a task
    that is working again.
    """
    for k in [k for k in _FAILED if k[0] == task_id]:
        del _FAILED[k]

def tick(was_open):
    global _BOOKED, _FAILED, _PREV_SESSIONS, _PREV_WINDOWS
    usage = _req("GET", "/api/v1/agents/usage")
    now = time.time()
    windows = parse_windows(usage)

    # Observe the whole board first, decide second. Returning early while the gate was shut
    # recorded nothing for exactly the period that matters most: turns keep running -- and
    # burning -- after the gate closes, and that is when the line gets crossed. Costs one
    # tasks call plus one sessions call per task per tick, against a local server.
    tasks = paced_tasks()
    task_sessions = {t["id"]: sessions_of(t["id"]) for t in tasks}
    snapshot = sessions_snapshot(task_sessions)
    for ev in diff_sessions(_PREV_SESSIONS, snapshot, now):
        log_event(ev)
    for ev in detect_resets(_PREV_WINDOWS, windows, now):
        log_event(ev)
    _PREV_SESSIONS, _PREV_WINDOWS = snapshot, windows

    _BOOKED = prune_booked(_BOOKED, tasks, task_sessions, now, BOOKING_TTL)
    _FAILED = prune_failures(_FAILED, tasks, task_sessions)
    running = sum(1 for t in tasks for s in task_sessions[t["id"]] if s.get("state") in RUNNING_STATES)
    occupied = running + len(_BOOKED)
    # Launch candidates only. Counting drains too would need queued_counts(), a WebSocket
    # round trip, on every tick -- including the paused ones this block exists to record.
    launchable = sum(1 for t in tasks
                     if is_launchable(t, task_sessions[t["id"]], _WORK_STEPS, _QUEUE_STEPS,
                                      set(_BOOKED) | backed_off(_FAILED, now)))
    wrecs = window_records(windows, now, ALPHA)

    def emit(gate, action, launched=None):
        log_event(tick_record(now, gate, action, wrecs, running, launchable,
                              CONCURRENCY, len(_BOOKED), launched))

    if not windows and provider_missing(usage, PROVIDER):
        print(f"provider {PROVIDER!r} not in usage {sorted(usage_providers(usage))} "
              f"-> WITHHOLDING (check PACER_PROVIDER)", flush=True)
        emit("blind", "withheld")
        return False  # fail closed: never pace freely against a quota we can't see
    wh = worst_headroom(windows, now, ALPHA)
    if ceiling_breached(windows, BACKSTOP):
        print(f"headroom={wh:+.3f} CEILING (consumed>{BACKSTOP:.2f}) -> withholding", flush=True)
        emit("ceiling", "withheld")
        return False
    is_open = gate_open(wh, was_open, RESUME_MARGIN)
    if not is_open:
        print(f"headroom={wh:+.3f} PAUSED ({running} running)", flush=True)
        emit("paused", "withheld")
        return is_open

    if occupied >= CONCURRENCY:
        pending = f", {len(_BOOKED)} pending" if _BOOKED else ""
        print(f"headroom={wh:+.3f} open, full ({occupied}/{CONCURRENCY}; "
              f"{running} running{pending})", flush=True)
        emit("open", "full")
        return is_open

    ordered = ordered_tasks(tasks, user_settings(), ORDER, _STEP_RANKS)
    queued = queued_counts(task_sessions)
    # Work down the candidate list rather than stopping at the first one: each rejection adds
    # the task to the backoff set, which is what makes the next select_action pick someone else.
    for _ in range(MAX_ATTEMPTS):
        skip = set(_BOOKED) | backed_off(_FAILED, now)
        kind, task = select_action(ordered, task_sessions, queued,
                                   _WORK_STEPS, _QUEUE_STEPS, skip)
        if kind is None:
            held = f", {len(_FAILED)} held after a failed launch" if _FAILED else ""
            print(f"headroom={wh:+.3f} open, nothing to launch "
                  f"({running}/{CONCURRENCY} running{held})", flush=True)
            emit("open", "nothing")
            return is_open
        if attempt(kind, task, action_key(task, task_sessions.get(task["id"], [])),
                   wh, occupied, emit):
            return is_open
    print(f"headroom={wh:+.3f} open, {MAX_ATTEMPTS} candidates rejected this tick "
          f"-> waiting for the next one", file=sys.stderr, flush=True)
    emit("open", "exhausted")
    return is_open

def resolve_globals(entries):
    """Fill the scope globals from [(workflow, steps)]. Returns the skipped list for reporting."""
    global _SCOPE, _WORK_STEPS, _QUEUE_STEPS, _ADMISSION, _STEP_NAMES, _WF_NAMES, _STEP_RANKS
    _SCOPE, _WORK_STEPS, _QUEUE_STEPS, _ADMISSION, skipped = resolve_scope(
        entries, PACED_WORKFLOWS, ADMISSION_OVERRIDE)
    _STEP_NAMES = {sid: name for _, steps in entries for sid, name in step_names(steps).items()}
    # Column order per board. Step ids are globally unique, so one flat dict serves every
    # workflow; the rank is an index within its own board, which is what pull order compares.
    _STEP_RANKS = {sid: rank for _, steps in entries for sid, rank in step_ranks(steps).items()}
    _WF_NAMES = {wf["id"]: wf.get("name") or wf["id"] for wf, _ in entries}
    return skipped

def _dump_order(entries, skipped):
    """Print the queue the pacer would work down, so the effect of a drag is checkable without
    waiting a tick (or spending quota) to see which task it picks."""
    settings = user_settings()
    manual = bool(settings) and (ORDER == "custom" or has_custom_sort_view(settings))
    placed = ((settings or {}).get("sidebar_task_prefs") or {}).get("ordered_task_ids") or []
    # Only report the placed count when the manual order is actually in use -- in board mode the
    # settings are never fetched, so "0 placed" would read as "you have dragged nothing".
    print(f"order={ORDER} -> " + ("manual sidebar order over board pull order "
                                  f"({len(placed)} task(s) placed by hand)" if manual
                                  else "board pull order (sidebar order not consulted)"))
    print(f"scope: {[named(w, _WF_NAMES) for w in _SCOPE]}")
    tasks = paced_tasks()
    for i, t in enumerate(ordered_tasks(tasks, settings, ORDER, _STEP_RANKS)):
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

def watch_usage(interval):
    """Poll usage on its own cadence and append `usage` records. Read-only; no launching.

    Run alongside the pacer to time how long after a turn starts utilization_pct actually
    moves. That dead time is what bounds any concurrency rule -- N parallel turns overshoot
    by N x burn_rate x dead_time -- and PACER_INTERVAL (120s) is far too coarse to resolve
    it. Separate process so the measurement cadence never perturbs the control loop.
    """
    print(f"watching {URL} usage every {interval}s -> {LOG_PATH or '(logging disabled)'}", flush=True)
    while True:
        now = time.time()
        try:
            wrecs = window_records(parse_windows(_req("GET", "/api/v1/agents/usage")), now, ALPHA)
            log_event({"t": round(now, 3), "kind": "usage", "windows": wrecs})
            print(f"{datetime.fromtimestamp(now):%H:%M:%S} "
                  + " ".join(f"{w['label']}={w['c']:.2f}" for w in wrecs), flush=True)
        except Exception as e:
            log_event({"t": round(now, 3), "kind": "error", "where": "watch_usage", "msg": str(e)})
            print(f"watch error: {e}", file=sys.stderr, flush=True)
        time.sleep(interval)

def main():
    if "--dump-usage" in sys.argv:
        print(json.dumps(_req("GET", "/api/v1/agents/usage"), indent=2))
        return
    if "--watch-usage" in sys.argv:
        i = sys.argv.index("--watch-usage") + 1
        watch_usage(int(sys.argv[i]) if len(sys.argv) > i else 30)
        return
    if ORDER not in ("auto", "custom", "board"):
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
          f"(alpha={ALPHA}, concurrency={CONCURRENCY}, provider={PROVIDER}, order={ORDER})\n"
          f"  log={LOG_PATH or '(disabled)'}", flush=True)
    log_event({"t": round(time.time(), 3), "kind": "start", "url": URL, "scope": list(_SCOPE),
               "interval": INTERVAL, "alpha": ALPHA, "cap": CONCURRENCY, "order": ORDER,
               "backstop": BACKSTOP, "resume_margin": RESUME_MARGIN, "provider": PROVIDER})
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
            log_event({"t": round(time.time(), 3), "kind": "error", "where": "tick", "msg": str(e)})
            log_event({"t": round(time.time(), 3), "kind": "error", "where": "tick", "msg": str(e)})
            was_open = False
        time.sleep(INTERVAL)

if __name__ == "__main__":
    sys.exit(main() or 0)
