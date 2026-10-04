import errno
import json
import os
import select
import sqlite3
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from kandev_pacer import learn


@pytest.mark.parametrize("operation", ["mkdir", "open", "flock"])
@pytest.mark.parametrize("code", [errno.EROFS, errno.EACCES, errno.EPERM, errno.ENOSPC])
def test_lock_access_errors(tmp_path, monkeypatch, operation, code):
    path = tmp_path / "state/consumer.lock"
    if operation != "mkdir":
        path.parent.mkdir()
        path.write_text("keep lock contents")
    failed_path = path.parent if operation == "mkdir" else path
    error = OSError(code, os.strerror(code), str(failed_path))
    owner = learn.fcntl if operation == "flock" else Path
    original = getattr(owner, operation)
    handles = []

    def denied(target, *args, **kwargs):
        selected = (
            target.name == str(path) if operation == "flock" else target == failed_path
        )
        if selected:
            if operation == "flock":
                handles.append(target)
            raise error
        return original(target, *args, **kwargs)

    monkeypatch.setattr(owner, operation, denied)
    with pytest.raises(OSError) as raised, learn.locked(path):
        pytest.fail("entered without acquiring the lock")
    monkeypatch.undo()
    assert raised.value.errno == code
    assert raised.value.filename == str(failed_path)
    if code == errno.ENOSPC:
        assert raised.value is error
    else:
        assert raised.value.__cause__ is error
        message = str(raised.value)
        assert str(path) in message
        assert "sandbox" in message
        assert "same paths" in message
        assert (
            "read-only" in message if code == errno.EROFS else "permissions" in message
        )
    assert all(handle.closed for handle in handles)
    if operation != "mkdir":
        assert path.read_text() == "keep lock contents"


def test_lock_serializes_and_releases_after_body_error(tmp_path):
    path = tmp_path / "new/state/consumer.lock"
    error = OSError(errno.EROFS, "body write failed", str(tmp_path / "other"))
    child = None
    try:
        with pytest.raises(OSError) as raised, learn.locked(path):
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    """
import fcntl
import sys
with open(sys.argv[1], "a") as file:
    try:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("blocked", flush=True)
    else:
        raise AssertionError("second process acquired the held lock")
    fcntl.flock(file, fcntl.LOCK_EX)
    print("acquired", flush=True)
""",
                    str(path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert child.stdout is not None
            assert select.select([child.stdout], [], [], 5)[0], (
                "child did not attempt lock"
            )
            assert child.stdout.readline() == "blocked\n"
            assert child.poll() is None
            raise error
        assert raised.value is error
        assert child is not None
        output, errors = child.communicate(timeout=5)
        assert child.returncode == 0, errors
        assert output == "acquired\n"
        with learn.locked(path):
            assert path.exists()
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


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
        path.with_suffix(".json"), json.dumps({"task": task_id, "archived_at": at})
    )
    learn.publish(path, "# Session observations\n")
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
    learn.commit_records(args.vault, [path, path.with_suffix(".json")])
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


def test_observation_text_is_bounded_deterministically():
    record = {
        "sessions": [
            {
                "initial_prompt": "p" * 2000,
                "final_response": "r" * 2000,
            }
        ]
    }
    session = learn.formatted_record(record)["sessions"][0]
    assert session["initial_prompt"] == "p" * 997 + "..."
    assert session["final_response"] == "r" * 997 + "..."
    assert len(session["initial_prompt"]) == learn.TEXT_LIMIT
    assert len(session["final_response"]) == learn.TEXT_LIMIT


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
        learn.commit_records(sweep_args.vault, [path, path.with_suffix(".json")])
    monkeypatch.setattr(learn, "command", original)
    learn.commit_records(sweep_args.vault, [path, path.with_suffix(".json")])
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
    assert output.count("> No data.") == 4
    assert "| session |" not in output
    assert "| session-1 |" not in output
    assert "| session-2 |" not in output
    assert "| Implement | 1 | unknown | 1 |" in output
    assert '"step": "step-id"' in output
    assert "| Implement |" in output
    assert "## Appendix: Step IDs" in output
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
        learn.commit_records(sweep_args.vault, [path, path.with_suffix(".json")])
    monkeypatch.setattr(learn, "command", original)
    learn.commit_records(sweep_args.vault, [path, path.with_suffix(".json")])
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
            "messages": [
                ("user", "first"),
                ("assistant", "earlier"),
                ("user", "later"),
                ("assistant", "final"),
            ],
        },
    )
    record = learn.collect_task(db_path, "task", tmp_path)
    session = record["sessions"][0]
    assert session["initial_prompt"] == "first"
    assert session["final_response"] == "final"
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


