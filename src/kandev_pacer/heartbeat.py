"""Deterministic Heartbeat reads, MCP payloads, and finished-run cleanup."""

import argparse
import json
import math
import re
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import client, tasks_md
from .config import RUNNING_STATES
from .learn_sources import read_db, timestamp
from .observation_consumer import closed, is_observation_consumer, unread

REPOSITORIES = (
    "prg/syncop",
    "prg/filemill",
    "repos/ai/artificialanalysis-ai-parser",
    "repos/nixos-config",
    "prg/kasvimuseo",
    "prg/dallape",
    "prg/kandev-config",
    "prg/kandev-pacer",
    "my-knowledge",
)
WORKFLOWS = {
    "complex": "Paced auto-implement",
    "simple": "Paced auto-implement without planning",
    "research": "Paced exploration",
    "consumer": "Paced Kanban",
}
HEARTBEAT_TITLE = "[Auto] Heartbeat"


def rows(db, sql, parameters=()):
    return [dict(row) for row in db.execute(sql, parameters)]


def one(db, sql, parameters, label):
    found = rows(db, sql, parameters)
    if len(found) != 1:
        raise ValueError(f"Expected exactly one {label}, found {len(found)}")
    return found[0]["id"]


def resolve_ids(db, workspace_name):
    workspace = one(
        db, "SELECT id FROM workspaces WHERE name=?", (workspace_name,), workspace_name
    )
    workflows = {
        kind: one(
            db,
            "SELECT id FROM workflows WHERE workspace_id=? AND name=?",
            (workspace, name),
            name,
        )
        for kind, name in WORKFLOWS.items()
    }
    profiles = {
        name: one(
            db,
            "SELECT p.id FROM agent_profiles p JOIN agents a ON a.id=p.agent_id "
            "WHERE p.name=? AND a.name=? "
            "AND p.deleted_at IS NULL AND p.enabled=1 AND (p.workspace_id=? OR p.workspace_id='')",
            (name, agent, workspace),
            name,
        )
        for name, agent in [
            ("Luna Medium", "codex-acp"),
            ("Astra High", "codex-acp"),
            ("Opus Medium", "claude-acp"),
        ]
    }
    executor = one(
        db,
        "SELECT id FROM executor_profiles WHERE name=? AND executor_id=?",
        ("Worktree", "exec-worktree"),
        "Worktree executor",
    )
    return {
        "workspace": workspace,
        "workflows": workflows,
        "profiles": profiles,
        "executor": executor,
    }


def profile_choice(path, now, max_age=900):
    data = json.loads(path.read_text())
    # The collector timestamp, rather than a copied file's mtime, owns freshness.
    collected = data.get("collected_at", data.get("timestamp", data.get("updated_at")))
    if collected is None or not -60 <= now - timestamp(collected) <= max_age:
        raise ValueError("Quota snapshot is missing its timestamp or is stale")
    fractions = {}
    for service in ("codex", "claude"):
        value = data["services"][service]["windows"]["7d"]["used_fraction"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"Invalid {service} quota fraction")
        fractions[service] = value
    choice = (
        "Luna Medium"
        if min(fractions.values()) > 1.05
        else (
            "Astra High" if fractions["codex"] <= fractions["claude"] else "Opus Medium"
        )
    )
    return {"profile": choice, "fractions": fractions}


def instructions(path):
    if not path.exists():
        return "", "AGENTS.md absent; inspect repository instructions before creating"
    text = path.read_text()
    headings = list(re.finditer(r"^## .+$", text, re.M))
    sections = [
        text[
            heading.start() : headings[i + 1].start()
            if i + 1 < len(headings)
            else len(text)
        ]
        for i, heading in enumerate(headings)
        if "heartbeat" in heading[0].lower()
    ]
    source_lines = [
        line
        for line in text.splitlines()
        if "TASKS.md" in line or "docs/issues/" in line
    ]
    return text, "\n".join(source_lines + sections)


