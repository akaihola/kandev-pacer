import contextlib
import json
import os
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


def test_message_text_extracts_prompt_and_response():
    assert sources.message_text(
        "claude",
        {"type": "user", "message": {"content": [{"type": "text", "text": "start"}]}},
    ) == ("user", "start")
    assert sources.message_text(
        "codex",
        {"payload": {"type": "output_text", "text": "done"}},
    ) == ("assistant", "done")
    assert sources.message_text(
        "codex",
        {
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "start"}],
            }
        },
    ) == ("user", "start")


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


def test_codex_owner_rejected_before_next_line(tmp_path, monkeypatch):
    file = tmp_path / ".codex/sessions/rollout.jsonl"
    file.parent.mkdir(parents=True)
    file.touch()
    os.utime(file, (200, 200))
    consumed = []

    def lines():
        consumed.append("session_meta")
        yield json.dumps({"type": "session_meta", "payload": {"cwd": "/other"}})
        pytest.fail("read beyond mismatched session_meta")

    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path == file:
            return contextlib.closing(lines())
        return original_open(path, *args, **kwargs)

    def unexpected(*args):
        pytest.fail("downstream analysis of rejected transcript")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(sources.cache_waste, "codex_session", unexpected)
    monkeypatch.setattr(sources, "ccusage", unexpected)
    result = sources.collect_transcripts(
        tmp_path,
        {"agent": "codex", "workspace_path": "/gone", "start": 100, "end": 200},
    )
    assert consumed == ["session_meta"]
    assert result["ids"] == []
    assert result["notes"] == ["no transcript"]


@pytest.mark.parametrize("agent", ["claude", "codex", "pi"])
@pytest.mark.parametrize(
    "outside, full",
    [([], True), ([99], False), ([201], False), ([0], True), ([201, 150, 99], False)],
)
def test_streamed_window(tmp_path, monkeypatch, agent, outside, full):
    directory = {
        "claude": ".claude/projects/-gone",
        "codex": ".codex/sessions",
        "pi": ".pi/agent/sessions/--gone--",
    }[agent]
    file = tmp_path / directory / "rollout.jsonl"
    file.parent.mkdir(parents=True)
    entries = [{"timestamp": at} for at in [100, *outside, 200]]
    if agent == "codex":
        entries.insert(
            0,
            {
                "type": "session_meta",
                "timestamp": 100,
                "payload": {"cwd": "/gone", "id": "owned"},
            },
        )
    entries.extend([{}, {"timestamp": None}])
    file.write_text("\n".join(json.dumps(e) for e in entries) + "\n\ninvalid\n[]\n")
    os.utime(file, (200, 200))
    original_open = Path.open

    def streaming_open(path, *args, **kwargs):
        if path == file:
            # Only iteration is available; read()/readlines() would fail.
            @contextlib.contextmanager
            def stream():
                with original_open(path, *args, **kwargs) as source:
                    yield (line for line in source)

            return stream()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", streaming_open)
    usage_files = []

    def usage(*args):
        usage_files.extend(args[-1])
        return []

    monkeypatch.setattr(sources, "ccusage", usage)
    identity = "owned" if agent == "codex" else "rollout"
    window = sources.transcript_window(file, agent, "/gone", 100, 200)
    assert window == (
        identity,
        [
            e
            for e in entries
            if e.get("timestamp") is not None and 100 <= e["timestamp"] <= 200
        ],
        full,
    )
    result = sources.collect_transcripts(
        tmp_path,
        {"agent": agent, "workspace_path": "/gone", "start": 100, "end": 200},
    )
    assert result["ids"] == [identity]
    assert result["aggregate_ids"] == ([identity] if full or agent == "claude" else [])
    assert usage_files == ([file] if full or agent == "claude" else [])


@pytest.mark.parametrize("agent", ["claude", "codex", "pi"])
def test_transcript_mtime_before_start_never_opened(tmp_path, monkeypatch, agent):
    file = tmp_path / "old.jsonl"
    file.touch()
    os.utime(file, (99, 99))

    def unexpected(*args, **kwargs):
        pytest.fail("opened a transcript older than the window")

    monkeypatch.setattr(Path, "open", unexpected)
    assert sources.transcript_window(file, agent, "/gone", 100, 200) is None