@pytest.fixture
def observation():
    return {
        "task": "task",
        "archived_at": "2026-09-01T00:00:00.123456Z",
        "notes": [],
        "diffs": [],
        "sessions": [
            {
                "id": "session",
                "initial_prompt": "first line\n" + "é" * 1200,
                "final_response": "answer\n" + "r" * 1200,
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-09-01T00:00:02Z",
                "notes": [],
                "steps": [],
                "tools": [],
                "gaps": [],
                "signals": [],
                "blockers": {},
                "transcript_ids": ["one"],
                "tool_calls": learn.measure(0, "fixture"),
                "totals": {
                    k: learn.measure(1.234567, "fixture") for k in learn.METRICS
                },
            }
        ],
    }


def test_split_write_and_cli(sweep_args, observation, monkeypatch, capsys):
    args = sweep_args
    add(args, "task", observation["archived_at"])
    monkeypatch.setattr(learn, "collect_task", lambda *_: observation)
    assert (
        learn.main(
            [
                "task",
                "--db",
                str(args.db),
                "--vault",
                str(args.vault),
                "--state",
                str(args.state),
            ]
        )
        == 0
    )
    path = learn.record_path(args.vault, "task", observation["archived_at"])
    assert str(path) in capsys.readouterr().out
    assert json.loads(path.with_suffix(".json").read_text()) == observation
    assert learn.read_record(path) == observation
    markdown = path.read_text()
    assert markdown.startswith("# Session observations")
    assert "[JSON](2026-09-01T00-00-00.123%20task.json)" in markdown
    assert '"totals"' not in markdown
    for key, heading in [
        ("initial_prompt", "Initial user prompt"),
        ("final_response", "Final answer"),
    ]:
        excerpt = markdown.split(f"### {heading}\n\n", 1)[1].split("\n\n###", 1)[0]
        text = "\n".join(line.removeprefix("> ") for line in excerpt.splitlines())
        assert text == observation["sessions"][0][key][:997] + "..."
        assert len(text) == learn.TEXT_LIMIT


