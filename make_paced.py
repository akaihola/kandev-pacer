#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Adapt an existing Kandev workflow for pacing (Design 13) via its SQLite DB.

Kandev has no supported surface for one of the two edits pacing needs
(`stage_type` is display-only in the UI, absent from the step APIs and the
import format -- see docs/pacing-designs.md, Design 13), so this script edits
the database directly. Per step it:

- strips every `auto_start_agent` action from the step's events, so a finished
  turn parks the task WAITING_FOR_INPUT instead of self-starting past the gate;
- normalizes `stage_type`: 'work' for steps that route away on turn completion
  (the same routing-derived rule pacer.py classifies by), 'review' for parked
  steps named in --review (default: any step named "Review"), 'custom' otherwise.

Dry run by default (opens the DB read-only); pass --apply to write. The kandev
server can stay up: the DB runs in WAL mode and steps are re-read on every use,
so the change is visible immediately. A workflow re-import resets stage_type
to 'custom' -- rerun this script after any import.

Usage:
    ./make_paced.py                      # list workflows
    ./make_paced.py 'Paced Kanban'       # dry run (name or id)
    ./make_paced.py 'Paced Kanban' --apply
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from pacer import is_work_step, lint_workflow

DEFAULT_DB = Path.home() / ".kandev" / "data" / "kandev.db"

# --- pure planning (unit-tested in test_make_paced.py) ---

def strip_auto_start(events):
    """Drop every auto_start_agent action from an events dict.

    Returns (new_events, n_removed). A trigger left with no actions is dropped
    entirely, matching how the workflow export renders untouched steps.
    """
    out, removed = {}, 0
    for trigger, actions in (events or {}).items():
        kept = [a for a in (actions or []) if a.get("type") != "auto_start_agent"]
        removed += len(actions or []) - len(kept)
        if kept:
            out[trigger] = kept
    return out, removed

def target_stage(step, review_names):
    """The stage_type a paced step should carry.

    Routing wins: a step that routes away on turn completion is 'work' even if
    its name matches --review (tagging it 'review' would contradict the
    operative classifier and trip the startup lint).
    """
    if is_work_step(step):
        return "work"
    if step.get("name", "").strip().lower() in review_names:
        return "review"
    return "custom"

def plan_changes(steps, review_names=frozenset({"review"})):
    """Per-step change records for steps that need edits; already-paced steps are skipped.

    steps: dicts with at least id, name, stage_type, and parsed events.
    """
    changes = []
    for s in steps:
        events, removed = strip_auto_start(s.get("events"))
        stage = target_stage({**s, "events": events}, review_names)
        if removed or stage != s.get("stage_type"):
            changes.append({"id": s["id"], "name": s.get("name", ""), "events": events,
                            "removed": removed, "stage_from": s.get("stage_type"),
                            "stage_to": stage})
    return changes

def planned_steps(steps, changes):
    """The steps as they will look after the plan is applied (for linting)."""
    by_id = {c["id"]: c for c in changes}
    return [{**s, "events": c["events"], "stage_type": c["stage_to"]}
            if (c := by_id.get(s["id"])) else s for s in steps]

# --- sqlite glue ---

def connect(db_path, writable):
    if not Path(db_path).exists():
        sys.exit(f"error: database not found: {db_path}")
    if writable:
        con = sqlite3.connect(db_path, timeout=15)
    else:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    return con

def resolve_workflow(con, needle):
    """Workflow row for an exact id or a case-insensitive name; exits on miss/ambiguity."""
    rows = con.execute("SELECT id, name FROM workflows").fetchall()
    matches = ([w for w in rows if w["id"] == needle]
               or [w for w in rows if w["name"].lower() == needle.lower()])
    if len(matches) == 1:
        return matches[0]
    listing = "\n".join(f"  {w['id']}  {w['name']}" for w in rows)
    sys.exit(f"error: {'ambiguous' if matches else 'no'} workflow {needle!r}; have:\n{listing}")

def load_steps(con, workflow_id):
    rows = con.execute(
        "SELECT id, name, position, events, is_start_step, stage_type,"
        " agent_profile_id, prompt, auto_advance_requires_signal"
        " FROM workflow_steps WHERE workflow_id = ? ORDER BY position",
        (workflow_id,)).fetchall()
    return [{**dict(r), "events": json.loads(r["events"] or "{}")} for r in rows]

def apply_changes(con, changes):
    with con:  # one transaction; the server's WAL readers see it atomically
        for c in changes:
            con.execute(
                "UPDATE workflow_steps SET events = ?, stage_type = ?,"
                " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(c["events"], separators=(",", ":")), c["stage_to"], c["id"]))

# --- cli ---

def main():
    ap = argparse.ArgumentParser(
        description="Make a Kandev workflow paced: strip auto_start_agent, set stage_type.")
    ap.add_argument("workflow", nargs="?", help="workflow id or name (omit to list workflows)")
    ap.add_argument("--db", default=os.environ.get("KANDEV_DB", str(DEFAULT_DB)),
                    metavar="PATH", help=f"kandev SQLite database (default: {DEFAULT_DB})")
    ap.add_argument("--review", action="append", default=[], metavar="NAME",
                    help="additional step name to tag stage_type='review' (repeatable;"
                         " 'Review' is always included)")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = ap.parse_args()

    con = connect(args.db, writable=args.apply)
    if not args.workflow:
        for w in con.execute("SELECT id, name FROM workflows ORDER BY name"):
            print(f"{w['id']}  {w['name']}")
        return

    wf = resolve_workflow(con, args.workflow)
    steps = load_steps(con, wf["id"])
    review_names = {"review"} | {n.strip().lower() for n in args.review}
    changes = plan_changes(steps, review_names)

    print(f"workflow {wf['name']!r} ({wf['id']})")
    by_id = {c["id"]: c for c in changes}
    for s in steps:
        c = by_id.get(s["id"])
        if not c:
            print(f"  {s['name']}: ok (stage_type={s['stage_type']})")
            continue
        edits = []
        if c["removed"]:
            edits.append(f"remove {c['removed']} auto_start_agent action(s)")
        if c["stage_to"] != c["stage_from"]:
            edits.append(f"stage_type {c['stage_from']} -> {c['stage_to']}")
        print(f"  {s['name']}: " + "; ".join(edits))

    for w in lint_workflow(planned_steps(steps, changes)):
        print(f"lint: {w}", file=sys.stderr)

    if not changes:
        print("already paced; nothing to do")
    elif args.apply:
        apply_changes(con, changes)
        print(f"applied {len(changes)} step update(s)"
              " (a workflow re-import resets stage_type; rerun after one)")
    else:
        print("dry run; pass --apply to write")

if __name__ == "__main__":
    main()
