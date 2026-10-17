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
        self.data = {
            "projects": [],
            "threads": [],
            "archivedThreads": [],
            "pacedRuns": [],
        }
        self.calls = []
        self.lose_reply = False
        self.reject = 0
        self.broken = set()
        self.leftover_branches = set()

    def snapshot(self):
        return copy.deepcopy(self.data)

    def call(self, method, command):
        assert method == "orchestration.launchThread"
        self.calls.append(copy.deepcopy(command))
        worktree = command["workspaceStrategy"]
        branch = worktree["branch"]
        if (
            self.reject
            or branch in self.leftover_branches
            or self.project_root(command) in self.broken
        ):
            # Simulate refusal with leftover branches. V2 cleanup is unverified.
            self.reject = max(self.reject - 1, 0)
            self.leftover_branches.add(branch)
            raise t3.T3Rejected("T3 rejected the RPC: worktree setup failed")
        if not any(t["id"] == command["threadId"] for t in self.data["threads"]):
            self.data["threads"].append(
                {
                    **command,
                    "id": command["threadId"],
                    "worktreePath": "/isolated/worktree",
                    "branch": "pacer-branch",
                    "status": "starting",
                    "activeRunId": "run",
                }
            )
        if self.lose_reply:
            raise TimeoutError("reply lost")
        return {"threadId": command["threadId"], "projection": {}, "resumed": False}

    def project_root(self, command):
        return next(
            p["workspaceRoot"]
            for p in self.data["projects"]
            if p["id"] == command["projectId"]
        )


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
    return command["projectId"]


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
                "instanceId": "codex",
                "model": "gpt-5.3-codex",
                "options": [{"id": "reasoningEffort", "value": "high"}],
            },
            "runtimeMode": "full-access",
            "interactionMode": "default",
            "status": "idle",
            "activeRunId": None,
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


def record_paced_thread(pacer, thread):
    t3.save(
        pacer.admissions / (thread["id"] + ".json"),
        {
            "url": pacer.server.url,
            "phase": "done",
            "run": {
                "runId": "run-" + thread["id"],
                "threadId": thread["id"],
                "modelSelection": thread["modelSelection"],
            },
        },
    )


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
    prompt = server.calls[0]["initialMessage"]["text"]
    assert not prompt.rstrip().endswith(("...", "…"))
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


@pytest.mark.parametrize("wrong_tracker", [False, True])
def test_launch_record_mismatch_explains_recovery_without_mutating_work(
    setup, wrong_tracker
):
    pacer, tracker_path, server = setup
    pacer.tick()
    [record_path] = pacer.state.glob("*.json")
    record = json.loads(record_path.read_text())
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
    assert "saved T3 server URL differs" in message
    assert "secret" not in message
    assert "private" not in message
    assert "Restore the intended server settings and restart" in message
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
    assert command["workspaceStrategy"]["type"] == "worktree"
    assert command["modelSelection"] == server.data["threads"][0]["modelSelection"]
    assert command["runtimeMode"] == "full-access"
    assert "Description 1" in command["initialMessage"]["text"]
    assert "Run tests before completion" in command["initialMessage"]["text"]
    assert (
        TasksStore(path).read()["workspace"]["issues"][2]["sourceState"]
        == "Scheduled"
    )
    restarted = t3.T3Pacer(pacer.config, server)
    restarted.tick()
    assert len(server.calls) == 1
    saved = json.loads(next(pacer.state.glob("*.json")).read_text())
    assert saved["thread"]["worktreePath"] == "/isolated/worktree"
    assert t3.git(path.parent, "status", "--porcelain") == ""


@pytest.mark.parametrize("state", ["completed", "failed", "interrupted"])
def test_finished_turn_never_relaunches_or_accepts(setup, state):
    pacer, path, server = setup
    pacer.tick()
    thread = server.data["threads"][-1]
    thread.update(status=state, activeRunId=None)
    pacer.tick()  # May launch task 2, but never task 1 again.
    pacer.tick()
    assert len({c["threadId"] for c in server.calls}) == len(server.calls) == 2
    issues = TasksStore(path).read()["workspace"]["issues"]
    assert not any(i["accepted"] for i in issues)
    assert all(
        i["sourceState"] == "Scheduled"
        for i in issues
        if i["id"] in {"task-1", "task-2"}
    )


def test_observed_thread_logs_one_readable_line_and_keeps_json_at_debug(setup, caplog):
    pacer, path, server = setup
    pacer.tick()
    thread = server.data["threads"][-1]
    thread.update(
        status="completed",
        activeRunId=None,
        modelSelection={"instanceId": "codex", "model": "test-model"},
        lastError="workspace folder no longer exists",
        pendingRuntimeRequest={"id": "request-1", "kind": "user_input"},
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
        "status completed, needs response; "
        "error: workspace folder no longer exists"
    )
    assert "{" not in info
    [debug] = [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "DEBUG" and thread["id"] in r.getMessage()
    ]
    assert (
        json.loads(debug.split(": ", 1)[1])["pendingRuntimeRequest"]["id"]
        == "request-1"
    )


