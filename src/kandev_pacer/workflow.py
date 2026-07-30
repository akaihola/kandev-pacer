"""Workflow classification, task ordering, and launch eligibility."""

from .config import (
    ADMISSION_OVERRIDE,
    CREATED_STATE,
    DRAINABLE_STATES,
    FAIL_BACKOFF,
    FAIL_BACKOFF_MAX_MULT,
    IDLE_STATES,
    PRIORITY_RANK,
    READY_STATE,
    TRANSITION_KINDS,
)


def is_work_step(step):
    events = step.get("events") or {}
    return any(
        action.get("type") in TRANSITION_KINDS
        for action in events.get("on_turn_complete") or []
    )


def is_paced_step(step):
    return (step.get("stage_type") or "").lower() == "work" and is_work_step(step)


def paced_step_ids(steps):
    return {step["id"] for step in steps if is_paced_step(step)}


def queue_step_id(steps):
    return next(
        (step["id"] for step in steps if step.get("is_start_step")),
        None,
    )


def admission_target(steps, override=None):
    if override:
        return override
    work = [step for step in steps if is_paced_step(step)]
    return min(work, key=lambda step: step.get("position", 0))["id"] if work else None


def resolve_scope(entries, restrict=(), override=None):
    wanted = [value.lower() for value in restrict]
    scope, work, queues, admission, skipped = [], set(), set(), {}, []
    for workflow, steps in entries:
        if (
            wanted
            and workflow["id"].lower() not in wanted
            and (workflow.get("name") or "").lower() not in wanted
        ):
            skipped.append((workflow, "not named in PACED_WORKFLOW_ID"))
            continue
        eligible = paced_step_ids(steps)
        if not eligible:
            skipped.append(
                (
                    workflow,
                    "not enrolled: no step is tagged stage_type=work *and* routes "
                    "on turn complete -- run make_paced.py to enroll it",
                )
            )
            continue
        scope.append(workflow["id"])
        work |= eligible
        queue = queue_step_id(steps)
        if queue:
            queues.add(queue)
        own_override = override if override in {step["id"] for step in steps} else None
        target = admission_target(steps, own_override)
        if target:
            admission[workflow["id"]] = target
    return scope, work, queues, admission, skipped


def step_ranks(steps):
    return {step["id"]: step.get("position", 0) for step in steps}


def step_names(steps):
    return {step["id"]: step.get("name") or step["id"] for step in steps}


def named(id_, names):
    return names.get(id_, id_)


def _has_on_enter(step, kind):
    events = step.get("events") or {}
    return any(
        action.get("type") == kind for action in events.get("on_enter") or []
    )


def lint_workflow(steps, enrolled_only=True, admission_override=ADMISSION_OVERRIDE):
    works = paced_step_ids(steps)
    warnings = []
    for step in steps:
        name = step.get("name", "?")
        stage = (step.get("stage_type") or "").lower()
        routes = is_work_step(step)
        if _has_on_enter(step, "auto_start_agent"):
            warnings.append(
                f"step {name!r} still has on_enter:auto_start_agent (bypasses the gate)"
            )
        if step.get("agent_profile_id") and (step.get("prompt") or "").strip():
            warnings.append(
                f"step {name!r} has both an agent-profile override and a prompt "
                "(auto-launch bypass)"
            )
        if step["id"] in works:
            if step.get("auto_advance_requires_signal"):
                warnings.append(
                    f"work step {name!r} gates routing on the agent step_complete signal"
                )
        elif routes and stage in ("review", "approval"):
            warnings.append(
                f"step {name!r} routes on on_turn_complete (work-shaped) "
                f"but is tagged stage_type={stage!r}"
            )
        elif routes and enrolled_only:
            warnings.append(
                f"step {name!r} routes on on_turn_complete but is tagged "
                f"stage_type={stage or 'custom'!r} -> NOT paced "
                "(make_paced.py tags it 'work')"
            )
        elif stage == "work":
            warnings.append(
                f"step {name!r} is tagged stage_type='work' but has no "
                "on_turn_complete transition (parked)"
            )
    if not enrolled_only:
        return warnings
    if not works:
        warnings.append(
            "no pace-eligible step (stage_type=work + on_turn_complete transition) "
            "-- nothing to pace"
        )
    if admission_target(steps, admission_override) is None:
        warnings.append("no admission target derivable")
    queue = queue_step_id(steps)
    if queue is None:
        warnings.append("no start step (parked queue) found")
    elif queue in works:
        warnings.append(
            f"start step {named(queue, step_names(steps))!r} is itself pace-eligible "
            "-- the parked queue will execute rather than hold (tasks never wait there)"
        )
    return warnings


