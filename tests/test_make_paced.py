"""Pure planning tests for make_paced.py (no kandev)."""

from kandev_pacer.make_paced import (
    SIGNAL_INSTRUCTION,
    ensure_signal_instruction,
    plan_changes,
    plan_unpace,
    planned_steps,
    strip_auto_start,
    target_stage,
)
from kandev_pacer.workflow import SIGNAL_TOOL


def _step(id, name, events=None, stage="custom", prompt="", signal=0):
    return {"id": id, "name": name, "events": events or {}, "stage_type": stage,
            "is_start_step": id == "bk", "position": 0, "agent_profile_id": "",
            "prompt": prompt, "auto_advance_requires_signal": signal}

# The live pre-reconfig shape: auto-start plus a completion route on one step.
DIRTY_EVENTS = {"on_enter": [{"type": "auto_start_agent"}],
                "on_turn_complete": [{"type": "move_to_step", "config": {"step_id": "x"}}]}

def test_strip_auto_start():
    ev, n = strip_auto_start(DIRTY_EVENTS)
    assert n == 1
    assert "on_enter" not in ev                                   # emptied trigger dropped
    assert ev["on_turn_complete"] == DIRTY_EVENTS["on_turn_complete"]  # routing untouched
    assert strip_auto_start({}) == ({}, 0)
    assert strip_auto_start(None) == ({}, 0)
    # only the auto_start_agent action goes; siblings under the same trigger stay
    ev, n = strip_auto_start({"on_enter": [{"type": "auto_start_agent"}, {"type": "add_label"}]})
    assert n == 1 and ev == {"on_enter": [{"type": "add_label"}]}

def test_target_stage():
    review = frozenset({"review"})
    otc = {"on_turn_complete": [{"type": "move_to_next"}]}
    assert target_stage(_step("ip", "In Progress", otc), review) == "work"
    assert target_stage(_step("rv", "Review"), review) == "review"
    assert target_stage(_step("rv", "  REVIEW  "), review) == "review"      # case/space-insensitive
    assert target_stage(_step("bk", "Backlog"), review) == "custom"
    # routing wins over the name: a routing step named Review is still 'work'
    assert target_stage(_step("rv", "Review", otc), review) == "work"
    # a hand-set valid parked tag is kept, not clobbered to 'custom'
    assert target_stage(_step("ap", "Sign-off", stage="approval"), review) == "approval"
    assert target_stage(_step("rv", "QA", stage="review"), review) == "review"
    # but a wrong 'work' tag on a parked step is reset to 'custom'
    assert target_stage(_step("bk", "Backlog", stage="work"), review) == "custom"
    # --approval names tag a parked step 'approval'; routing still wins
    approval = frozenset({"sign-off"})
    assert target_stage(_step("ap", "Sign-off"), review, approval) == "approval"
    assert target_stage(_step("ap", "Sign-off", otc), review, approval) == "work"
    # an explicit --review name overrides a hand-set 'approval'
    assert target_stage(_step("rv", "Review", stage="approval"), review) == "review"

def test_plan_changes():
    steps = [_step("bk", "Backlog"),
             _step("ip", "In Progress", DIRTY_EVENTS, stage="custom"),
             _step("rv", "Review", {"on_turn_start": [{"type": "move_to_step"}]}),
             _step("dn", "Done", {"on_turn_start": [{"type": "move_to_step"}]})]
    changes = plan_changes(steps)
    assert [c["id"] for c in changes] == ["ip", "rv"]             # Backlog/Done already correct
    ip, rv = changes
    assert ip["removed"] == 1 and ip["stage_to"] == "work" and "on_enter" not in ip["events"]
    assert rv["removed"] == 0 and rv["stage_to"] == "review"
    # extra --review names are honored
    assert [c["stage_to"] for c in plan_changes(steps, frozenset({"review", "done"}))
            if c["id"] == "dn"] == ["review"]
    # applying the plan makes a second plan empty (idempotent)
    assert plan_changes(planned_steps(steps, changes)) == []

def test_planned_steps_merge():
    steps = [_step("ip", "In Progress", DIRTY_EVENTS), _step("bk", "Backlog")]
    after = planned_steps(steps, plan_changes(steps))
    assert [s["id"] for s in after] == ["ip", "bk"]               # order and untouched steps kept
    assert after[0]["stage_type"] == "work" and "on_enter" not in after[0]["events"]
    assert after[1] == steps[1]