def test_thread_note_without_turn_or_provider():
    record = {
        "issue": "task-9",
        "tracker": "/repo/TASKS.md",
        "thread": {"id": "abc", "status": "starting", "activeRunId": None},
    }
    assert t3.thread_note(record) == (
        "task-9 from /repo/TASKS.md on abc: status starting"
    )


@pytest.mark.parametrize("flag", ["pendingRuntimeRequest", "pendingBackgroundTasks"])
def test_waiting_occupies_capacity(setup, flag):
    pacer, _, server = setup
    pacer.tick()
    server.data["threads"][-1].update(
        status="idle", activeRunId=None, **{flag: [{"id": "pending"}]}
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
    record_paced_thread(pacer, parent)
    parent["modelSelection"] = selection
    if activity == "running":
        parent["status"] = "running"
    elif activity == "approval":
        parent["pendingRuntimeRequest"] = {"id": "approval"}
    else:
        parent["pendingBackgroundTasks"] = [{"taskId": "background", "kind": "command"}]
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
        parent["modelSelection"]["instanceId"] = "unknown"
    elif block == "state":
        parent["status"] = "new-state"
    elif block == "total":
        pacer.config.concurrency_total = 1
        server.data["threads"].append(
            {
                **parent,
                "id": "other-provider",
                "status": "running",
                "modelSelection": {"provider": "claudeAgent", "model": "claude"},
            }
        )
        record_paced_thread(pacer, server.data["threads"][-1])
    elif block == "starting":
        parent["status"] = "starting"
        record_paced_thread(pacer, parent)
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
        input={"sourceState": "Completed / Accepted"},
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
    server.data["threads"][-1].update(status="idle", activeRunId=None)
    server.data["threads"][-1]["activeRunId"] = None
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
    busy.update(status="running", activeRunId="run")
    server.data["threads"].append(busy)
    record_paced_thread(pacer, busy)
    clock[0] += 60
    pacer.tick()
    assert len(server.calls) == 1
    assert "pending launch of task-1" in caplog.records[-1].getMessage()
    busy.update(status="idle", activeRunId=None)
    pacer.tick()
    first, second = server.calls
    assert first["threadId"] != second["threadId"]
    assert first["commandId"] != second["commandId"]
    branch = second["workspaceStrategy"]["branch"]
    assert branch == f"pacer-{second['threadId']}"
    assert first["initialMessage"]["text"] == second["initialMessage"]["text"]
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
    assert "Hai issue ID: task-1" in server.calls[-1]["initialMessage"]["text"]
    [record] = [json.loads(p.read_text()) for p in pacer.state.glob("*.json")]
    assert record["phase"] == "accepted"


def test_rejection_holds_tracker_and_others_launch_in_the_same_tick(setup, tmp_path):
    pacer, first, server = setup
    second = add_tracker(pacer, server, tmp_path / "repo2")
    server.broken.add(str(first.parent))
    pacer.tick()
    assert [project_of(c) for c in server.calls] == [
        "project",
        "project2",
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
    add_tracker(pacer, server, tmp_path / "repo2")
    pacer.tick()
    assert [project_of(c) for c in server.calls] == [
        "project",
        "project2",
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
    add_tracker(pacer, server, tmp_path / "repo2")
    # Stopping right after saving a launch record leaves it prepared.
    store, _ = pacer.trackers[str(first)]
    source = store.read()
    issue = t3.eligible(source)[0]
    parent, project = server.data["threads"][0], server.data["projects"][0]
    pacer.prepare(str(first), store, source, issue, parent, project)
    t3.git(first.parent, "switch", "-c", "feature")
    pacer.tick()
    assert [project_of(c) for c in server.calls] == ["project2"]
    [warning] = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert f"repository {first.parent} for tracker {first}" in warning
    assert "not on the configured base branch main" in warning


@pytest.mark.parametrize("phase", ["accepted", "observed"])
def test_missing_thread_holds_record_and_keeps_pacing(
    setup, tmp_path, clock, caplog, phase
):
    pacer, first, server = setup
    pacer.tick()
    [record_path] = pacer.state.glob("*.json")
    thread = server.data["threads"][-1]
    if phase == "observed":
        pacer.tick()
    record = json.loads(record_path.read_text())
    assert record["phase"] == phase
    server.data["threads"].pop()
    second = add_tracker(pacer, server, tmp_path / "second")
    pacer.config.t3_trackers.append(
        {"tracker": str(second), "parent_thread": "parent2"}
    )
    # The missing record must consume no capacity.
    pacer.config.concurrency_total = 1
    pacer.tick()
    record = json.loads(record_path.read_text())
    hold = record["hold"]
    assert hold["scope"] == ("issue" if phase == "observed" else "tracker")
    assert hold["retry_at"] is None
    assert record["phase"] == phase
    assert record["command"]["threadId"] == thread["id"]
    assert project_of(server.calls[-1]) == (
        "project" if phase == "observed" else "project2"
    )
    [line] = held_lines(caplog)
    for value in (str(record_path), str(first), "task-1", thread["id"]):
        assert value in line
    assert "Previously accepted thread is missing" in line

    # Even well past the failed-launch cooldown, the hold stays and cannot
    # relaunch the missing thread's issue, including after a pacer restart.
    caplog.clear()
    clock[0] += 2 * t3.REJECTION_COOLDOWN
    pacer = t3.T3Pacer(pacer.config, server)
    pacer.tick()
    assert len(server.calls) == 2
    assert json.loads(record_path.read_text())["hold"] == hold
    assert len(held_lines(caplog)) == 1

    # A thread restored to the snapshot clears its hold without another dispatch.
    server.data["threads"].append(thread)
    pacer.tick()
    restored = json.loads(record_path.read_text())
    assert restored["phase"] == "observed"
    assert "hold" not in restored
    assert len(server.calls) == 2


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


@pytest.mark.parametrize(
    "phase", [None, "prepared", "submitted", "accepted", "observed"]
)
def test_unreadable_tracker_skips_only_itself_and_retains_records(
    setup, tmp_path, caplog, monkeypatch, phase
):
    pacer, first, server = setup
    pacer.config.concurrency_total = 10
    if phase:
        with monkeypatch.context() as interrupted:

            def crash(*args):
                raise TimeoutError("interrupted launch")

            if phase in {"prepared", "submitted"}:
                interrupted.setattr(
                    pacer, "schedule" if phase == "prepared" else "dispatch", crash
                )
                with pytest.raises(TimeoutError):
                    pacer.tick()
            else:
                pacer.tick()
                if phase == "observed":
                    pacer.tick()
    pacer.config.concurrency = 10
    saved = {p: p.read_bytes() for p in pacer.state.glob("*.json")}
    if phase:
        assert json.loads(next(iter(saved.values())))["phase"] == phase
    second = add_tracker(pacer, server, tmp_path / "repo2")
    pacer.config.t3_trackers.append(
        {"tracker": str(second), "parent_thread": "parent2"}
    )
    original = first.read_text()
    first.write_text(
        original.replace("[1]: docs/tasks/1.md", "[1]:\n  docs/tasks/1.md")
    )
    t3.git(first.parent, "commit", "-am", "break link definition")
    calls_before = len(server.calls)

    # Both a running pacer and a restarted one must skip the bad tracker.
    for restart in (False, True):
        if restart:
            pacer = t3.T3Pacer(pacer.config, server)
        caplog.clear()
        pacer.tick()
        assert all(p.read_bytes() == data for p, data in saved.items())
        [warning] = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert f"skipping unreadable tracker {first}" in warning
        assert "Task [1] has no description reference" in warning
    assert [project_of(c) for c in server.calls[calls_before:]] == [
        "project2",
        "project2",
    ]

    first.write_text(original)
    t3.git(first.parent, "commit", "-am", "repair link definition")
    pacer.tick()
    assert project_of(server.calls[-1]) == "project"
    if saved:
        record = json.loads(next(iter(saved.values())))
        assert json.loads(next(iter(saved)).read_text())["command"] == record["command"]


@pytest.mark.parametrize("dump_order", [False, True])
def test_startup_and_dump_order_skip_unreadable_tracker(
    setup, tmp_path, caplog, capsys, monkeypatch, dump_order
):
    pacer, first, server = setup
    second = add_tracker(pacer, server, tmp_path / "repo2")
    pacer.config.t3_trackers.append(
        {"tracker": str(second), "parent_thread": "parent2"}
    )
    first.write_text(
        first.read_text().replace("[1]: docs/tasks/1.md", "[1]:\n  docs/tasks/1.md")
    )

    def stop_after_tick(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(t3, "Server", lambda *args: server)
    monkeypatch.setattr(t3.time, "sleep", stop_after_tick)
    assert t3.run(pacer.config, dump_order=dump_order) == 0
    if dump_order:
        rows = json.loads(capsys.readouterr().out)
        assert [row["tracker"] for row in rows] == [str(second), str(second)]
        assert not list(pacer.state.glob("*.json"))
    else:
        assert [project_of(c) for c in server.calls] == ["project2"]
    assert "Task [1] has no description reference" in caplog.text
    assert str(first) in caplog.text


def test_scheduled_before_backlog(setup):
    _, path, _ = setup
    store = TasksStore(path)
    source = store.read()
    store.mutate(
        "update", source["revision"], id="task-2", input={"sourceState": "Scheduled"}
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
        ({}, ".tracker must be a nonempty source path"),
        ({"tracker": ""}, ".tracker must be a nonempty source path"),
        ({"tracker": "  "}, ".tracker must be a nonempty source path"),
        ({"tracker": None}, ".tracker must be a nonempty source path"),
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


@pytest.mark.parametrize("problem", ["duplicate", "empty-parent"])
def test_startup_identifies_tracker_path_and_parent_errors(
    setup, monkeypatch, caplog, problem
):
    pacer, path, server = setup
    monkeypatch.setattr(t3, "Server", lambda *a: server)
    entry = {"tracker": str(path), "parent_thread": "parent"}
    if problem == "duplicate":
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
        input={"sourceState": "In Progress"},
    )
    t3.git(path.parent, "commit", "-am", "docs(tasks): start task-1 by hand")
    pacer = t3.T3Pacer(pacer.config, server)
    pacer.tick()
    # The tracker itself is fine, so its next issue launches in the same tick.
    assert len(server.calls) == 2
    assert "Hai issue ID: task-2" in server.calls[1]["initialMessage"]["text"]
    hold = json.loads(record_path.read_text())["hold"]
    assert hold["scope"] == "issue"
    assert "no longer eligible" in hold["reason"]
    server.data["threads"][-1]["activeRunId"] = None
    server.data["threads"][-1].update(status="idle", activeRunId=None)
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
    assert (
        "T3 [openai] quota bypass, concurrency only (0/1 paced), 0 active"
        in caplog.text
    )
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
        server.data["threads"][0]["status"] = "running"
        record_paced_thread(pacer, server.data["threads"][0])
    elif block == "total_cap":
        pacer.config.concurrency_total = 1
        server.data["threads"].append(
            {
                **server.data["threads"][0],
                "id": "claude",
                "modelSelection": {"instanceId": "claudeAgent", "model": "claude"},
                "status": "running",
            }
        )
        record_paced_thread(pacer, server.data["threads"][-1])
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
    assert (
        "T3 [openai] headroom=-0.300 PAUSED (0/1 paced), 0 active, resumes in"
        in caplog.text
    )
    assert "T3 [anthropic] headroom=+0.400 open (0/1 paced), 0 active" in caplog.text
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
    assert (
        "T3 [anthropic] quota unavailable, WITHHOLDING (0/1 paced), 0 active"
        in caplog.text
    )
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
    for settings in (command,):
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
                "status": "running",
            }
        )
        record_paced_thread(pacer, server.data["threads"][-1])
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
    for settings in (command,):
        assert settings["modelSelection"] == expected
        assert settings["runtimeMode"] == "approval-required"
        assert settings["interactionMode"] == "plan"
    assert command["projectId"] == "project"
    assert command["workspaceStrategy"]["baseRef"] == "main"
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
@pytest.mark.parametrize("pending", [False, True])
def test_tracker_requires_matching_project(
    setup, tmp_path, caplog, problem, with_parent, pending
):
    pacer, path, server = setup
    add_tracker(pacer, server, tmp_path / "repo2")
    project = copy.deepcopy(server.data["projects"][0])
    store, _ = pacer.trackers[str(path)]
    if pending:
        source = store.read()
        record_path, _ = pacer.prepare(
            str(path),
            store,
            source,
            t3.eligible(source)[0],
            server.data["threads"][0],
            project,
        )
        before_record = record_path.read_bytes()
    configure_models(pacer)
    pacer.config.t3_model = pacer.config.t3_models[0]
    pacer.config.t3_runtime_mode = "full-access"
    pacer.config.t3_interaction_mode = "default"
    if not with_parent:
        pacer.trackers[str(path)] = (store, None)
    if problem == "missing":
        server.data["projects"].remove(server.data["projects"][0])
    elif problem == "wrong-root":
        server.data["projects"][0]["workspaceRoot"] = "/other"
    else:
        server.data["projects"].append(
            {"id": "duplicate", "workspaceRoot": str(path.parent)}
        )
    source = path.read_bytes()
    for _ in range(2):
        caplog.clear()
        pacer.tick()
        [message] = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert str(path) in message
        assert f"workspaceRoot {str(path.parent)!r}" in message
        assert f"found {2 if problem == 'duplicate' else 0}" in message
        assert "retry next tick" in message
        assert "t3_url" in message
        if problem == "duplicate":
            assert "resolve duplicate workspace roots" in message
        else:
            assert "Open or add this directory as a project" in message
            assert "t3_trackers[].tracker" in message
    assert [project_of(c) for c in server.calls] == ["project2"]
    assert path.read_bytes() == source
    assert t3.git(path.parent, "status", "--porcelain") == ""
    if pending:
        assert record_path.read_bytes() == before_record
    server.data["threads"][-1].update(status="idle", activeRunId=None)
    server.data["projects"] = [
        project,
        server.data["projects"][0 if problem == "missing" else 1],
    ]
    pacer.tick()
    assert project_of(server.calls[-1]) == "project"
    assert len(server.calls) == 2


def test_empty_parent_is_invalid_configuration(setup):
    pacer, path, server = setup
    pacer.config.t3_trackers = [{"tracker": str(path), "parent_thread": ""}]
    with pytest.raises(t3.Withheld, match="parent_thread"):
        t3.T3Pacer(pacer.config, server)
    assert not server.calls


@pytest.mark.parametrize("problem", ["missing", "wrong-project"])
@pytest.mark.parametrize("pending", [False, True])
def test_bad_parent_skips_only_its_tracker(setup, tmp_path, caplog, problem, pending):
    pacer, path, server = setup
    add_tracker(pacer, server, tmp_path / "repo2")
    parent = copy.deepcopy(server.data["threads"][0])
    if pending:
        store, _ = pacer.trackers[str(path)]
        source = store.read()
        record_path, _ = pacer.prepare(
            str(path),
            store,
            source,
            t3.eligible(source)[0],
            parent,
            server.data["projects"][0],
        )
        before_record = record_path.read_bytes()
    if problem == "missing":
        server.data["threads"].pop(0)
    else:
        server.data["threads"][0]["projectId"] = "other"
    for _ in range(2):
        caplog.clear()
        pacer.tick()
        [message] = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert str(path) in message
        assert "parent_thread 'parent'" in message
        assert "set parent_thread to a thread in the matching T3 project" in message
        assert "remove it and configure t3_model" in message
    assert [project_of(c) for c in server.calls] == ["project2"]
    if pending:
        assert record_path.read_bytes() == before_record
    server.data["threads"] = [parent, server.data["threads"][-1]]
    server.data["threads"][-1].update(status="idle", activeRunId=None)
    pacer.tick()
    assert project_of(server.calls[-1]) == "project"
    assert len(server.calls) == 2


@pytest.mark.parametrize("pending", [False, True])
def test_unconfigured_records_are_retained_and_do_not_block(
    setup, tmp_path, caplog, pending
):
    pacer, path, server = setup
    add_tracker(pacer, server, tmp_path / "repo2")
    store, parent_id = pacer.trackers[str(path)]
    source = store.read()
    for issue in t3.eligible(source):
        record_path, record = pacer.prepare(
            str(path),
            store,
            store.read(),
            issue,
            server.data["threads"][0],
            server.data["projects"][0],
        )
        if not pending:
            pacer.launch(record_path, record, set(), t3.time.time())
    originals = {p: p.read_bytes() for p in pacer.state.glob("*.json")}
    del pacer.trackers[str(path)]
    caplog.set_level("INFO", logger="kandev-pacer")
    # Running retired threads still consume the only available slot.
    pacer.tick()
    assert len(server.calls) == (1 if pending else 2)
    for thread in server.data["threads"]:
        thread.update(status="idle", activeRunId=None)
    for _ in range(2):
        caplog.clear()
        pacer.tick()
        [message] = [
            r.getMessage() for r in caplog.records if "T3 ignoring" in r.getMessage()
        ]
        assert f"2 launch record(s) for unconfigured tracker {path}" in message
        assert "retained" in message
    assert project_of(server.calls[-1]) == "project2"
    assert len(server.calls) == (2 if pending else 3)
    assert all(p.read_bytes() == contents for p, contents in originals.items())
    # Returning config after restart uses the same records, with no new thread IDs.
    server.data["threads"][-1].update(status="idle", activeRunId=None)
    t3.T3Pacer(pacer.config, server).tick()
    if pending:
        assert server.calls[-1]["threadId"] in {
            json.loads(contents)["command"]["threadId"]
            for contents in originals.values()
        }
    else:
        assert len(server.calls) == 3


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
    assert "PAUSED (0/1 paced), 0 active, resumes in" in caplog.text
    assert ", burn ~9h 56m" in caplog.text


@pytest.mark.parametrize(
    "status",
    [
        "idle",
        "queued",
        "completed",
        "failed",
        "interrupted",
        "cancelled",
        "rolled_back",
    ],
)
def test_v2_parked_or_finished_thread_is_not_activity(status):
    assert not t3.activity({"status": status, "activeRunId": None})


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "preparing"},
        {"status": "starting"},
        {"status": "running"},
        {"status": "waiting"},
        {"activeRunId": "run"},
        {"activityRunStatus": "preparing"},
        {"activityRunStatus": "waiting"},
        {"pendingRuntimeRequest": {"id": "request", "kind": "approval"}},
        {"pendingBackgroundTasks": [{"taskId": "task", "kind": "command"}]},
    ],
)
def test_v2_activity_fields(fields):
    assert t3.activity({"status": "completed", "activeRunId": None, **fields})


