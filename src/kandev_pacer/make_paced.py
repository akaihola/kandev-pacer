"""Adapt an existing Kandev workflow for pacing (Design 13) through its step API.

`stage_type` is display-only in kandev's UI and absent from the import format
(see docs/history/pacing-designs.md, Design 13), but the step update endpoint,
`PUT /api/v1/workflow/steps/{id}`, takes every step field, needs no
authentication, and changes only the fields the body names. This script reads
the steps over the same REST calls the pacer makes and writes through that
endpoint. Per step it:

- strips every `auto_start_agent` action from the step's events, so a finished
  turn parks the task WAITING_FOR_INPUT instead of self-starting past the gate;
- normalizes `stage_type`: 'work' for steps that route away on turn completion
  (the same routing-derived rule workflow.py classifies by), 'review'/'approval'
  for parked steps named in --review/--approval (default: any step named
  "Review" is 'review'). A parked step that already carries a valid tag
  ('review', 'approval', 'custom') keeps it; only a missing tag or a wrong
  'work' tag on a parked step falls back to 'custom';
- appends a signal instruction to the prompt of any signal-gated work step
  (`auto_advance_requires_signal`) whose prompt never names `step_complete_kandev`.
  kandev routes such a step onward only when the agent calls that tool, so a
  prompt that omits it leaves the agent ending turns without signalling -- the
  task reads idle, the pacer relaunches the same prompt, and after enough
  no-change turns the stall detector parks the card for a person who, on an
  unattended board, never comes.

Dry run by default (only reads); pass --apply to write. Kandev must be running
(KANDEV_URL says where). Each changed step is one request, so a failure part
way leaves the board partly enrolled -- rerunning finishes the job, because
already-paced steps are skipped. A workflow re-import resets stage_type to
'custom' -- rerun this script after any import.

Usage:
    uv run make-paced                              # list workflows
    uv run make-paced 'Paced Kanban'               # dry run (name or id)
    uv run make-paced 'Paced Kanban' --apply       # enrol it
    uv run make-paced 'Paced Kanban' --unpace --apply   # stop pacing it
"""
import argparse
import sys

from . import client
from .workflow import SIGNAL_TOOL, is_work_step, lint_workflow, prompt_mentions_signal

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

# What gets appended to a silent signal-gated prompt. Generic on purpose: it has to
# read sensibly at the end of any step prompt, in any workflow.
SIGNAL_INSTRUCTION = (
    f"Call the {SIGNAL_TOOL} tool once this step's work is complete, then stop. "
    "The workflow does not advance without that signal."
)


def ensure_signal_instruction(step, stage):
    """The prompt this step needs, and whether that means appending the instruction.

    Only a signal-gated step headed for 'work' is touched: a parked step never routes,
    an ungated one advances on turn completion alone, and a prompt that already names
    the tool needs nothing. Everything else keeps its prompt byte-for-byte (including
    NULL), so re-running the tool never rewrites what a person wrote.
    """
    prompt = step.get("prompt")
    if (stage != "work" or not step.get("auto_advance_requires_signal")
            or prompt_mentions_signal(step)):
        return prompt, False
    if not (prompt or "").strip():
        return SIGNAL_INSTRUCTION, True
    return f"{prompt.rstrip()}\n\n{SIGNAL_INSTRUCTION}", True


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
        prompt, signal_added = ensure_signal_instruction(step, stage)
        if removed or stage != step.get("stage_type") or signal_added:
            changes.append({"id": step["id"], "name": step.get("name", ""),
                            "events": events, "removed": removed,
                            "stage_from": step.get("stage_type"), "stage_to": stage,
                            "prompt": prompt, "signal_added": signal_added})
    return changes


def plan_unpace(steps):
    """Change records that un-enrol a workflow: drop the 'work' tag from every step.

    The pacer paces a workflow exactly while some step is tagged 'work', so clearing
    the tag is the whole of stopping. `auto_start_agent` is deliberately *not* put
    back: nothing records what was there, and restoring it would let tasks start
    turns by themselves, which is the opposite of what someone stopping the pacer
    wants. An unpaced board is a manual one -- a finished turn waits for a person.
    """
    return [
        {
            "id": step["id"],
            "name": step.get("name", ""),
            "events": step.get("events") or {},
            "removed": 0,
            "stage_from": step.get("stage_type"),
            "stage_to": "custom",
            # The appended signal instruction is not peeled back out either: the
            # prompt may have been edited around it since, and on a manual board a
            # stale "call step_complete_kandev" line is harmless.
            "prompt": step.get("prompt"),
            "signal_added": False,
        }
        for step in steps
        if (step.get("stage_type") or "").lower() == "work"
    ]


