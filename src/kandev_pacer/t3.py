"""Admit Hai issues to T3, retaining one launch record per tracker and issue."""

import fcntl
import hashlib
import importlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from . import provider
from .config import API_KEY
from .quota import (
    class_windows,
    quota_verdict,
    resume_note,
    scope_names,
    windows_by_provider,
)
from .selection import matched_scopes
from .t3_client import Server, T3ConfigurationError, T3ConnectionError

log = logging.getLogger("kandev-pacer")
PROVIDERS = {"codex": "openai", "claudeAgent": "anthropic"}


def selection_provider(selection):
    """Map default T3 instances and legacy selections to quota providers."""
    return PROVIDERS.get(selection.get("instanceId", selection.get("provider")))


class Withheld(ValueError):
    """An admission failure with a safe, operator-facing explanation."""


def eligible(snapshot):
    issues = snapshot["workspace"]["issues"]
    completed = {
        i["sourceReference"][1:-1]
        for i in issues
        if i["sourceSection"] == "Completed / Accepted"
    }
    return [
        issue
        for section in ("Scheduled", "Ordered backlog")
        for issue in issues
        if issue["sourceSection"] == section and set(issue["dependencies"]) <= completed
    ]


def save(path, record):
    """Replace a private record durably before any external write."""
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), *args],
        text=True,
        stderr=subprocess.PIPE,
        timeout=30,
        env={**os.environ, "SHELL": shutil.which("bash") or "bash"},
    ).strip()


def activity(thread):
    session, turn = thread.get("session"), thread.get("latestTurn")
    if session and session.get("status") not in {
        "idle",
        "starting",
        "running",
        "ready",
        "interrupted",
        "stopped",
        "error",
    }:
        raise Withheld("Unknown T3 session state; withholding work")
    if turn and turn.get("state") not in {
        "running",
        "interrupted",
        "completed",
        "error",
    }:
        raise Withheld("Unknown T3 turn state; withholding work")
    if "session" not in thread or "latestTurn" not in thread:
        raise Withheld("Incomplete T3 snapshot; withholding work")
    return bool(
        (
            session
            and (
                session["status"] in {"starting", "running"}
                or session.get("activeTurnId")
            )
        )
        or (turn and turn["state"] == "running")
        or thread.get("backgroundLiveness") in {"working", "monitoring"}
        or thread.get("hasPendingApprovals")
        or thread.get("hasPendingUserInput")
    )