def test_v1_snapshot_fails_closed():
    with pytest.raises(t3.Withheld, match="Incomplete T3 V2"):
        t3.activity({"session": {"status": "idle"}, "latestTurn": None})


def parked(server, run_id="paced-1", requested="2026-10-15T09:00:00Z", **fields):
    thread_id = "thread-" + run_id
    server.data["threads"].append(
        {
            **server.data["threads"][0],
            "id": thread_id,
            "status": "completed",
            "activeRunId": None,
            **fields,
        }
    )
    run = {
        "threadId": thread_id,
        "runId": run_id,
        "requestedAt": requested,
        "modelSelection": server.data["threads"][-1]["modelSelection"],
    }
    server.data["pacedRuns"].append(run)
    return run


def admit_server(pacer, server, monkeypatch, replies):
    """Verify durable IDs at the boundary and replay stored server receipts."""
    launch = server.call
    receipts = {}
    started = []

    def call(method, command):
        if method != "orchestration.dispatchCommand":
            return launch(method, command)
        assert command["type"] == "queued-run.admit"
        saved = [json.loads(p.read_text()) for p in pacer.admissions.glob("*.json")]
        assert any(r["command"] == command and r["phase"] == "sending" for r in saved)
        server.calls.append(copy.deepcopy(command))
        if command["commandId"] in receipts:
            return receipts[command["commandId"]]
        reply = replies.pop(0) if replies else {"sequence": 42}
        if isinstance(reply, Exception):
            raise reply
        receipts[command["commandId"]] = reply
        started.append(command["runId"])
        return reply

    monkeypatch.setattr(server, "call", call)
    return started, receipts


