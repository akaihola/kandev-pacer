"""Real Hai trackers and Git commits; no paid turns or live credentials."""

import copy
import json
import shlex
import subprocess
import time

import pytest

from kandev_pacer import t3
from kandev_pacer.config import Settings
from kandev_pacer.quota import Verdict

hai_tasks = pytest.importorskip("hai_tasks")
TasksStore = hai_tasks.TasksStore


class Server:
    url = "http://localhost:3773"

    def __init__(self):
        self.data = {"projects": [], "threads": []}
        self.calls = []
        self.lose_reply = False
        self.reject = 0
        self.broken = set()
        self.leftover_branches = set()

    def snapshot(self):
        return copy.deepcopy(self.data)

    def call(self, method, command):
        assert method == "orchestration.dispatchCommand"
        self.calls.append(copy.deepcopy(command))
        worktree = command["bootstrap"]["prepareWorktree"]
        branch = worktree["branch"]
        if (
            self.reject
            or branch in self.leftover_branches
            or worktree["projectCwd"] in self.broken
        ):
            # Like T3 0.0.44, a failed bootstrap deletes the thread but keeps the
            # worktree branch, so the same branch name can never be created again.
            self.reject = max(self.reject - 1, 0)
            self.leftover_branches.add(branch)
            raise t3.T3Rejected("T3 rejected the RPC: worktree setup failed")
        if not any(t["id"] == command["threadId"] for t in self.data["threads"]):
            self.data["threads"].append(
                {
                    **command["bootstrap"]["createThread"],
                    "id": command["threadId"],
                    "worktreePath": "/isolated/worktree",
                    "branch": "pacer-branch",
                    "session": {"status": "starting", "activeTurnId": None},
                    "latestTurn": None,
                }
            )
        if self.lose_reply:
            raise TimeoutError("reply lost")
        return {}


def tracker(root, backlog="- [1] First\n- [2] Second\n", completed=""):
    root.mkdir()
    (root / "docs/tasks").mkdir(parents=True)
    for i in range(1, 5):
        (root / f"docs/tasks/{i}.md").write_text(f"Description {i}\n")
    path = root / "TASKS.md"
    path.write_text(
        "## Unverified proposals\n\n- [4] Proposal\n\n## Ordered backlog\n\n"
        + backlog
        + "\n## Scheduled\n\n## In Progress\n\n## Completed / Accepted\n\n"
        + completed
        + "\n"
        + "\n".join(f"[{i}]: docs/tasks/{i}.md" for i in range(1, 5))
        + "\n\n## Rules\n\nRun tests before completion.\n"
    )
    for args in [
        ["init", "-b", "main"],
        ["config", "user.name", "Test"],
        ["config", "user.email", "test@example.com"],
        ["config", "commit.gpgsign", "false"],
        ["add", "."],
        ["commit", "-m", "initial"],
    ]:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return path


def add_tracker(pacer, server, root):
    """Configure another tracker, with its own T3 project and parent thread."""
    path = tracker(root)
    pacer.trackers[str(path)] = (TasksStore(path), "parent2")
    server.data["projects"].append({"id": "project2", "workspaceRoot": str(root)})
    server.data["threads"].append(
        {**server.data["threads"][0], "id": "parent2", "projectId": "project2"}
    )
    return path


def held_lines(caplog):
    return [r.getMessage() for r in caplog.records if "T3 HELD" in r.getMessage()]


def project_of(command):
    return command["bootstrap"]["prepareWorktree"]["projectCwd"]


@pytest.fixture
def clock(monkeypatch):
    # Whole seconds, so stored timestamps compare exactly at the cooldown boundary.
    now = [float(int(time.time()))]
    monkeypatch.setattr(t3.time, "time", lambda: now[0])
    return now


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tracker(tmp_path / "repo")
    server = Server()
    server.data["projects"] = [{"id": "project", "workspaceRoot": str(path.parent)}]
    server.data["threads"] = [
        {
            "id": "parent",
            "projectId": "project",
            "modelSelection": {
                "provider": "codex",
                "model": "gpt-5.3-codex",
                "options": {"reasoningEffort": "high"},
            },
            "runtimeMode": "full-access",
            "interactionMode": "default",
            "session": {"status": "idle"},
            "latestTurn": None,
        }
    ]
    config = Settings(
        PACER_PROVIDER="openai",
        PACER_T3_TRACKERS=[{"tracker": str(path), "parent_thread": "parent"}],
        PACER_T3_STATE_DIR=str(tmp_path / "state"),
    )
    monkeypatch.setattr(t3.provider, "read", lambda *a, **kw: ({}, {}))
    monkeypatch.setattr(
        t3,
        "quota_verdict",
        lambda *a, **kw: Verdict("open", 1.0, ""),
    )
    return t3.T3Pacer(config, server), path, server


@pytest.mark.parametrize("on_path", [True, False])
def test_worker_prompt_names_resolved_cli(setup, tmp_path, monkeypatch, on_path):
    pacer, _, server = setup
    path_bin = tmp_path / "path bin"
    venv_bin = tmp_path / "venv bin"
    for directory in (path_bin, venv_bin):
        directory.mkdir()
    fallback = venv_bin / "hai-tasks"
    fallback.write_text("#!/bin/sh\n")
    fallback.chmod(0o755)
    selected = fallback
    if on_path:
        selected = path_bin / "hai-tasks"
        selected.write_text("#!/bin/sh\n")
        selected.chmod(0o755)
    with monkeypatch.context() as lookup:
        lookup.setenv("PATH", str(path_bin))
        lookup.setattr(t3.sys, "executable", str(venv_bin / "python"))
        pacer = t3.T3Pacer(pacer.config, server)
    # Changing the lookup result after startup must not change worker commands.
    monkeypatch.setattr(t3.shutil, "which", lambda name: None)
    pacer.tick()
    prompt = server.calls[0]["message"]["text"]
    command = shlex.quote(str(selected))
    assert f"`{command} read <repo>`" in prompt
    assert f"`{command} update <repo> <id> --revision <rev> --json <patch>`" in prompt
    assert "`hai_tasks` is Hai's Python package, not a tool" in prompt
    assert "Load the `hai-tasks` skill if it is listed" in prompt


@pytest.mark.parametrize("fallback_kind", ["missing", "directory", "not executable"])
def test_startup_requires_hai_tasks_cli(
    setup, tmp_path, monkeypatch, caplog, fallback_kind
):
    pacer, _, server = setup
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    fallback = scripts / "hai-tasks"
    if fallback_kind == "directory":
        fallback.mkdir()
    elif fallback_kind == "not executable":
        fallback.touch(mode=0o644)
    monkeypatch.setenv("PATH", str(scripts))
    monkeypatch.setattr(t3.sys, "executable", str(scripts / "python"))
    assert t3.run(pacer.config, dump_order=True) == 1
    assert "T3 startup failed: hai-tasks CLI not found: looked on PATH" in caplog.text
    assert str(fallback) in caplog.text
    assert "uv sync --extra t3" in caplog.text
    assert server.calls == []