def sources(root, agents):
    items, revisions = [], {}
    inbox_only = bool(re.search(r"Read `docs/issues/` only", agents))
    path = root / "TASKS.md"
    if path.exists() and not inbox_only:
        revision, found = tasks_md.tracker(path)
        items.extend(found)
        revisions[str(path)] = revision
    for suffix in (".md", ".rst"):
        folder = root / "docs/issues"
        if folder.exists() and (inbox_only or "docs/issues/" in agents):
            items.extend(tasks_md.issue_files(folder, suffix))
    if not revisions and not items and not (root / "docs/issues").exists():
        raise ValueError(
            "No supported work-item source; inspect repository instructions"
        )
    tasks_md.check_keys(items)
    return items, revisions


def coverage(item, tasks, bound=()):
    matches = [
        task
        for task in tasks
        if task.get("external_id") == item["external_id"]
        or (
            task["id"] in bound
            and not closed(task)
            and task["title"].strip().casefold() == item["title"].strip().casefold()
        )
    ]
    if len(matches) > 1:
        return "collision", [task["id"] for task in matches]
    if matches:
        task = matches[0]
        if not closed(task):
            return "open", [task["id"]]
        return ("collision" if item["section"] == "Ordered backlog" else "finished"), [
            task["id"]
        ]
    if item["section"] in {"Scheduled", "In Progress", "Completed / Accepted"}:
        return ("finished" if item["section"] == "Completed / Accepted" else "open"), []
    return "free", []


def rate_limit(agents, tasks, prefix, now):
    matching = [
        task for task in tasks if (task.get("external_id") or "").startswith(prefix)
    ]
    result: dict[str, Any] = {"open": sum(not closed(task) for task in matching)}
    if "At most one open heartbeat task" in agents:
        result["open_cap"] = 1
    # This repository's temporary cap expired; the rolling limit still applies.
    match = re.search(r"at most one new task.*?in any (\d+) days", agents, re.I | re.S)
    if match:
        seconds = int(match[1]) * 86400
        recent = [
            task for task in matching if timestamp(task["created_at"]) > now - seconds
        ]
        result.update(cap=1, window_days=int(match[1]), count=len(recent))
        if recent:
            result["reopens_at"] = (
                max(timestamp(task["created_at"]) for task in recent) + seconds
            )
            result["matching_tasks"] = [task["id"] for task in recent]
    result["blocked"] = bool(
        result.get("reopens_at", 0) > now
        or result["open"] >= result.get("open_cap", math.inf)
    )
    return result


def preflight(
    db_path, repositories, quota_path, workspace_name="Default Workspace", now=None
):
    now = time.time() if now is None else now
    with closing(read_db(db_path)) as db:
        db.execute("BEGIN")
        ids = resolve_ids(db, workspace_name)
        tasks = rows(
            db,
            "SELECT id, workspace_id, workflow_id, title, state, archived_at, "
            "external_id, created_at FROM tasks WHERE workspace_id=?",
            (ids["workspace"],),
        )
        bindings = rows(
            db,
            "SELECT tr.task_id, r.local_path FROM task_repositories tr "
            "JOIN repositories r ON r.id=tr.repository_id",
        )
    open_tasks = [
        task
        for task in tasks
        if not closed(task) and not task["title"].startswith(HEARTBEAT_TITLE)
    ]
    result = {
        "capacity": {
            "open": len(open_tasks),
            "cap": 20,
            "remaining": max(0, 20 - len(open_tasks)),
        },
        "ids": ids,
        "repositories": [],
        "errors": [],
    }
    try:
        result["quota"] = profile_choice(quota_path, now)
    except (OSError, ValueError, KeyError, TypeError) as error:
        result["errors"].append(f"quota: {error}")
    for root in repositories:
        root = root.resolve()
        entry = {"path": str(root)}
        try:
            agents, rules = instructions(root / "AGENTS.md")
            items, revisions = sources(root, agents)
            prefix = f"heartbeat:{root.name}:"
            limit = rate_limit(agents, tasks, prefix, now)
            bound = {
                binding["task_id"]
                for binding in bindings
                if binding["local_path"]
                and Path(binding["local_path"]).resolve() == root
            }
            entry.update(
                open_tasks=[task for task in open_tasks if task["id"] in bound],
                old_heartbeat_tasks=[
                    task
                    for task in tasks
                    if (task.get("external_id") or "").startswith(prefix)
                    and not closed(task)
                    and timestamp(task["created_at"]) < now - 21 * 86400
                ],
                open=sum(task["id"] in bound for task in open_tasks),
                active=sum(
                    item["section"] in {"Ordered backlog", "Scheduled", "In Progress"}
                    for item in items
                ),
                revisions=revisions,
                rules=rules,
                rate_limit=limit,
                commits=tasks_md.git(root, "log", "-5", "--format=%s").splitlines(),
                dirty=bool(tasks_md.git(root, "status", "--porcelain")),
            )
            for item in items:
                item["external_id"] = prefix + item["key"]
                item["coverage"], item["task_ids"] = coverage(item, tasks, bound)
                if sum(other["key"] == item["key"] for other in items) > 1:
                    item["coverage"] = "collision"
                item["eligible"] = (
                    tasks_md.eligible(item, items)
                    and item["coverage"] == "free"
                    and not limit["blocked"]
                    and result["capacity"]["remaining"] > 0
                )
            entry["items"] = items
        except Exception as error:
            entry["error"] = str(error)
            result["errors"].append(f"{root}: {error}")
        result["repositories"].append(entry)
    return result


