"""Workflow classification, task ordering, and launch eligibility."""

from dataclasses import dataclass

from .config import (
    CREATED_STATE,
    DRAINABLE_STATES,
    FAIL_BACKOFF_MAX_MULT,
    IDLE_STATES,
    PRIORITY_RANK,
    READY_STATE,
    TRANSITION_KINDS,
    settings,
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


@dataclass(frozen=True)
class Board:
    """What one startup scan of kandev's workflows established: who gets paced, and how.

    Fixed for the life of the process -- re-reading it per tick would mean re-linting
    and re-deriving admission targets every two minutes for a board that changes when
    a person edits a workflow. `steps` keeps the raw scan so startup can report on it.
    """

    scope: list          # workflow ids being paced
    work_steps: set      # every pace-eligible step id, across all workflows
    queue_steps: set     # the parked start step of each, where admission picks tasks up
    admission: dict      # workflow id -> the step a queued task is admitted into
    step_names: dict     # step id -> name, for readable log lines
    step_ranks: dict     # step id -> position, for the board's pull order
    workflow_names: dict
    skipped: list        # (workflow, reason) for everything out of scope
    steps: dict          # workflow id -> its steps, as scanned

    @classmethod
    def scan(cls, entries, restrict=(), override=None):
        """Build from `client.workflow_entries()`: a (workflow, steps) pair per workflow."""
        scope, work, queues, admission, skipped = resolve_scope(entries, restrict, override)
        return cls(
            scope=scope,
            work_steps=work,
            queue_steps=queues,
            admission=admission,
            step_names={
                step_id: name
                for _, steps in entries
                for step_id, name in step_names(steps).items()
            },
            step_ranks={
                step_id: rank
                for _, steps in entries
                for step_id, rank in step_ranks(steps).items()
            },
            workflow_names={
                workflow["id"]: workflow.get("name") or workflow["id"]
                for workflow, _ in entries
            },
            skipped=skipped,
            steps={workflow["id"]: steps for workflow, steps in entries},
        )

    def name_of(self, id_):
        """A step or workflow id as its readable name, or the id when unknown."""
        return self.step_names.get(id_) or self.workflow_names.get(id_, id_)


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
                    "on turn complete -- run make-paced to enroll it",
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


def lint_workflow(steps, enrolled_only=True, admission_override=None):
    if admission_override is None:
        admission_override = settings.admission_step_id
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
                "(make-paced tags it 'work')"
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
    if step in work_steps:
        return state in IDLE_STATES
    return step in queue_steps and (newest is None or state in IDLE_STATES)


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


def effective_sidebar_view(sidebar):
    views = sidebar.get("sidebar_views") or []
    active = next(
        (
            view
            for view in views
            if view.get("id") == sidebar.get("sidebar_active_view_id")
        ),
        None,
    )
    active = active or (views[0] if views else None)
    if active is None:
        return None
    draft = sidebar.get("sidebar_draft") or None
    if draft and draft.get("base_view_id") == active.get("id"):
        return {
            **active,
            "filters": draft.get("filters"),
            "sort": draft.get("sort"),
            "group": draft.get("group"),
        }
    return active


def has_custom_sort_view(sidebar):
    views = list(sidebar.get("sidebar_views") or [])
    effective = effective_sidebar_view(sidebar)
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


def ordered_tasks(tasks, sidebar, mode, ranks=None):
    base = sorted(tasks, key=lambda task: task_order_key(task, ranks or {}))
    if mode == "board" or not sidebar:
        return base
    if mode != "custom" and not has_custom_sort_view(sidebar):
        return base
    prefs = sidebar.get("sidebar_task_prefs") or {}
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
    base = settings.fail_backoff if base is None else base
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
        if key[2] is None or state == CREATED_STATE:
            if now - booked[key] < ttl:
                kept[key] = booked[key]
        elif state == READY_STATE:
            kept[key] = booked[key]
    return kept
