"""What kandev's workflows mean to the pacer: which steps it may launch into, and why not.

Everything here is about the *shape of a board*, fixed until someone edits a workflow.
Deciding which task goes next, and whether the pacer may act on it, is selection.py.
"""

from dataclasses import dataclass, field

from .config import TRANSITION_KINDS


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


def admission_target(steps):
    """The step a task admitted from the parked queue lands on: the first work step."""
    work = [step for step in steps if is_paced_step(step)]
    return min(work, key=lambda step: step.get("position", 0))["id"] if work else None


@dataclass(frozen=True)
class Board:
    """What one startup scan of kandev's workflows established: who gets paced, and how.

    Fixed for the life of the process -- re-reading it per tick would mean re-linting
    and re-deriving admission targets every two minutes for a board that changes when
    a person edits a workflow. `steps` keeps the raw scan so startup can report on it.
    """

    # Every field defaults to empty -- that is a real state (nothing enrolled), and it
    # lets a test name only the parts it is about.
    scope: list = field(default_factory=list)          # workflow ids being paced
    work_steps: set = field(default_factory=set)       # pace-eligible step ids, all workflows
    queue_steps: set = field(default_factory=set)      # the parked start step of each board
    admission: dict = field(default_factory=dict)      # workflow id -> its admission step
    step_names: dict = field(default_factory=dict)     # step id -> name, for readable logs
    step_ranks: dict = field(default_factory=dict)     # step id -> position, for pull order
    workflow_names: dict = field(default_factory=dict)
    skipped: list = field(default_factory=list)        # (workflow, reason), out of scope
    steps: dict = field(default_factory=dict)          # workflow id -> its steps, as scanned

    @classmethod
    def scan(cls, entries):
        """Build from `client.workflow_entries()`: a (workflow, steps) pair per workflow."""
        scope, work, queues, admission, skipped = resolve_scope(entries)
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

    @staticmethod
    def label_of(workflow):
        """A workflow dict as its readable name. `skipped` holds these, not ids."""
        return workflow.get("name") or workflow["id"]


def resolve_scope(entries):
    """Split every workflow kandev knows into the paced ones and the reasons for the rest.

    Enrollment is the only opt-in: a workflow is paced exactly when `make-paced` has
    tagged a step, and `make-paced --unpace` is how it stops being paced.
    """
    scope, work, queues, admission, skipped = [], set(), set(), {}, []
    for workflow, steps in entries:
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
        target = admission_target(steps)
        if target:
            admission[workflow["id"]] = target
    return scope, work, queues, admission, skipped


def step_ranks(steps):
    return {step["id"]: step.get("position", 0) for step in steps}


def step_names(steps):
    return {step["id"]: step.get("name") or step["id"] for step in steps}


def _has_on_enter(step, kind):
    events = step.get("events") or {}
    return any(
        action.get("type") == kind for action in events.get("on_enter") or []
    )


def _bypass_warnings(step, name, is_paced):
    """Ways a step can start a turn behind the pacer's back, whatever else it is."""
    if is_paced and step.get("wip_limit"):
        # kandev queues the overflow rather than refusing it, and a queued card created
        # with a launch request carries metadata.deferred_launch -- kandev starts it
        # itself the moment a slot frees, which is a turn the pacer never gated.
        yield (
            f"work step {name!r} has wip_limit={step['wip_limit']} -- kandev queues "
            "overflow there and may auto-start it on promotion (deferred_launch)"
        )
    if _has_on_enter(step, "auto_start_agent"):
        yield f"step {name!r} still has on_enter:auto_start_agent (bypasses the gate)"
    if step.get("agent_profile_id") and (step.get("prompt") or "").strip():
        yield (
            f"step {name!r} has both an agent-profile override and a prompt "
            "(auto-launch bypass)"
        )


def _classification_warning(step, name, is_paced):
    """The one thing to say about how this step is tagged versus how it behaves.

    At most one, deliberately: a step is either paced, work-shaped but tagged
    something else, or tagged work while parked. Saying two of those about the same
    step would just be the same confusion described twice.
    """
    stage = (step.get("stage_type") or "").lower()
    routes = is_work_step(step)
    if is_paced:
        if step.get("auto_advance_requires_signal"):
            return f"work step {name!r} gates routing on the agent step_complete signal"
        return None
    if routes and stage in ("review", "approval"):
        return (
            f"step {name!r} routes on on_turn_complete (work-shaped) "
            f"but is tagged stage_type={stage!r}"
        )
    if routes:
        return (
            f"step {name!r} routes on on_turn_complete but is tagged "
            f"stage_type={stage or 'custom'!r} -> NOT paced "
            "(make-paced tags it 'work')"
        )
    if stage == "work":
        return (
            f"step {name!r} is tagged stage_type='work' but has no "
            "on_turn_complete transition (parked)"
        )
    return None


def _shape_warnings(steps, works):
    """Ways the workflow as a whole cannot support pacing, whatever its steps say."""
    if not works:
        yield (
            "no pace-eligible step (stage_type=work + on_turn_complete transition) "
            "-- nothing to pace"
        )
    if admission_target(steps) is None:
        yield "no admission target derivable"
    queue = queue_step_id(steps)
    if queue is None:
        yield "no start step (parked queue) found"
    elif queue in works:
        yield (
            f"start step {step_names(steps).get(queue, queue)!r} is itself "
            "pace-eligible -- the parked queue will execute rather than hold "
            "(tasks never wait there)"
        )


def lint_workflow(steps):
    """Everything suspicious about a workflow the pacer is about to run against."""
    works = paced_step_ids(steps)
    warnings = []
    for step in steps:
        name = step.get("name", "?")
        is_paced = step["id"] in works
        warnings.extend(_bypass_warnings(step, name, is_paced))
        classification = _classification_warning(step, name, is_paced)
        if classification:
            warnings.append(classification)
    warnings.extend(_shape_warnings(steps, works))
    return warnings
