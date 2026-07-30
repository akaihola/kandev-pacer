"""Tests for kandev_pacer/workflow.py: step classification, scope, and the startup lint.

No network.
"""

from conftest import CLEAN, ENTRIES, FEATURE_DEV
from conftest import step as _step

from kandev_pacer.workflow import (
    admission_target,
    is_paced_step,
    is_work_step,
    lint_workflow,
    paced_step_ids,
    queue_step_id,
    resolve_scope,
    step_names,
)


def test_paced_step_ids():
    # only In Progress routes on complete
    assert paced_step_ids(CLEAN) == {"ip"}
    assert is_work_step(_step("x", "X", otc=["move_to_next"]))      # every transition kind counts
    assert is_work_step(_step("x", "X", otc=["move_to_previous"]))
    assert is_work_step(_step("x", "X", otc=["move_to_step"]))
    # on_turn_start is not routing-away
    assert not is_work_step(_step("x", "X", ots=["move_to_step"]))

def test_is_paced_step_needs_both_halves():
    """The tag is the enrollment marker; the routing is the safety property. Neither alone.

    Tag without routing would relaunch the same task every tick forever (the step never routes
    away, so it reads idle after each turn) -- an infinite prompt loop billed to the quota.
    Routing without the tag is an un-enrolled workflow the pacer merely discovered.
    """
    assert is_paced_step(_step("x", "X", otc=["move_to_next"], stage="work"))
    assert not is_paced_step(_step("x", "X", stage="work"))                    # tagged, parked
    assert not is_paced_step(_step("x", "X", otc=["move_to_next"]))            # routes, untagged
    assert not is_paced_step(_step("x", "X", otc=["move_to_next"], stage="review"))
    # tag is case-insensitive
    assert is_paced_step(_step("x", "X", otc=["move_to_next"], stage="WORK"))

def test_step_names():
    names = step_names(CLEAN)
    assert names["ip"] == "In Progress"
    # An unknown id has to stay identifiable in the log rather than vanish.
    assert names.get("gone", "gone") == "gone"
    assert step_names([{"id": "x"}])["x"] == "x"     # nameless step falls back to its id


def test_resolve_scope_enrolls_only_tagged_workflows():
    scope, work, queues, admission, skipped = resolve_scope(ENTRIES)
    assert scope == ["wf-ck", "wf-fd"]                 # Priority Kanban is not enrolled
    assert work == {"ip", "fd-spec", "fd-work"}        # unioned; step ids are globally unique
    assert queues == {"bk", "fd-todo"}                 # one start step per scoped workflow
    # lowest-position paced step, per board
    assert admission == {"wf-ck": "ip", "wf-fd": "fd-spec"}
    assert [wf["name"] for wf, _ in skipped] == ["Priority Kanban"]
    assert "make-paced" in skipped[0][1]               # the reason is actionable, not just a drop

def test_resolve_scope_never_paces_an_untagged_queue():
    """Priority Kanban's Backlog routes on turn complete, so the routing-only classifier would
    treat that parked queue as a work step and start turns on tasks merely sitting there."""
    _, work, _, _, _ = resolve_scope(ENTRIES)
    assert "pk-bk" not in work
    assert "pk-ip" not in work

def test_resolve_scope_gives_each_workflow_its_own_admission_step():
    """Admission is per workflow: a queued task has to enter a step of its own board."""
    _, _, _, admission, _ = resolve_scope(ENTRIES)
    # Each lands on its own lowest-position work step: Feature Dev admits into Spec, not Work.
    assert admission == {"wf-ck": "ip", "wf-fd": "fd-spec"}

def test_queue_and_admission_target():
    assert queue_step_id(CLEAN) == "bk"
    assert admission_target(CLEAN) == "ip"                      # lowest-position paced step
    assert admission_target([_step("a", "A", otc=["move_to_next"], stage="work", pos=5),
                             _step("b", "B", otc=["move_to_next"], stage="work", pos=2)]) == "b"
    assert admission_target([_step("bk", "Backlog", start=True)]) is None  # no paced step
    # Work-shaped but untagged is not an admission target either -- admitting a task into an
    # un-enrolled step would start turns the pacer then cannot track as paced work.
    assert admission_target([_step("a", "A", otc=["move_to_next"])]) is None

# --- lint ---

def test_lint_workflow():
    # Live (pre-reconfig) In Progress still auto-starts -> flagged.
    dirty = [_step("ip", "In Progress", oe=["auto_start_agent"], otc=["move_to_step"],
                   stage="work", pos=0)]
    assert any("auto_start_agent" in w for w in lint_workflow(dirty))
    # profile switch + prompt on a work step -> bypass.
    ps = [
        _step(
            "ip",
            "In Progress",
            otc=["move_to_step"],
            stage="work",
            profile="p",
            prompt="do it",
        )
    ]
    assert any("agent-profile" in w for w in lint_workflow(ps))
    # tagged work but parked (no transition) -> mismatch.
    assert any("stage_type='work'" in w for w in lint_workflow([_step("x", "X", stage="work")]))
    # routes on complete but tagged review -> mismatch.
    assert any("tagged stage_type" in w for w in lint_workflow(
        [_step("x", "X", otc=["move_to_step"], stage="review")]))
    # signal-gated work step -> flagged.
    assert any("step_complete" in w for w in lint_workflow(
        [_step("x", "X", otc=["move_to_step"], stage="work", signal=True)]))
    # Work-shaped but untagged -> NOT paced. This is the warning that explains a task sitting
    # on such a step forever; it names the fix rather than just reporting the state.
    untagged = lint_workflow([_step("x", "X", otc=["move_to_step"])])
    assert any("NOT paced" in w and "make-paced" in w for w in untagged)
    # P2: a PARKED step (no on_turn_complete) leaks too -- auto_start_agent / profile+prompt
    # auto-launch on entry regardless of classification, so both must be flagged there.
    assert any("auto_start_agent" in w for w in lint_workflow(
        [_step("rv", "Review", oe=["auto_start_agent"], ots=["move_to_step"])]))
    assert any("agent-profile" in w for w in lint_workflow(
        [_step("rv", "Review", ots=["move_to_step"], profile="p", prompt="review it")]))
    # A start step that is itself pace-eligible has no holding area, so the backlog executes
    # instead of queueing. This is the shape Priority Kanban takes once make_paced tags it --
    # every other check passes, so without this one the hazard ships silently.
    executing_queue = [
        _step("bk", "Backlog", otc=["move_to_step"], stage="work", start=True, pos=0),
        _step("ip", "In Progress", otc=["move_to_step"], stage="work", pos=1),
    ]
    assert any("parked queue will execute" in w for w in lint_workflow(executing_queue))
    # clean workflow -> no warnings.
    assert lint_workflow(CLEAN) == []
    assert lint_workflow(FEATURE_DEV) == []
