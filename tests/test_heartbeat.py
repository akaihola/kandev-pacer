"""Offline database snapshots, real Hai trackers, and mocked Kandev writes."""

import copy
import json
import sqlite3
from datetime import UTC, datetime

import pytest
from test_t3 import tracker as make_tracker

from kandev_pacer import heartbeat as hb
from kandev_pacer import tasks_md

pytest.importorskip("hai_tasks")
NOW = 1791381600.0


@pytest.fixture
def setup(tmp_path):
    source = make_tracker(
        tmp_path / "repo",
        backlog="- [*] Task [4] tried to condense provider models\n"
        "- [2] Second\n    - Depends on: [1]\n",
    )
    (source.parent / "AGENTS.md").write_text("Issues are tracked in TASKS.md.\n")
    quota = tmp_path / "quota.json"
    quota.write_text(
        json.dumps(
            {
                "collected_at": NOW,
                "services": {
                    service: {"windows": {"7d": {"used_fraction": value}}}
                    for service, value in [("codex", 0.4), ("claude", 0.6)]
                },
            }
        )
    )
    db = tmp_path / "kandev.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE workspaces (id TEXT, name TEXT);
            CREATE TABLE workflows (id TEXT, name TEXT, workspace_id TEXT);
            CREATE TABLE agents (id TEXT, name TEXT);
            CREATE TABLE agent_profiles (id TEXT, name TEXT, agent_id TEXT,
                deleted_at TEXT, enabled INT, workspace_id TEXT);
            CREATE TABLE executor_profiles (id TEXT, name TEXT, executor_id TEXT);
            CREATE TABLE tasks (id TEXT, workspace_id TEXT, workflow_id TEXT,
                title TEXT, state TEXT, archived_at TEXT, external_id TEXT,
                created_at TEXT, parent_id TEXT);
            CREATE TABLE repositories (id TEXT, local_path TEXT);
            CREATE TABLE task_repositories (task_id TEXT, repository_id TEXT);
            CREATE TABLE task_sessions (task_id TEXT, state TEXT);
            CREATE TABLE automation_runs (task_id TEXT, status TEXT);
            INSERT INTO workspaces VALUES ('ws', 'Default Workspace');
            INSERT INTO agents VALUES ('codex', 'codex-acp'), ('claude', 'claude-acp');
            INSERT INTO executor_profiles VALUES ('exec', 'Worktree', 'exec-worktree');
        """)
        for kind, name in hb.WORKFLOWS.items():
            conn.execute("INSERT INTO workflows VALUES (?, ?, 'ws')", (kind, name))
        for name, agent in [
            ("Luna Medium", "codex"),
            ("Astra High", "codex"),
            ("Opus Medium", "claude"),
        ]:
            conn.execute(
                "INSERT INTO agent_profiles VALUES (?, ?, ?, NULL, 1, '')",
                (name, name, agent),
            )
        conn.execute(
            "INSERT INTO repositories VALUES ('repo', ?)", (str(source.parent),)
        )
    return source, db, quota


def insert(
    db,
    id,
    title="Work",
    external_id=None,
    state="TODO",
    archived=None,
    created=NOW,
    workspace="ws",
):
    created = datetime.fromtimestamp(created, UTC).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, 'complex', ?, ?, ?, ?, ?, '')",
            (id, workspace, title, state, archived, external_id, created),
        )
        conn.execute("INSERT INTO task_repositories VALUES (?, 'repo')", (id,))


def preflight(setup):
    source, db, quota = setup
    return hb.preflight(db, [source.parent], quota, now=NOW)


def test_complete_sources_dependencies_capacity_and_payload(setup):
    source, db, _ = setup
    insert(db, "heartbeat", hb.HEARTBEAT_TITLE)
    insert(db, "ordinary")
    insert(db, "other-workspace", workspace="elsewhere")
    result = preflight(setup)
    assert result["capacity"] == {"open": 1, "cap": 20, "remaining": 19}
    open_tasks = result["repositories"][0]["open_tasks"]
    assert [task["id"] for task in open_tasks] == ["ordinary"]
    assert "description" not in open_tasks[0]
    items = result["repositories"][0]["items"]
    first = next(
        item for item in items if item["key"] == "task-4-tried-to-condense-provider"
    )
    assert first["eligible"]
    assert not next(item for item in items if item["key"] == "2")["eligible"]
    payload = hb.create(
        result, source.parent, first["key"], "Fix the UI; test selection."
    )
    arguments = payload["arguments"]
    assert arguments["agent_profile_id"] == "Astra High"
    assert arguments["start_agent"] is False
    assert (
        arguments["external_id"] == "heartbeat:repo:task-4-tried-to-condense-provider"
    )
    assert first["bullet"] in arguments["description"]
    assert not ({"repository_id", "parent_id", "base_branch"} & arguments.keys())
    assert len(json.dumps(hb.digest(result))) < 4096


@pytest.mark.parametrize(
    "state, archived, expected",
    [
        ("TODO", None, "open"),
        ("COMPLETED", None, "collision"),
        ("TODO", "yesterday", "collision"),
    ],
)
def test_identity_reconciliation_and_finished_collision(
    setup, state, archived, expected
):
    source, db, _ = setup
    key = "task-4-tried-to-condense-provider"
    insert(
        db, "old", external_id=f"heartbeat:repo:{key}", state=state, archived=archived
    )
    result = preflight(setup)
    item = next(
        item for item in result["repositories"][0]["items"] if item["key"] == key
    )
    assert item["coverage"] == expected and not item["eligible"]
    if expected == "open":
        assert hb.create(result, source.parent, key, "details")["status"] == "verify"
    else:
        with pytest.raises(ValueError, match="not eligible"):
            hb.create(result, source.parent, key, "details")


def test_manual_task_title_coverage_and_capacity_limit(setup):
    insert(setup[1], "manual", title="Task [4] tried to condense provider models")
    result = preflight(setup)
    assert result["repositories"][0]["items"][1]["coverage"] == "open"
    for i in range(19):
        insert(setup[1], str(i))
    assert not any(
        item["eligible"] for item in preflight(setup)["repositories"][0]["items"]
    )


def test_numbered_dependencies_accept_unaccepted_completion(setup):
    source, _, _ = setup
    source.write_text(
        source.read_text().replace("- [2] Second", "- [1] Predecessor\n- [2] Second")
    )
    store = tasks_md.hai().TasksStore(source)
    snapshot = store.read()
    first = next(
        issue for issue in snapshot["workspace"]["issues"] if issue["id"] == "task-1"
    )
    store.mutate(
        "update",
        snapshot["revision"],
        id=first["id"],
        input={"sourceSection": "Completed / Accepted", "accepted": False},
    )
    assert next(
        item
        for item in preflight(setup)["repositories"][0]["items"]
        if item["key"] == "2"
    )["eligible"]


@pytest.mark.parametrize(
    "fractions, expected",
    [
        ((0.2, 0.3), "Astra High"),
        ((0.8, 0.3), "Opus Medium"),
        ((1.06, 1.07), "Luna Medium"),
        ((1.05, 1.1), "Astra High"),
    ],
)
def test_quota_profile_policy(setup, fractions, expected):
    quota = setup[2]
    data = json.loads(quota.read_text())
    for service, value in zip(("codex", "claude"), fractions, strict=True):
        data["services"][service]["windows"]["7d"]["used_fraction"] = value
    quota.write_text(json.dumps(data))
    assert hb.profile_choice(quota, NOW)["profile"] == expected
    with pytest.raises(ValueError, match="stale"):
        hb.profile_choice(quota, NOW + 901)


def test_rolling_limits_include_archived_tasks_and_expire():
    agents = "Create at most one new task from this repository in any 7 days."
    tasks = [
        {
            "external_id": "heartbeat:nixos-config:file",
            "id": "old",
            "created_at": NOW - 1,
            "state": "COMPLETED",
            "archived_at": "yesterday",
        }
    ]
    limited = hb.rate_limit(agents, tasks, "heartbeat:nixos-config:", NOW)
    assert limited["blocked"] and limited["matching_tasks"] == ["old"]
    assert not hb.rate_limit(agents, tasks, "heartbeat:nixos-config:", NOW + 7 * 86400)[
        "blocked"
    ]


def test_gate_skips_only_known_empty_or_open_consumers(setup, monkeypatch, tmp_path):
    result = preflight(setup)
    for item in result["repositories"][0]["items"]:
        item["eligible"] = False
    monkeypatch.setattr(hb, "preflight", lambda *a: result)
    for status in ("empty", "open", "ready", "blocked"):
        monkeypatch.setattr(hb, "observation_status", lambda *a, status=status: status)
        allowed, _ = hb.gate(setup[1], [], setup[2], "Default Workspace", tmp_path)
        assert allowed == (status in {"ready", "blocked"})
    result["errors"].append("Unreadable tracker")
    assert hb.gate(setup[1], [], setup[2], "Default Workspace", tmp_path)[0]


def test_binding_verification_and_closed_collision(setup, monkeypatch):
    source, _, _ = setup
    task = {
        "external_id": "heartbeat:repo:key",
        "workspace_id": "ws",
        "state": "TODO",
        "repositories": [{"repository_id": "repo"}],
    }
    repository = {"name": "repo", "local_path": str(source.parent)}
    monkeypatch.setattr(
        hb.client, "get", lambda path: repository if "repositories" in path else task
    )
    assert (
        hb.verify("task", task["external_id"], source.parent, "ws")["status"] == "open"
    )
    repository["local_path"] = "/another"
    with pytest.raises(ValueError, match="wrong repository"):
        hb.verify("task", task["external_id"], source.parent, "ws")
    task["state"] = "COMPLETED"
    with pytest.raises(ValueError, match="finished work"):
        hb.verify("task", task["external_id"], source.parent, "ws")


def test_archive_only_successful_finished_heartbeat_and_recheck_sessions(
    setup, monkeypatch
):
    _, db, _ = setup
    for id, title, status, state in [
        ("done", hb.HEARTBEAT_TITLE, "succeeded", "WAITING_FOR_INPUT"),
        ("running", hb.HEARTBEAT_TITLE, "succeeded", "RUNNING"),
        ("failed", hb.HEARTBEAT_TITLE, "failed", "FAILED"),
        ("ordinary", "Work", "succeeded", "WAITING_FOR_INPUT"),
    ]:
        insert(db, id, title)
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO automation_runs VALUES (?, ?)", (id, status))
            conn.execute("INSERT INTO task_sessions VALUES (?, ?)", (id, state))
    assert hb.archive_finished(db)["task_ids"] == ["done"]
    calls = []
    task = {"title": hb.HEARTBEAT_TITLE, "archived_at": None}
    monkeypatch.setattr(hb.client, "get", lambda path: copy.copy(task))
    monkeypatch.setattr(hb.client, "sessions_of", lambda id: [{"state": "RUNNING"}])
    monkeypatch.setattr(hb.client, "ws_request", lambda *a: calls.append(a))
    assert hb.archive_finished(db, apply=True)["task_ids"] == [] and not calls
    monkeypatch.setattr(
        hb.client, "sessions_of", lambda id: [{"state": "WAITING_FOR_INPUT"}]
    )

    def archive(action, arguments):
        calls.append((action, arguments))
        task["archived_at"] = "now"

    monkeypatch.setattr(hb.client, "ws_request", archive)
    assert hb.archive_finished(db, apply=True)["task_ids"] == ["done"]
    assert calls == [("task.archive", {"id": "done"})]


def test_failed_consumer_pending_batch_keeps_recovery_visible(setup, tmp_path):
    _, db, _ = setup
    vault = tmp_path / "vault"
    vault.mkdir()
    state = tmp_path / "consumer"
    state.mkdir()
    insert(
        db,
        "consumer",
        "[Auto] Learn from session observations",
        "consumer:batch",
        state="FAILED",
    )
    (state / "pending.json").write_text(
        json.dumps(
            {
                "vault": str(vault),
                "records": [{"record": "record.md"}],
                "arguments": {"external_id": "consumer:batch"},
            }
        )
    )
    assert hb.observation_status(db, "ws", vault, state) == "blocked"


def test_snapshot_connection_refuses_sql_writes(setup):
    db = hb.read_db(setup[1])
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM tasks")
    finally:
        db.close()


def test_colliding_six_word_keys_do_not_hide_other_work(setup):
    source, _, _ = setup
    source.write_text(
        source.read_text().replace(
            "- [2] Second",
            "- [*] Task [4] tried to condense provider differently\n"
            "- [*] Independent work\n- [2] Second",
        )
    )
    result = preflight(setup)
    items = result["repositories"][0]["items"]
    colliding = [
        item for item in items if item["key"] == "task-4-tried-to-condense-provider"
    ]
    assert len(colliding) == 2 and all(
        item["coverage"] == "collision" and not item["eligible"] for item in colliding
    )
    assert next(item for item in items if item["key"] == "independent-work")["eligible"]


def test_create_and_focused_preflight_cli_use_repository_paths(
    setup, monkeypatch, tmp_path, capsys
):
    source, db, quota = setup
    description = tmp_path / "description.txt"
    description.write_text("Repair the provider selector and test Firefox.")
    monkeypatch.setattr(hb.time, "time", lambda: NOW)
    common = ["--db", str(db), "--quota", str(quota)]
    assert (
        hb.main(
            common
            + [
                "create",
                str(source.parent),
                "task-4-tried-to-condense-provider",
                "--description-file",
                str(description),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["arguments"]["local_path"] == str(source.parent)
    assert hb.main(common + ["--repository", str(source.parent), "preflight"]) == 0
    assert len(json.loads(capsys.readouterr().out)["repositories"]) == 1
