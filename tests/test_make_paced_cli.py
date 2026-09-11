"""make-paced end to end, against a fake kandev step API.

The pure planning is covered in test_make_paced.py; this exercises the glue that talks
to kandev -- listing, resolving, reading steps, and the PUT per changed step -- with
`client.get`/`client.put` replaced by an in-memory board that records every write.
"""
import copy
import sys

import pytest

from kandev_pacer import client
from kandev_pacer.make_paced import main, resolve_workflow, step_update

WORK_EVENTS = {"on_enter": [{"type": "auto_start_agent"}],
               "on_turn_complete": [{"type": "move_to_step", "config": {"step_id": "x"}}]}


class FakeSteps:
    """Just enough of kandev: one workflow list, steps by workflow, partial PUT."""

    def __init__(self):
        self.workflows = [{"id": "w1", "name": "Paced Kanban"}]
        self.steps = {"w1": [
            {"id": "bk", "workflow_id": "w1", "name": "Backlog", "position": 0,
             "events": {}, "is_start_step": True, "stage_type": "custom",
             "auto_advance_requires_signal": False},
            {"id": "ip", "workflow_id": "w1", "name": "In Progress", "position": 1,
             "events": copy.deepcopy(WORK_EVENTS), "is_start_step": False,
             "stage_type": "custom", "auto_advance_requires_signal": False},
            {"id": "rv", "workflow_id": "w1", "name": "Review", "position": 2,
             "events": {}, "is_start_step": False, "stage_type": "custom",
             "auto_advance_requires_signal": False},
        ]}
        self.puts = []

    def get(self, path):
        if path == "/api/v1/workflows":
            return {"workflows": self.workflows}
        workflow_id = path.split("/")[4]
        return {"steps": copy.deepcopy(self.steps[workflow_id])}

    def put(self, path, body):
        self.puts.append((path, body))
        step_id = path.rsplit("/", 1)[1]
        step = next(s for steps in self.steps.values() for s in steps if s["id"] == step_id)
        step.update(copy.deepcopy(body))  # a partial update, like kandev's
        return {"step": step}

    def step(self, step_id):
        return next(s for s in self.steps["w1"] if s["id"] == step_id)

    def stages(self):
        return {s["name"]: s["stage_type"] for s in self.steps["w1"]}


@pytest.fixture
def kandev(monkeypatch):
    fake = FakeSteps()
    monkeypatch.setattr(client, "get", fake.get)
    monkeypatch.setattr(client, "put", fake.put)
    return fake


def _run(*args):
    """Run make-paced's main() as the command line would."""
    sys.argv = ["make-paced", *args]
    main()


def test_resolve_by_name_or_id_and_errors_exit():
    workflows = [{"id": "w1", "name": "Paced Kanban"}]
    assert resolve_workflow(workflows, "paced kanban")["id"] == "w1"  # case-insensitive
    assert resolve_workflow(workflows, "w1")["id"] == "w1"            # exact id
    workflows.append({"id": "w2", "name": "Paced Kanban"})
    for needle in ("nope", "Paced Kanban"):                          # missing, ambiguous
        with pytest.raises(SystemExit):
            resolve_workflow(workflows, needle)


def test_cli_lists_workflows_when_given_no_name(kandev, capsys):
    _run()
    assert capsys.readouterr().out.splitlines() == ["w1  Paced Kanban"]


def test_cli_enrols_unpaces_and_is_idempotent_both_ways(kandev, capsys):
    """The whole lifecycle a user drives, one PUT per changed step and nothing else."""
    _run("Paced Kanban")
    assert "dry run; pass --apply to write" in capsys.readouterr().out
    assert kandev.puts == [], "a dry run must not write"

    # Two steps change: In Progress becomes work, and the step named Review is tagged
    # review by default. Each PUT names only the fields that move.
    _run("Paced Kanban", "--apply")
    assert "applied 2 step update(s)" in capsys.readouterr().out
    assert kandev.puts == [
        ("/api/v1/workflow/steps/ip",
         {"events": {"on_turn_complete": WORK_EVENTS["on_turn_complete"]},
          "stage_type": "work"}),
        ("/api/v1/workflow/steps/rv", {"events": {}, "stage_type": "review"}),
    ]
    assert kandev.step("ip")["stage_type"] == "work"

    _run("Paced Kanban", "--apply")
    assert "already paced; nothing to do" in capsys.readouterr().out

    kandev.puts.clear()
    _run("Paced Kanban", "--unpace", "--apply")
    out = capsys.readouterr().out
    assert "stage_type work -> custom" in out and "no longer paced" in out
    assert kandev.puts == [("/api/v1/workflow/steps/ip", {"stage_type": "custom"})]

    _run("Paced Kanban", "--unpace", "--apply")
    assert "not paced; nothing to do" in capsys.readouterr().out


def test_cli_tags_a_named_step_for_review(kandev, capsys):
    _run("Paced Kanban", "--review", "Backlog", "--apply")
    assert kandev.stages() == {"Backlog": "review", "In Progress": "work", "Review": "review"}


def test_cli_appends_signal_instruction_to_gated_prompt(kandev, capsys):
    """A signal-gated work step whose prompt never names the tool gets the instruction.

    This is the retrofit path too: the step is gated in kandev's own settings, so an
    already-enrolled board picks up the instruction on the next --apply.
    """
    kandev.step("ip").update(auto_advance_requires_signal=True, prompt="Do the work.")

    _run("Paced Kanban", "--apply")
    out = capsys.readouterr().out
    assert "append the step_complete_kandev instruction to the prompt" in out
    prompt = kandev.step("ip")["prompt"]
    assert prompt.startswith("Do the work.") and "step_complete_kandev" in prompt

    # Idempotent: the stored prompt now mentions the tool, so nothing is re-appended.
    _run("Paced Kanban", "--apply")
    assert "already paced; nothing to do" in capsys.readouterr().out


def test_prompt_is_sent_only_when_it_changes():
    """kandev omits an empty prompt from GET; sending `prompt: null` back would be a write."""
    change = {"id": "ip", "events": {}, "stage_to": "work", "prompt": None,
              "signal_added": False}
    assert step_update(change, unpace=False) == {"events": {}, "stage_type": "work"}


def test_cli_exits_with_a_hint_when_kandev_is_down(monkeypatch, capsys):
    def down(path):
        raise client.ReadFailed(f"GET {path}: connection refused")

    monkeypatch.setattr(client, "get", down)
    with pytest.raises(SystemExit) as raised:
        _run("Paced Kanban")
    assert "Is kandev running" in str(raised.value)