@pytest.mark.parametrize(
    "wrong_url,wrong_tracker", [(True, False), (False, True), (True, True)]
)
def test_launch_record_mismatch_explains_recovery_without_mutating_work(
    setup, wrong_url, wrong_tracker
):
    pacer, tracker_path, server = setup
    pacer.tick()
    [record_path] = pacer.state.glob("*.json")
    record = json.loads(record_path.read_text())
    if wrong_url:
        record["url"] = "https://user:secret@old-server:3773/private?token=secret"
    if wrong_tracker:
        record["tracker"] = str(tracker_path.parent / "old" / "TASKS.md")
    t3.save(record_path, record)
    before_record = record_path.read_bytes()
    before_tracker = tracker_path.read_bytes()
    before_server = copy.deepcopy(server.data)
    before_calls = copy.deepcopy(server.calls)

    with pytest.raises(t3.Withheld) as error:
        pacer.tick()

    message = str(error.value)
    assert "No new tasks will launch this tick; existing T3 threads continue" in message
    assert str(record_path) in message
    assert record["issue"] in message
    assert "prevent duplicate threads" in message
    assert ("saved T3 server URL differs" in message) == wrong_url
    assert ("is absent from t3_trackers" in message) == wrong_tracker
    if wrong_tracker:
        assert record["tracker"] in message
    assert "secret" not in message
    assert "private" not in message
    assert "Restore the intended server/tracker settings and restart" in message
    assert "stop the pacer, inspect its threadId" in message
    assert "move only this file outside t3_state_dir" in message
    assert "can launch it again" in message
    assert "docs/t3-pacing.md#launch-record-mismatch" in message
    assert record_path.read_bytes() == before_record
    assert tracker_path.read_bytes() == before_tracker
    assert server.data == before_server
    assert server.calls == before_calls


def test_lost_reply_restart_preserves_one_thread_and_settings(setup):
    pacer, path, server = setup
    server.lose_reply = True
    with pytest.raises(TimeoutError):
        pacer.tick()
    record = json.loads(next(pacer.state.glob("*.json")).read_text())
    assert record["phase"] == "submitted"
    assert record["tracker"] == str(path.resolve())
    assert record["issue"] == "task-1"
    command = server.calls[0]
    assert command["bootstrap"]["prepareWorktree"]["requireWorktree"] is True
    assert command["modelSelection"] == server.data["threads"][0]["modelSelection"]
    assert command["runtimeMode"] == "full-access"
    assert "Description 1" in command["message"]["text"]
    assert "Run tests before completion" in command["message"]["text"]
    assert (
        TasksStore(path).read()["workspace"]["issues"][2]["sourceSection"]
        == "Scheduled"
    )
    restarted = t3.T3Pacer(pacer.config, server)
    restarted.tick()
    assert len(server.calls) == 1
    saved = json.loads(next(pacer.state.glob("*.json")).read_text())
    assert saved["thread"]["worktreePath"] == "/isolated/worktree"
    assert t3.git(path.parent, "status", "--porcelain") == ""


@pytest.mark.parametrize("state", ["completed", "error", "interrupted"])
def test_finished_turn_never_relaunches_or_accepts(setup, state):
    pacer, path, server = setup
    pacer.tick()
    thread = server.data["threads"][-1]
    thread.update(session={"status": "idle"}, latestTurn={"state": state})
    pacer.tick()  # May launch task 2, but never task 1 again.
    pacer.tick()
    assert len({c["threadId"] for c in server.calls}) == len(server.calls) == 2
    issues = TasksStore(path).read()["workspace"]["issues"]
    assert not any(i["accepted"] for i in issues)
    assert all(
        i["sourceSection"] == "Scheduled"
        for i in issues
        if i["id"] in {"task-1", "task-2"}
    )


def test_observed_thread_logs_one_readable_line_and_keeps_json_at_debug(setup, caplog):
    pacer, path, server = setup
    pacer.tick()
    thread = server.data["threads"][-1]
    thread.update(
        session={
            "status": "stopped",
            "providerName": "codex",
            "lastError": "workspace folder no longer exists",
        },
        latestTurn={"state": "completed", "turnId": "turn-1"},
        hasPendingUserInput=True,
    )
    caplog.set_level("DEBUG", logger="kandev-pacer")
    pacer.tick()
    [info] = [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "INFO" and thread["id"] in r.getMessage()
    ]
    assert info == (
        f"T3 task-1 from {path.resolve()} on {thread['id']} (codex): "
        "session stopped, turn completed, needs input; "
        "error: workspace folder no longer exists"
    )
    assert "{" not in info
    [debug] = [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "DEBUG" and thread["id"] in r.getMessage()
    ]
    assert json.loads(debug.split(": ", 1)[1])["latestTurn"]["turnId"] == "turn-1"


def test_thread_note_without_turn_or_provider():
    record = {
        "issue": "task-9",
        "tracker": "/repo/TASKS.md",
        "thread": {"id": "abc", "session": {"status": "starting"}, "latestTurn": None},
    }
    assert t3.thread_note(record) == (
        "task-9 from /repo/TASKS.md on abc: session starting"
    )


@pytest.mark.parametrize("flag", ["hasPendingApprovals", "hasPendingUserInput"])
def test_waiting_occupies_capacity(setup, flag):
    pacer, _, server = setup
    pacer.tick()
    server.data["threads"][-1].update(
        session={"status": "idle"}, latestTurn=None, **{flag: True}
    )
    pacer.tick()
    assert len(server.calls) == 1


@pytest.mark.parametrize(
    "instance, provider", [("codex", "openai"), ("claudeAgent", "anthropic")]
)
def test_instance_selection_launches_and_counts_capacity(setup, instance, provider):
    pacer, _, server = setup
    pacer.config.providers_raw = provider
    parent = server.data["threads"][0]
    parent["modelSelection"] = {"instanceId": instance, "model": "test-model"}
    pacer.tick()
    assert len(server.calls) == 1
    assert server.calls[0]["modelSelection"] == parent["modelSelection"]
    pacer.tick()
    assert len(server.calls) == 1


