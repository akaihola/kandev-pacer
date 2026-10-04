"""Real Hai trackers and Git commits; no paid turns or live credentials."""

import copy
import json
import subprocess

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

    def snapshot(self):
        return copy.deepcopy(self.data)

    def call(self, method, command):
        assert method == "orchestration.dispatchCommand"
        self.calls.append(copy.deepcopy(command))
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


def test_revision_conflict_prevents_dispatch(setup, monkeypatch):
    pacer, path, server = setup
    original = pacer.schedule

    def conflict(record_path, record):
        path.write_text(path.read_text() + "Concurrent edit\n")
        original(record_path, record)

    monkeypatch.setattr(pacer, "schedule", conflict)
    with pytest.raises(ValueError, match="revision changed"):
        pacer.tick()
    assert not server.calls
    with pytest.raises(ValueError, match="revision changed"):
        t3.T3Pacer(pacer.config, server).tick()
    assert "Concurrent edit" in path.read_text()


def test_overlapping_tracker_numbers(setup, tmp_path):
    pacer, first, server = setup
    second = tracker(tmp_path / "repo2")
    pacer.trackers[str(second)] = (TasksStore(second), "parent2")
    server.data["projects"].append(
        {"id": "project2", "workspaceRoot": str(second.parent)}
    )
    server.data["threads"].append(
        {**server.data["threads"][0], "id": "parent2", "projectId": "project2"}
    )
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
    assert "PACER_T3_TRACKERS needs tracker/parent_thread entries" in caplog.text


def test_startup_hides_unclassified_exception_contents(setup, monkeypatch, caplog):
    pacer, _, _ = setup

    def fail(*args):
        raise ValueError("private-token")

    monkeypatch.setattr(t3, "Server", fail)
    assert t3.run(pacer.config) == 1
    assert "ValueError" in caplog.text
    assert "private-token" not in caplog.text


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


def test_submitted_record_with_changed_tracker_withholds(setup):
    pacer, path, server = setup
    pacer.tick()
    record_path = next(pacer.state.glob("*.json"))
    record = json.loads(record_path.read_text())
    record["phase"] = "submitted"
    t3.save(record_path, record)
    server.data["threads"].pop()
    path.write_text(path.read_text() + "Changed while offline\n")
    with pytest.raises(ValueError, match="changed before dispatch"):
        t3.T3Pacer(pacer.config, server).tick()
    assert len(server.calls) == 1


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
    "overrides, expected",
    [
        ("codex-acp=__api_key__", True),
        ("codex-acp=__api_key__ codex=openai", False),
        ("claudeAgent=__api_key__", False),
    ],
)
def test_bypass_does_not_change_other_provider_gates(
    setup, monkeypatch, overrides, expected
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
        is expected
    )
    assert pacer.admitted(
        {"instanceId": "claudeAgent", "model": "claude-fable"}, Counter(), {}, {}, 0
    ) is (overrides == "claudeAgent=__api_key__")


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
