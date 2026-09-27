"""Prepare bounded review tasks and acknowledge immutable session observations."""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from . import client
from .config import settings
from .learn import OBSERVATIONS, commit_records, locked, read_record, save_state
from .learn_sources import command, timestamp
from .selection import OBSERVATION_CONSUMER_TITLE, is_observation_consumer
from .workflow import admission_target, queue_step_id

BATCH_SIZE = 20


def git(vault, *args):
    return command(["git", "-C", str(vault), *args]).strip()


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def ledger(vault, state):
    """Finish our interrupted commit before returning any consumed names."""
    path = vault / OBSERVATIONS / "consumed.md"
    journal = state / "acknowledgement.json"
    if journal.exists():
        pending = json.loads(journal.read_text())
        if pending["vault"] != str(vault.resolve()):
            raise ValueError("acknowledgement belongs to another vault")
        current = path.read_text() if path.exists() else ""
        if current not in (pending["before"], pending["after"]):
            raise ValueError("consumed.md changed during acknowledgement recovery")
        atomic_text(path, pending["after"])
        commit_records(vault, [path], journal=state / "ledger-commit.json")
        journal.unlink()
    relative = str(path.relative_to(vault))
    if git(vault, "status", "--porcelain", "--", relative):
        raise ValueError("consumed.md has uncommitted changes outside this helper")
    text = path.read_text() if path.exists() else ""
    return text, {line.split("\t", 1)[0] for line in text.splitlines() if line}


def consumed(name, names):
    return name in names or Path(name).name in names


def unread(vault, names):
    records, skipped, errors = [], [], []
    root = vault / OBSERVATIONS
    for path in root.glob("*/*.md"):
        name = path.relative_to(root).as_posix()
        if consumed(name, names):
            continue
        try:
            record = read_record(path)
            if is_observation_consumer(record):
                skipped.append(name)
            else:
                records.append((timestamp(record["archived_at"]), name, record["task"]))
        except (OSError, ValueError, TypeError) as error:
            errors.append({"record": name, "error": str(error)})
    records.sort()
    return (
        [{"record": name, "task": task} for _, name, task in records[:BATCH_SIZE]],
        sorted(skipped),
        errors,
    )


def closed(task):
    return bool(task.get("archived_at")) or task["state"] in {
        "COMPLETED",
        "CANCELLED",
        "FAILED",
    }


def workspace_tasks(workspace):
    tasks: list[dict[str, Any]] = []
    page = 1
    expected_total = None
    while True:
        response = client.get(
            f"/api/v1/workspaces/{quote(workspace, safe='')}/tasks?page={page}&page_size=100"
        )
        rows, total = response["tasks"], response["total"]
        if not isinstance(rows, list) or not isinstance(total, int):
            raise ValueError("invalid task listing")
        if expected_total is not None and total != expected_total:
            raise ValueError("task listing changed while paging; retry")
        expected_total = total
        if any(row["id"] in {t["id"] for t in tasks} for row in rows):
            raise ValueError("task listing changed while paging; retry")
        tasks.extend(rows)
        if len(tasks) == total:
            return tasks
        if not rows or len(tasks) > total:
            raise ValueError("incomplete task listing; retry")
        page += 1


def lookup(workspace, external_id):
    try:
        return client.get(
            f"/api/v1/workspaces/{quote(workspace, safe='')}/tasks/by-external-id?"
            + urlencode({"external_id": external_id})
        )
    except client.ReadFailed as error:
        cause = error.__cause__
        if isinstance(cause, urllib.error.HTTPError) and cause.code == 404:
            return None
        raise


def project_repository(task):
    bindings = task.get("repositories") or []
    if len(bindings) != 1:
        raise ValueError("original task must have exactly one repository binding")
    repository = client.get(
        "/api/v1/repositories/" + quote(bindings[0]["repository_id"], safe="")
    )
    path = repository.get("local_path")
    if not path or not Path(path).is_absolute():
        raise ValueError("repository has no absolute local path")
    return str(Path(path).resolve())


def target_repository(kind, task_id, home):
    if kind == "kandev":
        return str(home / "prg/kandev-config")
    if kind == "global":
        return str(home / "repos/nixos-config")
    if kind != "project":
        raise ValueError("unknown finding kind")
    task = client.get("/api/v1/tasks/" + quote(task_id, safe=""))
    return project_repository(task)


def verify_task(task, arguments):
    if (
        not is_observation_consumer(task)
        or task["workspace_id"] != arguments["workspace_id"]
    ):
        raise ValueError("consumer task identity does not match")
    if task["workflow_id"] != arguments["workflow_id"]:
        raise ValueError("consumer workflow does not match")
    if project_repository(task) != str(Path(arguments["local_path"]).resolve()):
        raise ValueError("consumer repository binding does not match")
    if task.get("priority") != "low":
        client.ws_request("task.update", {"id": task["id"], "priority": "low"})
        fresh = client.get("/api/v1/tasks/" + quote(task["id"], safe=""))
        if fresh.get("priority") != "low":
            raise ValueError("consumer priority update is unconfirmed; retry same task")