def test_split_sweep_recovers_publication_and_commit(
    sweep_args, observation, monkeypatch
):
    args = sweep_args
    args.source_timeout = 1
    learn.sweep(args)
    add(args, "task", observation["archived_at"])
    monkeypatch.setattr(learn, "collect_task", lambda *_: observation)
    publish = learn.publish

    def interrupted(path, content):
        if path.suffix == ".md":
            raise OSError("interrupted")
        publish(path, content)

    monkeypatch.setattr(learn, "publish", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        learn.sweep(args)
    assert state(args)["pending"]
    path = learn.record_path(args.vault, "task", observation["archived_at"])
    assert path.with_suffix(".json").exists() and not path.exists()
    monkeypatch.setattr(learn, "publish", publish)
    monkeypatch.setattr(learn, "collect_task", lambda *_: pytest.fail("recollected"))
    commit = learn.commit_records

    def failed_commit(*_):
        raise RuntimeError("commit")

    monkeypatch.setattr(learn, "commit_records", failed_commit)
    with pytest.raises(RuntimeError, match="commit"):
        learn.sweep(args)
    before = (path.read_bytes(), path.with_suffix(".json").read_bytes())
    monkeypatch.setattr(learn, "commit_records", commit)
    learn.sweep(args)
    assert not state(args)["pending"]
    assert before == (path.read_bytes(), path.with_suffix(".json").read_bytes())
    committed = (
        subprocess.check_output(
            ["git", "-C", str(args.vault), "ls-tree", "-r", "--name-only", "HEAD"]
        )
        .decode()
        .splitlines()
    )
    index = args.vault / learn.OBSERVATIONS / "index.md"
    assert committed == sorted(
        str(p.relative_to(args.vault)) for p in [path, path.with_suffix(".json"), index]
    )


@pytest.mark.parametrize(
    "sidecar", [None, "broken", "[]", '{"task":"wrong","archived_at":"2026-09-01"}']
)
def test_split_rejects_missing_corrupt_or_conflicting_json(sweep_args, sidecar):
    args = sweep_args
    learn.sweep(args)
    add(args, "task", "2026-09-01")
    path = learn.record_path(args.vault, "task", "2026-09-01")
    learn.publish(path, "# Session observations\n")
    if sidecar is not None:
        learn.publish(path.with_suffix(".json"), sidecar)
    with pytest.raises(ValueError):
        learn.sweep(args)
    assert state(args)["pending"]


def test_legacy_identity_and_mixed_digest(sweep_args, observation, capsys):
    args = sweep_args
    add(args, "task", observation["archived_at"])
    path = learn.record_path(args.vault, "task", observation["archived_at"])
    legacy = learn.formatted_record(observation)
    learn.publish(path, "---\n" + json.dumps(legacy) + "\n---\n")
    with pytest.raises(ValueError, match="precision"):
        learn.write_task(args, "task")
    assert not path.with_suffix(".json").exists()
    new_record = {**observation, "task": "second"}
    second = learn.record_path(args.vault, "second", observation["archived_at"])
    learn.publish(second.with_suffix(".json"), json.dumps(new_record))
    learn.publish(second, learn.render_record(new_record))
    learn.digest(Namespace(vault=args.vault, since="2026-09-01"))
    assert "2 sessions with data" in capsys.readouterr().out
    with pytest.raises(ValueError, match="identity conflict"):
        learn.checked_record(second, "second", "2026-09-01T00:00:00.123999Z")
    with pytest.raises(ValueError, match="identity conflict"):
        learn.checked_record(path, "task", "2026-09-01T00:00:00.124Z")


def test_archive_changes_during_json_recovery(sweep_args, observation, monkeypatch):
    args = sweep_args
    add(args, "task", observation["archived_at"])
    path = learn.record_path(args.vault, "task", observation["archived_at"])
    learn.publish(path.with_suffix(".json"), json.dumps(observation))
    read = learn.read_record

    def changed(path):
        with sqlite3.connect(args.db) as db:
            db.execute("UPDATE tasks SET archived_at='2026-09-02'")
        return read(path)

    monkeypatch.setattr(learn, "read_record", changed)
    assert learn.write_task(args, "task") is None
    assert not path.exists()


@pytest.mark.parametrize("length", [0, 999, 1000, 1001])
def test_prompt_display_limit_boundary(observation, length):
    text = "é" * length
    observation["sessions"][0].update(initial_prompt=text, final_response=text)
    expected = text if length <= learn.TEXT_LIMIT else text[:997] + "..."
    formatted = learn.formatted_record(observation)
    assert formatted["sessions"][0]["initial_prompt"] == expected
    assert formatted["sessions"][0]["final_response"] == expected
    assert observation["sessions"][0]["initial_prompt"] == text


@pytest.mark.parametrize("action", ["task", "sweep", "backfill"])
@pytest.mark.parametrize(
    "stored",
    [
        {"task": "other", "archived_at": "2026-09-01T00:00:00.123456Z"},
        {"task": "task", "archived_at": "2026-09-01T00:00:00.123999Z"},
        {"task": "task"},
        {"task": "task", "archived_at": 42},
    ],
)
def test_identity_cli_diagnostic(sweep_args, capsys, action, stored):
    args = sweep_args
    at = "2026-09-01T00:00:00.123456Z"
    if action != "backfill":
        learn.sweep(args)
    add(args, "task", at)
    if action == "backfill":
        learn.sweep(args)
    before_state = state(args)
    path = fake_write(args, "task", at)
    sidecar = path.with_suffix(".json")
    sidecar.write_text(json.dumps(stored))
    before = (path.read_bytes(), sidecar.read_bytes())
    head = subprocess.check_output(["git", "-C", str(args.vault), "rev-parse", "HEAD"])
    argv = [
        "sweep" if action == "backfill" else action,
        "--db",
        str(args.db),
        "--vault",
        str(args.vault),
        "--state",
        str(args.state),
    ]
    if action == "backfill":
        argv.append("--backfill")
    capsys.readouterr()
    assert learn.main(argv) == 1
    error = capsys.readouterr().err
    for expected in (
        "observation identity conflict",
        str(path),
        str(sidecar),
        str(args.db),
        "Expected",
        "task='task'",
        repr(at),
        "Stored",
        repr(stored.get("task")),
        repr(stored.get("archived_at")),
        "--db",
        "--vault",
        "outside",
        "both",
        "same command",
    ):
        assert expected in error
    assert "Traceback" not in error
    assert before == (path.read_bytes(), sidecar.read_bytes())
    assert head == subprocess.check_output(
        ["git", "-C", str(args.vault), "rev-parse", "HEAD"]
    )
    after = state(args)
    for key in ("watermark", "boundary", "backfill_done"):
        assert after.get(key) == before_state.get(key)
    if action != "task":
        assert after["backfill_pending" if action == "backfill" else "pending"]


def test_legacy_same_millisecond_is_ambiguous(sweep_args, observation):
    args = sweep_args
    at = observation["archived_at"]
    path = learn.record_path(args.vault, "task", at)
    learn.publish(
        path, "---\n" + json.dumps(learn.formatted_record(observation)) + "\n---\n"
    )
    other = "2026-09-01T00:00:00.123999Z"
    assert learn.record_path(args.vault, "task", other) == path
    before = path.read_bytes()
    for expected in (at, other):
        with pytest.raises(ValueError, match="precision"):
            learn.checked_record(path, "task", expected)
    assert path.read_bytes() == before


def test_identity_backup_recovery_and_exact_retry(sweep_args, observation, monkeypatch):
    args = sweep_args
    args.source_timeout = 1
    at = observation["archived_at"]
    add(args, "task", at)
    path = fake_write(args, "task", at)
    sidecar = path.with_suffix(".json")
    sidecar.write_text(json.dumps({**observation, "task": "other"}))
    with pytest.raises(ValueError, match="move both"):
        learn.write_task(args, "task")
    backup = args.home / "backup"
    backup.mkdir()
    for file in (path, sidecar):
        file.rename(backup / file.name)
    monkeypatch.setattr(learn, "collect_task", lambda *_: observation)
    assert learn.write_task(args, "task") == path
    before = (path.read_bytes(), sidecar.read_bytes())
    monkeypatch.setattr(
        learn, "collect_task", lambda *_: pytest.fail("retry collected")
    )
    assert learn.write_task(args, "task") == path
    learn.sweep(args)
    args.backfill = True
    learn.sweep(args)
    assert before == (path.read_bytes(), sidecar.read_bytes())
    assert json.loads(sidecar.read_text()) == observation
    assert len(list((args.vault / learn.OBSERVATIONS).glob("*/*.md"))) == 1
    assert len(list(args.vault.rglob("*.json"))) == 1
    assert json.loads((backup / sidecar.name).read_text())["task"] == "other"


def test_exact_legacy_identity_retry(sweep_args, observation):
    args = sweep_args
    at = observation["archived_at"]
    add(args, "task", at)
    path = learn.record_path(args.vault, "task", at)
    learn.publish(path, "---\n" + json.dumps(observation) + "\n---\n")
    before = path.read_bytes()
    assert learn.write_task(args, "task") == path
    assert path.read_bytes() == before
    assert not path.with_suffix(".json").exists()


@pytest.mark.parametrize("content", ["{broken", "[]", "{}"])
def test_invalid_metadata_diagnostic(sweep_args, capsys, content):
    args = sweep_args
    add(args, "task", "2026-09-01")
    path = fake_write(args, "task", "2026-09-01")
    sidecar = path.with_suffix(".json")
    sidecar.write_text(content)
    assert (
        learn.main(
            [
                "task",
                "--db",
                str(args.db),
                "--vault",
                str(args.vault),
                "--state",
                str(args.state),
            ]
        )
        == 1
    )
    error = capsys.readouterr().err
    assert str(sidecar) in error
    assert "Expected task='task', archived_at='2026-09-01'" in error
    assert "move both" in error
    assert "Traceback" not in error
    assert sidecar.read_text() == content


def test_sweep_writes_index_with_records(sweep_args, observation, monkeypatch):
    args = sweep_args
    legacy = learn.record_path(args.vault, "legacy", "2026-08-31T23:59:00Z")
    learn.publish(
        legacy,
        "---\n"
        + json.dumps(
            {"task": "legacy", "archived_at": "2026-08-31T23:59:00Z", "title": "Old"}
        )
        + "\n---\n",
    )
    broken = learn.record_path(args.vault, "broken", "2026-08-30T00:00:00Z")
    learn.publish(broken, "# no metadata\n")
    add(args, "task", observation["archived_at"])
    record = {**observation, "title": "[Auto] Fix a|b", "workflow": "Paced"}
    monkeypatch.setattr(learn, "collect_task", lambda *_: record)
    learn.main(
        ["sweep", "--backfill", "--db", str(args.db), "--vault", str(args.vault)]
        + ["--state", str(args.state)]
    )
    index = args.vault / learn.OBSERVATIONS / "index.md"
    text = index.read_text()
    assert text.index("## 2026-09") < text.index("## 2026-08")
    row = "| [(Auto) Fix a/b](2026-09/2026-09-01T00-00-00.123%20task.md) | Paced |"
    assert row in text
    assert "[Old](2026-08/2026-08-31T23-59-00.000%20legacy.md)" in text
    assert "broken" not in text
    committed = subprocess.run(
        ["git", "-C", str(args.vault), "show", "--name-only", "--format=", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(learn.OBSERVATIONS / "index.md") in committed
    assert "task.json" in committed
    before = index.stat().st_mtime_ns
    learn.write_index(args.vault)
    assert index.stat().st_mtime_ns == before