@pytest.mark.parametrize(
    "selection, detail",
    [
        ({"instanceId": "hermes"}, 'instanceId="hermes"'),
        ({"provider": "unknown"}, 'provider="unknown"'),
        ({}, "provider=null"),
        ({"instanceId": "custom", "provider": "codex"}, 'instanceId="custom"'),
    ],
)
@pytest.mark.parametrize("activity", ["running", "approval", "background"])
def test_unknown_provider_explains_blocking_thread(setup, selection, detail, activity):
    pacer, path, server = setup
    parent = server.data["threads"][0]
    parent["modelSelection"] = selection
    if activity == "running":
        parent["session"] = {"status": "running"}
    elif activity == "approval":
        parent["hasPendingApprovals"] = True
    else:
        parent["backgroundLiveness"] = "working"
    with pytest.raises(t3.Withheld) as caught:
        pacer.tick()
    message = str(caught.value)
    assert 'Active T3 thread "parent"' in message
    assert detail in message
    assert "let it finish or stop its activity" in message
    assert "codex (OpenAI) and claudeAgent (Anthropic)" in message
    assert "--agent-providers-raw and --apikey-policy do not add support" in message
    assert "docs/t3-pacing.md#unknown-providers" in message
    assert not server.calls
    assert not list(pacer.state.glob("*.json"))
    assert t3.git(path.parent, "status", "--porcelain") == ""


@pytest.mark.parametrize(
    "block", ["quota", "provider", "state", "total", "starting", "quota_error"]
)
def test_withholding(setup, monkeypatch, block):
    pacer, path, server = setup
    parent = server.data["threads"][0]
    if block == "quota":
        monkeypatch.setattr(
            t3,
            "quota_verdict",
            lambda *a, **kw: Verdict("paused", -0.1, "headroom=-0.100 PAUSED", 3600),
        )
    elif block == "provider":
        parent["modelSelection"]["provider"] = "unknown"
    elif block == "state":
        parent["session"]["status"] = "new-state"
    elif block == "total":
        pacer.config.concurrency_total = 1
        server.data["threads"].append(
            {
                **parent,
                "id": "other-provider",
                "session": {"status": "running"},
                "modelSelection": {"provider": "claudeAgent", "model": "claude"},
            }
        )
    elif block == "starting":
        parent["session"]["status"] = "starting"
    else:
        monkeypatch.setattr(
            t3.provider, "read", lambda *a, **kw: ({}, {"openai": "unavailable"})
        )
    if block == "state":
        with pytest.raises(ValueError, match="Unknown T3"):
            pacer.tick()
    else:
        pacer.tick()
    assert not server.calls
    assert not list(pacer.state.glob("*.json"))
    assert t3.git(path.parent, "status", "--porcelain") == ""


def test_dependency_order_and_excluded_sections(tmp_path):
    path = tracker(
        tmp_path / "repo",
        "- [1] Blocked\n    - Depends on: [2]\n- [2] Ready\n"
        "- [3] Missing\n    - Depends on: [99]\n",
    )
    store = TasksStore(path)
    source = store.read()
    assert [i["id"] for i in t3.eligible(source)] == ["task-2"]
    store.mutate(
        "update",
        source["revision"],
        id="task-2",
        input={"sourceSection": "Completed / Accepted"},
    )
    assert [i["id"] for i in t3.eligible(store.read())] == ["task-1"]


def test_revision_conflict_holds_tracker_without_dispatch(setup, monkeypatch, caplog):
    pacer, path, server = setup
    original = pacer.schedule

    def conflict(record_path, record):
        path.write_text(path.read_text() + "Concurrent edit\n")
        original(record_path, record)

    monkeypatch.setattr(pacer, "schedule", conflict)
    pacer.tick()
    assert not server.calls
    caplog.clear()
    t3.T3Pacer(pacer.config, server).tick()
    assert not server.calls
    [line] = held_lines(caplog)
    assert f"tracker {path} since " in line
    assert "revision changed before scheduling" in line
    assert "to release: rm " in line
    assert "Concurrent edit" in path.read_text()


def test_overlapping_tracker_numbers(setup, tmp_path):
    pacer, first, server = setup
    second = add_tracker(pacer, server, tmp_path / "repo2")
    pacer.tick()
    # With the first tracker excluded by its parent gate, the second still admits [1].
    server.data["threads"][0]["modelSelection"] = {"provider": "unknown"}
    server.data["threads"][-1]["session"] = {"status": "idle"}
    server.data["threads"][-1]["latestTurn"] = {"state": "completed"}
    pacer.tick()
    records = [json.loads(p.read_text()) for p in pacer.state.glob("*.json")]
    assert {r["tracker"] for r in records} == {str(first), str(second)}
    assert {r["issue"] for r in records} == {"task-1"}
    assert len(server.calls) == 2


def test_absent_command_reuses_saved_ids(setup):
    pacer, _, server = setup
    pacer.tick()
    record_path = next(pacer.state.glob("*.json"))
    record = json.loads(record_path.read_text())
    record["phase"] = "submitted"
    t3.save(record_path, record)
    server.data["threads"].pop()
    t3.T3Pacer(pacer.config, server).tick()
    assert server.calls[0] == server.calls[1]


def test_rejected_launch_holds_tracker_and_retries_after_cooldown(setup, clock, caplog):
    caplog.set_level("INFO", logger="kandev-pacer")
    pacer, path, server = setup
    server.reject = 1
    pacer.tick()
    [record_path] = pacer.state.glob("*.json")
    hold = json.loads(record_path.read_text())["hold"]
    assert hold["scope"] == "tracker"
    assert "T3 rejected launching task-1 (1/3)" in hold["reason"]
    [warning] = [
        r.getMessage() for r in caplog.records if "T3 holding" in r.getMessage()
    ]
    assert "worktree setup failed" in warning
    assert f"leftover worktree branches in {path.parent}: pacer-" in warning
    # Another worker merges its own tracker update. The held tracker launches
    # nothing, not even its next issue, until the cooldown is over.
    path.write_text(path.read_text() + "Another worker's note\n")
    t3.git(path.parent, "commit", "-am", "docs(tasks): complete other work")
    clock[0] += t3.REJECTION_COOLDOWN - 60
    caplog.clear()
    pacer.tick()
    assert len(server.calls) == 1
    [line] = held_lines(caplog)
    assert "retries automatically in ~1m 00s" in line
    assert line.endswith(f"to retry now: rm {shlex.quote(str(record_path))}")
    # After the cooldown it is a pending launch again and waits for a free slot.
    busy = {**server.data["threads"][0], "id": "busy"}
    busy["session"] = {"status": "running", "activeTurnId": "turn"}
    server.data["threads"].append(busy)
    clock[0] += 60
    pacer.tick()
    assert len(server.calls) == 1
    assert "pending launch of task-1" in caplog.records[-1].getMessage()
    busy["session"] = {"status": "idle", "activeTurnId": None}
    pacer.tick()
    first, second = server.calls
    assert first["threadId"] != second["threadId"]
    assert first["commandId"] != second["commandId"]
    branch = second["bootstrap"]["prepareWorktree"]["branch"]
    assert branch == f"pacer-{second['threadId']}"
    assert first["message"]["text"] == second["message"]["text"]
    record = json.loads(record_path.read_text())
    assert record["phase"] == "accepted"
    assert "hold" not in record