def prepare(vault, state, workspace, workflow, agent, executor, prompt_path, home):
    """Return MCP create arguments or the existing task, never create a task here."""
    with locked(state / "consumer.lock"):
        text, names = ledger(vault, state)
        tasks = workspace_tasks(workspace)
        open_consumers = [
            t for t in tasks if is_observation_consumer(t) and not closed(t)
        ]
        if len(open_consumers) > 1:
            raise ValueError("multiple open consumers already exist")
        pending_path = state / "pending.json"
        pending = (
            json.loads(pending_path.read_text()) if pending_path.exists() else None
        )
        if pending:
            if (
                pending["server"] != settings.url
                or pending["arguments"]["workspace_id"] != workspace
                or pending["vault"] != str(vault.resolve())
            ):
                raise ValueError(
                    "pending batch belongs to another server, workspace or vault"
                )
            task = lookup(workspace, pending["arguments"]["external_id"])
            if task:
                if not task.get("creation_complete"):
                    return {"status": "unsettled", "task_id": task["id"]}
                if not closed(task):
                    verify_task(task, pending["arguments"])
                    return {"status": "open", "task_id": task["id"]}
            if all(consumed(r["record"], names) for r in pending["records"]):
                pending_path.unlink()
                pending = None
            elif task:
                return {
                    "status": "blocked",
                    "task_id": task["id"],
                    "reason": "closed batch has unread records",
                }
        if open_consumers:
            return {"status": "open", "task_id": open_consumers[0]["id"]}
        if pending:
            return {"status": "create", "arguments": pending["arguments"]}
        records, skipped, errors = unread(vault, names)
        if not records:
            return {"status": "empty", "skipped_self": len(skipped), "errors": errors}
        steps = client.steps_of(workflow)
        if not admission_target(steps) or not queue_step_id(steps):
            raise ValueError("consumer workflow has no paced admission and queue")
        identity = hashlib.sha256(
            json.dumps([workspace, text, records]).encode()
        ).hexdigest()
        arguments = {
            "workspace_id": workspace,
            "workflow_id": workflow,
            "workflow_step_id": queue_step_id(steps),
            "agent_profile_id": agent,
            "executor_profile_id": executor,
            "title": OBSERVATION_CONSUMER_TITLE,
            "local_path": str(home / "prg/kandev-config"),
            "external_id": "observation-consumer:" + identity,
            "start_agent": False,
            "prompt": prompt_path.read_text()
            + "\n\n## Frozen batch\n\n"
            + json.dumps(
                {
                    "external_id": "observation-consumer:" + identity,
                    "vault": str(vault),
                    "records": records,
                    "skipped_self": skipped,
                },
                indent=2,
            ),
        }
        save_state(
            pending_path,
            {
                "server": settings.url,
                "vault": str(vault.resolve()),
                "arguments": arguments,
                "records": records,
                "skipped_self": skipped,
            },
        )
        return {"status": "create", "arguments": arguments, "errors": errors}


def acknowledge(vault, state, external_id, handled):
    """Commit only the records the consumer successfully finished, plus self skips."""
    with locked(state / "consumer.lock"):
        before, names = ledger(vault, state)
        pending = json.loads((state / "pending.json").read_text())
        if pending["vault"] != str(vault.resolve()):
            raise ValueError("pending batch belongs to another vault")
        if external_id != pending["arguments"]["external_id"]:
            raise ValueError("acknowledgement does not match pending batch")
        selected = {row["record"] for row in pending["records"]}
        if not set(handled) <= selected:
            raise ValueError("acknowledgement includes records outside the batch")
        additions = [
            name for name in dict.fromkeys(handled) if not consumed(name, names)
        ]
        additions += [
            name + "\tskipped: consumer's own run"
            for name in pending["skipped_self"]
            if not consumed(name, names)
        ]
        if additions:
            after = before + ("\n" if before and not before.endswith("\n") else "")
            after += "\n".join(additions) + "\n"
            save_state(
                state / "acknowledgement.json",
                {"vault": str(vault.resolve()), "before": before, "after": after},
            )
            ledger(vault, state)
        return {"status": "acknowledged", "records": len(additions)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=Path.home() / "my-knowledge")
    parser.add_argument(
        "--state",
        type=Path,
        default=Path.home() / ".local/state/kandev-observation-consumer",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    prepare_parser = sub.add_parser("prepare")
    for name in ("workspace", "workflow", "agent", "executor"):
        prepare_parser.add_argument("--" + name, required=True)
    prepare_parser.add_argument(
        "--prompt",
        type=Path,
        default=Path.home() / "prg/kandev-config/prompts/observation-consumer.md",
    )
    route = sub.add_parser("route")
    route.add_argument("kind", choices=["project", "kandev", "global"])
    route.add_argument("task_id")
    ack = sub.add_parser("acknowledge")
    ack.add_argument("external_id")
    ack.add_argument("records", nargs="*")
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            result = prepare(
                args.vault,
                args.state,
                args.workspace,
                args.workflow,
                args.agent,
                args.executor,
                args.prompt,
                Path.home(),
            )
        elif args.action == "route":
            result = {
                "local_path": target_repository(args.kind, args.task_id, Path.home())
            }
        else:
            result = acknowledge(args.vault, args.state, args.external_id, args.records)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"observation-consumer: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