def test_paced_discovery_oldest_first_options_and_one_per_tick(setup, monkeypatch):
    pacer, _, server = setup
    later = parked(server, "later", "2026-10-15T10:00:00Z")
    first = parked(server, "first")
    first["modelSelection"] = {
        "instanceId": "codex",
        "model": "cheap",
        "options": [
            {"id": "effort", "value": "low"},
            {"id": "fastMode", "value": True},
        ],
    }
    started, _ = admit_server(pacer, server, monkeypatch, [])
    pacer.tick()
    assert started == ["first"]
    [record] = [json.loads(p.read_text()) for p in pacer.admissions.glob("*.json")]
    assert record["run"]["modelSelection"] == first["modelSelection"]
    assert record["sequence"] == 42
    assert server.calls == [
        {
            "type": "queued-run.admit",
            "commandId": "pace-admit:first:1",
            "threadId": first["threadId"],
            "runId": "first",
        }
    ]
    # Until activity appears, the successful admission reserves the only slot.
    pacer.tick()
    assert started == ["first"]
    assert later in server.data["pacedRuns"]


@pytest.mark.parametrize(
    "reason", ["busy", "not_next", "usage_limited", "previously rejected"]
)
def test_paced_refusal_retries_with_new_id_after_restart(setup, monkeypatch, reason):
    pacer, _, server = setup
    parked(server)
    message = (
        "Command was previously rejected"
        if reason == "previously rejected"
        else f"queued-run.admit refused: {reason}"
    )
    started, _ = admit_server(pacer, server, monkeypatch, [t3.T3Rejected(message)])
    pacer.tick()
    assert not started
    t3.T3Pacer(pacer.config, server).tick()
    assert started == ["paced-1"]
    assert [c["commandId"] for c in server.calls] == [
        "pace-admit:paced-1:1",
        "pace-admit:paced-1:2",
    ]


