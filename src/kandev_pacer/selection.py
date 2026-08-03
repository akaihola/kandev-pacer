"""Which task goes next, whether the pacer may act on it, and what it has already spent.

All pure: these functions take the board scan, the tasks and sessions one tick read, and
the pacer's own booking/failure records, and answer with a decision. No I/O.
"""

from typing import NamedTuple

from .config import (
    CREATED_STATE,
    DRAINABLE_STATES,
    FAIL_BACKOFF_MAX_MULT,
    IDLE_STATES,
    PRIORITY_RANK,
    READY_STATE,
    settings,
)


def priority_rank(task):
    return PRIORITY_RANK.get(
        (task.get("priority") or "").strip().lower(), PRIORITY_RANK["none"]
    )


# A step id the workflow no longer has: sorted after every real column rather than
# before it, so a stale task waits instead of jumping the queue.
UNKNOWN_STEP = float("inf")


def task_order_key(task, ranks):
    """The board's own pull order: rightmost column first, then the drag order within it.

    Positions are scoped to a step, so comparing them across columns is meaningless --
    the column has to be decided first. Negating the position sorts the rightmost
    (most downstream) column first, which is kanban pull: finish what is in flight
    before admitting anything new.
    """
    step = task.get("workflow_step_id", "")
    column = -ranks[step] if step in ranks else UNKNOWN_STEP
    return (
        column,
        task.get("position") or 0,     # the drag order inside that column
        priority_rank(task),
        task.get("created_at") or "",
        task.get("id") or "",          # keeps the order total, so ties never flap
    )


def newest_session(sessions):
    return max(sessions, key=lambda session: session.get("started_at", "")) if sessions else None


def is_wip_admitted(task):
    """Whether kandev has admitted this card into its step, rather than queueing it there.

    Absent -- or null -- means admitted, deliberately: the field arrived in kandev 0.84,
    and an older build, or any endpoint that leaves it out, must not read as a board where
    nothing is admitted, which would stall the pacer completely. Only an explicit false
    holds a card back.
    """
    admitted = task.get("wip_admitted")
    return admitted is None or bool(admitted)


def is_launchable(task, sessions, board, skip):
    """Whether starting a turn on this task now is both allowed and meaningful."""
    step = task.get("workflow_step_id", "")
    newest = newest_session(sessions)
    session_id = newest["id"] if newest else None
    if ActionKey(task["id"], step, session_id) in skip:
        return False
    # A card kandev queued because the step's WIP limit was full sits on that step with
    # no session -- indistinguishable, from everything below, from a card waiting to be
    # launched. Launching it would defeat the limit kandev is holding it behind.
    if not is_wip_admitted(task):
        return False
    state = newest["state"] if newest else None
    # A sessionless task counts on a work step too, not only in the queue. kandev moves a
    # task to the requested step *before* it validates that the launch can happen (state ->
    # SCHEDULING, then move, then the agent-profile check), so an admission it rejects
    # strands the card on the work step with no session. Requiring an idle session there
    # made that state permanently unlaunchable -- nothing creates the session afterwards,
    # so the card was skipped on every later tick while the log said "nothing to launch".
    # Four subtasks of a Heartbeat task sat like that for ~7.75h.
    if step in board.work_steps or step in board.queue_steps:
        return newest is None or state in IDLE_STATES
    return False


def drain_session(task, sessions, queued, skip=frozenset()):
    """The session whose queued prompt this task should drain, or None.

    A task can hold several idle sessions, and the queue lives on whichever one the user
    had open -- often the primary session created with the task, not the newest. Picking
    the newest and reading its (empty) queue made a task with a waiting prompt look like
    it had none, so the drain never fired: the card stayed parked while the pacer spent
    its slots launching tasks further down the order. Among the idle sessions that do
    have a queue, the newest wins.
    """
    step = task.get("workflow_step_id", "")
    holding = [
        session
        for session in sessions
        if session.get("state") in DRAINABLE_STATES
        and queued.get(session["id"], 0) > 0
        and ActionKey(task["id"], step, session["id"]) not in skip
    ]
    return newest_session(holding)