def digest(result, limit=2):
    """Bound candidate text without truncating the data used for decisions."""
    output = {**result, "repositories": []}
    for repo in result["repositories"]:
        entry = {
            key: value
            for key, value in repo.items()
            if key
            not in {
                "items",
                "rules",
                "commits",
                "revisions",
                "open_tasks",
                "old_heartbeat_tasks",
            }
        }
        items = repo.get("items", [])
        candidates = [item for item in items if item["section"] == "Ordered backlog"]
        candidates.sort(key=lambda item: not item["eligible"])
        entry["candidates"] = [
            {
                key: value
                for key, value in item.items()
                if key in {"key", "coverage", "task_ids", "eligible", "depends_on"}
            }
            | {"title": item["title"][:180]}
            for item in candidates[:limit]
        ]
        entry["omitted"] = max(0, len(candidates) - limit)
        entry["sources"] = sorted(
            {
                str(Path(item["source"]).parent)
                if "/docs/issues/" in item["source"]
                else item["source"]
                for item in items
            }
            | set(repo.get("revisions", {}))
        )
        output["repositories"].append(entry)
    return output


def create(result, repository, key, description, kind="complex", trivial=False):
    if not description.strip():
        raise ValueError("Description must include the problem and acceptance criteria")
    repo = next(
        (
            repo
            for repo in result["repositories"]
            if repo["path"] == str(repository.resolve())
        ),
        None,
    )
    if repo is None or "error" in repo:
        raise ValueError("Repository preflight failed")
    item = next((item for item in repo["items"] if item["key"] == key), None)
    if item is None:
        raise ValueError("Work-item key does not exist")
    # After an uncertain create, reconcile the same identity before eligibility checks.
    if item["coverage"] == "open" and item["task_ids"]:
        return {
            "status": "verify",
            "task_id": item["task_ids"][0],
            "external_id": item["external_id"],
            "local_path": repo["path"],
            "workspace_id": result["ids"]["workspace"],
        }
    if not item["eligible"]:
        raise ValueError(
            f"Item is not eligible: {item['coverage']}, dependencies or capacity/rate limit"
        )
    if "quota" not in result:
        raise ValueError("Cannot choose a profile without a valid quota snapshot")
    ids = result["ids"]
    profile = "Luna Medium" if trivial else result["quota"]["profile"]
    arguments = {
        "workspace_id": ids["workspace"],
        "workflow_id": ids["workflows"][kind],
        "agent_profile_id": ids["profiles"][profile],
        "executor_profile_id": ids["executor"],
        "local_path": repo["path"],
        "external_id": item["external_id"],
        "start_agent": False,
        "title": item["title"],
        "description": (
            f"Selected issue, quoted exactly:\n\n{item['bullet']}\n\n{description.strip()}\n\n"
            f"Repository: {repo['path']}\nSource: {item['source']}\n"
            "Launched by a heartbeat on the gogo development server. Start by following "
            "the repository's rules for marking this issue In Progress. Move exactly one "
            "issue in the tracker diff; preserve every other issue's text from the base branch."
        ),
    }
    return {
        "status": "create",
        "arguments": arguments,
        "source": item["source"],
        "revision": repo["revisions"].get(item["source"]),
    }