@pytest.mark.parametrize("agent", ["claude", "codex", "pi"])
def test_transcript_empty_window_and_mtime_boundary(tmp_path, agent):
    file = tmp_path / "empty.jsonl"
    file.write_text(
        json.dumps(
            {"type": "session_meta", "timestamp": 99, "payload": {"cwd": "/gone"}}
        )
    )
    os.utime(file, (100, 100))
    assert sources.transcript_window(file, agent, "/gone", 100, 200) == (
        "empty",
        [],
        False,
    )


def test_codex_first_metadata_identity_and_missing_owner(tmp_path):
    file = tmp_path / "rollout.jsonl"
    entries = [
        {"timestamp": 100},
        {
            "type": "session_meta",
            "timestamp": 99,
            "payload": {"cwd": "/gone", "id": "first"},
        },
        {
            "type": "session_meta",
            "timestamp": 201,
            "payload": {"cwd": "/other", "id": "second"},
        },
    ]
    file.write_text("\n".join(json.dumps(e) for e in entries))
    os.utime(file, (200, 200))
    assert sources.transcript_window(file, "codex", "/gone", 100, 200) == (
        "first",
        [entries[0]],
        False,
    )
    file.write_text(json.dumps(entries[0]))
    os.utime(file, (200, 200))
    assert sources.transcript_window(file, "codex", "/gone", 100, 200) is None


def test_transcript_id_filter_precedes_loading(tmp_path, monkeypatch):
    file = tmp_path / ".codex/sessions/unrelated.jsonl"
    file.parent.mkdir(parents=True)
    file.touch()

    def unexpected(*args):
        pytest.fail("loaded a transcript excluded by ID")

    monkeypatch.setattr(sources, "transcript_window", unexpected)
    result = sources.collect_transcripts(
        tmp_path,
        {
            "agent": "codex",
            "workspace_path": "/gone",
            "start": 100,
            "end": 200,
            "ambiguous_window": True,
            "transcript_id": "expected",
        },
    )
    assert result["ids"] == []


@pytest.mark.parametrize("fail", [False, True])
def test_codex_ccusage_sees_only_selected_files(tmp_path, monkeypatch, fail):
    package = tmp_path / ".npm/_npx/prepared/node_modules/ccusage/package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"version": sources.CCUSAGE_VERSION}))
    selected = tmp_path / ".codex/sessions/2026/10/03/rollout-owned.jsonl"
    selected.parent.mkdir(parents=True)
    selected.write_text("selected")
    selected.with_name("unrelated.jsonl").write_text("unrelated")
    temporary = []

    def command(args, timeout, env):
        root = Path(env["CODEX_HOME"])
        temporary.append(root)
        files = list(root.rglob("*.jsonl"))
        assert len(files) == 1
        assert files[0].read_bytes() == selected.read_bytes()
        assert files[0].relative_to(root) == selected.relative_to(tmp_path / ".codex")
        assert args[-2:] == ["--since", "2026-10-03"]
        if fail:
            raise RuntimeError("ccusage failed")
        return json.dumps({"sessions": [{"sessionId": "rollout-owned"}]})

    monkeypatch.setattr(sources, "command", command)
    if fail:
        with pytest.raises(RuntimeError, match="ccusage failed"):
            sources.ccusage(tmp_path, ["owned"], 45, "codex", "2026-10-03", [selected])
    else:
        assert sources.ccusage(
            tmp_path, ["owned"], 45, "codex", "2026-10-03", [selected]
        ) == [{"sessions": [{"sessionId": "rollout-owned"}]}]
    assert len(temporary) == 1
    assert not temporary[0].exists()