def test_repeated_rejections_hold_until_the_record_is_removed(setup, clock, caplog):
    pacer, _, server = setup
    server.reject = 3
    for _ in range(3):
        pacer.tick()
        clock[0] += t3.REJECTION_COOLDOWN
    assert len(server.calls) == 3
    [record_path] = pacer.state.glob("*.json")
    record = json.loads(record_path.read_text())
    assert record["hold"]["retry_at"] is None
    # The leftover worktree branches and T3's reasons outlive the console.
    assert [r["branch"] for r in record["rejected"]] == [
        f"pacer-{c['threadId']}" for c in server.calls
    ]
    assert all("worktree setup failed" in r["error"] for r in record["rejected"])
    clock[0] += 30 * 86400
    caplog.clear()
    pacer.tick()
    assert len(server.calls) == 3
    [line] = held_lines(caplog)
    assert "(3/3); check the project's T3 worktree setup" in line
    release = line.split("to release: ", 1)[1]
    assert shlex.split(release) == ["rm", str(record_path)]
    subprocess.run(shlex.split(release), check=True)
    pacer.tick()
    assert len(server.calls) == 4
    assert "Hai issue ID: task-1" in server.calls[-1]["message"]["text"]
    [record] = [json.loads(p.read_text()) for p in pacer.state.glob("*.json")]
    assert record["phase"] == "accepted"


def test_rejection_holds_tracker_and_others_launch_in_the_same_tick(setup, tmp_path):
    pacer, first, server = setup
    second = add_tracker(pacer, server, tmp_path / "repo2")
    server.broken.add(str(first.parent))
    pacer.tick()
    assert [project_of(c) for c in server.calls] == [
        str(first.parent),
        str(second.parent),
    ]
    records = {
        r["tracker"]: r
        for r in (json.loads(p.read_text()) for p in pacer.state.glob("*.json"))
    }
    assert records[str(first)]["hold"]["scope"] == "tracker"
    assert records[str(second)]["phase"] == "accepted"


def test_record_stuck_at_rejection_limit_holds_only_its_tracker(
    setup, tmp_path, caplog
):
    # 2026-10-07: a filemill launch record reached three rejections, and every
    # tick withheld all 17 trackers until a person deleted it, three days later.
    pacer, first, server = setup
    server.reject = 1
    pacer.tick()
    [record_path] = pacer.state.glob("*.json")
    record = json.loads(record_path.read_text())
    del record["hold"], record["rejected"]
    record["rejections"] = 3
    t3.save(record_path, record)
    second = add_tracker(pacer, server, tmp_path / "repo2")
    pacer.tick()
    assert [project_of(c) for c in server.calls] == [
        str(first.parent),
        str(second.parent),
    ]
    hold = json.loads(record_path.read_text())["hold"]
    assert hold["scope"] == "tracker"
    assert hold["retry_at"] is None
    caplog.clear()
    pacer.tick()
    [line] = held_lines(caplog)
    assert f"tracker {first} since " in line
    assert "T3 rejected launching task-1 (3/3)" in line
    assert len(server.calls) == 2


def test_pending_record_off_base_branch_skips_only_its_tracker(setup, tmp_path, caplog):
    pacer, first, server = setup
    second = add_tracker(pacer, server, tmp_path / "repo2")
    # Stopping right after saving a launch record leaves it prepared.
    store, _ = pacer.trackers[str(first)]
    source = store.read()
    issue = t3.eligible(source)[0]
    parent, project = server.data["threads"][0], server.data["projects"][0]
    pacer.prepare(str(first), store, source, issue, parent, project)
    t3.git(first.parent, "switch", "-c", "feature")
    pacer.tick()
    assert [project_of(c) for c in server.calls] == [str(second.parent)]
    [warning] = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert f"repository {first.parent} for tracker {first}" in warning
    assert "not on the configured base branch main" in warning


def test_accepted_missing_thread_blocks_new_work(setup):
    pacer, _, server = setup
    pacer.tick()
    server.data["threads"].pop()
    with pytest.raises(ValueError, match="accepted thread is missing"):
        pacer.tick()
    assert len(server.calls) == 1