def verify(task_id, external_id, repository, workspace):
    task = client.get("/api/v1/tasks/" + quote(task_id, safe=""))
    if task.get("external_id") != external_id or task.get("workspace_id") != workspace:
        raise ValueError(f"Task {task_id}: identity/workspace mismatch")
    if closed(task):
        raise ValueError(f"Task {task_id}: identity collides with finished work")
    bindings = task.get("repositories") or []
    if len(bindings) != 1:
        raise ValueError(f"Task {task_id}: expected one repository binding")
    observed = client.get(
        "/api/v1/repositories/" + quote(bindings[0]["repository_id"], safe="")
    )
    if (
        not observed.get("local_path")
        or Path(observed["local_path"]).resolve() != repository.resolve()
    ):
        raise ValueError(f"Task {task_id}: wrong repository {observed.get('name')}")
    return {"status": "open", "task_id": task_id}


def observation_status(db_path, workspace, vault, state=None):
    """Read only; never freeze a batch or repair the consumer's ledger in the gate."""
    with closing(read_db(db_path)) as db:
        tasks = rows(
            db,
            "SELECT title, state, archived_at, external_id FROM tasks WHERE workspace_id=?",
            (workspace,),
        )
    if any(is_observation_consumer(task) and not closed(task) for task in tasks):
        return "open"
    ledger = vault / "pages/PKB/Kandev Session Observations/consumed.md"
    names = (
        {line.split("\t", 1)[0] for line in ledger.read_text().splitlines() if line}
        if ledger.exists()
        else set()
    )
    state = state or Path.home() / ".local/state/kandev-observation-consumer"
    if (state / "acknowledgement.json").exists() or (
        state / "ledger-commit.json"
    ).exists():
        return "blocked"
    pending_path = state / "pending.json"
    if pending_path.exists():
        pending = json.loads(pending_path.read_text())
        if pending["vault"] != str(vault.resolve()):
            return "blocked"
        outstanding = any(
            record["record"] not in names for record in pending["records"]
        )
        if outstanding:
            existing = next(
                (
                    task
                    for task in tasks
                    if task["external_id"] == pending["arguments"]["external_id"]
                ),
                None,
            )
            return "blocked" if existing and closed(existing) else "ready"
    records, _, errors = unread(vault, names)
    if errors:
        raise ValueError(f"Malformed observations: {errors[0]}")
    return "ready" if records else "empty"


def gate(db_path, repositories, quota_path, workspace_name, vault, now=None):
    result = preflight(db_path, repositories, quota_path, workspace_name, now)
    if result["errors"]:
        return True, "preflight needs attention: " + "; ".join(result["errors"])
    if any(
        item["eligible"]
        for repo in result["repositories"]
        for item in repo.get("items", [])
    ):
        return True, "eligible work exists"
    status = observation_status(db_path, result["ids"]["workspace"], vault)
    if status == "blocked":
        return True, "observation consumer needs recovery"
    if status not in {"open", "empty"} and result["capacity"]["remaining"]:
        return True, "unread observations need review"
    return False, f"no free eligible work; observations {status}"