def priority_rank(task):
    return PRIORITY_RANK.get(
        (task.get("priority") or "").strip().lower(), PRIORITY_RANK["none"]
    )


def task_order_key(task, ranks):
    step = task.get("workflow_step_id", "")
    return (
        -ranks.get(step, -1),
        task.get("position") or 0,
        priority_rank(task),
        task.get("created_at") or "",
        task.get("id") or "",
    )


def newest_session(sessions):
    return max(sessions, key=lambda session: session.get("started_at", "")) if sessions else None


def is_launchable(task, sessions, work_steps, queue_steps, skip):
    step = task.get("workflow_step_id", "")
    newest = newest_session(sessions)
    session_id = newest["id"] if newest else None
    if (task["id"], step, session_id) in skip:
        return False
    state = newest["state"] if newest else None
    # A sessionless task counts on a work step too, not only in the queue. Kandev moves a task
    # to the requested step *before* it validates that the launch can happen (startTask: state
    # -> SCHEDULING, moveTaskToWorkflowStep, then the agent-profile check), so an admission it
    # rejects strands the card on the work step with no session. Requiring an idle session made
    # that state permanently unlaunchable -- nothing creates the session afterwards, so the card
    # was skipped on every later tick while the log said "nothing to launch".
    if step in work_steps or step in queue_steps:
        return newest is None or state in IDLE_STATES
    return False


def is_drainable(task, sessions, queued, skip):
    newest = newest_session(sessions)
    if newest is None or newest.get("state") not in DRAINABLE_STATES:
        return False
    if queued.get(newest["id"], 0) <= 0:
        return False
    key = (task["id"], task.get("workflow_step_id", ""), newest["id"])
    return key not in skip


def select_action(ordered, task_sessions, queued, work_steps, queue_steps, skip):
    for task in ordered:
        if is_drainable(task, task_sessions.get(task["id"], []), queued, skip):
            return "drain", task
    for task in ordered:
        if is_launchable(
            task, task_sessions.get(task["id"], []), work_steps, queue_steps, skip
        ):
            return "launch", task
    return None, None


def launch_target(task, work_steps, admission):
    step = task.get("workflow_step_id", "")
    return step if step in work_steps else admission.get(task.get("workflow_id", ""))


def pick_intent(sessions):
    newest = newest_session(sessions)
    return ("start", None) if newest is None else ("workflow_step", newest["id"])


def task_agent_profile(task):
    """The task's own agent profile from its launch metadata, or None.

    The pacer has no opinion about which agent runs a task -- it passes this back verbatim so
    kandev's intent:start path can find it. That path needs the help: it resolves the *executor*
    from task metadata but not the agent profile, and no profile means the launch is refused
    outright ("task has no agent_profile_id configured"). The prepare path does resolve it, so
    the same task can be prepared and yet not started.

    That refusal is expensive, not merely noisy: kandev sets the task SCHEDULING and moves it to
    the target step *before* the check, so a refused admission strands the card on a work step
    with no session. Four subtasks of the Heartbeat task sat like that for ~7.75h.

    Kanban tasks carry the value already -- the UI sets it on create, and create_task_kandev
    requires one (resolveMCPLaunchMetadata) -- so reading it here is enough, and it costs no
    extra request: it rides along in the task list every tick already fetches.
    """
    metadata = task.get("metadata") or {}
    profile = metadata.get("agent_profile_id")
    return profile if isinstance(profile, str) and profile else None