def planned_steps(steps, changes):
    """The steps as they will look after the plan is applied (for linting)."""
    by_id = {change["id"]: change for change in changes}
    return [
        {**step, "events": change["events"], "stage_type": change["stage_to"],
         "prompt": change["prompt"]}
        if (change := by_id.get(step["id"]))
        else step
        for step in steps
    ]


# --- kandev API glue (tested in tests/test_make_paced_cli.py) ---


def resolve_workflow(workflows, needle):
    """Workflow dict for an exact id or a case-insensitive name; exits on miss/ambiguity."""
    matches = ([row for row in workflows if row["id"] == needle]
               or [row for row in workflows if row["name"].lower() == needle.lower()])
    if len(matches) == 1:
        return matches[0]
    listing = "\n".join(f"  {row['id']}  {row['name']}" for row in workflows)
    sys.exit(f"error: {'ambiguous' if matches else 'no'} workflow {needle!r}; have:\n{listing}")


def step_update(change, unpace):
    """The request body for one change: only the fields that move.

    Kandev leaves out every field the body does not name, so a minimal body keeps the
    rest of the step byte-identical -- including a prompt a person edited since.
    """
    if unpace:
        return {"stage_type": change["stage_to"]}
    body = {"events": change["events"], "stage_type": change["stage_to"]}
    if change["signal_added"]:
        body["prompt"] = change["prompt"]
    return body


def apply_changes(changes, unpace=False):
    for change in changes:
        client.put(f"/api/v1/workflow/steps/{change['id']}", step_update(change, unpace))


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
    if change["signal_added"]:
        edits.append(f"append the {SIGNAL_TOOL} instruction to the prompt")
    return f"  {step['name']}: " + "; ".join(edits)


def build_parser():
    """The command line: one enrolment tool, dry by default."""
    parser = argparse.ArgumentParser(
        prog="make-paced",
        description="Make a Kandev workflow paced: strip auto_start_agent, set stage_type.",
    )
    parser.add_argument(
        "workflow", nargs="?", help="workflow id or name (omit to list workflows)"
    )
    parser.add_argument(
        "--review",
        action="append",
        default=[],
        metavar="NAME",
        help="additional step name to tag stage_type='review' (repeatable;"
        " 'Review' is always included)",
    )
    parser.add_argument(
        "--approval",
        action="append",
        default=[],
        metavar="NAME",
        help="step name to tag stage_type='approval' (repeatable). A step"
        " already tagged 'approval' by hand is kept without this.",
    )
    parser.add_argument(
        "--unpace",
        action="store_true",
        help="stop pacing this workflow: clear stage_type='work' from"
        " every step. Leaves auto_start_agent off, so the board"
        " becomes manual rather than self-starting.",
    )
    parser.add_argument(
        "--apply", action="store_true", help="write the changes (default: dry run)"
    )
    return parser


def main():
    args = build_parser().parse_args()
    try:
        report(args)
    except client.ReadFailed as error:
        sys.exit(f"error: {error}\nIs kandev running? Set KANDEV_URL if it listens elsewhere.")
    except client.WriteFailed as error:
        sys.exit(f"error: {error}\nSome steps may already be updated; rerunning is safe.")


def report(args):
    """List the workflows, or plan and optionally apply the change to one."""
    workflows = (client.get("/api/v1/workflows") or {}).get("workflows") or []
    if not args.workflow:
        for row in sorted(workflows, key=lambda row: row["name"]):
            print(f"{row['id']}  {row['name']}")
        return

    workflow = resolve_workflow(workflows, args.workflow)
    steps = sorted(client.steps_of(workflow["id"]), key=lambda step: step["position"])
    if args.unpace:
        changes = plan_unpace(steps)
    else:
        changes = plan_changes(
            steps,
            {"review"} | {name.strip().lower() for name in args.review},
            {name.strip().lower() for name in args.approval},
        )
    by_id = {change["id"]: change for change in changes}

    print(f"workflow {workflow['name']!r} ({workflow['id']})")
    for step in steps:
        print(describe(step, by_id.get(step["id"])))
    # Linting an un-enrolled workflow would warn about every step for not being paced,
    # which is the point of unpacing rather than a problem with it.
    if not args.unpace:
        for warning in lint_workflow(planned_steps(steps, changes)):
            print(f"lint: {warning}", file=sys.stderr)

    if not changes:
        print(
            "not paced; nothing to do"
            if args.unpace
            else "already paced; nothing to do"
        )
    elif args.apply:
        apply_changes(changes, args.unpace)
        print(f"applied {len(changes)} step update(s)")
        print(
            "no longer paced"
            if args.unpace
            else "a workflow re-import resets stage_type; rerun after one"
        )
    else:
        print("dry run; pass --apply to write")