def test_ccusage_without_aggregate_ids_does_not_scan(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("scanned for usage without aggregate IDs")

    monkeypatch.setattr(Path, "glob", unexpected)
    assert sources.ccusage(tmp_path, [], 45, "codex") == []


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


@pytest.mark.parametrize("agent", ["claude", "codex", "pi", "hermes", "opencode"])
def test_message_text_filters_nonconversation_data(agent):
    def entry(role, content, **extra):
        message = {"type": "message", "role": role, "content": content, **extra}
        return {"payload" if agent == "codex" else "message": message}

    assert sources.message_text(agent, entry("user", "hello\nworld")) == (
        "user",
        "hello\nworld",
    )
    for role in ["system", "developer", "tool", "toolResult"]:
        assert sources.message_text(agent, entry(role, "secret")) == (None, "")
    for channel in ["analysis", "commentary"]:
        assert sources.message_text(
            agent, entry("assistant", "internal", channel=channel)
        ) == (None, "")
    parts = [
        {"type": kind, "text": "secret", "content": "secret"}
        for kind in [
            "tool_use",
            "tool_result",
            "thinking",
            "image",
            "function_call_output",
        ]
    ]
    parts += [{"type": "text", "text": "answer"}, {"type": "text", "text": "continued"}]
    assert sources.message_text(agent, entry("assistant", parts)) == (
        "assistant",
        "answer\ncontinued",
    )


def test_opencode_keeps_message_boundaries():
    entries, _ = sources.opencode_entries(
        {
            "messages": [
                {
                    "info": {"role": "assistant"},
                    "parts": [{"type": "text", "text": text} for text in parts],
                }
                for parts in [["first", "second"], ["final"]]
            ]
        }
    )
    assert [sources.message_text("opencode", e) for e in entries] == [
        ("assistant", "first\nsecond"),
        ("assistant", "final"),
    ]


def test_collection_orders_transcripts_without_merging_messages(tmp_path, monkeypatch):
    directory = tmp_path / ".claude/projects/-gone"
    directory.mkdir(parents=True)
    for name, messages in [
        ("a", [(3, "assistant", "old answer"), (4, "assistant", "final")]),
        ("z", [(1, "user", "first"), (2, "user", "second")]),
    ]:
        entries = [
            {
                "timestamp": f"2026-09-01T00:00:0{second}Z",
                "type": role,
                "message": {"role": role, "content": text},
            }
            for second, role, text in messages
        ]
        (directory / f"{name}.jsonl").write_text(
            "\n".join(json.dumps(e) for e in entries)
        )
    monkeypatch.setattr(sources, "ccusage", lambda *_: [])
    result = sources.collect_transcripts(
        tmp_path,
        {
            "agent": "claude",
            "workspace_path": "/gone",
            "start": "2026-09-01",
            "end": "2026-09-02",
        },
    )
    assert result["messages"] == [
        ("user", "first"),
        ("user", "second"),
        ("assistant", "old answer"),
        ("assistant", "final"),
    ]


def test_hermes_messages_follow_session_time_not_database_order(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / ".hermes/profiles/tyko/state.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (id, cwd, started_at, ended_at)")
        db.executemany(
            "INSERT INTO sessions VALUES (?, '/gone', ?, ?)",
            [
                ("late", 1788220803, 1788220804),
                ("early", 1788220801, 1788220802),
            ],
        )

    def export(_home, _agent, identity, *_):
        return "\n".join(
            json.dumps({"message": {"role": role, "content": f"{identity} {role}"}})
            for role in ["user", "assistant"]
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
    assert result["messages"] == [
        ("user", "early user"),
        ("assistant", "early assistant"),
        ("user", "late user"),
        ("assistant", "late assistant"),
    ]


@pytest.mark.parametrize("agent", ["claude", "codex", "pi", "hermes", "opencode"])
def test_message_text_ignores_empty_envelopes(agent):
    key = "payload" if agent == "codex" else "message"
    assert sources.message_text(agent, {key: None}) == (None, "")
    for content in [None, [{"type": "text", "text": None}]]:
        entry = {key: {"type": "message", "role": "assistant", "content": content}}
        assert sources.message_text(agent, entry) == ("assistant", "")


def test_collection_skips_empty_messages_before_final_answer(tmp_path, monkeypatch):
    path = tmp_path / ".claude/projects/-gone/session.jsonl"
    path.parent.mkdir(parents=True)
    messages = [
        None,
        {"role": "user", "content": "prompt"},
        {"role": "assistant", "content": None},
        {"role": "assistant", "content": "final answer"},
    ]
    path.write_text(
        "\n".join(
            json.dumps({"timestamp": "2026-09-01T00:00:01Z", "message": message})
            for message in messages
        )
    )
    monkeypatch.setattr(sources, "ccusage", lambda *_: [])
    result = sources.collect_transcripts(
        tmp_path,
        {
            "agent": "claude",
            "workspace_path": "/gone",
            "start": "2026-09-01",
            "end": "2026-09-02",
        },
    )
    assert result["messages"] == [("user", "prompt"), ("assistant", "final answer")]