def archive_finished(db_path, workspace_name="Default Workspace", apply=False):
    """Only successful automation turns without active sessions or child tasks."""
    with closing(read_db(db_path)) as db:
        candidates = rows(
            db,
            "SELECT t.id FROM tasks t JOIN workspaces w ON w.id=t.workspace_id "
            "WHERE w.name=? AND t.title=? AND t.archived_at IS NULL "
            "AND EXISTS (SELECT 1 FROM automation_runs a "
            "WHERE a.task_id=t.id AND a.status='succeeded') "
            "AND NOT EXISTS (SELECT 1 FROM automation_runs a WHERE a.task_id=t.id "
            "AND a.status NOT IN ('succeeded','failed','skipped')) "
            "AND NOT EXISTS (SELECT 1 FROM tasks child WHERE child.parent_id=t.id) "
            "AND NOT EXISTS (SELECT 1 FROM task_sessions s WHERE s.task_id=t.id "
            "AND s.state NOT IN ('WAITING_FOR_INPUT','COMPLETED','CANCELLED','FAILED'))",
            (workspace_name, HEARTBEAT_TITLE),
        )
    archived = []
    for task in candidates:
        if apply:
            fresh = client.get("/api/v1/tasks/" + quote(task["id"], safe=""))
            if fresh.get("title") != HEARTBEAT_TITLE or fresh.get("archived_at"):
                continue
            sessions = client.sessions_of(task["id"])
            if any(
                session["state"]
                not in {"WAITING_FOR_INPUT", "COMPLETED", "CANCELLED", "FAILED"}
                or session["state"] in RUNNING_STATES
                for session in sessions
            ):
                continue
            client.ws_request("task.archive", {"id": task["id"]})
            if not client.get("/api/v1/tasks/" + quote(task["id"], safe="")).get(
                "archived_at"
            ):
                raise ValueError(f"Task {task['id']}: archive is unconfirmed")
        archived.append(task["id"])
    return {"status": "archived" if apply else "preview", "task_ids": archived}


def main(argv=None):
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=home / ".kandev/data/kandev.db")
    parser.add_argument("--repository", dest="repositories", action="append", type=Path)
    parser.add_argument(
        "--quota", type=Path, default=home / ".local/state/pebble-quota/latest.json"
    )
    parser.add_argument("--workspace", default="Default Workspace")
    sub = parser.add_subparsers(dest="action", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument(
        "--all",
        action="store_true",
        help="Include full bullets, rules, commits and revisions",
    )
    pre.add_argument("--limit", type=int, default=2)
    prepare = sub.add_parser("create")
    prepare.add_argument("repository", type=Path)
    prepare.add_argument("key")
    prepare.add_argument("--description-file", type=Path, required=True)
    prepare.add_argument("--kind", choices=list(WORKFLOWS)[:3], default="complex")
    prepare.add_argument("--trivial", action="store_true")
    check = sub.add_parser("verify")
    check.add_argument("task_id")
    check.add_argument("external_id")
    check.add_argument("repository", type=Path)
    check.add_argument("--workspace-id", required=True)
    sweep = sub.add_parser("archive")
    sweep.add_argument("--apply", action="store_true")
    gate_parser = sub.add_parser("gate")
    gate_parser.add_argument("--vault", type=Path, default=home / "my-knowledge")
    args = parser.parse_args(argv)
    repositories = args.repositories or (
        [args.repository]
        if args.action == "create"
        else [home / path for path in REPOSITORIES]
    )
    try:
        if args.action == "verify":
            result = verify(
                args.task_id, args.external_id, args.repository, args.workspace_id
            )
        elif args.action == "archive":
            result = archive_finished(args.db, args.workspace, args.apply)
        elif args.action == "gate":
            allowed, reason = gate(
                args.db, repositories, args.quota, args.workspace, args.vault
            )
            result = {"run": allowed, "reason": reason}
        else:
            result = preflight(args.db, repositories, args.quota, args.workspace)
            if args.action == "create":
                result = create(
                    result,
                    args.repository,
                    args.key,
                    args.description_file.read_text(),
                    args.kind,
                    args.trivial,
                )
            elif not args.all:
                if args.limit < 0:
                    raise ValueError("Candidate limit must be nonnegative")
                result = digest(result, args.limit)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except Exception as error:
        print(f"heartbeat: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
