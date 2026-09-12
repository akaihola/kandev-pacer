import json
import time
from pathlib import Path

import pytest

from kandev_pacer import learn_sources as sources

FIXTURES = Path(__file__).parent / "fixtures/learn"


@pytest.mark.parametrize("agent", ["claude", "codex", "opencode", "pi", "hermes"])
def test_tool_formats(agent):
    raw = json.loads((FIXTURES / f"{agent}.json").read_text())
    entries = sources.opencode_entries(raw)[0] if agent == "opencode" else raw
    calls = sources.tool_events(agent, entries)
    assert len(calls) == 2
    assert sum(c.get("error", False) for c in calls) == 1
    assert calls[0]["input"]["command"] == calls[1]["input"]["command"]
    assert calls[1]["output_bytes"] == 400
    assert len(calls[1]["excerpt"]) == 120


def test_encodings():
    assert sources.claude_slug("/a/b.c_d e:@!") == "-a-b-c-d-e---"
    assert sources.pi_slug("/a/b.c_d e:@!") == "--a-b.c_d e-@!--"
    assert sources.pi_slug("C:\\a\\b") == "--C--a-b--"
    assert sources.pi_slug("/a/") == "--a---"


def test_read_only_database(tmp_path):
    import sqlite3

    path = tmp_path / "db"
    sqlite3.connect(path).close()
    with sources.read_db(path) as db, pytest.raises(sqlite3.OperationalError):
        db.execute("CREATE TABLE forbidden (id)")


def test_deadline_and_subprocess_cleanup():
    with pytest.raises(TimeoutError), sources.deadline(0.02):
        time.sleep(1)
    with pytest.raises(TimeoutError), sources.deadline(0.02):
        sources.command(["bash", "-c", "sleep 10 & wait"])


def test_missing_store(tmp_path):
    session = {
        "agent": "hermes",
        "workspace_path": "/gone",
        "start": "2026-09-01",
        "end": "2026-09-02",
    }
    result = sources.collect_transcripts(tmp_path, session)
    assert "no transcript" in result["notes"]
    assert any(n.startswith("no data for hermes") for n in result["notes"])


def test_session_window(tmp_path, monkeypatch):
    path = tmp_path / ".claude/projects/-gone/a.jsonl"
    path.parent.mkdir(parents=True)
    entries = json.loads((FIXTURES / "claude.json").read_text())
    path.write_text("\n".join(json.dumps(e) for e in entries))
    monkeypatch.setattr(sources, "ccusage", lambda *_: [])
    session = {
        "agent": "claude",
        "workspace_path": "/gone",
        "start": "2026-09-01T00:00:01Z",
        "end": "2026-09-01T00:00:03Z",
    }
    result = sources.collect_transcripts(tmp_path, session)
    assert len(result["tools"]) == 1
    assert result["tools"][0]["id"].endswith(":two")


def test_hermes_metadata_survives_export_failure(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / ".hermes/profiles/tyko/state.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE sessions (id, cwd, started_at, ended_at, "
            "actual_cost_usd, tool_call_count, model)"
        )
        db.execute(
            "INSERT INTO sessions VALUES "
            "('one', '/gone', 1788220801, 1788220802, 0.5, 3, 'fixture')"
        )

    def unavailable(*_):
        raise FileNotFoundError()

    monkeypatch.setattr(sources, "export_session", unavailable)
    result = sources.collect_transcripts(
        tmp_path,
        {
            "agent": "hermes",
            "workspace_path": "/gone",
            "start": "2026-09-01",
            "end": "2026-09-02",
        },
    )
    assert result["hermes"]["tool_call_count"] == 3
    assert result["hermes"]["actual_cost_usd"] == 0.5
    assert "no transcript" in result["notes"]


def test_ambiguous_window_without_id(tmp_path):
    result = sources.collect_transcripts(
        tmp_path,
        {
            "agent": "claude",
            "workspace_path": "/gone",
            "start": "2026-09-01",
            "end": "2026-09-02",
            "ambiguous_window": True,
        },
    )
    assert result["tools"] == []
    assert any("ambiguous" in note for note in result["notes"])