def is_drainable(task, sessions, queued, skip):
    """Whether this task has a user prompt parked and waiting to be released."""
    return drain_session(task, sessions, queued, skip) is not None


class Candidate(NamedTuple):
    """The one action a tick will spend its turn on."""

    kind: str            # "drain" or "launch"
    task: dict
    # For a drain, the session holding the queue -- which is not always the task's
    # newest. None for a launch, which resumes the newest.
    session: str | None = None


def select_action(ordered, task_sessions, queued, board, skip):
    """The one turn to spend now, or None.

    Drains come first, in full: launching a task that has a prompt queued spends the
    turn on the step prompt and lets the queued entry leak out un-paced afterwards.
    """
    for task in ordered:
        session = drain_session(task, task_sessions.get(task["id"], []), queued, skip)
        if session is not None:
            return Candidate("drain", task, session["id"])
    for task in ordered:
        if is_launchable(task, task_sessions.get(task["id"], []), board, skip):
            return Candidate("launch", task)
    return None


def launch_target(task, board):
    """The step to launch the task into: where it already sits, or its board's admission step."""
    step = task.get("workflow_step_id", "")
    return step if step in board.work_steps else board.admission.get(task.get("workflow_id", ""))


def task_agent_profile(task):
    """The task's own agent profile from its launch metadata, or None.

    The pacer has no opinion about which agent runs a task -- it passes this back verbatim
    so kandev's intent:start path can find it. That path needs the help: it resolves the
    *executor* from task metadata but not the agent profile, and no profile means the
    launch is refused outright ("task has no agent_profile_id configured"). The prepare
    path does resolve it, so the same task can be prepared and yet not started.

    Costs no extra request: it rides along in the task list every tick already fetches.
    """
    profile = (task.get("metadata") or {}).get("agent_profile_id")
    return profile if isinstance(profile, str) and profile else None


def pick_intent(sessions):
    newest = newest_session(sessions)
    return ("start", None) if newest is None else ("workflow_step", newest["id"])


def effective_sidebar_view(sidebar):
    """The sidebar view the user is actually looking at.

    An unsaved draft wins over the view it is based on: what is on screen is what the
    user means, even if they have not saved it.
    """
    views = sidebar.get("sidebar_views") or []
    active = next(
        (view for view in views if view.get("id") == sidebar.get("sidebar_active_view_id")),
        views[0] if views else None,
    )
    if active is None:
        return None
    draft = sidebar.get("sidebar_draft") or {}
    if draft.get("base_view_id") != active.get("id"):
        return active
    return {**active, **{key: draft.get(key) for key in ("filters", "sort", "group")}}


def has_custom_sort_view(sidebar):
    """Whether any sidebar view sorts by hand -- i.e. whether a drag order exists to follow."""
    views = [*(sidebar.get("sidebar_views") or []), effective_sidebar_view(sidebar)]
    # `or {}` twice, not .get(default): the payload carries explicit nulls.
    return any(((view or {}).get("sort") or {}).get("key") == "custom" for view in views)


def uses_manual_order(sidebar):
    """Whether the sidebar drag order overrides the board's pull order this run.

    It does exactly when some sidebar view sorts by hand -- that is what asking for a
    drag order looks like. Shared by the pacer and by --dump-order, so the report cannot
    claim one thing while the loop does another.
    """
    return bool(sidebar) and has_custom_sort_view(sidebar)


def _rank(tasks, ordered_ids):
    """Tasks in the order `ordered_ids` gives; anything unlisted keeps its place, at the end."""
    index = {task_id: position for position, task_id in enumerate(ordered_ids)}
    # Unlisted tasks all score len(index), and sorted() is stable, so they stay in
    # their incoming order behind every listed one.
    return sorted(tasks, key=lambda task: index.get(task["id"], len(index)))


