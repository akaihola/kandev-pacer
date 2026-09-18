import json
import sqlite3
import subprocess
from argparse import Namespace

import pytest

from kandev_pacer import learn


@pytest.fixture
def sweep_args(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    subprocess.run(["git", "init", str(vault)], check=True, capture_output=True)
    for key, value in [
        ("user.name", "Fixture"),
        ("user.email", "fixture@example.invalid"),
    ]:
        subprocess.run(["git", "-C", str(vault), "config", key, value], check=True)
    subprocess.run(
        ["git", "-C", str(vault), "commit", "--allow-empty", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    db = tmp_path / "db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE tasks (id TEXT, archived_at TEXT)")
    state = tmp_path / "state"
    state.mkdir()
    return Namespace(
        db=db,
        vault=vault,
        state=state,
        home=tmp_path,
        dry_run=False,
        backfill=False,
        batch_size=1,
    )


def add(args, task, at):
    with sqlite3.connect(args.db) as db:
        db.execute("INSERT INTO tasks VALUES (?, ?)", (task, at))


def fake_write(args, task_id, at):
    path = learn.record_path(args.vault, task_id, at)
    learn.publish(
        path, "---\n" + json.dumps({"task": task_id, "archived_at": at}) + "\n---\n"
    )
    return path


def state(args):
    return json.loads((args.state / "state.json").read_text())


def test_dry_initialization_and_ties(sweep_args, monkeypatch):
    args = sweep_args
    add(args, "old", "2026-09-01")
    args.dry_run = True
    learn.sweep(args)
    assert not (args.state / "state.json").exists()
    args.dry_run = False
    learn.sweep(args)
    monkeypatch.setattr(learn, "write_task", fake_write)
    add(args, "one", "2026-09-02")
    add(args, "two", "2026-09-02")
    learn.sweep(args)
    learn.sweep(args)
    assert set(state(args)["boundary"]) == {"one", "two"}
    assert len(list((args.vault / learn.OBSERVATIONS).glob("*/*.md"))) == 2
    before = subprocess.check_output(
        ["git", "-C", str(args.vault), "rev-parse", "HEAD"]
    )
    learn.sweep(args)
    assert (
        subprocess.check_output(["git", "-C", str(args.vault), "rev-parse", "HEAD"])
        == before
    )


def test_rearchive_and_commit_recovery(sweep_args, monkeypatch):
    args = sweep_args
    learn.sweep(args)
    monkeypatch.setattr(learn, "write_task", fake_write)
    add(args, "task", "2026-09-02T00:00:00.001001Z")
    commit = learn.commit_records

    def fail(*_):
        raise RuntimeError("fixture commit failure")

    monkeypatch.setattr(learn, "commit_records", fail)
    with pytest.raises(RuntimeError):
        learn.sweep(args)
    assert state(args)["watermark"] == ""
    assert state(args)["pending"]
    monkeypatch.setattr(learn, "commit_records", commit)
    learn.sweep(args)
    with sqlite3.connect(args.db) as db:
        db.execute("UPDATE tasks SET archived_at=NULL")
    learn.sweep(args)
    with sqlite3.connect(args.db) as db:
        db.execute("UPDATE tasks SET archived_at='2026-09-02T00:00:00.002002Z'")
    learn.sweep(args)
    assert len(list((args.vault / learn.OBSERVATIONS).glob("*/*.md"))) == 2


def test_commit_preserves_user_index_and_disables_hooks(sweep_args):
    args = sweep_args
    user_file = args.vault / "unrelated"
    user_file.write_text("staged")
    subprocess.run(["git", "-C", str(args.vault), "add", "unrelated"], check=True)
    user_file.write_text("unstaged")
    hook = args.vault / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\ntouch hook-ran\n")
    hook.chmod(0o755)
    path = fake_write(args, "task", "2026-09-01")
    learn.commit_records(args.vault, [path])
    assert user_file.read_text() == "unstaged"
    assert (
        subprocess.check_output(["git", "-C", str(args.vault), "show", ":unrelated"])
        == b"staged"
    )
    assert not (args.vault / "hook-ran").exists()
    assert (
        subprocess.check_output(["git", "-C", str(args.vault), "status", "--porcelain"])
        .decode()
        .strip()
        == "AM unrelated"
    )


def test_never_overwrite_and_same_day_precision(tmp_path):
    first = learn.record_path(tmp_path, "task", "2026-09-01T00:00:00.001001Z")
    second = learn.record_path(tmp_path, "task", "2026-09-01T00:00:00.002002Z")
    assert first != second
    learn.publish(first, "first")
    learn.publish(first, "second")
    assert first.read_text() == "first"


def test_observation_precision_formatting(tmp_path):
    record = {
        "archived_at": "2026-09-01T00:00:00.123456789+02:00",
        "sessions": [
            {
                "start": "2026-09-01T00:00:00.123456789+02:00",
                "end": "2026-09-01T00:00:01.987654321+02:00",
                "signals": [{"at": "2026-09-01T00:00:00.123456789+02:00"}],
                "steps": [
                    {
                        "wall_seconds": 1.234567,
                        "preceding_pacer_wait": 0.0009876,
                        "totals": {"cost_usd": {"value": 1.23456}},
                    }
                ],
                "gaps": [{"start": "2026-09-01T00:00:00Z", "seconds": 2.34567, "ttl": 3600.1234}],
                "totals": {"cost_usd": {"value": 9.87654}},
            }
        ],
    }
    formatted = learn.formatted_record(record)
    session = formatted["sessions"][0]
    assert formatted["archived_at"] == "2026-08-31T22:00:00.123"
    assert session["start"] == "2026-08-31T22:00:00.123"
    assert session["end"] == "2026-08-31T22:00:01.987"
    assert session["signals"][0]["at"] == "2026-08-31T22:00:00.123"
    assert session["steps"][0]["wall_seconds"] == 1.235
    assert session["steps"][0]["preceding_pacer_wait"] == 0.001
    assert session["steps"][0]["totals"]["cost_usd"]["value"] == 1.235
    assert session["gaps"][0]["seconds"] == 2.346
    assert session["gaps"][0]["ttl"] == 3600.123
    assert learn.record_path(tmp_path, "task", record["archived_at"]).name == (
        "2026-08-31T22-00-00.123 task.md"
    )


def test_actual_cost_only():
    row = {
        "tokens_in": 1,
        "tokens_cached_read": 2,
        "tokens_cached_write": 3,
        "tokens_out": 4,
        "tokens_thought": 5,
        "cost_subcents": 10000,
    }
    totals = learn.usage_totals(
        [{**row, "estimated": 1, "cost_source": "models_dev_list"}]
    )
    assert totals["cost_usd"]["value"] is None
    totals = learn.usage_totals(
        [{**row, "estimated": 0, "cost_source": "provider_reported"}]
    )
    assert totals["cost_usd"]["value"] == 1


def test_backfill_has_separate_cursor(sweep_args, monkeypatch):
    args = sweep_args
    for task in ["one", "two"]:
        add(args, task, "2026-09-01")
    learn.sweep(args)
    monkeypatch.setattr(learn, "write_task", fake_write)
    args.backfill = True
    learn.sweep(args)
    assert len(state(args)["backfill_done"]) == 1
    assert state(args)["watermark"] == "2026-09-01"
    learn.sweep(args)
    assert len(state(args)["backfill_done"]) == 2


def test_step_usage_does_not_allocate_crossing_turn():
    turns = [
        {
            "id": "turn",
            "started_at": "2026-09-01T00:00:01Z",
            "completed_at": "2026-09-01T00:00:03Z",
        }
    ]
    event = {"turn_id": "turn", "occurred_at": "2026-09-01T00:00:03Z"}
    rows = learn.session_steps(
        [
            {
                "from_step_id": "a",
                "to_step_id": "b",
                "created_at": "2026-09-01T00:00:02Z",
            }
        ],
        "2026-09-01T00:00:00Z",
        "2026-09-01T00:00:04Z",
        [event],
        turns,
        [],
    )
    assert [r["turns"] for r in rows] == [0, 0]
    assert [r["wall_seconds"] for r in rows] == [2, 2]


def test_recover_after_head_update(sweep_args, monkeypatch):
    path = fake_write(sweep_args, "task", "2026-09-01")
    original = learn.command

    def fail_reset(args, **kwargs):
        if "reset" in args:
            raise RuntimeError("crash after update-ref")
        return original(args, **kwargs)

    monkeypatch.setattr(learn, "command", fail_reset)
    with pytest.raises(RuntimeError):
        learn.commit_records(sweep_args.vault, [path])
    monkeypatch.setattr(learn, "command", original)
    learn.commit_records(sweep_args.vault, [path])
    assert (
        subprocess.check_output(
            ["git", "-C", str(sweep_args.vault), "status", "--porcelain"]
        )
        == b""
    )


def test_ccusage_entries_are_windowed():
    raw = [
        {
            "entries": [
                {"timestamp": "2026-09-01", "inputTokens": 3},
                {"timestamp": "2026-09-03", "inputTokens": 100},
            ]
        }
    ]
    assert learn.ccusage_totals(raw, "2026-09-01", "2026-09-02")["input"]["value"] == 3


def test_unknown_task_and_corrupt_state(sweep_args):
    args = sweep_args
    assert (
        learn.main(["unknown", "--db", str(args.db), "--state", str(args.state)]) == 1
    )
    (args.state / "state.json").write_text("corrupt")
    with pytest.raises(ValueError):
        learn.sweep(args)


def test_archive_changes_during_collection(sweep_args, monkeypatch):
    args = sweep_args
    args.source_timeout = 1
    add(args, "task", "2026-09-01")

    def changed(*_):
        with sqlite3.connect(args.db) as db:
            db.execute("UPDATE tasks SET archived_at='2026-09-02'")
        return {"task": "task", "archived_at": "2026-09-02"}

    monkeypatch.setattr(learn, "collect_task", changed)
    assert learn.write_task(args, "task") is None
    assert not list(args.vault.rglob("*.md"))


def test_digest_aggregates_with_coverage(tmp_path, capsys):
    record = {
        "task": "task",
        "archived_at": "2026-09-01",
        "sessions": [
            {
                "id": "session",
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-09-01T00:00:02Z",
                "steps": [{"turns": 1}],
                "tools": [],
                "tool_calls": learn.measure(0, "fixture"),
                "transcript_ids": ["one"],
                "notes": [],
                "totals": {k: learn.measure(3, "fixture") for k in learn.METRICS},
            }
        ],
    }
    path = learn.record_path(tmp_path, "task", record["archived_at"])
    learn.publish(path, "---\n" + json.dumps(record) + "\n---\n")
    learn.digest(Namespace(vault=tmp_path, since="2026-09-01"))
    output = capsys.readouterr().out
    assert "| total |" in output
    assert '"value": 2.0' in output
    assert "1 sessions with data" in output
    learn.digest(Namespace(vault=tmp_path, since="2026-09-02"))
    assert "| task | 2026" not in capsys.readouterr().out


def test_render_record_scopes_session_ids_to_session_sections(tmp_path):
    session = {
        "id": "session-1",
        "notes": [],
        "totals": {k: learn.measure(3, "fixture") for k in learn.METRICS},
        "steps": [
            {
                "step": "step-id",
                "wall_seconds": 1,
                "preceding_pacer_wait": None,
                "turns": 1,
                "totals": {k: learn.measure(3, "fixture") for k in learn.METRICS},
            }
        ],
        "tools": [],
        "gaps": [],
        "signals": [{"at": "2026-09-01", "step": "step-id", "signal": "done"}],
        "blockers": {},
    }
    record = {
        "task": "task-uuid",
        "archived_at": "2026-09-01",
        "sessions": [session, {**session, "id": "session-2"}],
        "step_names": {"step-id": "Implement"},
        "diffs": [],
        "notes": [],
    }

    output = learn.render_record(record)

    assert learn.record_path(tmp_path, record["task"], record["archived_at"]).name.endswith(
        " task-uuid.md"
    )
    assert "Observation file ID: `task-uuid` (task ID)." in output
    assert output.count("## Session session-") == 2
    assert "| session |" not in output
    assert "| session-1 |" not in output
    assert "| session-2 |" not in output
    assert "| Implement | 1 | unknown | 1 |" in output
    assert '"step": "step-id"' in output
    assert "| Implement |" in output
    assert "## Step IDs" in output
    assert "| Implement | step-id |" in output


def test_retry_after_concurrent_head_change(sweep_args, monkeypatch):
    path = fake_write(sweep_args, "task", "2026-09-01")
    original = learn.command

    def advance_head(args, **kwargs):
        if "update-ref" in args:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(sweep_args.vault),
                    "commit",
                    "--allow-empty",
                    "-m",
                    "concurrent",
                ],
                check=True,
                capture_output=True,
            )
        return original(args, **kwargs)

    monkeypatch.setattr(learn, "command", advance_head)
    with pytest.raises(RuntimeError):
        learn.commit_records(sweep_args.vault, [path])
    monkeypatch.setattr(learn, "command", original)
    learn.commit_records(sweep_args.vault, [path])
    history = subprocess.check_output(
        ["git", "-C", str(sweep_args.vault), "log", "--format=%s"]
    ).decode()
    assert "concurrent" in history
    assert history.count("record archived") == 1


