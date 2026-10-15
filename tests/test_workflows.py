"""Pace real file workflows without backend-specific selection or transitions."""

import copy

import pytest
from test_t3 import setup, tracker  # noqa: F401

from kandev_pacer import heartbeat, t3, tasks_md

hai = pytest.importorskip("hai_tasks")


@pytest.fixture(params=["tasks", "scratch", "backlog"])
def workflow(request, tmp_path):
    root = tmp_path / "workflow"
    path = tracker(root, backlog="- [1] First\n- [2] Blocked\n    - Depends on: [1]\n")
    if request.param == "tasks":
        source = path
    else:
        path.unlink()
        if request.param == "scratch":
            source = root / ".scratch/feature"
            folder = source / "issues"
            folder.mkdir(parents=True)
            for number, state, extra in [
                (1, "ready-for-agent", ""),
                (2, "open", "Blocked by: 01\n"),
                (3, "ready-for-human", ""),
                (4, "resolved", ""),
            ]:
                (folder / f"{number:02}-ticket.md").write_text(
                    f"# Ticket {number}\nStatus: {state}\n{extra}"
                )
        else:
            source = root / "docs/backlog"
            source.mkdir()
            (source / "first.md").write_text("# First\n")
            (root / "docs/completed").mkdir()
            (root / "docs/completed/done.md").write_text("# Done\n")
    tasks_md.git(root, "add", "--all")
    tasks_md.git(root, "commit", "--allow-empty", "-m", "workflow fixture")
    return root, source


def test_eligibility_scheduling_and_heartbeat(workflow):
    root, source = workflow
    store = hai.open_source(source)
    before = store.read()
    chosen = t3.eligible(before)
    assert len(chosen) == 1
    revision, items = tasks_md.tracker(source)
    item = next(i for i in items if i["id"] == chosen[0]["id"])
    assert tasks_md.eligible(item, items)
    found, _ = heartbeat.sources(root, "")
    assert {i["id"] for i in found} == {i["id"] for i in items}
    result = tasks_md.move(source, item["key"], revision, "Scheduled", "main")
    after = store.read()
    scheduled = tasks_md.state_for(before, "scheduled")
    if scheduled:
        assert after["revision"] != before["revision"]
        assert (
            next(i for i in after["workspace"]["issues"] if i["id"] == item["id"])[
                "sourceState"
            ]
            == scheduled
        )
    else:
        assert after == before
    assert result["revision"] == after["revision"]
    assert t3.eligible(after)[0]["id"] == chosen[0]["id"]
    assert tasks_md.git(root, "status", "--porcelain") == ""
    assert heartbeat.coverage({**item, "external_id": "test"}, [])[0] == "free"
    payload = heartbeat.create(
        {
            "repositories": [
                {
                    "path": str(root),
                    "items": [
                        {
                            **item,
                            "eligible": True,
                            "coverage": "free",
                            "external_id": "test",
                        }
                    ],
                    "revisions": {},
                }
            ],
            "quota": {"profile": "test"},
            "ids": {
                "workspace": "ws",
                "workflows": {"complex": "wf"},
                "profiles": {"test": "profile"},
                "executor": "executor",
            },
        },
        root,
        item["key"],
        "Problem and test criteria",
    )
    assert tasks_md.workflow_prompt(before) in payload["arguments"]["description"]


def test_t3_launch_all_workflows(workflow, setup):  # noqa: F811
    root, source = workflow
    pacer, _, server = setup
    pacer.config.t3_trackers = [{"tracker": str(source), "parent_thread": "parent"}]
    server.data["projects"][0]["workspaceRoot"] = str(root)
    pacer = t3.T3Pacer(pacer.config, server)
    before = hai.open_source(source).read()
    pacer.tick()
    assert len(server.calls) == 1
    command = server.calls[0]
    assert command["bootstrap"]["prepareWorktree"]["projectCwd"] == str(root)
    prompt = command["message"]["text"]
    assert tasks_md.workflow_prompt(before) in prompt
    assert ("Do not check user acceptance" in prompt) == before["capabilities"][
        "fields"
    ]["acceptance"]
    after = hai.open_source(source).read()
    if tasks_md.state_for(before, "scheduled") is None:
        assert before == after


def test_dependencies_require_done_in_same_project(workflow):
    _, source = workflow
    store = hai.open_source(source)
    snapshot = store.read()
    if not snapshot["capabilities"]["fields"]["dependencies"]:
        return
    first = t3.eligible(snapshot)[0]
    store.mutate(
        "update",
        snapshot["revision"],
        id=first["id"],
        input={"sourceState": tasks_md.state_for(snapshot, "done")},
    )
    after = store.read()
    assert len(t3.eligible(after)) == 1
    dependent = t3.eligible(after)[0]
    assert dependent["dependencies"]
    # A missing prerequisite, or one completed in another project, cannot unblock it.
    for variant in ("missing", "other-project", "unfinished"):
        altered = copy.deepcopy(after)
        prerequisite = next(
            i for i in altered["workspace"]["issues"] if i["id"] == first["id"]
        )
        if variant == "missing":
            altered["workspace"]["issues"].remove(prerequisite)
        elif variant == "other-project":
            prerequisite["projectId"] = "other"
        else:
            prerequisite["sourceState"] = tasks_md.state_for(after, "active")
        assert not t3.eligible(altered)