def effective_sidebar_view(settings):
    views = settings.get("sidebar_views") or []
    active = next(
        (
            view
            for view in views
            if view.get("id") == settings.get("sidebar_active_view_id")
        ),
        None,
    )
    active = active or (views[0] if views else None)
    if active is None:
        return None
    draft = settings.get("sidebar_draft") or None
    if draft and draft.get("base_view_id") == active.get("id"):
        return {
            **active,
            "filters": draft.get("filters"),
            "sort": draft.get("sort"),
            "group": draft.get("group"),
        }
    return active


def has_custom_sort_view(settings):
    views = list(settings.get("sidebar_views") or [])
    effective = effective_sidebar_view(settings)
    if effective:
        views.append(effective)
    return any((view.get("sort") or {}).get("key") == "custom" for view in views)


def _rank(tasks, ordered_ids):
    index = {task_id: position for position, task_id in enumerate(ordered_ids)}
    listed = sorted(
        (task for task in tasks if task["id"] in index),
        key=lambda task: index[task["id"]],
    )
    return listed + [task for task in tasks if task["id"] not in index]


def visible_task_order(tasks, pinned_ids, ordered_ids, subtask_order=None):
    subtask_order = subtask_order or {}
    ids = {task["id"] for task in tasks}
    roots, children = [], {}
    for task in tasks:
        parent = task.get("parent_id") or ""
        if parent and parent in ids and parent != task["id"]:
            children.setdefault(parent, []).append(task)
        else:
            roots.append(task)
    ordered, seen = [], set()

    def visit(task):
        if task["id"] in seen:
            return
        seen.add(task["id"])
        ordered.append(task)
        child_order = subtask_order.get(task["id"]) or []
        for child in _rank(children.get(task["id"], []), child_order):
            visit(child)

    for task in _rank(_rank(roots, ordered_ids), pinned_ids):
        visit(task)
    ordered.extend(task for task in tasks if task["id"] not in seen)
    return ordered


def ordered_tasks(tasks, settings, mode, ranks=None):
    base = sorted(tasks, key=lambda task: task_order_key(task, ranks or {}))
    if mode == "board" or not settings:
        return base
    if mode != "custom" and not has_custom_sort_view(settings):
        return base
    prefs = settings.get("sidebar_task_prefs") or {}
    return visible_task_order(
        base,
        prefs.get("pinned_task_ids") or [],
        prefs.get("ordered_task_ids") or [],
        prefs.get("subtask_order_by_parent_id") or {},
    )


def action_key(task, sessions):
    newest = newest_session(sessions)
    return (
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


def fail_backoff(attempts, base=None):
    base = FAIL_BACKOFF if base is None else base
    return base * min(2 ** max(attempts - 1, 0), FAIL_BACKOFF_MAX_MULT)


def backed_off(failures, now, base=None):
    return {
        key
        for key, failure in failures.items()
        if now - failure["at"] < fail_backoff(failure["attempts"], base)
    }


def prune_failures(failures, tasks, task_sessions):
    live = live_keys(tasks, task_sessions)
    return {key: value for key, value in failures.items() if key in live}


def prune_booked(booked, tasks, task_sessions, now, ttl):
    kept = {}
    for key, state in live_keys(tasks, task_sessions).items():
        if key not in booked:
            continue
        booking = booked[key]
        booked_at = booking["at"] if isinstance(booking, dict) else booking
        uncertain = isinstance(booking, dict) and booking.get("uncertain")
        if key[2] is None or state == CREATED_STATE or uncertain:
            if (state in IDLE_STATES or state is None) and now - booked_at < ttl:
                kept[key] = booking
        # A launch kandev accepted but never turned into a turn leaves the session sitting at
        # READY with the same (task, step, session) key, so the booking has nothing to clear it.
        # The TTL bounds that wait here too: without it a no-op launch reserved a slot forever
        # and the card -- launchable in every other respect -- was skipped on every later tick.
        elif state == READY_STATE and now - booked_at < ttl:
            kept[key] = booking
    return kept