def _tree(tasks):
    """Split tasks into (roots, children-by-parent).

    A task counts as a root unless its parent is here too: a subtask whose parent
    sits in another workflow, or is its own parent, has nothing to nest under and
    would otherwise never be reached.
    """
    ids = {task["id"] for task in tasks}
    roots, children = [], {}
    for task in tasks:
        parent = task.get("parent_id") or ""
        if parent and parent in ids and parent != task["id"]:
            children.setdefault(parent, []).append(task)
        else:
            roots.append(task)
    return roots, children


def visible_task_order(tasks, pinned_ids, ordered_ids, subtask_order=None):
    """The sidebar's own reading order.

    Pinned first, then dragged, each parent immediately before its children.
    """
    subtask_order = subtask_order or {}
    roots, children = _tree(tasks)
    ordered, seen = [], set()

    def visit(task):
        # No repeat-visit guard: _tree() puts each task in exactly one place, either
        # roots or one parent's children, so nothing here is reachable twice.
        seen.add(task["id"])
        ordered.append(task)
        for child in _rank(children.get(task["id"], []), subtask_order.get(task["id"]) or []):
            visit(child)

    # Two passes, outermost last: the drag order first, then pinned tasks floated
    # above it. Pinning is itself a visible re-ordering, so it has to win.
    dragged = _rank(roots, ordered_ids)
    for task in _rank(dragged, pinned_ids):
        visit(task)
    # Tasks in a parent loop (A's parent is B, B's parent is A) are nobody's root, so
    # nothing above reaches them. They still have to launch.
    ordered.extend(task for task in tasks if task["id"] not in seen)
    return ordered


def ordered_tasks(tasks, sidebar, ranks=None):
    """The queue launches work down: the board's pull order, with your drag order on top."""
    base = sorted(tasks, key=lambda task: task_order_key(task, ranks or {}))
    if not uses_manual_order(sidebar):
        return base
    prefs = sidebar.get("sidebar_task_prefs") or {}
    return visible_task_order(
        base,
        prefs.get("pinned_task_ids") or [],
        prefs.get("ordered_task_ids") or [],
        prefs.get("subtask_order_by_parent_id") or {},
    )


class ActionKey(NamedTuple):
    """What a booking or a failure is *about*.

    Not just the task id: the moment the task moves step, or gets a new session, this
    is a different key, so the booking that stopped a second launch -- or the failure
    that held the task back -- stops applying by itself. `session` is None only for a
    task with no session at all.
    """

    task: str
    step: str
    session: str | None


def action_key(task, sessions, session_id=None):
    """The key a booking or failure about this task is filed under.

    `session_id` names the session explicitly: a drain books against the session that
    holds the queue, which is not always the newest. Without it, the newest is used --
    that is the session a launch resumes.
    """
    if session_id is None:
        newest = newest_session(sessions)
        session_id = newest["id"] if newest else None
    return ActionKey(task["id"], task.get("workflow_step_id", ""), session_id)


def live_keys(tasks, task_sessions):
    return {
        action_key(task, task_sessions.get(task["id"], [])): (
            newest_session(task_sessions.get(task["id"], [])) or {}
        ).get("state")
        for task in tasks
    }


def fail_backoff(attempts):
    """Seconds to hold a task after `attempts` consecutive refusals: doubling, then flat."""
    return settings.fail_backoff * min(2 ** max(attempts - 1, 0), FAIL_BACKOFF_MAX_MULT)


def backed_off(failures, now):
    """The action keys still inside their backoff window."""
    return {
        key
        for key, failure in failures.items()
        if now - failure["at"] < fail_backoff(failure["attempts"])
    }


def prune_failures(failures, tasks, task_sessions):
    """Forget failures whose situation no longer exists -- the task moved, or was re-sessioned."""
    live = live_keys(tasks, task_sessions)
    return {key: value for key, value in failures.items() if key in live}