def test_plan_unpace_clears_only_the_work_tag():
    """Unpacing is how a board stops being paced, so it has to undo exactly enrollment."""
    steps = [
        _step("bk", "Backlog", stage="custom"),
        _step(
            "ip",
            "In Progress",
            {"on_turn_complete": [{"type": "move_to_step"}]},
            stage="work",
        ),
        _step("rv", "Review", stage="review"),
    ]
    changes = plan_unpace(steps)
    assert [(c["id"], c["stage_from"], c["stage_to"]) for c in changes] == [
        ("ip", "work", "custom")
    ]
    # The events are carried through untouched: auto_start_agent stays stripped, so an
    # unpaced board is manual, not self-starting.
    assert changes[0]["events"] == {"on_turn_complete": [{"type": "move_to_step"}]}
    assert changes[0]["removed"] == 0
    # Nothing tagged work -> nothing to do, so a second --unpace is a no-op.
    assert plan_unpace(planned_steps(steps, changes)) == []
    # The prompt rides through untouched -- including a signal instruction a previous
    # enrolment appended. See plan_unpace for why it is not peeled back out.
    gated = _step("ip", "In Progress", {"on_turn_complete": [{"type": "move_to_next"}]},
                  stage="work", prompt=f"Do it.\n\n{SIGNAL_INSTRUCTION}", signal=1)
    assert plan_unpace([gated])[0]["prompt"] == gated["prompt"]


# --- the signal instruction (a silent gated prompt strands the card) ---

OTC = {"on_turn_complete": [{"type": "move_to_next"}]}


def test_ensure_signal_instruction():
    # A gated work step with no prompt gets the bare instruction.
    assert ensure_signal_instruction(_step("x", "X", OTC, signal=1), "work") == (
        SIGNAL_INSTRUCTION, True)
    # An existing prompt keeps its text; the instruction lands after a blank line.
    prompt, added = ensure_signal_instruction(
        _step("x", "X", OTC, prompt="Do the work.\n", signal=1), "work")
    assert added and prompt == f"Do the work.\n\n{SIGNAL_INSTRUCTION}"
    # Already mentioning the tool anywhere means hands off, byte for byte.
    said = f"Finish, then call {SIGNAL_TOOL}."
    assert ensure_signal_instruction(
        _step("x", "X", OTC, prompt=said, signal=1), "work") == (said, False)
    # Ungated work step: turn completion alone advances it, nothing to instruct.
    assert ensure_signal_instruction(_step("x", "X", OTC, prompt="p"), "work") == ("p", False)
    # Parked step: never routes, so the gate never binds -- even if the flag is set.
    assert ensure_signal_instruction(_step("rv", "Review", signal=1), "review") == ("", False)
    # A NULL prompt that needs nothing stays NULL rather than becoming ''.
    step = {**_step("x", "X", OTC), "prompt": None}
    assert ensure_signal_instruction(step, "work") == (None, False)


def test_plan_changes_appends_signal_instruction():
    # Enrolled and gated, but the prompt never names the tool: the retrofit case --
    # nothing else about the step changes, and the plan still has to pick it up.
    steps = [_step("ip", "In Progress", OTC, stage="work", prompt="Implement.", signal=1)]
    (change,) = plan_changes(steps)
    assert change["signal_added"] and change["removed"] == 0
    assert change["stage_to"] == change["stage_from"] == "work"
    assert change["prompt"] == f"Implement.\n\n{SIGNAL_INSTRUCTION}"
    # planned_steps carries the new prompt, so applying the plan makes a second one
    # empty (idempotent) -- the instruction is never appended twice.
    assert plan_changes(planned_steps(steps, [change])) == []
    # A fresh enrolment (custom -> work) gets the instruction in the same pass.
    fresh = [_step("ip", "In Progress", OTC, prompt="Implement.", signal=1)]
    (change,) = plan_changes(fresh)
    assert change["signal_added"] and change["stage_to"] == "work"
    # An ungated board plans exactly as before: no prompt edits.
    ungated = [_step("ip", "In Progress", OTC, prompt="Implement.")]
    (change,) = plan_changes(ungated)
    assert not change["signal_added"] and change["prompt"] == "Implement."