class T3Pacer:
    def __init__(self, config, server):
        TasksStore = importlib.import_module("hai_tasks").TasksStore

        self.config, self.server = config, server
        entries = json.loads(config.t3_trackers)
        if not isinstance(entries, list) or not entries:
            raise Withheld("PACER_T3_TRACKERS needs tracker/parent_thread entries")
        self.trackers = {}
        for entry in entries:
            path = str(Path(entry["tracker"]).expanduser().resolve(strict=True))
            parent = entry["parent_thread"]
            if path in self.trackers or not isinstance(parent, str) or not parent:
                raise Withheld("Each tracker needs one explicit parent thread")
            self.trackers[path] = (TasksStore(path), parent)
        self.state = Path(config.t3_state_dir).expanduser()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)

    def record_path(self, tracker, issue):
        key = hashlib.sha256((tracker + "\0" + issue).encode()).hexdigest()
        return self.state / (key + ".json")

    def prepare(self, tracker, store, snapshot, issue, parent, project):
        root = store.root
        if git(root, "symbolic-ref", "--short", "HEAD") != self.config.t3_base_branch:
            raise Withheld("Schedule from the configured base branch")
        if git(root, "status", "--porcelain"):
            raise Withheld("Scheduling requires a clean repository")
        thread_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        inherited = {
            k: parent[k] for k in ("modelSelection", "runtimeMode", "interactionMode")
        }
        instructions = root / "AGENTS.md"
        prompt = (
            f"Implement {issue['sourceReference']}: {issue['title']}\n\n"
            f"Canonical tracker: {tracker}\nHai issue ID: {issue['id']}\n\n"
            f"{issue['description']}\n\nRepository instructions:\n"
            + (
                instructions.read_text()
                if instructions.exists()
                else "Read repository instructions."
            )
            + "\n\nTracker workflow:\n"
            + snapshot["parsed"]["source"].split("## Rules", 1)[1]
            + "\n\nScope: this issue and its required tests and documentation. "
            "Use hai_tasks for tracker updates with revision checks. The issue is already "
            "scheduled and committed on the base branch. Follow repository rules for "
            "moving it to In Progress in this worktree, validation, merge, and completion. "
            "Do not check user acceptance [x]. Do not send messages to others. "
            "Do not change unrelated work. Report blockers and validation evidence."
        )
        command = dict(
            type="thread.turn.start",
            commandId=str(uuid.uuid4()),
            threadId=thread_id,
            **inherited,
            createdAt=now,
            message={
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "text": prompt,
                "attachments": [],
            },
            bootstrap={
                "createThread": dict(
                    projectId=project["id"],
                    title=issue["title"][:200],
                    **inherited,
                    branch=None,
                    worktreePath=None,
                    createdAt=now,
                ),
                "prepareWorktree": {
                    "projectCwd": str(root),
                    "baseBranch": self.config.t3_base_branch,
                    "branch": f"pacer-{thread_id}",
                    "startFromOrigin": False,
                    "requireWorktree": True,
                },
            },
        )
        record = {
            "tracker": tracker,
            "issue": issue["id"],
            "revision": snapshot["revision"],
            "url": self.server.url,
            "command": command,
            "phase": "prepared",
        }
        path = self.record_path(tracker, issue["id"])
        save(path, record)
        return path, record

    def schedule(self, path, record):
        store, _ = self.trackers[record["tracker"]]
        snapshot = store.read()
        if record["revision"] != snapshot["revision"]:
            raise Withheld("Tracker revision changed; inspect pending launch record")
        issue = next(
            (i for i in eligible(snapshot) if i["id"] == record["issue"]), None
        )
        if issue is None:
            raise Withheld("Pending issue is no longer eligible")
        if (
            git(store.root, "symbolic-ref", "--short", "HEAD")
            != self.config.t3_base_branch
        ):
            raise Withheld("Tracker is no longer on the configured base branch")
        if record["phase"] == "prepared":
            result = store.mutate(
                "update",
                snapshot["revision"],
                id=issue["id"],
                input={"sourceSection": "Scheduled"},
            )
            record.update(revision=result["revision"], phase="scheduled")
            save(path, record)
        if store.read()["revision"] != record["revision"]:
            raise Withheld("Tracker changed during scheduling; inspect launch record")
        # Restrict the commit to TASKS.md, including after an interrupted commit.
        if git(store.root, "diff", "HEAD", "--", store.tasks_file.name):
            git(store.root, "add", "--", store.tasks_file.name)
            git(
                store.root,
                "commit",
                "--only",
                "-m",
                f"docs(tasks): schedule {issue['identifier']}",
                "--",
                store.tasks_file.name,
            )
        if store.read()["revision"] != record["revision"]:
            raise Withheld("Tracker changed during commit; inspect launch record")
        record["phase"] = "submitted"
        save(path, record)

    def admitted(self, selection, running, usage, errors, now):
        name = selection_provider(selection)
        if name not in self.config.providers or not selection.get("model"):
            return False
        if running[name] >= self.config.concurrency:
            return False
        if (
            self.config.concurrency_total
            and sum(running.values()) >= self.config.concurrency_total
        ):
            return False
        if self.quota_bypassed(name):
            return self.config.apikey_policy == "cap"
        if name in errors:
            return False
        windows = windows_by_provider(usage).get(name, [])
        scopes = matched_scopes([selection["model"]], scope_names(windows))
        verdict = quota_verdict(
            usage, class_windows(windows, scopes), now, self.config, provider=name
        )
        return verdict.is_open

    def quota_bypassed(self, name):
        instance = next(
            (key for key, value in PROVIDERS.items() if value == name), None
        )
        overrides = self.config.agent_provider_overrides
        return (
            overrides.get(
                instance, overrides.get("codex-acp") if instance == "codex" else None
            )
            == API_KEY
        )

    def report_status(self, running, usage, errors, now, threads):
        windows = windows_by_provider(usage)
        parents = {
            selection_provider(threads[parent].get("modelSelection", {}))
            for _, parent in self.trackers.values()
            if parent in threads
        }
        for name in self.config.providers:
            capacity = f" ({running[name]}/{self.config.concurrency} running)"
            if self.config.concurrency_total:
                capacity += (
                    f", {sum(running.values())}/{self.config.concurrency_total} total"
                )
            if name not in parents:
                capacity += ", no tracker parent uses this provider"
            if self.quota_bypassed(name):
                state = (
                    "quota bypass, concurrency only"
                    if self.config.apikey_policy == "cap"
                    else "WITHHELD by apikey-policy=withhold"
                )
                log.info("T3 [%s] %s%s", name, state, capacity)
                continue
            if name in errors:
                log.warning("T3 [%s] quota unavailable, WITHHOLDING%s", name, capacity)
                continue
            provider_windows = windows.get(name, [])
            for scopes in [(), *((scope,) for scope in scope_names(provider_windows))]:
                verdict = quota_verdict(
                    usage,
                    class_windows(provider_windows, scopes),
                    now,
                    self.config,
                    provider=name,
                )
                label = name + (f" ({'+'.join(scopes)})" if scopes else "")
                state = verdict.message or f"headroom={verdict.headroom:+.3f} open"
                log.info(
                    "T3 [%s] %s%s%s, next tick in %ss",
                    label,
                    state,
                    capacity,
                    resume_note(verdict.resumes_at, now),
                    self.config.interval,
                )

    def tick(self):
        snapshot = self.server.snapshot()
        threads = {t["id"]: t for t in snapshot["threads"]}
        running = Counter()
        for thread in threads.values():
            if activity(thread):
                selection = thread.get("modelSelection", {})
                name = selection_provider(selection)
                if name is None:
                    field = "instanceId" if "instanceId" in selection else "provider"
                    raise Withheld(
                        f"Active T3 thread {json.dumps(thread['id'])} has unsupported "
                        f"or missing modelSelection.{field}="
                        f"{json.dumps(selection.get(field))}; cannot assign it to a "
                        "subscription for capacity checks. "
                        "Pacer supports codex (OpenAI) and claudeAgent (Anthropic). "
                        "Inspect this thread in T3 and let it finish or stop its activity; "
                        "resolve any pending approval/input or background work. Use a "
                        "supported subscription-backed provider for paced threads. "
                        "--agent-providers-raw and --apikey-policy do not add support for "
                        "T3 provider instances. See docs/t3-pacing.md#unknown-providers."
                    )
                running[name] += 1
        records, pending = {}, []
        for path in sorted(self.state.glob("*.json")):
            record = json.loads(path.read_text())
            if (
                record["url"] != self.server.url
                or record["tracker"] not in self.trackers
            ):
                raise Withheld("Launch record does not match endpoint/trackers")
            records[(record["tracker"], record["issue"])] = record
            thread = threads.get(record["command"]["threadId"])
            if thread:
                record["phase"] = "observed"
                record["thread"] = {
                    k: thread.get(k)
                    for k in (
                        "id",
                        "branch",
                        "worktreePath",
                        "session",
                        "latestTurn",
                        "hasPendingApprovals",
                        "hasPendingUserInput",
                    )
                }
                save(path, record)
                log.info("T3 %s: %s", thread["id"], json.dumps(record["thread"]))
                if not thread.get("session") and not thread.get("latestTurn"):
                    raise Withheld("Launched thread state is unknown; withholding work")
            elif record["phase"] in {"observed", "accepted"}:
                raise Withheld("Previously accepted thread is missing; inspect T3")
            elif record["phase"] in {"prepared", "scheduled", "submitted"}:
                pending.append((path, record))
            else:
                raise Withheld("Unknown launch record phase; inspect state directory")
        now = time.time()
        usage, errors = provider.read(self.config.providers, now=now)
        self.report_status(running, usage, errors, now, threads)
        # An unresolved launch blocks new admissions, even when its gate is closed.
        if pending:
            path, record = pending[0]
            if self.admitted(
                record["command"]["modelSelection"], running, usage, errors, now
            ):
                if record["phase"] in {"prepared", "scheduled"}:
                    self.schedule(path, record)
                self.dispatch(path, record)
            return
        for tracker, (store, parent_id) in self.trackers.items():
            parent = threads.get(parent_id)
            projects = [
                p for p in snapshot["projects"] if p["workspaceRoot"] == str(store.root)
            ]
            if (
                not parent
                or len(projects) != 1
                or parent["projectId"] != projects[0]["id"]
            ):
                raise Withheld("Parent thread must belong to the tracker project")
            if not self.admitted(parent["modelSelection"], running, usage, errors, now):
                continue
            source = store.read()
            for issue in eligible(source):
                if (tracker, issue["id"]) in records:
                    continue
                path, record = self.prepare(
                    tracker, store, source, issue, parent, projects[0]
                )
                self.schedule(path, record)
                self.dispatch(path, record)
                return

    def dispatch(self, path, record):
        store, _ = self.trackers[record["tracker"]]
        snapshot = store.read()
        if snapshot["revision"] != record["revision"]:
            raise Withheld(
                "Tracker changed before dispatch; inspect pending launch record"
            )
        if not any(i["id"] == record["issue"] for i in eligible(snapshot)):
            raise Withheld("Pending issue is no longer eligible")
        self.server.call("orchestration.dispatchCommand", record["command"])
        record["phase"] = "accepted"
        save(path, record)
        log.info("T3 launch accepted: %s", record["command"]["threadId"])


def run(config, dump_order=False):
    try:
        server = (
            None if dump_order else Server(config.t3_url, config.t3_base_dir or None)
        )
        pacer = T3Pacer(config, server)
        if dump_order:
            print(
                json.dumps(
                    [
                        {"tracker": tracker, "issue": issue}
                        for tracker, (store, _) in pacer.trackers.items()
                        for issue in eligible(store.read())
                    ],
                    indent=2,
                )
            )
            return 0
        with (pacer.state / "pacer.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while True:
                try:
                    pacer.tick()
                except (Withheld, T3ConfigurationError, T3ConnectionError) as error:
                    log.error("T3 withheld: %s", error)
                except Exception as error:
                    # Transport exceptions can contain credential-bearing URLs.
                    log.error(
                        "T3 tick withheld (%s); inspect tracker and launch records",
                        type(error).__name__,
                    )
                time.sleep(config.interval)
    except KeyboardInterrupt:
        return 0
    except (Withheld, T3ConfigurationError, T3ConnectionError) as error:
        log.error("T3 startup failed: %s", error)
        return 1
    except Exception as error:
        log.error(
            "T3 startup failed (%s); check configuration and Hai installation",
            type(error).__name__,
        )
        return 1
