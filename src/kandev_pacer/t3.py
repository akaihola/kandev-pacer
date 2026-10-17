"""Admit Hai issues to T3, retaining one launch record per tracker and issue."""

import fcntl
import hashlib
import importlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from . import provider, resets, tasks_md
from .burn import BurnMeter, burn_note
from .config import API_KEY
from .quota import (
    class_windows,
    format_wait,
    quota_verdict,
    resume_note,
    scope_names,
    windows_by_provider,
)
from .selection import matched_scopes
from .t3_client import Server, T3ConfigurationError, T3ConnectionError, T3Rejected

log = logging.getLogger("kandev-pacer")
PROVIDERS = {"codex": "openai", "claudeAgent": "anthropic"}
# Kandev agent names accepted in PACER_AGENT_PROVIDERS for the T3 instance, so the Kandev
# quota-bypass workaround carries over. The instance name wins when both are set.
KANDEV_ALIASES = {"codex": "codex-acp", "claudeAgent": "claude-acp"}


def selection_provider(selection):
    """Map default T3 instances and legacy selections to quota providers."""
    return PROVIDERS.get(selection.get("instanceId", selection.get("provider")))


class Withheld(ValueError):
    """An admission failure with a safe, operator-facing explanation."""


class CheckoutBlocked(Withheld):
    """A tracker's own checkout is unusable; other trackers can still launch."""


class Held(Withheld):
    """A pending launch that waits for a person or a cooldown while other work goes on.

    A tracker hold stops new launches from its whole tracker, because a broken project
    setup would fail the next issue the same way. An issue hold covers only the issue.
    """

    def __init__(self, reason, scope="tracker", retry_at=None, detail=""):
        super().__init__(reason)
        self.scope, self.retry_at, self.detail = scope, retry_at, detail


# Each rejected launch can leave a worktree behind, so stop retrying after a few.
REJECTION_LIMIT = 3
# Space retries out so that a short hiccup, such as another process holding the
# project's .git/config.lock for two minutes, cannot use up every attempt.
REJECTION_COOLDOWN = 3600
NOT_ELIGIBLE = (
    "Pending issue is no longer eligible; "
    "check its section and dependencies in TASKS.md"
)


