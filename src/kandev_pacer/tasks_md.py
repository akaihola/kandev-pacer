"""Read complete work-item sources and schedule workflow issues through Hai."""

import argparse
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def hai():
    try:
        return importlib.import_module("hai_tasks")
    except ImportError as error:
        raise ValueError(
            "Install the heartbeat extra: uv sync --extra heartbeat"
        ) from error


def slug(text):
    return "-".join(re.sub(r"[\W_]", "", word) for word in text.lower().split()[:6])


def states(snapshot):
    return {
        state["id"]: state["stage"]
        for state in snapshot["capabilities"]["sourceStates"]
    }


def state_for(snapshot, stage):
    return next(
        (name for name, value in states(snapshot).items() if value == stage), None
    )


def issue_key(issue):
    reference = issue.get("sourceReference")
    if reference and reference != "[*]":
        return reference[1:-1]
    if reference:
        return slug(issue["title"])
    return issue["identifier"]


def dependency_key(issue, reference):
    # Ticket numbers are local to a project and may include leading zeroes.
    number = str(int(reference)) if reference.isdecimal() else reference
    return issue.get("projectId"), number


def eligible_issues(snapshot):
    catalog = states(snapshot)
    issues = snapshot["workspace"]["issues"]
    done = {
        dependency_key(i, issue_key(i).rsplit("/", 1)[-1])
        for i in issues
        if catalog[i["sourceState"]] == "done"
    }
    return [
        issue
        for stage in ("scheduled", "ready")
        for issue in issues
        if catalog[issue["sourceState"]] == stage
        and all(dependency_key(issue, dep) in done for dep in issue["dependencies"])
    ]


def workflow_prompt(snapshot):
    text = (
        f"Move this issue to {state_for(snapshot, 'active')} when starting work and "
        f"to {state_for(snapshot, 'done')} when complete. "
    )
    if snapshot["capabilities"]["fields"].get("acceptance"):
        text += "Do not check user acceptance [x]. "
    return text


def tracker(path):
    api = hai()
    snapshot: dict[str, Any]
    if path.is_file() and not re.search(
        r"^## Unverified proposals$", path.read_text(), re.M
    ):
        # Legacy trackers are readable without rewriting them.
        source = path.read_text().replace(
            "## Ordered backlog", "## Unverified proposals\n\n## Ordered backlog", 1
        )
        parsed = api.parse_tasks(source)
        snapshot = {
            "parsed": parsed,
            "workspace": {"issues": [task["issue"] for task in parsed["tasks"]]},
            "capabilities": api.TasksStore.capabilities,
            "revision": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    else:
        snapshot = api.open_source(path).read()
    catalog = states(snapshot)
    raw = {
        task["issue"]["id"]: task["raw"] for task in snapshot["parsed"].get("tasks", [])
    }
    eligible_ids = {issue["id"] for issue in eligible_issues(snapshot)}
    items = [
        {
            "key": issue_key(issue),
            "id": issue["id"],
            "title": issue["title"],
            "bullet": raw.get(issue["id"], issue["description"]),
            "section": issue["sourceState"],
            "stage": catalog[issue["sourceState"]],
            "workflow_eligible": issue["id"] in eligible_ids,
            "workflow_prompt": workflow_prompt(snapshot),
            "depends_on": issue["dependencies"],
            "source": str(path),
        }
        for issue in snapshot["workspace"]["issues"]
    ]
    check_keys(items)
    return snapshot["revision"], items


def check_keys(items):
    keys = [item["key"] for item in items]
    if any(not key for key in keys):
        raise ValueError("Work-item key is empty; clarify the source item")


def issue_files(root, suffix):
    items = []
    for path in sorted(root.glob("*" + suffix)):
        if path.stem.lower() in {"readme", "index", "next"}:
            continue
        text = path.read_text()
        if path.stem == "incoming" and suffix == ".rst":
            pending = text.split("Waiting\n-------", 1)
            if len(pending) == 2 and not pending[1].strip():
                continue
            raise ValueError(
                f"Unsplit incoming reports in {path}; inspect them before selecting"
            )
        if suffix == ".rst":
            fields = dict(re.findall(r"^:([^:\n]+):\s*([^\n]*)", text, re.M))
            status = fields.get("Status", "").lower()
            if status not in {
                "open",
                "accepted",
                "in progress",
                "fixed",
                "rejected",
                "deferred",
            }:
                raise ValueError(f"Missing or unknown :Status: in {path}")
            section = {
                "open": "Ordered backlog",
                "accepted": "Ordered backlog",
                "fixed": "Completed / Accepted",
                "rejected": "Completed / Accepted",
                "in progress": "In Progress",
                "deferred": "Deferred",
            }[status]
            if fields.get("Claimed", "").strip():
                section = "In Progress"
            key = path.stem.split("-", 1)[0]
            dependency = re.search(
                r"^:Depends on:\s*(.*(?:\n[ \t]+[^\n]+)*)", text, re.M
            )
            value = dependency[1] if dependency else ""
            dependencies = re.findall(r"\b\d{3}\b", value)
            if (
                value.strip()
                and not value.strip().startswith("(none)")
                and not dependencies
            ):
                raise ValueError(f"Unrecognized dependencies in {path}")
            title = next(
                line.strip()
                for line in text.splitlines()
                if line.strip() and not re.fullmatch(r"[=\-~]+", line.strip())
            )
        else:
            section, key, dependencies = "Ordered backlog", path.stem, []
            title = next(
                (
                    line.strip().lstrip("# ")
                    for line in text.splitlines()
                    if line.strip()
                ),
                path.stem,
            )
        items.append(
            {
                "key": key,
                "id": str(path),
                "title": title,
                "bullet": text,
                "section": section,
                "stage": {
                    "Ordered backlog": "ready",
                    "In Progress": "active",
                    "Completed / Accepted": "done",
                }.get(section, "waiting"),
                "depends_on": dependencies,
                "source": str(path),
            }
        )
    check_keys(items)
    return items


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "SHELL": "/bin/bash"},
    ).stdout.strip()


