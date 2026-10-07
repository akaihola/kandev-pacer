"""Read complete work-item sources and schedule TASKS.md issues through Hai."""

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


def tracker(path):
    api = hai()
    source = path.read_text()
    snapshot: dict[str, Any]
    if not re.search(r"^## Unverified proposals$", source, re.M):
        # Read legacy trackers without rewriting them. Mutations still use TasksStore.
        source = source.replace(
            "## Ordered backlog", "## Unverified proposals\n\n## Ordered backlog", 1
        )
        snapshot = {
            "parsed": api.parse_tasks(source),
            "revision": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    else:
        snapshot = api.TasksStore(path).read()
    items = []
    for task in snapshot["parsed"]["tasks"]:
        issue = task["issue"]
        items.append(
            {
                "key": task["marker"]
                if task["marker"] != "*"
                else slug(issue["title"]),
                "id": issue["id"],
                "title": issue["title"],
                "bullet": task["raw"],
                "section": issue["sourceSection"],
                "depends_on": issue["dependencies"],
                "source": str(path),
            }
        )
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
    if git(path.parent, "symbolic-ref", "--short", "HEAD") != base_branch:
        raise ValueError(f"Scheduling requires the {base_branch} checkout")
    if git(path.parent, "status", "--porcelain"):
        raise ValueError("Scheduling requires a clean checkout, including its index")
    current_revision, items = tracker(path)
    if current_revision != revision:
        raise ValueError("Tracker changed; rerun list before scheduling")
    item = next((item for item in items if item["key"] == key), None)
    if item is None or item["section"] != "Ordered backlog":
        raise ValueError("Only an Ordered backlog issue can be scheduled")
    if not eligible(item, items):
        raise ValueError("Issue has unfinished or missing dependencies")
    result = (
        hai()
        .TasksStore(path)
        .mutate(
            "update",
            revision,
            id=item["id"],
            input={"sourceSection": destination},
        )
    )
    tracker(path)  # Check the whole file before committing.
    git(path.parent, "add", "--", path.name)
    git(
        path.parent,
        "commit",
        "-m",
        f"docs(tasks): schedule {item['id']}",
        "--",
        path.name,
    )
    return {
        "revision": result["revision"],
        "commit": git(path.parent, "rev-parse", "HEAD"),
    }


def eligible(item, items):
    by_key = {entry["key"]: entry for entry in items}
    return item["section"] == "Ordered backlog" and all(
        dependency in by_key and by_key[dependency]["section"] == "Completed / Accepted"
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