def test_sweep_rejects_existing_identity_conflict(sweep_args):
    args = sweep_args
    learn.sweep(args)
    add(args, "task", "2026-09-02")
    path = learn.record_path(args.vault, "task", "2026-09-02")
    learn.publish(path, '---\n{"task":"other","archived_at":"2026-09-02"}\n---\n')
    with pytest.raises(ValueError, match="identity conflict"):
        learn.sweep(args)
    assert state(args)["watermark"] == ""
    assert subprocess.check_output(["git", "-C", str(args.vault), "ls-files"]) == b""


def test_collect_preserves_usage_sources_and_command_types(tmp_path, monkeypatch):
    db_path = tmp_path / "kandev.db"
    with sqlite3.connect(db_path) as db:
        for schema in [
            "tasks (id, title, workflow_id, archived_at, archived_by_cascade_id, metadata)",
            "workflows (id, name)",
            "agents (id, name)",
            "agent_profiles (id, agent_id, model)",
            "task_sessions (id, task_id, agent_profile_id, started_at, completed_at, "
            "workspace_path, metadata, agent_profile_snapshot)",
            "session_step_history (id, session_id, created_at, from_step_id, to_step_id, metadata)",
            "task_usage_events (id, session_id, usage_event_id, occurred_at, "
            "turn_id, tokens_in, tokens_out, tokens_cached_read, "
            "tokens_cached_write, tokens_thought, estimated, cost_source, cost_subcents)",
            "task_session_turns (id, task_session_id, started_at, completed_at)",
            "task_session_commits (session_id)",
        ]:
            db.execute("CREATE TABLE " + schema)
        db.execute(
            "INSERT INTO tasks VALUES ('task', 'fixture', NULL, '2026-09-02', NULL, '{}')"
        )
        db.execute("INSERT INTO agents VALUES ('agent', 'claude-acp')")
        db.execute("INSERT INTO agent_profiles VALUES ('profile', 'agent', 'fixture')")
        db.execute(
            "INSERT INTO task_sessions VALUES ('s', 'task', 'profile', '2026-09-01', "
            "'2026-09-02', '/gone', '{}', '{}')"
        )
        db.execute(
            "INSERT INTO task_usage_events VALUES ('e', 's', 'e', "
            "'2026-09-01T01:00:00Z', NULL, 10, 0, 0, 0, 0, 0, 'provider_reported', 500)"
        )
    monkeypatch.setattr(
        learn,
        "collect_transcripts",
        lambda *_: {
            "models": [],
            "ids": ["one"],
            "notes": [],
            "requests": {},
            "tools": [{"id": "one", "called": True, "input": {"command": ["pwd"]}}],
            "ccusage": [{"inputTokens": 100}],
        },
    )
    record = learn.collect_task(db_path, "task", tmp_path)
    session = record["sessions"][0]
    assert session["totals"]["input"]["value"] == 100
    assert session["steps"][0]["totals"]["input"]["value"] == 10
    assert session["unattributed_usage"]["input"]["value"] == 0
    assert session["unattributed_usage"]["input"]["source"].startswith(
        "task_usage_events"
    )
    assert "command" not in session["tools"][0]


def test_boundary_turn_is_counted_once():
    steps = learn.session_steps(
        [
            {
                "created_at": "2026-09-01T01:00:00Z",
                "from_step_id": "one",
                "to_step_id": "two",
            }
        ],
        "2026-09-01",
        "2026-09-02",
        [],
        [
            {
                "id": "turn",
                "started_at": "2026-09-01T01:00:00Z",
                "completed_at": "2026-09-01T01:00:00Z",
            }
        ],
        [],
    )
    assert [step["turns"] for step in steps] == [0, 1]