@pytest.mark.parametrize("reason", ["not_waiting", "inactive"])
def test_paced_terminal_refusal_is_dropped(setup, monkeypatch, reason):
    pacer, _, server = setup
    parked(server)
    admit_server(
        pacer,
        server,
        monkeypatch,
        [t3.T3Rejected(f"queued-run.admit refused: {reason}")],
    )
    pacer.tick()
    t3.T3Pacer(pacer.config, server).tick()
    admits = [c for c in server.calls if c.get("type") == "queued-run.admit"]
    assert len(admits) == 1
    [record] = [json.loads(p.read_text()) for p in pacer.admissions.glob("*.json")]
    assert record["phase"] == "dropped"


@pytest.mark.parametrize("accepted", [False, True])
def test_paced_timeout_replays_same_id_even_if_no_longer_listed(
    setup, monkeypatch, accepted
):
    pacer, _, server = setup
    run = parked(server)
    started, receipts = admit_server(
        pacer, server, monkeypatch, [TimeoutError("reply lost")]
    )
    with pytest.raises(TimeoutError):
        pacer.tick()
    if accepted:
        receipts[server.calls[0]["commandId"]] = {"sequence": 73}
        server.data["pacedRuns"] = []
        server.data["threads"][-1].update(status="running", activeRunId=run["runId"])
    t3.T3Pacer(pacer.config, server).tick()
    assert server.calls[0] == server.calls[1]
    assert started == ([] if accepted else [run["runId"]])
    [record] = [json.loads(p.read_text()) for p in pacer.admissions.glob("*.json")]
    assert record["phase"] == "accepted"
    assert record["sequence"] == (73 if accepted else 42)


