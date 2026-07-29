#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""SQLite round-trip tests for make_paced.py's glue (connect/resolve/load/apply).

The pure planning is covered in test_make_paced.py; this exercises the parts
that actually touch a database, on a throwaway temp file. Run:
    uv run tests/test_make_paced_db.py
"""
import json
import sqlite3
import tempfile
from pathlib import Path

from support import run_tests  # noqa: F401  -- sets up sys.path

from make_paced import apply_changes, connect, load_steps, plan_changes, resolve_workflow

# Just the columns load_steps SELECTs and apply_changes writes -- a minimal
# stand-in for kandev's real workflow_steps table.
SCHEMA = """
CREATE TABLE workflows (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE workflow_steps (
    id TEXT PRIMARY KEY, workflow_id TEXT, name TEXT, position INTEGER,
    events TEXT, is_start_step INTEGER DEFAULT 0, stage_type TEXT DEFAULT 'custom',
    agent_profile_id TEXT DEFAULT '', prompt TEXT DEFAULT '',
    auto_advance_requires_signal INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
"""
WORK_EVENTS = {"on_enter": [{"type": "auto_start_agent"}],
               "on_turn_complete": [{"type": "move_to_step", "config": {"step_id": "x"}}]}

def _make_db(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO workflows VALUES ('w1', 'Paced Kanban')")
    rows = [("bk", "Backlog", 0, {}, 1, "custom"),
            ("ip", "In Progress", 1, WORK_EVENTS, 0, "custom"),
            ("rv", "Review", 2, {}, 0, "custom")]
    for id, name, pos, ev, start, stage in rows:
        con.execute("INSERT INTO workflow_steps (id, workflow_id, name, position,"
                    " events, is_start_step, stage_type) VALUES (?,?,?,?,?,?,?)",
                    (id, "w1", name, pos, json.dumps(ev), start, stage))
    con.commit()
    con.close()

def test_apply_roundtrip_and_idempotent():
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "kandev.db"
        _make_db(db)

        # dry run opens read-only: an apply against it must fail, proving no writes leak.
        ro = connect(db, writable=False)
        try:
            ro.execute("UPDATE workflow_steps SET name='x'")
            raise AssertionError("read-only connection allowed a write")
        except sqlite3.OperationalError:
            pass
        ro.close()

        con = connect(db, writable=True)
        wf = resolve_workflow(con, "paced kanban")          # case-insensitive name
        assert wf["id"] == "w1"
        assert resolve_workflow(con, "w1")["id"] == "w1"     # exact id also resolves
        steps = load_steps(con, wf["id"])
        assert [s["id"] for s in steps] == ["bk", "ip", "rv"]  # ordered by position
        assert isinstance(steps[1]["events"], dict)          # events parsed from JSON

        changes = plan_changes(steps)
        # ip: strip auto_start + stage->work; rv: name "Review" retags custom->review.
        assert [c["id"] for c in changes] == ["ip", "rv"]
        apply_changes(con, changes)
        con.close()

        # reopen: the strip + stage_type landed and survived the transaction.
        con = connect(db, writable=True)
        after = load_steps(con, "w1")
        ip = next(s for s in after if s["id"] == "ip")
        assert ip["stage_type"] == "work"
        assert "on_enter" not in ip["events"]                # auto_start_agent gone
        assert ip["events"]["on_turn_complete"]              # routing kept
        assert plan_changes(after) == []                     # idempotent on the stored rows
        con.close()

def test_missing_db_exits():
    try:
        connect(Path(tempfile.gettempdir()) / "definitely-not-here.db", writable=False)
        raise AssertionError("connect on a missing db should SystemExit")
    except SystemExit:
        pass

def test_resolve_errors_exit():
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "kandev.db"
        _make_db(db)
        seed = sqlite3.connect(db)
        seed.execute("INSERT INTO workflows VALUES ('w2', 'Paced Kanban')")  # duplicate name
        seed.commit()
        seed.close()
        con = connect(db, writable=False)
        for needle in ("nope", "Paced Kanban"):              # missing, then ambiguous
            try:
                resolve_workflow(con, needle)
                raise AssertionError(f"resolve_workflow({needle!r}) should SystemExit")
            except SystemExit:
                pass
        con.close()

if __name__ == "__main__":
    run_tests(globals())
