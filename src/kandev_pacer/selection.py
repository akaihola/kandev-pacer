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


def is_launchable(task, sessions, board, skip):
    """Whether starting a turn on this task now is both allowed and meaningful."""
    step = task.get("workflow_step_id", "")
    newest = newest_session(sessions)
    session_id = newest["id"] if newest else None
    if ActionKey(task["id"], step, session_id) in skip:
        return False
    state = newest["state"] if newest else None
    if step in board.work_steps:
        return state in IDLE_STATES
    return step in board.queue_steps and (newest is None or state in IDLE_STATES)


def is_drainable(task, sessions, queued, skip):
    """Whether this task has a user prompt parked and waiting to be released."""
    newest = newest_session(sessions)
    if newest is None or newest.get("state") not in DRAINABLE_STATES:
        return False
    if queued.get(newest["id"], 0) <= 0:
        return False
    return ActionKey(task["id"], task.get("workflow_step_id", ""), newest["id"]) not in skip


def select_action(ordered, task_sessions, queued, board, skip):
    """The one turn to spend now, or (None, None).

    Drains come first, in full: launching a task that has a prompt queued spends the
    turn on the step prompt and lets the queued entry leak out un-paced afterwards.
    """
    for task in ordered:
        if is_drainable(task, task_sessions.get(task["id"], []), queued, skip):
            return "drain", task
    for task in ordered:
        if is_launchable(task, task_sessions.get(task["id"], []), board, skip):
            return "launch", task
    return None, None


def launch_target(task, board):
    """The step to launch the task into: where it already sits, or its board's admission step."""
    step = task.get("workflow_step_id", "")
    return step if step in board.work_steps else board.admission.get(task.get("workflow_id", ""))


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


def uses_manual_order(sidebar, mode):
    """Whether the sidebar drag order overrides the board's pull order this run.

    Shared by the pacer and by --dump-order, so the report cannot claim one thing
    while the loop does another.
    """
    if mode == "board" or not sidebar:
        return False
    return mode == "custom" or has_custom_sort_view(sidebar)


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


def ordered_tasks(tasks, sidebar, mode, ranks=None):
    """The queue launches work down: the board's pull order, with your drag order on top."""
    base = sorted(tasks, key=lambda task: task_order_key(task, ranks or {}))
    if not uses_manual_order(sidebar, mode):
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


def action_key(task, sessions):
    newest = newest_session(sessions)
    return ActionKey(
        task["id"],
        task.get("workflow_step_id", ""),
        newest["id"] if newest else None,
    )


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


def booking_still_applies(state, age, ttl):
    """Whether a booking is still doing its job, given the session's state now.

    A booking exists to stop the pacer spending a second turn on something it has
    already paid for. Three cases, and they are the whole rule:

    - no session yet, or `CREATED` -- the launch was made but the agent has not
      visibly picked it up. Hold, but only up to `ttl`: if the turn never appears,
      something went wrong out of view and the task has to become launchable again.
    - `WAITING_FOR_INPUT` -- the turn ran and finished. Hold with no expiry, or the
      next tick would launch the same session at the same step all over again.
    - anything else (`STARTING`/`RUNNING`) -- the turn is visibly in flight. The
      booking has done its job; the concurrency count takes over from here.
    """
    if state is None or state == CREATED_STATE:
        return age < ttl
    return state == READY_STATE


def prune_booked(booked, tasks, task_sessions, now, ttl):
    """Drop the bookings that have stopped meaning anything."""
    return {
        key: booked[key]
        for key, state in live_keys(tasks, task_sessions).items()
        # `state` is None exactly when the task has no session, which is the same
        # thing key.session being None says -- both come from the one newest_session().
        if key in booked and booking_still_applies(state, now - booked[key], ttl)
    }