def move(path, key, revision, destination, base_branch):
    """Require the caller's revision and a clean base checkout before scheduling."""
    root = Path(
        git(path if path.is_dir() else path.parent, "rev-parse", "--show-toplevel")
    )
    if git(root, "symbolic-ref", "--short", "HEAD") != base_branch:
        raise ValueError(f"Scheduling requires the {base_branch} checkout")
    if git(root, "status", "--porcelain"):
        raise ValueError("Scheduling requires a clean checkout, including its index")
    current_revision, items = tracker(path)
    if current_revision != revision:
        raise ValueError("Tracker changed; rerun list before scheduling")
    item = next((item for item in items if item["key"] == key), None)
    if item is None or item["stage"] not in {"ready", "scheduled"}:
        raise ValueError("Only a ready or scheduled issue can be scheduled")
    if not eligible(item, items):
        raise ValueError("Issue has unfinished or missing dependencies")
    store = hai().open_source(path)
    snapshot = store.read()
    if snapshot["revision"] != revision:
        raise ValueError("Tracker changed; rerun list before scheduling")
    destination = state_for(snapshot, "scheduled")
    if destination is None or item["section"] == destination:
        return {"revision": current_revision, "commit": git(root, "rev-parse", "HEAD")}
    result = store.mutate(
        "update",
        revision,
        id=item["id"],
        input={"sourceState": destination},
    )
    tracker(path)  # Check the whole file before committing.
    source_path = str(path.relative_to(root))
    git(root, "add", "--", source_path)
    git(
        root,
        "commit",
        "-m",
        f"docs(tasks): schedule {item['id']}",
        "--",
        source_path,
    )
    return {
        "revision": result["revision"],
        "commit": git(root, "rev-parse", "HEAD"),
    }


def eligible(item, items):
    if "workflow_eligible" in item:
        return item["workflow_eligible"]
    by_key = {entry["key"]: entry for entry in items}
    return item["stage"] in {"ready", "scheduled"} and all(
        dependency in by_key and by_key[dependency]["stage"] == "done"
        for dependency in item["depends_on"]
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("TASKS.md"))
    parser.add_argument("--format", choices=["tasks", "md", "rst"], default="tasks")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    sub.add_parser("check")
    schedule = sub.add_parser("move")
    schedule.add_argument("key")
    schedule.add_argument("--revision", required=True)
    schedule.add_argument("--to", choices=["Scheduled"], default="Scheduled")
    schedule.add_argument("--base-branch", default="main")
    args = parser.parse_args(argv)
    try:
        path = args.source.resolve()
        if args.action == "move":
            if args.format != "tasks":
                raise ValueError(
                    "Issue files use their repository's claim workflow; move supports TASKS.md"
                )
            result = move(path, args.key, args.revision, args.to, args.base_branch)
        else:
            revision, items = (
                tracker(path)
                if args.format == "tasks"
                else (None, issue_files(path, "." + args.format))
            )
            result = {"revision": revision, "count": len(items)}
            if args.action == "list":
                result["items"] = [
                    {**item, "eligible": eligible(item, items)} for item in items
                ]
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except Exception as error:
        print(f"tasks-md: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