def test_paced_success_running_snapshot_does_not_double_book(setup, monkeypatch):
    pacer, _, server = setup
    run = parked(server)
    admit_server(pacer, server, monkeypatch, [])
    pacer.tick()
    server.data["pacedRuns"] = []
    server.data["threads"][-1].update(status="running", activeRunId=run["runId"])
    pacer.tick()
    assert len(server.calls) == 1
    server.data["threads"][-1].update(status="completed", activeRunId=None)
    pacer.tick()
    assert len(server.calls) == 2  # A tracker launch can use the released slot.


def test_paced_success_unobserved_booking_expires(setup, monkeypatch):
    pacer, _, server = setup
    parked(server)
    admit_server(pacer, server, monkeypatch, [])
    pacer.tick()
    server.data["pacedRuns"] = []
    for _ in range(2):
        t3.T3Pacer(pacer.config, server).tick()
        assert len(server.calls) == 1
    t3.T3Pacer(pacer.config, server).tick()
    assert len(server.calls) == 2


def test_missing_hook_logs_once_and_empty_hook_is_supported(setup, caplog):
    pacer, _, server = setup
    del server.data["pacedRuns"]
    pacer.tick()
    pacer.tick()
    assert caplog.text.count("pacing hook unavailable") == 1
    caplog.clear()
    server.data["pacedRuns"] = []
    t3.T3Pacer(pacer.config, server).tick()
    assert "pacing hook unavailable" not in caplog.text