def test_cache_import_and_pacer_attribution(tmp_path, monkeypatch):
    monkeypatch.setattr(sources.cache_waste, "CODEX_READ", None)
    log = tmp_path / ".local/state/kandev-pacer/pacer.jsonl"
    log.parent.mkdir(parents=True)
    base = sources.timestamp("2026-09-01")
    rows = [
        {"kind": "tick", "provider": "openai", "t": base + 200, "action": "withheld"},
        {
            "kind": "tick",
            "provider": "openai",
            "t": base + 400,
            "launched": {"task": "task", "session": "session"},
        },
    ]
    log.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows))
    session = {
        "id": "session",
        "agent": "codex",
        "workspace_path": "/gone",
        "models": ["fixture"],
        "start": "2026-09-01",
        "end": "2026-09-02",
    }
    requests = [
        (base, {"input_tokens": 10}),
        (base + 410, {"input_tokens": 20, "cached_input_tokens": 0}),
    ]
    waits, gaps = sources.pacer_observations(
        tmp_path, session, [(base, base + 10)], {"one": requests}, 1
    )
    assert waits[0]["wait"] == 390
    assert gaps[0]["pacer_withheld"] is True
    assert gaps[0]["ttl"] == sources.cache_waste.TTL_CODEX


def test_gap_survives_missing_pacer_log(tmp_path):
    base = sources.timestamp("2026-09-01")
    session = {
        "id": "one",
        "agent": "codex",
        "workspace_path": "/gone",
        "models": [],
        "start": "2026-09-01",
        "end": "2026-09-02",
    }
    waits, gaps = sources.pacer_observations(
        tmp_path, session, [], {"one": [(base, {}), (base + 500, {})]}, 1
    )
    assert waits == []
    assert gaps[0]["seconds"] == 500
    assert gaps[0]["pacer_withheld"] is None


def test_sqlite_source_deadline(tmp_path):
    import sqlite3

    path = tmp_path / "db"
    sqlite3.connect(path).close()
    began = time.monotonic()
    with (
        sources.read_db(path) as db,
        pytest.raises((sqlite3.OperationalError, TimeoutError)),
        sources.deadline(0.02),
    ):
        db.execute(
            "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n "
            "WHERE x<1000000000) SELECT sum(x) FROM n"
        ).fetchone()
    assert time.monotonic() - began < 1


def test_cache_gaps_do_not_cross_transcripts(tmp_path):
    base = sources.timestamp("2026-09-01")
    session = {
        "id": "one",
        "agent": "codex",
        "workspace_path": "/gone",
        "models": [],
        "start": "2026-09-01",
        "end": "2026-09-02",
    }
    _, gaps = sources.pacer_observations(
        tmp_path,
        session,
        [],
        {"a": [(base, {})], "b": [(base + 500, {})]},
        1,
    )
    assert gaps == []


@pytest.mark.parametrize(
    "output,error",
    [
        ('{"exit_code": 1, "output": "failure"}', True),
        ("Process exit code: 0\nOutput:\nProcess exited with code 1", False),
        ("Process exited with code 2\nOutput:\nfailed", True),
    ],
)
def test_codex_exit_envelope(output, error):
    entries = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "one",
                "output": output,
            },
        }
    ]
    assert sources.tool_events("codex", entries)[0]["error"] is error


def test_multiple_hermes_sessions_with_missing_export(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / ".hermes/profiles/tyko/state.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE sessions (id, cwd, started_at, ended_at, actual_cost_usd, "
            "tool_call_count, model)"
        )
        db.executemany(
            "INSERT INTO sessions VALUES (?, '/gone', 1788220801, ?, 0.5, 3, 'fixture')",
            [
                ("one", 1788220802),
                ("two", 1788220803),
                ("open", None),
            ],
        )

    def export(_home, _agent, identity, *_):
        if identity == "two":
            raise FileNotFoundError()
        return "\n".join(
            json.dumps(e) for e in json.loads((FIXTURES / "hermes.json").read_text())
        )

    monkeypatch.setattr(sources, "export_session", export)
    monkeypatch.setattr(sources, "ccusage", lambda *_: [])
    result = sources.collect_transcripts(
        tmp_path,
        {
            "agent": "hermes",
            "workspace_path": "/gone",
            "start": "2026-09-01",
            "end": "2026-09-02",
        },
    )
    assert result["hermes"]["actual_cost_usd"] == 1
    assert result["hermes"]["tool_call_count"] == 6
    assert result["ids"] == ["one"]
    assert len(result["tools"]) == 2