def stamp(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def rejected_reason(issue, rejections):
    reason = f"T3 rejected launching {issue} ({rejections}/{REJECTION_LIMIT})"
    if rejections >= REJECTION_LIMIT:
        reason += "; check the project's T3 worktree setup"
    return reason


def eligible(snapshot):
    return tasks_md.eligible_issues(snapshot)


def repository_root(store):
    return Path(git(store.root, "rev-parse", "--show-toplevel"))


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


def thread_note(record):
    """Sum up an observed thread on one line: whose it is, where it stands, what it needs.

    The full snapshot stays in the launch record on disk and in the DEBUG log, so the
    INFO line only keeps what a person watching the console acts on: the issue and
    tracker, the provider, the session and turn states, flags that wait for a human,
    and T3's last error text.
    """
    thread = record["thread"]
    parts = [f"status {thread.get('status', 'unknown')}"]
    if thread.get("activityRunStatus"):
        parts.append(f"run {thread['activityRunStatus']}")
    if thread.get("pendingRuntimeRequest"):
        parts.append("needs response")
    if thread.get("pendingBackgroundTasks"):
        parts.append("background work")
    instance = thread.get("modelSelection", {}).get("instanceId")
    note = (
        f"{record['issue']} from {record['tracker']} on {thread['id']}"
        f"{f' ({instance})' if instance else ''}: {', '.join(parts)}"
    )
    if thread.get("lastError"):
        note += f"; error: {thread['lastError']}"
    return note


def activity(thread):
    if "status" not in thread or "activeRunId" not in thread:
        raise Withheld("Incomplete T3 V2 snapshot; withholding work")
    active = {"preparing", "starting", "running", "waiting"}
    if thread["status"] not in active | {
        "idle",
        "queued",
        "completed",
        "interrupted",
        "failed",
        "cancelled",
        "rolled_back",
    } or thread.get("activityRunStatus") not in active | {None}:
        raise Withheld("Unknown T3 V2 thread state; withholding work")
    return bool(
        thread["status"] in active
        or thread["activeRunId"]
        or thread.get("activityRunStatus")
        or thread.get("pendingRuntimeRequest")
        or thread.get("pendingBackgroundTasks")
    )


class T3Pacer:
    def __init__(self, config, server):
        cli = shutil.which("hai-tasks")
        if cli is None:
            fallback = Path(sys.executable).with_name("hai-tasks")
            if not fallback.is_file() or not os.access(fallback, os.X_OK):
                raise Withheld(
                    f"hai-tasks CLI not found: looked on PATH and at {fallback}. "
                    "Install it with uv sync --extra t3."
                )
            cli = str(fallback)
        self.hai_tasks_command = shlex.quote(str(Path(cli).absolute()))
        open_source = importlib.import_module("hai_tasks").open_source

        self.config, self.server = config, server
        entries = config.t3_trackers
        if not isinstance(entries, list) or not entries:
            raise Withheld("PACER_T3_TRACKERS needs tracker entries")
        self.trackers = {}
        for index, entry in enumerate(entries):
            location = f"t3_trackers[{index}]"
            if not isinstance(entry, dict):
                raise Withheld(f"{location} must be a tracker table")
            tracker = entry.get("tracker")
            if not isinstance(tracker, str) or not tracker.strip():
                raise Withheld(f"{location}.tracker must be a nonempty source path")
            try:
                resolved = Path(tracker).expanduser().resolve(strict=True)
            except (OSError, ValueError) as error:
                raise Withheld(
                    f"{location}.tracker cannot be opened ({type(error).__name__}); "
                    "check that the path exists and is accessible"
                ) from error
            if not (resolved.is_file() or resolved.is_dir()):
                raise Withheld(f"{location}.tracker must point to a file or directory")
            path = str(resolved)
            parent = entry.get("parent_thread")
            if path in self.trackers:
                raise Withheld(f"{location}: Each tracker must appear only once")
            if "parent_thread" in entry and (not isinstance(parent, str) or not parent):
                raise Withheld(
                    f"{location}.parent_thread must be a nonempty thread ID when supplied"
                )
            if parent is None:
                missing = [
                    key
                    for key in ("t3_model", "t3_runtime_mode", "t3_interaction_mode")
                    if getattr(config, key) is None
                ]
                if missing:
                    raise Withheld(
                        f"{location}: Trackers without parent_thread require "
                        + ", ".join(missing)
                        + "; set parent_thread to inherit these settings, "
                        "or configure them explicitly"
                    )
            self.trackers[path] = (open_source(path), parent)
        self.state = Path(config.t3_state_dir).expanduser()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.hook_missing_logged = False
        self.admissions = self.state / "admissions"
        self.admissions.mkdir(exist_ok=True, mode=0o700)
        # How fast the agents have been seen to empty a weekly allowance, per provider.
        self.burn = BurnMeter(config.burn_state_path)
        known = {*PROVIDERS, *KANDEV_ALIASES.values()}
        for agent, value in config.agent_provider_overrides.items():
            if agent not in known:
                log.warning(
                    "T3 ignores --agent-providers-raw entry %s=%s: it matches no T3 "
                    "provider instance. Use %s, or the Kandev aliases %s",
                    agent,
                    value,
                    " or ".join(PROVIDERS),
                    " or ".join(KANDEV_ALIASES.values()),
                )

    def record_path(self, tracker, issue):
        key = hashlib.sha256((tracker + "\0" + issue).encode()).hexdigest()
        return self.state / (key + ".json")

    def require_base_branch(self, tracker, root):
        if git(root, "symbolic-ref", "--short", "HEAD") != self.config.t3_base_branch:
            raise CheckoutBlocked(
                f"Scheduling blocked: repository {root} for tracker {tracker} "
                f"is not on the configured base branch {self.config.t3_base_branch}"
            )

    def prepare(self, tracker, store, snapshot, issue, parent, project):
        root = repository_root(store)
        self.require_base_branch(tracker, root)
        if git(root, "status", "--porcelain"):
            raise CheckoutBlocked(
                f"Scheduling blocked: repository {root} for tracker {tracker} "
                "has staged, unstaged, or untracked changes. "
                f"Inspect with: git -C {shlex.quote(str(root))} status --short. "
                "Commit or stash your changes (including untracked files), "
                "then retry scheduling."
            )
        thread_id = str(uuid.uuid4())
        inherited = {
            k: parent[k] for k in ("modelSelection", "runtimeMode", "interactionMode")
        }
        instructions = root / "AGENTS.md"
        prompt = (
            f"Implement {issue.get('sourceReference', issue['identifier'])}: {issue['title']}\n\n"
            f"Canonical tracker: {tracker}\nHai issue ID: {issue['id']}\n\n"
            f"{issue['description']}\n\nRepository instructions:\n"
            + (
                instructions.read_text()
                if instructions.exists()
                else "Read repository instructions."
            )
            + "\n\nTracker workflow:\n"
            + snapshot["parsed"]["source"].partition("## Rules")[2]
            + "\n\nScope: this issue and its required tests and documentation. "
            "`hai_tasks` is Hai's Python package, not a tool. Update the tracker with "
            f"`{self.hai_tasks_command} read <repo>`, then "
            f"`{self.hai_tasks_command} update <repo> <id> "
            "--revision <rev> --json <patch>`. Load the `hai-tasks` skill if it is listed. "
            "Follow repository rules for validation, merge, and completion. "
            + tasks_md.workflow_prompt(snapshot)
            + "Do not send messages to others. "
            "Do not change unrelated work. Report blockers and validation evidence."
        )
        command = dict(
            commandId=str(uuid.uuid4()),
            threadId=thread_id,
            projectId=project["id"],
            title=issue["title"][:200],
            **inherited,
            workspaceStrategy={
                "type": "worktree",
                "baseRef": self.config.t3_base_branch,
                "branch": f"pacer-{thread_id}",
                "startFromOrigin": False,
            },
            initialMessage={
                "messageId": str(uuid.uuid4()),
                "text": prompt,
                "attachments": [],
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
            raise Held(
                "Tracker revision changed before scheduling; "
                "inspect TASKS.md and its Git history"
            )
        issue = next(
            (i for i in eligible(snapshot) if i["id"] == record["issue"]), None
        )
        if issue is None:
            raise Held(NOT_ELIGIBLE, scope="issue")
        self.require_base_branch(record["tracker"], repository_root(store))
        destination = tasks_md.state_for(snapshot, "scheduled")
        if (
            record["phase"] == "prepared"
            and destination
            and issue["sourceState"] != destination
        ):
            result = store.mutate(
                "update",
                snapshot["revision"],
                id=issue["id"],
                input={"sourceState": destination},
            )
            record.update(revision=result["revision"], phase="scheduled")
            save(path, record)
        if store.read()["revision"] != record["revision"]:
            raise Held(
                "Tracker changed during scheduling; inspect TASKS.md and its Git history"
            )
        root = repository_root(store)
        source_path = str(Path(record["tracker"]).relative_to(root))
        # Recover an interrupted scheduling commit within the configured source.
        if destination and git(root, "diff", "HEAD", "--", source_path):
            git(root, "add", "--", source_path)
            git(
                root,
                "commit",
                "--only",
                "-m",
                f"docs(tasks): schedule {issue['identifier']}",
                "--",
                source_path,
            )
        if store.read()["revision"] != record["revision"]:
            raise Held(
                "Tracker changed during commit; inspect TASKS.md and its Git history"
            )
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
        return self.selection_verdict(selection, usage, now).is_open

    def selection_verdict(self, selection, usage, now):
        name = selection_provider(selection)
        windows = windows_by_provider(usage).get(name, [])
        scopes = matched_scopes([selection["model"]], scope_names(windows))
        return quota_verdict(
            usage,
            class_windows(windows, scopes),
            now,
            self.config,
            provider=name,
            burn=self.burn.seconds(name, now),
        )

    def model_candidates(self, parent):
        configured = self.config.t3_model
        if configured == "automatic":
            candidates = [
                model.model_dump(exclude_none=True) for model in self.config.t3_models
            ]
        elif configured is not None:
            candidates = [configured.model_dump(exclude_none=True)]
        else:
            candidates = [parent["modelSelection"]]
        return candidates

    def launch_settings(self, parent, running, usage, errors, now):
        """Choose settings before preparing a durable launch record."""
        available = [
            selection
            for selection in self.model_candidates(parent)
            if self.admitted(selection, running, usage, errors, now)
        ]
        if not available:
            return None
        selection = max(
            available,
            key=lambda item: (
                0.0
                if self.quota_bypassed(selection_provider(item))
                else self.selection_verdict(item, usage, now).headroom
            ),
        )
        return {
            **parent,
            "modelSelection": selection,
            "runtimeMode": self.config.t3_runtime_mode or parent["runtimeMode"],
            "interactionMode": self.config.t3_interaction_mode
            or parent["interactionMode"],
        }

    def quota_bypassed(self, name):
        instance = next(
            (key for key, value in PROVIDERS.items() if value == name), None
        )
        overrides = self.config.agent_provider_overrides
        alias = overrides.get(KANDEV_ALIASES.get(instance))
        return overrides.get(instance, alias) == API_KEY

    def report_status(self, running, usage, errors, now, threads):
        windows = windows_by_provider(usage)
        for name, provider_windows in windows.items():
            if self.burn.observe(name, provider_windows, now):
                log.info(
                    "T3 [%s] burn time %s",
                    name,
                    burn_note(self.burn.estimate(name, now)),
                )
        parents = {
            selection_provider(threads[parent].get("modelSelection", {}))
            for _, parent in self.trackers.values()
            if parent in threads
        }
        configured = self.config.t3_model
        if configured is not None:
            candidates = (
                self.config.t3_models if configured == "automatic" else [configured]
            )
            parents = {PROVIDERS[model.instanceId] for model in candidates}
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
                    burn=self.burn.seconds(name, now),
                )
                label = name + (f" ({'+'.join(scopes)})" if scopes else "")
                state = verdict.message or f"headroom={verdict.headroom:+.3f} open"
                burn = self.burn.estimate(name, now)
                log.info(
                    "T3 [%s] %s%s%s%s, next tick in %ss",
                    label,
                    state,
                    capacity,
                    resume_note(verdict.resumes_at, now),
                    f", burn {burn_note(burn)}" if burn and not scopes else "",
                    self.config.interval,
                )

    def paced_runs(self, snapshot, threads, running):
        """Discover parked work and retain capacity while an admission becomes visible."""
        if "pacedRuns" not in snapshot:
            if not self.hook_missing_logged:
                log.warning(
                    "T3 shell has no pacedRuns; pacing hook unavailable, admitting no paced runs"
                )
                self.hook_missing_logged = True
            return []
        listed = {run["runId"]: run for run in snapshot["pacedRuns"]}
        records = {}
        for path in self.admissions.glob("*.json"):
            record = json.loads(path.read_text())
            if record["url"] != self.server.url:
                continue
            run = record["run"]
            records[run["runId"]] = record
            if record["phase"] == "accepted":
                thread = threads.get(run["threadId"])
                record["unobserved_ticks"] = record.get("unobserved_ticks", 0) + 1
                if (thread and activity(thread)) or record["unobserved_ticks"] >= 3:
                    record["phase"] = "done"
                else:
                    running[selection_provider(run["modelSelection"])] += 1
                save(path, record)
        candidates = []
        for run in listed.values():
            record = records.get(run["runId"])
            if record and record["phase"] in {"accepted", "done", "dropped"}:
                continue
            selection = run["modelSelection"]
            if selection_provider(selection) is None:
                log.warning(
                    "T3 skipping paced run %s: unsupported provider instance %r",
                    run["runId"],
                    selection.get("instanceId"),
                )
                continue
            if threads.get(run["threadId"], {}).get("lastErrorClass") == "usage_limit":
                continue
            candidates.append(run)
        # A lost reply must be reconciled even after the run leaves pacedRuns.
        uncertain = [r["run"] for r in records.values() if r["phase"] == "sending"]
        uncertain_ids = {r["runId"] for r in uncertain}
        return sorted(uncertain, key=lambda r: r["requestedAt"]) + sorted(
            (r for r in candidates if r["runId"] not in uncertain_ids),
            key=lambda r: r["requestedAt"],
        )

    def admit_paced(self, runs, running, usage, errors, now):
        for run in runs:
            key = hashlib.sha256(
                (self.server.url + "\0" + run["runId"]).encode()
            ).hexdigest()
            path = self.admissions / (key + ".json")
            record: dict = (
                json.loads(path.read_text())
                if path.exists()
                else {
                    "url": self.server.url,
                    "run": run,
                    "attempt": 0,
                    "phase": "retry",
                }
            )
            if record["phase"] != "sending":
                if not self.admitted(
                    run["modelSelection"], running, usage, errors, now
                ):
                    continue
                record["attempt"] += 1
                record["command"] = {
                    "type": "queued-run.admit",
                    "commandId": f"pace-admit:{run['runId']}:{record['attempt']}",
                    "threadId": run["threadId"],
                    "runId": run["runId"],
                }
                record["phase"] = "sending"
                save(path, record)
            try:
                result = self.server.call(
                    "orchestration.dispatchCommand", record["command"]
                )
            except T3Rejected as error:
                message = str(error)
                if any(
                    f"queued-run.admit refused: {reason}" in message
                    for reason in ("not_waiting", "inactive")
                ):
                    record["phase"] = "dropped"
                elif "previously rejected" in message or any(
                    f"queued-run.admit refused: {reason}" in message
                    for reason in ("busy", "not_next", "usage_limited")
                ):
                    record["phase"] = "retry"
                else:
                    raise
                record["error"] = message
                save(path, record)
                log.info("T3 paced run %s: %s", run["runId"], message)
            else:
                record.update(
                    phase="accepted", sequence=result["sequence"], unobserved_ticks=0
                )
                save(path, record)
                log.info("T3 paced run admitted: %s", run["runId"])
            return True
        return False

    def tick(self):
        snapshot = self.server.snapshot()
        threads = {t["id"]: t for t in snapshot["threads"]}
        # Archived threads remain observable but consume no capacity.
        archived = {t["id"]: t for t in snapshot.get("archivedThreads", [])}
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
        now = time.time()
        records, pending, held = {}, [], []
        ignored = Counter()
        for path in sorted(self.state.glob("*.json")):
            record = json.loads(path.read_text())
            if record["url"] != self.server.url:
                raise Withheld(
                    f"No new tasks will launch this tick; existing T3 threads continue. "
                    f"Saved launch record {path} for issue {record['issue']} "
                    "remembers a launch to prevent duplicate threads, but "
                    "saved T3 server URL differs from t3_url (PACER_T3_URL). "
                    "Restore the intended server settings and restart. "
                    "If this record is obsolete, stop the pacer, inspect its threadId "
                    "in the original T3 server, then move only this file outside "
                    "t3_state_dir before restarting. Removing a record for queued "
                    "work can launch it again. See docs/t3-pacing.md#launch-record-mismatch."
                )
            if record["tracker"] not in self.trackers:
                ignored[record["tracker"]] += 1
                continue
            records[(record["tracker"], record["issue"])] = record
            thread = threads.get(record["command"]["threadId"]) or archived.get(
                record["command"]["threadId"]
            )
            if thread:
                record.pop("hold", None)
                record["phase"] = "observed"
                record["thread"] = {
                    k: thread.get(k)
                    for k in (
                        "id",
                        "branch",
                        "worktreePath",
                        "status",
                        "activeRunId",
                        "activityRunStatus",
                        "pendingRuntimeRequest",
                        "pendingBackgroundTasks",
                        "modelSelection",
                        "lastError",
                    )
                }
                save(path, record)
                log.info("T3 %s", thread_note(record))
                log.debug("T3 %s: %s", thread["id"], json.dumps(record["thread"]))
            elif record["phase"] in {"observed", "accepted"}:
                if not record.get("hold"):
                    record["hold"] = {
                        "scope": "issue"
                        if record["phase"] == "observed"
                        else "tracker",
                        "reason": "Previously accepted thread is missing; inspect T3",
                        "since": stamp(now),
                        "retry_at": None,
                    }
                    save(path, record)
                held.append((path, record))
            elif record["phase"] in {"prepared", "scheduled", "submitted"}:
                hold = record.get("hold")
                if hold and (
                    hold["retry_at"] is None
                    or datetime.fromisoformat(hold["retry_at"]).timestamp() > now
                ):
                    held.append((path, record))
                    continue
                if hold:
                    del record["hold"]
                    save(path, record)
                    log.info(
                        "T3 hold on %s from %s ended; retrying its launch",
                        record["issue"],
                        record["tracker"],
                    )
                pending.append((path, record))
            else:
                raise Withheld("Unknown launch record phase; inspect state directory")
        skipped = {r["tracker"] for _, r in held if r["hold"]["scope"] == "tracker"}
        for tracker, count in ignored.items():
            log.info(
                "T3 ignoring %s launch record(s) for unconfigured tracker %s; "
                "retained for duplicate prevention if reconfigured",
                count,
                tracker,
            )
        projects_by_tracker = {}
        for tracker, (store, parent_id) in self.trackers.items():
            if tracker in skipped:
                continue
            parent = threads.get(parent_id) if parent_id is not None else {}
            projects = [
                p
                for p in snapshot["projects"]
                if p["workspaceRoot"] == str(repository_root(store))
            ]
            if len(projects) != 1:
                fix = (
                    "Open or add this directory as a project in the T3 server selected "
                    "by t3_url, or correct t3_trackers[].tracker to that project's TASKS.md"
                    if not projects
                    else "Inspect projects in the T3 server selected by t3_url and "
                    "resolve duplicate workspace roots so exactly one project uses this directory"
                )
                log.warning(
                    f"T3 skipping tracker {tracker!r} this tick: requires "
                    f"exactly one T3 project with workspaceRoot {str(repository_root(store))!r}; "
                    f"found {len(projects)}. {fix}; retry next tick"
                )
                skipped.add(tracker)
                continue
            if parent_id is not None and (
                not parent or parent["projectId"] != projects[0]["id"]
            ):
                log.warning(
                    "T3 skipping tracker %r this tick: parent_thread %r is missing or "
                    "belongs to another project; set parent_thread to a thread in "
                    "the matching T3 project, or remove it and configure t3_model, "
                    "t3_runtime_mode and t3_interaction_mode; retry next tick",
                    tracker,
                    parent_id,
                )
                skipped.add(tracker)
                continue
            projects_by_tracker[tracker] = projects[0]
        pending = [
            (path, record)
            for path, record in pending
            if record["tracker"] not in skipped
        ]
        usage, errors = provider.read(self.config.providers, now=now)
        paced = self.paced_runs(snapshot, threads, running)
        waiting = {
            selection_provider(run["modelSelection"])
            for run in snapshot.get("pacedRuns", [])
        }
        if pending:
            waiting.add(selection_provider(pending[0][1]["command"]["modelSelection"]))
        else:
            for tracker, (store, parent_id) in self.trackers.items():
                if tracker in skipped or not any(
                    (tracker, issue["id"]) not in records
                    for issue in eligible(store.read())
                ):
                    continue
                parent = threads.get(parent_id, {}) if parent_id else {}
                waiting.update(
                    selection_provider(model) for model in self.model_candidates(parent)
                )
        waiting = {
            name
            for name in waiting
            if name in self.config.providers and not self.quota_bypassed(name)
        }
        usage, errors = resets.apply(
            usage,
            errors,
            waiting,
            self.config,
            now,
            {name: self.burn.seconds(name, now) for name in waiting},
        )
        self.report_status(running, usage, errors, now, threads)
        for path, record in held:
            log.warning("T3 HELD %s", self.hold_note(path, record, now))
        if self.admit_paced(paced, running, usage, errors, now):
            return
        # An unresolved launch blocks new admissions, even when its gate is closed.
        # A held launch is not unresolved: it blocks only its own tracker or issue.
        for path, record in pending:
            selection = record["command"]["modelSelection"]
            if not self.admitted(selection, running, usage, errors, now):
                log.info(
                    "T3 pending launch of %s from %s waits for %s admission; "
                    "new launches wait behind it",
                    record["issue"],
                    record["tracker"],
                    selection_provider(selection),
                )
                return
            if self.launch(path, record, skipped, now):
                return
        for tracker, (store, parent_id) in self.trackers.items():
            if tracker in skipped:
                continue
            parent = threads.get(parent_id) if parent_id is not None else {}
            parent = self.launch_settings(parent, running, usage, errors, now)
            if parent is None:
                continue
            source = store.read()
            for issue in eligible(source):
                if (tracker, issue["id"]) in records:
                    continue
                try:
                    path, record = self.prepare(
                        tracker,
                        store,
                        source,
                        issue,
                        parent,
                        projects_by_tracker[tracker],
                    )
                except CheckoutBlocked as error:
                    log.warning("T3 skipping tracker this tick: %s", error)
                    break
                if self.launch(path, record, skipped, now):
                    return
                break

    def launch(self, path, record, skipped, now):
        """Schedule and dispatch a record; return whether T3 accepted it.

        When the launch is held or its checkout is blocked, add the tracker to
        `skipped` if no other issue from it should launch this tick.
        """
        try:
            if record["phase"] in {"prepared", "scheduled"}:
                self.schedule(path, record)
            self.dispatch(path, record, now)
            return True
        except CheckoutBlocked as error:
            log.warning("T3 skipping tracker this tick: %s", error)
            skipped.add(record["tracker"])
        except Held as error:
            record["hold"] = {
                "scope": error.scope,
                "reason": str(error),
                "since": stamp(now),
                "retry_at": stamp(error.retry_at) if error.retry_at else None,
            }
            save(path, record)
            log.warning(
                "T3 holding %s", self.hold_note(path, record, now, error.detail)
            )
            if error.scope == "tracker":
                skipped.add(record["tracker"])
        return False

    def hold_note(self, path, record, now, detail=""):
        """Say on one line what is held, since when, why, and what releases it."""
        hold = record["hold"]
        what = (
            f"tracker {record['tracker']}"
            if hold["scope"] == "tracker"
            else f"issue {record['issue']} from {record['tracker']}"
        )
        since = datetime.fromisoformat(hold["since"]).astimezone()
        release = f"rm {shlex.quote(str(path))}"
        if hold["retry_at"]:
            retry = datetime.fromisoformat(hold["retry_at"]).timestamp()
            release = (
                f"retries automatically in ~{format_wait(retry - now)} "
                f"(~{datetime.fromtimestamp(retry):%H:%M}); to retry now: {release}"
            )
        else:
            release = "to release: " + release
        return (
            f"{what} since {since:%Y-%m-%d %H:%M}: {hold['reason']}{detail}; "
            f"issue {record['issue']} thread {record['command']['threadId']}; {release}"
        )

    def dispatch(self, path, record, now):
        store, _ = self.trackers[record["tracker"]]
        # The schedule commit is in, so workers merging their own tracker updates
        # may move the revision on; only this issue leaving the queue withholds it.
        if not any(i["id"] == record["issue"] for i in eligible(store.read())):
            raise Held(NOT_ELIGIBLE, scope="issue")
        rejections = record.get("rejections", 0)
        if rejections >= REJECTION_LIMIT:
            # Records saved before holds existed can reach the limit unheld.
            raise Held(rejected_reason(record["issue"], rejections))
        command = record["command"]
        try:
            if "bootstrap" in command:
                raise Held("Saved V1 launch needs inspection before retrying on V2")
            result = self.server.call("orchestration.launchThread", command)
            if result["threadId"] != command["threadId"]:
                raise Held(
                    "T3 returned a different thread ID; inspect the saved launch"
                )
            record["result"] = result
        except T3Rejected as error:
            threads = self.server.snapshot()["threads"]
            if any(t["id"] == command["threadId"] for t in threads):
                raise  # The next tick observes the thread instead of relaunching.
            # A refused workspace setup can leave a branch behind. V2 cleanup
            # is unverified, so keep bounded retries with fresh branch names.
            # Keep T3's reason: its own logs rotate away within days.
            worktree = command["workspaceStrategy"]
            record.setdefault("rejected", []).append(
                {
                    "branch": worktree["branch"],
                    "at": stamp(now),
                    "error": str(error)[:4000],
                }
            )
            thread_id = str(uuid.uuid4())
            command.update(threadId=thread_id, commandId=str(uuid.uuid4()))
            command["initialMessage"]["messageId"] = str(uuid.uuid4())
            worktree["branch"] = f"pacer-{thread_id}"
            record["rejections"] = rejections + 1
            save(path, record)
            leftovers = ", ".join(r["branch"] for r in record["rejected"])
            raise Held(
                rejected_reason(record["issue"], record["rejections"]),
                retry_at=(
                    now + REJECTION_COOLDOWN
                    if record["rejections"] < REJECTION_LIMIT
                    else None
                ),
                detail=f": {str(error)[:500]}; leftover worktree branches in "
                f"{store.root}: {leftovers}",
            ) from None
        record["phase"] = "accepted"
        save(path, record)
        log.info("T3 launch accepted: %s", command["threadId"])


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