def test_paced_skips_usage_limit_and_unmappable_without_blocking_other_work(
    setup, monkeypatch, caplog
):
    pacer, _, server = setup
    parked(server, "limited", lastErrorClass="usage_limit")
    parked(server, "unknown", modelSelection={"instanceId": "custom", "model": "model"})
    parked(server, "eligible", "2026-10-15T10:00:00Z")
    started, _ = admit_server(pacer, server, monkeypatch, [])
    pacer.tick()
    assert started == ["eligible"]
    assert "unsupported provider instance 'custom'" in caplog.text


@pytest.mark.parametrize(
    "block", ["quota", "capacity", "total", "active", "error", "disabled", "apikey"]
)
def test_paced_uses_existing_admission_gates(setup, monkeypatch, block):
    pacer, _, server = setup
    parked(server)
    if block == "quota":
        monkeypatch.setattr(
            t3, "quota_verdict", lambda *a, **kw: Verdict("paused", -1, "paused")
        )
    elif block == "error":
        monkeypatch.setattr(
            t3.provider, "read", lambda *a, **kw: ({}, {"openai": "unavailable"})
        )
    elif block == "active":
        pacer.config.t3_active_limit = 1
        server.data["threads"][0]["status"] = "waiting"
    elif block == "disabled":
        pacer.config.providers_raw = "anthropic"
    elif block == "apikey":
        pacer.config.agent_providers_raw = "codex=__api_key__"
        pacer.config.apikey_policy = "withhold"
    else:
        server.data["threads"][0]["status"] = "running"
        record_paced_thread(pacer, server.data["threads"][0])
        if block == "total":
            pacer.config.concurrency = 2
            pacer.config.concurrency_total = 1
    admit_server(pacer, server, monkeypatch, [])
    pacer.tick()
    assert not server.calls


def test_paced_provider_is_waiting_for_resets_even_without_tracker_work(
    setup, monkeypatch
):
    pacer, path, server = setup
    parked(server)
    # The proposal section has no schedulable tracker work.
    path.write_text(path.read_text().replace("- [1] First\n- [2] Second\n", ""))
    seen = []

    def apply(usage, errors, waiting, *args):
        seen.append(waiting)
        return usage, errors

    monkeypatch.setattr(t3.resets, "apply", apply)
    admit_server(pacer, server, monkeypatch, [])
    pacer.tick()
    assert seen == [{"openai"}]


def test_paced_admission_precedes_pending_launch_with_closed_gate(setup, monkeypatch):
    pacer, path, server = setup
    source, parent, project = (
        TasksStore(path).read(),
        server.data["threads"][0],
        server.data["projects"][0],
    )
    _, record = pacer.prepare(
        str(path), TasksStore(path), source, t3.eligible(source)[0], parent, project
    )
    # The pending launch belongs to the other provider with no allowance.
    record["command"]["modelSelection"] = {
        "instanceId": "claudeAgent",
        "model": "claude",
    }
    t3.save(pacer.record_path(str(path), "task-1"), record)
    parked(server)
    started, _ = admit_server(pacer, server, monkeypatch, [])
    pacer.tick()
    assert started == ["paced-1"]
    assert len(server.calls) == 1