# --- stalls: turns that changed nothing ---
#
# A failure is kandev refusing a launch. A stall is kandev accepting one, running the
# turn, and the turn having nothing to show for it -- which is what a *finished* task
# looks like from here, over and over. The two need separate records: a failure backs
# off and retries, while a stall is the pacer concluding the card needs a person.


def workspace_moved(previous, current):
    """Whether something changed between two launches of the same action.

    True whenever the pacer cannot be sure it did not. `None` is progress.py's "cannot
    say", so it never compares equal -- not even to another `None` -- and an unreadable
    worktree can never accumulate towards a stall. The first launch of an action has no
    `previous` and is progress by the same rule.
    """
    return previous is None or current is None or previous != current


def note_stall(stalls, key, fingerprint):
    """The stall record for `key` after one more launch, given the workspace right now.

    `count` is how many consecutive turns have left the workspace alone, so it is still
    0 on the launch that first observes a repeat -- the turn that changed nothing has
    only just been paid for.
    """
    previous = stalls.get(key) or {}
    if workspace_moved(previous.get("fingerprint"), fingerprint):
        return {"fingerprint": fingerprint, "count": 0, "flagged": False}
    return {**previous, "fingerprint": fingerprint, "count": previous.get("count", 0) + 1}


def stalled_out(stalls, key, limit):
    """Whether `key` has been launched `limit` times over with the workspace standing still."""
    return bool(limit) and (stalls.get(key) or {}).get("count", 0) >= limit


def stalled_keys(stalls, limit):
    """Every action the pacer has given up on, for this tick's skip set."""
    return {key for key in stalls if stalled_out(stalls, key, limit)}


def prune_stalls(stalls, tasks, task_sessions):
    """Forget stalls whose situation no longer exists -- the task moved, or was re-sessioned.

    This is the whole recovery path, and it needs no separate release: moving the card
    off the step, or giving the task a new session, changes the key, and the card is
    launchable again on the next tick.
    """
    live = live_keys(tasks, task_sessions)
    return {key: value for key, value in stalls.items() if key in live}


class Booking(NamedTuple):
    """A turn the pacer has spent, holding that task's slot until the board catches up.

    One shape for both kinds: a confirmed launch or drain, and a request kandev may have
    acted on whose answer never arrived (`uncertain`).
    """

    at: float            # when the turn was spent
    uncertain: bool = False


def booking_still_applies(state, age, ttl, uncertain=False):
    """Whether a booking is still doing its job, given the session's state now.

    A booking exists to stop the pacer spending a second turn on something it has
    already paid for. Three cases, and they are the whole rule:

    - no session yet, `CREATED`, or an unconfirmed request -- the launch was made but the
      agent has not visibly picked it up. Hold, but only while the session is still idle
      and only up to `ttl`: if the turn never appears, something went wrong out of view
      and the task has to become launchable again.
    - `WAITING_FOR_INPUT` -- the turn ran and finished. Held, but bounded by `ttl` too: a
      launch kandev accepted and never turned into a turn leaves the session at READY
      under the same key, so nothing else clears the booking. Without the bound a no-op
      launch reserved a slot forever and the card, launchable in every other respect, was
      skipped on every later tick.
    - anything else (`STARTING`/`RUNNING`) -- the turn is visibly in flight. The
      booking has done its job; the concurrency count takes over from here.
    """
    if age >= ttl:
        return False
    if state is None or state == CREATED_STATE or uncertain:
        return state is None or state in IDLE_STATES
    return state == READY_STATE


def prune_booked(booked, tasks, task_sessions, now, ttl):
    """Drop the bookings that have stopped meaning anything.

    A booking whose key is not on the board any more is gone by that alone: the task moved,
    or the session it named is not the one the task shows now.
    """
    live = live_keys(tasks, task_sessions)
    return {
        key: booking
        for key, booking in booked.items()
        if key in live
        and booking_still_applies(live[key], now - booking.at, ttl, booking.uncertain)
    }
