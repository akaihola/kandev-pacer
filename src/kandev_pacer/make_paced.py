"""Adapt an existing Kandev workflow for pacing (Design 13) via its SQLite DB.

Kandev has no supported surface for one of the two edits pacing needs
(`stage_type` is display-only in the UI, absent from the step APIs and the
import format -- see docs/pacing-designs.md, Design 13), so this script edits
the database directly. Per step it:

- strips every `auto_start_agent` action from the step's events, so a finished
  turn parks the task WAITING_FOR_INPUT instead of self-starting past the gate;
- normalizes `stage_type`: 'work' for steps that route away on turn completion
  (the same routing-derived rule workflow.py classifies by), 'review'/'approval'
  for parked steps named in --review/--approval (default: any step named
  "Review" is 'review'). A parked step that already carries a valid tag
  ('review', 'approval', 'custom') keeps it; only a missing tag or a wrong
  'work' tag on a parked step falls back to 'custom'.

Dry run by default (opens the DB read-only); pass --apply to write. The kandev
server can stay up: the DB runs in WAL mode and steps are re-read on every use,
so the change is visible immediately. A workflow re-import resets stage_type
to 'custom' -- rerun this script after any import.

Usage:
    uv run make-paced                      # list workflows
    uv run make-paced 'Paced Kanban'       # dry run (name or id)
    uv run make-paced 'Paced Kanban' --apply
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .workflow import is_work_step, lint_workflow

DEFAULT_DB = Path.home() / ".kandev" / "data" / "kandev.db"


# --- pure planning (unit-tested in tests/test_make_paced.py) ---


def strip_auto_start(events):
    """Drop every auto_start_agent action from an events dict.

    Returns (new_events, n_removed). A trigger left with no actions is dropped
    entirely, matching how the workflow export renders untouched steps.
    """
    stripped, removed = {}, 0
    for trigger, actions in (events or {}).items():
        kept = [action for action in (actions or [])
                if action.get("type") != "auto_start_agent"]
        removed += len(actions or []) - len(kept)
        if kept:
            stripped[trigger] = kept
    return stripped, removed

# stage_type values a parked (non-work) step is allowed to carry. A step
# already tagged one of these by hand is a hint we keep, not clobber (Design
# 13: treat a hand-set stage_type as a corroborating hint, never destroy it).
PARKED_STAGES = frozenset({"custom", "review", "approval"})


def target_stage(step, review_names, approval_names=frozenset()):
    """The stage_type a paced step should carry.

    Routing wins: a step that routes away on turn completion is 'work' even if
    its name matches --review (tagging it 'review' would contradict the
    operative classifier and trip the startup lint). For a parked step: an
    explicit --review/--approval name wins; otherwise we keep whatever valid
    parked value it already has (so a hand-set 'approval' or 'review' survives),
    and only fall back to 'custom' when the current value is missing or 'work'
    (a wrong tag a parked step shouldn't keep).
    """
    if is_work_step(step):
        return "work"
    name = step.get("name", "").strip().lower()
    if name in review_names:
        return "review"
    if name in approval_names:
        return "approval"
    current = (step.get("stage_type") or "").lower()
    return current if current in PARKED_STAGES else "custom"


def plan_changes(steps, review_names=frozenset({"review"}), approval_names=frozenset()):
    """Per-step change records for steps that need edits; already-paced steps are skipped.

    steps: dicts with at least id, name, stage_type, and parsed events.
    """
    changes = []
    for step in steps:
        events, removed = strip_auto_start(step.get("events"))
        stage = target_stage({**step, "events": events}, review_names, approval_names)
        if removed or stage != step.get("stage_type"):
            changes.append({"id": step["id"], "name": step.get("name", ""),
                            "events": events, "removed": removed,
                            "stage_from": step.get("stage_type"), "stage_to": stage})
    return changes


def planned_steps(steps, changes):
    """The steps as they will look after the plan is applied (for linting)."""
    by_id = {change["id"]: change for change in changes}
    return [
        {**step, "events": change["events"], "stage_type": change["stage_to"]}
        if (change := by_id.get(step["id"]))
        else step
        for step in steps
    ]


# --- sqlite glue ---


def connect(db_path, writable):
    """Open the kandev database. A dry run opens it read-only, so it cannot write."""
    if not Path(db_path).exists():
        sys.exit(f"error: database not found: {db_path}")
    target = str(db_path) if writable else f"file:{db_path}?mode=ro"
    connection = sqlite3.connect(target, uri=not writable, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def resolve_workflow(connection, needle):
    """Workflow row for an exact id or a case-insensitive name; exits on miss/ambiguity."""
    rows = connection.execute("SELECT id, name FROM workflows").fetchall()
    matches = ([row for row in rows if row["id"] == needle]
               or [row for row in rows if row["name"].lower() == needle.lower()])
    if len(matches) == 1:
        return matches[0]
    listing = "\n".join(f"  {row['id']}  {row['name']}" for row in rows)
    sys.exit(f"error: {'ambiguous' if matches else 'no'} workflow {needle!r}; have:\n{listing}")


def load_steps(connection, workflow_id):
    rows = connection.execute(
        "SELECT id, name, position, events, is_start_step, stage_type,"
        " agent_profile_id, prompt, auto_advance_requires_signal"
        " FROM workflow_steps WHERE workflow_id = ? ORDER BY position",
        (workflow_id,)).fetchall()
    steps = []
    for row in rows:
        try:
            events = json.loads(row["events"] or "{}")
        except json.JSONDecodeError as error:
            # kandev normally writes valid JSON here; a bad cell means the row
            # is corrupt. Name the step instead of dumping a raw traceback.
            sys.exit(f"error: step {row['name']!r} ({row['id']}) "
                     f"has invalid events JSON: {error}")
        steps.append({**dict(row), "events": events})
    return steps


def apply_changes(connection, changes):
    with connection:  # one transaction; the server's WAL readers see it atomically
        for change in changes:
            connection.execute(
                "UPDATE workflow_steps SET events = ?, stage_type = ?,"
                " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(change["events"], separators=(",", ":")),
                 change["stage_to"], change["id"]))


# --- cli ---


def describe(step, change):
    """The one line a step gets in the report: what will change, or that nothing will."""
    if change is None:
        return f"  {step['name']}: ok (stage_type={step['stage_type']})"
    edits = []
    if change["removed"]:
        edits.append(f"remove {change['removed']} auto_start_agent action(s)")
    if change["stage_to"] != change["stage_from"]:
        edits.append(f"stage_type {change['stage_from']} -> {change['stage_to']}")
    return f"  {step['name']}: " + "; ".join(edits)



def main():
    parser = argparse.ArgumentParser(
        prog="make-paced",
        description="Make a Kandev workflow paced: strip auto_start_agent, set stage_type.")
    parser.add_argument("workflow", nargs="?",
                        help="workflow id or name (omit to list workflows)")
    parser.add_argument("--db", default=os.environ.get("KANDEV_DB", str(DEFAULT_DB)),
                        metavar="PATH",
                        help=f"kandev SQLite database (default: {DEFAULT_DB})")
    parser.add_argument("--review", action="append", default=[], metavar="NAME",
                        help="additional step name to tag stage_type='review' (repeatable;"
                             " 'Review' is always included)")
    parser.add_argument("--approval", action="append", default=[], metavar="NAME",
                        help="step name to tag stage_type='approval' (repeatable). A step"
                             " already tagged 'approval' by hand is kept without this.")
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default: dry run)")
    args = parser.parse_args()

    connection = connect(args.db, writable=args.apply)
    if not args.workflow:
        for row in connection.execute("SELECT id, name FROM workflows ORDER BY name"):
            print(f"{row['id']}  {row['name']}")
        return

    workflow = resolve_workflow(connection, args.workflow)
    steps = load_steps(connection, workflow["id"])
    review_names = {"review"} | {name.strip().lower() for name in args.review}
    approval_names = {name.strip().lower() for name in args.approval}
    changes = plan_changes(steps, review_names, approval_names)
    by_id = {change["id"]: change for change in changes}

    print(f"workflow {workflow['name']!r} ({workflow['id']})")
    for step in steps:
        print(describe(step, by_id.get(step["id"])))
    for warning in lint_workflow(planned_steps(steps, changes)):
        print(f"lint: {warning}", file=sys.stderr)

    if not changes:
        print("already paced; nothing to do")
    elif args.apply:
        apply_changes(connection, changes)
        print(f"applied {len(changes)} step update(s)"
              " (a workflow re-import resets stage_type; rerun after one)")
    else:
        print("dry run; pass --apply to write")