def test_archived_launch_is_observed_without_occupying_capacity(setup):
    pacer, _, server = setup
    pacer.tick()
    thread = server.data["threads"].pop()
    server.data["archivedThreads"].append(thread)
    pacer.tick()
    assert len(server.calls) == 2
    records = [json.loads(p.read_text()) for p in pacer.state.glob("*.json")]
    assert any(
        r["phase"] == "observed" and r["thread"]["id"] == thread["id"] for r in records
    )


def test_saved_v1_launch_is_held_before_v2_dispatch(setup, caplog):
    pacer, path, server = setup
    source = TasksStore(path).read()
    record_path, record = pacer.prepare(
        str(path),
        TasksStore(path),
        source,
        t3.eligible(source)[0],
        server.data["threads"][0],
        server.data["projects"][0],
    )
    record["command"]["bootstrap"] = {"old": "V1"}
    t3.save(record_path, record)
    pacer.tick()
    assert not server.calls
    assert "Saved V1 launch needs inspection" in caplog.text


def test_paced_id_save_failure_prevents_send(setup, monkeypatch):
    pacer, _, server = setup
    parked(server)

    def fail_save(*args):
        raise OSError("disk full")

    monkeypatch.setattr(t3, "save", fail_save)
    with pytest.raises(OSError, match="disk full"):
        pacer.tick()
    assert not server.calls


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "running", "activeRunId": "interactive"},
        {"status": "waiting"},
        {"pendingRuntimeRequest": {"id": "input"}},
        {"pendingBackgroundTasks": [{"id": "background"}]},
    ],
)
def test_interactive_threads_leave_two_paced_slots(setup, fields, caplog):
    pacer, _, server = setup
    pacer.config.concurrency = 2
    pacer.config.concurrency_total = 2
    parent = server.data["threads"][0]
    parent.update(fields)
    server.data["threads"].append({**parent, "id": "interactive-2"})
    pacer.config.agent_providers_raw = "codex=__api_key__"
    pacer.config.apikey_policy = "cap"
    caplog.set_level("INFO", logger="kandev-pacer")
    for _ in range(3):
        # Durable launch records must retain the count after restart.
        t3.T3Pacer(pacer.config, server).tick()
    assert len(server.calls) == 2
    assert "2/2 paced" in caplog.text
    assert "2/2 paced total, 4 active" in caplog.text


@pytest.mark.parametrize("limit, expected", [(None, 1), (1, 0), (2, 0), (3, 1)])
def test_optional_all_active_limit(setup, limit, expected, caplog):
    pacer, _, server = setup
    pacer.config.t3_active_limit = limit
    parent = server.data["threads"][0]
    parent.update(status="running", activeRunId="interactive")
    # An interactive provider need not be supported for pacing.
    server.data["threads"].append(
        {**parent, "id": "custom", "modelSelection": {"instanceId": "custom"}}
    )
    pacer.config.agent_providers_raw = "codex=__api_key__"
    pacer.config.apikey_policy = "cap"
    caplog.set_level("INFO", logger="kandev-pacer")
    pacer.tick()
    assert len(server.calls) == expected
    assert "0/1 paced), 2 active" in caplog.text


@pytest.mark.parametrize("total", [False, True])
def test_mixed_providers_count_only_paced_threads(setup, total):
    pacer, _, server = setup
    pacer.config.concurrency = 2
    pacer.config.concurrency_total = 2 if total else None
    parent = server.data["threads"][0]
    parent.update(status="running", activeRunId="interactive")
    server.data["threads"].append(
        {
            **parent,
            "id": "paced-claude",
            "modelSelection": {"instanceId": "claudeAgent", "model": "claude"},
        }
    )
    record_paced_thread(pacer, server.data["threads"][-1])
    for _ in range(3):
        pacer.tick()
    assert len(server.calls) == (1 if total else 2)


def test_active_limit_reserves_accepted_parked_run_until_visible(setup, monkeypatch):
    pacer, _, server = setup
    pacer.config.concurrency = 2
    pacer.config.t3_active_limit = 1
    parked(server)
    parked(server, "paced-2")
    started, _ = admit_server(pacer, server, monkeypatch, [])
    pacer.tick()
    assert started == ["paced-1"]
    # The snapshot can lag a successful admission. Reserve its active slot too.
    t3.T3Pacer(pacer.config, server).tick()
    assert started == ["paced-1"]


@pytest.mark.parametrize("value", [0, -1])
def test_all_active_limit_requires_positive_integer(value):
    with pytest.raises(ValueError, match="PACER_T3_ACTIVE_LIMIT"):
        Settings(PACER_T3_ACTIVE_LIMIT=value)