def test_hai_rejects_stale_revision(setup, monkeypatch):
    pacer, path, server = setup
    store, _ = pacer.trackers[str(path)]
    original = store.mutate

    def changed(*args, **kwargs):
        path.write_text(path.read_text() + "External edit\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "mutate", changed)
    with pytest.raises(hai_tasks.TasksError) as error:
        pacer.tick()
    assert error.value.status == 412
    assert not server.calls


def test_real_gate_and_scoped_windows(setup, monkeypatch):
    from collections import Counter

    from kandev_pacer.quota import quota_verdict

    pacer, _, _ = setup
    monkeypatch.setattr(t3, "quota_verdict", quota_verdict)
    pacer.config.providers_raw = "anthropic"
    usage = {"agents": [{"provider": "anthropic", "windows": []}]}
    windows = [
        {"duration_s": 604800, "resets_at": 302400, "consumed_frac": 0.1},
        {
            "duration_s": 604800,
            "resets_at": 302400,
            "consumed_frac": 0.99,
            "scope": "Fable",
        },
    ]
    # Supply normalized windows so this tests gate/scoping composition directly.
    monkeypatch.setattr(t3, "windows_by_provider", lambda usage: {"anthropic": windows})
    assert not pacer.admitted(
        {"provider": "claudeAgent", "model": "claude-fable"}, Counter(), usage, {}, 0
    )
    assert pacer.admitted(
        {"provider": "claudeAgent", "model": "claude-other"}, Counter(), usage, {}, 0
    )


@pytest.mark.parametrize("changes", ["staged", "unstaged", "untracked"])
def test_dirty_repository_error_identifies_checkout_and_recovery(
    setup, changes, caplog
):
    pacer, path, server = setup
    root = path.parent.with_name("repo with 'quotes'")
    path.parent.rename(root)
    path = root / path.name
    pacer.config.t3_trackers = [{"tracker": str(path), "parent_thread": "parent"}]
    server.data["projects"][0]["workspaceRoot"] = str(root)
    pacer = t3.T3Pacer(pacer.config, server)
    store = TasksStore(path)
    head = t3.git(root, "rev-parse", "HEAD")
    changed = root / ("new.txt" if changes == "untracked" else "docs/tasks/1.md")
    changed.write_text("Work in progress\n")
    if changes == "staged":
        t3.git(root, "add", str(changed))
    before = store.read()

    pacer.tick()

    [message] = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert f"repository {root} for tracker {path}" in message
    command = message.split("Inspect with: ", 1)[1].split(". Commit", 1)[0]
    assert shlex.split(command) == ["git", "-C", str(root), "status", "--short"]
    assert "Commit or stash your changes (including untracked files)" in message
    assert "retry scheduling" in message
    assert store.read()["revision"] == before["revision"]
    assert t3.git(root, "rev-parse", "HEAD") == head
    assert not server.calls
    assert not list(pacer.state.glob("*.json"))


@pytest.mark.parametrize("block", ["dirty", "branch"])
def test_blocked_checkout_skips_only_its_tracker(setup, tmp_path, block, caplog):
    pacer, first, server = setup
    second = add_tracker(pacer, server, tmp_path / "repo2")
    if block == "dirty":
        (first.parent / "new.txt").write_text("Work in progress\n")
    else:
        t3.git(first.parent, "switch", "-c", "feature")
    head = t3.git(first.parent, "rev-parse", "HEAD")
    before = TasksStore(first).read()["revision"]

    pacer.tick()

    [record] = [json.loads(p.read_text()) for p in pacer.state.glob("*.json")]
    assert record["tracker"] == str(second)
    assert [c["threadId"] for c in server.calls] == [record["command"]["threadId"]]
    assert TasksStore(first).read()["revision"] == before
    assert t3.git(first.parent, "rev-parse", "HEAD") == head
    [warning] = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert str(first.parent) in warning


def test_scheduled_before_backlog(setup):
    _, path, _ = setup
    store = TasksStore(path)
    source = store.read()
    store.mutate(
        "update", source["revision"], id="task-2", input={"sourceSection": "Scheduled"}
    )
    assert [i["id"] for i in t3.eligible(store.read())] == ["task-2", "task-1"]


def test_cli_t3_does_not_contact_kandev(monkeypatch):
    from kandev_pacer import runtime

    monkeypatch.setattr("sys.argv", ["kandev-pacer", "--mode", "t3", "--dump-order"])
    monkeypatch.setattr(runtime, "load_settings", lambda *a: Settings(PACER_MODE="t3"))
    monkeypatch.setattr(t3, "run", lambda config, dump_order: 17 if dump_order else 0)
    monkeypatch.setattr(
        runtime.client, "workflow_entries", lambda: pytest.fail("Kandev called")
    )
    monkeypatch.setattr(runtime.settings, "mode", "kandev")
    assert runtime.main() == 17


def test_record_private_and_unknown_phase_blocks(setup):
    pacer, _, server = setup
    pacer.tick()
    path = next(pacer.state.glob("*.json"))
    assert path.stat().st_mode & 0o777 == 0o600
    record = json.loads(path.read_text())
    record["phase"] = "unexpected"
    t3.save(path, record)
    server.data["threads"].pop()
    with pytest.raises(ValueError, match="Unknown launch record phase"):
        pacer.tick()


def test_dump_order_needs_no_authentication(setup, monkeypatch, capsys):
    pacer, path, _ = setup
    monkeypatch.setattr(t3, "Server", lambda *a: pytest.fail("authenticated"))
    assert t3.run(pacer.config, dump_order=True) == 0
    assert [r["issue"]["id"] for r in json.loads(capsys.readouterr().out)] == [
        "task-1",
        "task-2",
    ]
    assert not list(pacer.state.glob("*.json"))
    assert t3.git(path.parent, "status", "--porcelain") == ""


def test_startup_explains_missing_remote_token(setup, monkeypatch, caplog):
    pacer, _, _ = setup
    monkeypatch.delenv("T3_THREAD_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(pacer.config, "t3_url", "http://100.81.121.7:3773")
    assert t3.run(pacer.config) == 1
    assert "requires T3_THREAD_AUTH_TOKEN" in caplog.text
    assert "t3 auth session issue" in caplog.text
    assert "--base-dir" in caplog.text
    assert "docs/t3-pacing.md#connection-errors" in caplog.text
    assert not list(pacer.state.glob("*.json"))


def test_startup_logs_safe_tracker_configuration_error(setup, monkeypatch, caplog):
    pacer, _, server = setup
    monkeypatch.setattr(t3, "Server", lambda *a: server)
    monkeypatch.setattr(pacer.config, "t3_trackers", [])
    assert t3.run(pacer.config) == 1
    assert "PACER_T3_TRACKERS needs tracker entries" in caplog.text


def test_startup_hides_unclassified_exception_contents(setup, monkeypatch, caplog):
    pacer, _, _ = setup

    def fail(*args):
        raise ValueError("private-token")

    monkeypatch.setattr(t3, "Server", fail)
    assert t3.run(pacer.config) == 1
    assert "ValueError" in caplog.text
    assert "private-token" not in caplog.text


@pytest.mark.parametrize(
    "entry, message",
    [
        ({}, ".tracker must be a nonempty TASKS.md path"),
        ({"tracker": ""}, ".tracker must be a nonempty TASKS.md path"),
        ({"tracker": "  "}, ".tracker must be a nonempty TASKS.md path"),
        ({"tracker": None}, ".tracker must be a nonempty TASKS.md path"),
        ("private-token", "must be a tracker table"),
        ({"tracker": "missing/private-token"}, "FileNotFoundError"),
        ({"tracker": "private-token\0"}, "ValueError"),
    ],
)
def test_startup_identifies_invalid_tracker_entry(
    setup, monkeypatch, caplog, entry, message
):
    pacer, path, server = setup
    monkeypatch.setattr(t3, "Server", lambda *a: server)
    pacer.config.t3_trackers.append(entry)
    assert t3.run(pacer.config) == 1
    assert "T3 startup failed: t3_trackers[1]" in caplog.text
    assert message in caplog.text
    assert "private-token" not in caplog.text
    assert not server.calls
    assert not list(pacer.state.glob("*.json"))
    assert t3.git(path.parent, "status", "--porcelain") == ""


@pytest.mark.parametrize("problem", ["directory", "duplicate", "empty-parent"])
def test_startup_identifies_tracker_path_and_parent_errors(
    setup, monkeypatch, caplog, problem
):
    pacer, path, server = setup
    monkeypatch.setattr(t3, "Server", lambda *a: server)
    entry = {"tracker": str(path), "parent_thread": "parent"}
    if problem == "directory":
        entry["tracker"] = str(path.parent)
        message = ".tracker must point to a file"
    elif problem == "duplicate":
        message = "Each tracker must appear only once"
    else:
        entry["parent_thread"] = ""
        message = ".parent_thread must be a nonempty thread ID"
    pacer.config.t3_trackers = [entry] if problem != "duplicate" else [entry, entry]
    assert t3.run(pacer.config) == 1
    assert "T3 startup failed: t3_trackers[" in caplog.text
    assert message in caplog.text
    assert not server.calls


def test_startup_explains_missing_parent_and_all_missing_settings(
    setup, monkeypatch, caplog
):
    pacer, path, server = setup
    monkeypatch.setattr(t3, "Server", lambda *a: server)
    pacer.config.t3_trackers = [{"tracker": str(path)}]
    assert t3.run(pacer.config) == 1
    assert (
        "t3_trackers[0]: Trackers without parent_thread require "
        "t3_model, t3_runtime_mode, t3_interaction_mode"
    ) in caplog.text
    assert "set parent_thread to inherit these settings" in caplog.text
    assert not server.calls
    assert not list(pacer.state.glob("*.json"))


def test_process_lock_prevents_second_pacer(setup, monkeypatch):
    import fcntl

    pacer, _, server = setup
    monkeypatch.setattr(t3, "Server", lambda *a: server)
    with (pacer.state / "pacer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert t3.run(pacer.config) == 1
    assert not server.calls


def test_connection_error_is_logged_and_tick_retries(setup, monkeypatch, caplog):
    pacer, _, server = setup
    message = "T3 HTTP ticket request failed: connection refused. Check PACER_T3_URL."

    def fail():
        raise t3.T3ConnectionError(message)

    def stop(interval):
        raise KeyboardInterrupt

    monkeypatch.setattr(t3, "Server", lambda *a: server)
    monkeypatch.setattr(server, "snapshot", fail)
    monkeypatch.setattr(t3.time, "sleep", stop)
    assert t3.run(pacer.config) == 0
    assert message in caplog.text
    assert not server.calls
    assert not list(pacer.state.glob("*.json"))


def test_submitted_record_for_moved_issue_holds_only_that_issue(setup, caplog):
    pacer, path, server = setup
    pacer.tick()
    record_path = next(pacer.state.glob("*.json"))
    record = json.loads(record_path.read_text())
    record["phase"] = "submitted"
    t3.save(record_path, record)
    server.data["threads"].pop()
    store = TasksStore(path)
    store.mutate(
        "update",
        store.read()["revision"],
        id="task-1",
        input={"sourceSection": "In Progress"},
    )
    t3.git(path.parent, "commit", "-am", "docs(tasks): start task-1 by hand")
    pacer = t3.T3Pacer(pacer.config, server)
    pacer.tick()
    # The tracker itself is fine, so its next issue launches in the same tick.
    assert len(server.calls) == 2
    assert "Hai issue ID: task-2" in server.calls[1]["message"]["text"]
    hold = json.loads(record_path.read_text())["hold"]
    assert hold["scope"] == "issue"
    assert "no longer eligible" in hold["reason"]
    server.data["threads"][-1]["latestTurn"] = {"state": "completed"}
    server.data["threads"][-1]["session"] = {"status": "idle"}
    caplog.clear()
    pacer.tick()
    [line] = held_lines(caplog)
    assert f"issue task-1 from {path} since " in line
    assert len(server.calls) == 2


@pytest.mark.parametrize("agent", ["codex-acp", "codex"])
@pytest.mark.parametrize("selection", ["provider", "instanceId"])
def test_quota_bypass_launches_and_retries_with_original_selection(
    setup, monkeypatch, caplog, agent, selection
):
    pacer, _, server = setup
    pacer.config.agent_providers_raw = f"{agent}=__api_key__ hermes-acp=__api_key__"
    server.data["threads"][0]["modelSelection"] = {
        selection: "codex",
        "model": "test-model",
    }
    monkeypatch.setattr(
        t3.provider, "read", lambda *a, **kw: ({}, {"openai": "unavailable"})
    )
    caplog.set_level("INFO", logger="kandev-pacer")
    pacer.tick()
    assert len(server.calls) == 1
    assert (
        server.calls[0]["modelSelection"] == server.data["threads"][0]["modelSelection"]
    )
    assert "T3 [openai] quota bypass, concurrency only (0/1 running)" in caplog.text
    record_path = next(pacer.state.glob("*.json"))
    record = json.loads(record_path.read_text())
    record["phase"] = "submitted"
    t3.save(record_path, record)
    server.data["threads"].pop()
    pacer.tick()
    assert server.calls[0] == server.calls[1]


@pytest.mark.parametrize("block", ["provider_cap", "total_cap", "withhold", "disabled"])
def test_quota_bypass_retains_admission_limits(setup, block):
    pacer, path, server = setup
    pacer.config.agent_providers_raw = "codex-acp=__api_key__"
    if block == "provider_cap":
        server.data["threads"][0]["session"] = {"status": "running"}
    elif block == "total_cap":
        pacer.config.concurrency_total = 1
        server.data["threads"].append(
            {
                **server.data["threads"][0],
                "id": "claude",
                "modelSelection": {"instanceId": "claudeAgent", "model": "claude"},
                "session": {"status": "running"},
            }
        )
    elif block == "withhold":
        pacer.config.apikey_policy = "withhold"
    else:
        pacer.config.providers_raw = "anthropic"
    pacer.tick()
    assert not server.calls
    assert not list(pacer.state.glob("*.json"))
    assert t3.git(path.parent, "status", "--porcelain") == ""


def test_status_reports_both_providers_scopes_and_resume(setup, monkeypatch, caplog):
    from kandev_pacer.quota import quota_verdict

    pacer, _, server = setup
    pacer.config.providers_raw = "openai anthropic"
    monkeypatch.setattr(t3, "quota_verdict", quota_verdict)
    monkeypatch.setattr(t3.time, "time", lambda: 0)
    windows = {
        "openai": [{"duration_s": 604800, "resets_at": 302400, "consumed_frac": 0.8}],
        "anthropic": [
            {"duration_s": 604800, "resets_at": 302400, "consumed_frac": 0.1},
            {
                "duration_s": 604800,
                "resets_at": 302400,
                "consumed_frac": 0.99,
                "scope": "Fable",
            },
        ],
    }
    monkeypatch.setattr(t3, "windows_by_provider", lambda usage: windows)
    caplog.set_level("INFO", logger="kandev-pacer")
    pacer.tick()
    assert "T3 [openai] headroom=-0.300 PAUSED (0/1 running), resumes in" in caplog.text
    assert "T3 [anthropic] headroom=+0.400 open (0/1 running)" in caplog.text
    assert "no tracker parent uses this provider" in caplog.text
    assert "T3 [anthropic (Fable)] headroom=-0.490 CEILING" in caplog.text
    assert "next tick in 30s" in caplog.text
    assert not server.calls


def test_status_reports_quota_failure_even_without_parent(setup, monkeypatch, caplog):
    pacer, _, _ = setup
    pacer.config.providers_raw = "openai anthropic"
    monkeypatch.setattr(
        t3.provider, "read", lambda *a, **kw: ({}, {"anthropic": "private-token"})
    )
    pacer.tick()
    assert "T3 [anthropic] quota unavailable, WITHHOLDING (0/1 running)" in caplog.text
    assert "no tracker parent uses this provider" in caplog.text
    assert "private-token" not in caplog.text


@pytest.mark.parametrize(
    "overrides, codex, claude",
    [
        ("codex-acp=__api_key__", True, False),
        ("codex=__api_key__", True, False),
        ("codex-acp=__api_key__ codex=openai", False, False),
        ("claude-acp=__api_key__", False, True),
        ("claudeAgent=__api_key__", False, True),
        ("claude-acp=__api_key__ claudeAgent=anthropic", False, False),
        ("codex=__api_key__ claudeAgent=__api_key__", True, True),
    ],
)
def test_bypass_does_not_change_other_provider_gates(
    setup, monkeypatch, overrides, codex, claude
):
    from collections import Counter

    pacer, _, _ = setup
    pacer.config.providers_raw = "openai anthropic"
    pacer.config.agent_providers_raw = overrides
    monkeypatch.setattr(
        t3, "quota_verdict", lambda *a, **kw: Verdict("paused", -0.1, "PAUSED")
    )
    assert (
        pacer.admitted({"instanceId": "codex", "model": "gpt"}, Counter(), {}, {}, 0)
        is codex
    )
    assert (
        pacer.admitted(
            {"instanceId": "claudeAgent", "model": "claude-fable"}, Counter(), {}, {}, 0
        )
        is claude
    )


def test_unknown_override_keys_warn_at_startup(setup, caplog):
    pacer, _, server = setup
    pacer.config.agent_providers_raw = (
        "codex=__api_key__ claudeAgent=__api_key__ codex-acp=__api_key__ "
        "claude-acp=__api_key__ hermes-acp=__api_key__"
    )
    t3.T3Pacer(pacer.config, server)
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
        "T3 ignores --agent-providers-raw entry hermes-acp=__api_key__: it matches "
        "no T3 provider instance. Use codex or claudeAgent, or the Kandev aliases "
        "codex-acp or claude-acp"
    ]


def configure_models(pacer, model="automatic"):
    from kandev_pacer.config import T3ModelSelection

    pacer.config.providers_raw = "openai anthropic"
    pacer.config.t3_models = [
        T3ModelSelection(instanceId="codex", model="gpt-test"),
        T3ModelSelection(
            instanceId="claudeAgent",
            model="claude-fable",
            options={"reasoningEffort": "high"},
        ),
    ]
    pacer.config.t3_model = model


def test_fixed_model_and_modes_override_parent(setup):
    pacer, _, server = setup
    configure_models(pacer)
    pacer.config.t3_model = pacer.config.t3_models[1]
    pacer.config.t3_runtime_mode = "approval-required"
    pacer.config.t3_interaction_mode = "plan"
    pacer.tick()
    command = server.calls[0]
    for settings in (command, command["bootstrap"]["createThread"]):
        assert settings["modelSelection"] == {
            "instanceId": "claudeAgent",
            "model": "claude-fable",
            "options": {"reasoningEffort": "high"},
        }
        assert settings["runtimeMode"] == "approval-required"
        assert settings["interactionMode"] == "plan"


@pytest.mark.parametrize("blocked", [None, "quota", "capacity", "error"])
def test_automatic_prefers_furthest_behind_available_model(setup, monkeypatch, blocked):
    pacer, _, server = setup
    configure_models(pacer)
    monkeypatch.setattr(
        t3,
        "quota_verdict",
        lambda *a, provider, **kw: (
            Verdict("paused", -0.1, "paused")
            if provider == "anthropic" and blocked == "quota"
            else Verdict("open", 0.8 if provider == "anthropic" else 0.2, "")
        ),
    )
    if blocked == "capacity":
        server.data["threads"].append(
            {
                **server.data["threads"][0],
                "id": "busy",
                "modelSelection": {
                    "instanceId": "claudeAgent",
                    "model": "claude-fable",
                },
                "session": {"status": "running"},
            }
        )
    if blocked == "error":
        monkeypatch.setattr(
            t3.provider, "read", lambda *a, **kw: ({}, {"anthropic": "offline"})
        )
    pacer.tick()
    assert server.calls[0]["modelSelection"]["instanceId"] == (
        "claudeAgent" if blocked is None else "codex"
    )


def test_automatic_all_paused_leaves_tracker_untouched(setup, monkeypatch):
    pacer, path, server = setup
    configure_models(pacer)
    monkeypatch.setattr(
        t3, "quota_verdict", lambda *a, **kw: Verdict("paused", -0.1, "paused")
    )
    pacer.tick()
    assert not server.calls
    assert not list(pacer.state.glob("*.json"))
    assert t3.git(path.parent, "status", "--porcelain") == ""


def test_pending_launch_keeps_selected_model_after_config_change(setup):
    pacer, _, server = setup
    configure_models(pacer)
    server.lose_reply = True
    with pytest.raises(TimeoutError):
        pacer.tick()
    selection = server.calls[0]["modelSelection"]
    server.data["threads"] = server.data["threads"][:1]
    pacer.config.t3_model = pacer.config.t3_models[1]
    server.lose_reply = False
    pacer.tick()
    assert server.calls[1]["modelSelection"] == selection
    assert server.calls[1]["commandId"] == server.calls[0]["commandId"]


def test_automatic_uses_scoped_headroom_and_reconsiders_each_launch(setup, monkeypatch):
    from collections import Counter

    from kandev_pacer.config import T3ModelSelection
    from kandev_pacer.quota import quota_verdict

    pacer, _, server = setup
    pacer.config.providers_raw = "anthropic"
    pacer.config.t3_model = "automatic"
    pacer.config.t3_models = [
        T3ModelSelection(instanceId="claudeAgent", model="claude-fable"),
        T3ModelSelection(instanceId="claudeAgent", model="claude-other"),
    ]
    windows = [
        {"duration_s": 604800, "resets_at": 302400, "consumed_frac": 0.1},
        {
            "duration_s": 604800,
            "resets_at": 302400,
            "consumed_frac": 0.3,
            "scope": "Fable",
        },
    ]
    monkeypatch.setattr(t3, "windows_by_provider", lambda usage: {"anthropic": windows})
    monkeypatch.setattr(t3, "quota_verdict", quota_verdict)
    parent = server.data["threads"][0]

    def choose():
        return pacer.launch_settings(parent, Counter(), {}, {}, 0)["modelSelection"]

    assert choose()["model"] == "claude-other"
    windows[1]["consumed_frac"] = 0.05
    # Both now have the same binding shared window; configured order breaks the tie.
    assert choose()["model"] == "claude-fable"


@pytest.mark.parametrize("automatic", [False, True])
def test_configured_launch_without_parent_and_restart(
    setup, automatic, monkeypatch, capsys
):
    pacer, path, server = setup
    configure_models(pacer)
    if not automatic:
        pacer.config.t3_model = pacer.config.t3_models[1]
    pacer.config.t3_runtime_mode = "approval-required"
    pacer.config.t3_interaction_mode = "plan"
    pacer.config.t3_trackers = [{"tracker": str(path)}]
    server.data["threads"] = []
    pacer = t3.T3Pacer(pacer.config, server)
    monkeypatch.setattr(t3, "Server", lambda *a: pytest.fail("authenticated"))
    assert t3.run(pacer.config, dump_order=True) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    server.lose_reply = True
    with pytest.raises(TimeoutError):
        pacer.tick()
    command = server.calls[0]
    expected = pacer.config.t3_models[0 if automatic else 1].model_dump(
        exclude_none=True
    )
    for settings in (command, command["bootstrap"]["createThread"]):
        assert settings["modelSelection"] == expected
        assert settings["runtimeMode"] == "approval-required"
        assert settings["interactionMode"] == "plan"
    assert command["bootstrap"]["createThread"]["projectId"] == "project"
    assert command["bootstrap"]["prepareWorktree"]["projectCwd"] == str(path.parent)
    server.data["threads"] = []
    server.lose_reply = False
    t3.T3Pacer(pacer.config, server).tick()
    assert server.calls == [command, command]


@pytest.mark.parametrize(
    "missing", ["t3_model", "t3_runtime_mode", "t3_interaction_mode"]
)
def test_without_parent_requires_explicit_launch_settings(setup, missing):
    pacer, path, server = setup
    configure_models(pacer)
    pacer.config.t3_runtime_mode = "full-access"
    pacer.config.t3_interaction_mode = "default"
    pacer.config.t3_trackers = [{"tracker": str(path)}]
    setattr(pacer.config, missing, None)
    with pytest.raises(t3.Withheld, match=f"without parent_thread require {missing}"):
        t3.T3Pacer(pacer.config, server)
    assert not server.calls
    assert not list(pacer.state.glob("*.json"))
    assert t3.git(path.parent, "status", "--porcelain") == ""


@pytest.mark.parametrize("problem", ["missing", "wrong-root", "duplicate"])
@pytest.mark.parametrize("with_parent", [False, True])
def test_tracker_requires_matching_project(setup, problem, with_parent):
    pacer, path, server = setup
    configure_models(pacer)
    pacer.config.t3_runtime_mode = "full-access"
    pacer.config.t3_interaction_mode = "default"
    if not with_parent:
        pacer.config.t3_trackers = [{"tracker": str(path)}]
        server.data["threads"] = []
    project = copy.deepcopy(server.data["projects"][0])
    if problem == "missing":
        server.data["projects"] = []
    elif problem == "wrong-root":
        server.data["projects"][0]["workspaceRoot"] = "/other"
    else:
        server.data["projects"].append(
            {"id": "duplicate", "workspaceRoot": str(path.parent)}
        )
    source = path.read_text()
    pacer = t3.T3Pacer(pacer.config, server)
    with pytest.raises(t3.Withheld, match="exactly one T3 project") as error:
        pacer.tick()
    message = str(error.value)
    assert str(path) in message
    assert f"workspaceRoot {str(path.parent)!r}" in message
    assert f"found {2 if problem == 'duplicate' else 0}" in message
    assert "Cannot launch new tasks this tick" in message
    assert "Existing T3 threads continue" in message
    assert "retry next tick" in message
    assert "t3_url" in message
    if problem == "duplicate":
        assert "resolve duplicate workspace roots" in message
    else:
        assert "Open or add this directory as a project" in message
        assert "t3_trackers[].tracker" in message
    assert not server.calls
    assert not list(pacer.state.glob("*.json"))
    assert path.read_text() == source
    assert t3.git(path.parent, "status", "--porcelain") == ""
    server.data["projects"] = [project]
    pacer.tick()
    assert len(server.calls) == 1


@pytest.mark.parametrize("parent", ["", "missing", "wrong-project"])
def test_supplied_parent_still_requires_valid_thread(setup, parent):
    pacer, path, server = setup
    configure_models(pacer)
    pacer.config.t3_runtime_mode = "full-access"
    pacer.config.t3_interaction_mode = "default"
    pacer.config.t3_trackers = [{"tracker": str(path), "parent_thread": parent}]
    if parent == "wrong-project":
        server.data["threads"][0].update(id=parent, projectId="other")
    with pytest.raises(t3.Withheld, match="parent_thread|Parent thread"):
        t3.T3Pacer(pacer.config, server).tick()
    assert not server.calls
    assert not list(pacer.state.glob("*.json"))


@pytest.mark.parametrize("bypass", [False, True])
def test_reset_aware_t3_launch_and_bypass_do_not_share_redemption(
    setup, monkeypatch, tmp_path, bypass
):
    from test_resets import NOW, envelope

    from kandev_pacer import resets
    from kandev_pacer.quota import quota_verdict

    pacer, _, server = setup
    before, after = envelope(), envelope(used=0, count=1, roll=NOW + 604800)
    pacer.config.reset_state_dir = str(tmp_path / "resets")
    if bypass:
        pacer.config.agent_providers_raw = (
            "codex-acp=__api_key__ hermes-acp=__api_key__"
        )
        pacer.config.apikey_policy = "cap"
    monkeypatch.setattr(t3.time, "time", lambda: NOW)
    monkeypatch.setattr(t3, "quota_verdict", quota_verdict)
    monkeypatch.setattr(t3.provider, "read", lambda *a, **kw: (before, {}))
    monkeypatch.setattr(resets, "_identity", lambda *a: ("account", "token"))
    reads = iter([before, after])
    monkeypatch.setattr(resets, "_fresh", lambda *a: next(reads))
    calls = []
    monkeypatch.setattr(
        resets, "_post", lambda *a: calls.append(a) or {"code": "reset"}
    )
    pacer.tick()
    assert len(server.calls) == 1
    assert len(calls) == (0 if bypass else 1)


def test_status_shows_the_measured_burn_time(setup, monkeypatch, caplog):
    """The T3 loop measures burn time from the same readings and says when it changes."""
    from kandev_pacer.quota import quota_verdict

    pacer, _, _ = setup
    monkeypatch.setattr(t3, "quota_verdict", quota_verdict)
    monkeypatch.setattr(t3.time, "time", lambda: 0)
    window = {"duration_s": 604800, "resets_at": 302400, "consumed_frac": 0.8}
    monkeypatch.setattr(t3, "windows_by_provider", lambda usage: {"openai": [window]})
    caplog.set_level("INFO", logger="kandev-pacer")
    pacer.tick()
    assert "burn" not in caplog.text, "no history yet, so nothing to say"
    # Readings before this tick: 28% -> 78% over five hours, ending ten minutes ago.
    # The tick's 80% then makes the fastest pair the five hours ending now.
    for index in range(0, 5 * 3600 + 1, 30):
        pacer.burn.observe(
            "openai", [{**window, "consumed_frac": 0.28 + index / (10 * 3600)}],
            index - 5 * 3600 - 600,
        )
    pacer.tick()
    assert "T3 [openai] burn time ~9h 56m" in caplog.text
    assert "per weekly allowance (rose 50% in 5h 00m 00s to " in caplog.text
    assert "PAUSED (0/1 running), resumes in" in caplog.text
    assert ", burn ~9h 56m" in caplog.text
