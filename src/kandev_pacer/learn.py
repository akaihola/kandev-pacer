"""Immutable measurements for archived Kandev tasks."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from textwrap import dedent
from typing import Any

from .learn_sources import (
    METRICS,
    collect_transcripts,
    command,
    deadline,
    measure,
    pacer_observations,
    read_db,
    timestamp,
)

OBSERVATIONS = Path("pages/PKB/Kandev Session Observations")
USAGE_FIELDS = dict(
    zip(
        METRICS[:-1],
        (
            "tokens_in",
            "tokens_cached_read",
            "tokens_cached_write",
            "tokens_out",
            "tokens_thought",
        ),
        strict=True,
    )
)


def decode(value):
    return json.loads(value or "{}") or {}


def table(headers, rows):
    def cell(value):
        if value is None:
            return "unknown"
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace("|", "&#124;")
            .replace("\n", "<br>")
        )

    return "\n".join(
        "| " + " | ".join(map(cell, row)) + " |"
        for row in [headers, ["---"] * len(headers), *rows]
    )


def usage_totals(events):
    totals = {
        key: measure(
            sum(e[field] for e in events if e.get(field) is not None)
            if any(e.get(field) is not None for e in events)
            else None,
            "task_usage_events." + field,
        )
        for key, field in USAGE_FIELDS.items()
    }
    actual = [
        e
        for e in events
        if not e.get("estimated")
        and e.get("cost_source") == "provider_reported"
        and e.get("cost_subcents") is not None
    ]
    totals["cost_usd"] = measure(
        sum(e["cost_subcents"] for e in actual) / 10000 if actual else None,
        "task_usage_events.provider_reported",
    )
    return totals


def ccusage_totals(raw, start=None, end=None):
    """Keep the selected unified session row; never aggregate a report twice."""
    rows = []
    for report in raw:
        if isinstance(report, dict):
            if isinstance(report.get("entries"), list):
                rows.extend(
                    e
                    for e in report["entries"]
                    if not start
                    or (
                        e.get("timestamp")
                        and timestamp(start)
                        <= timestamp(e["timestamp"])
                        <= timestamp(end)
                    )
                )
            elif isinstance(report.get("sessions"), list):
                rows.extend(
                    {
                        **r,
                        "reasoningTokens": r.get(
                            "reasoningOutputTokens", r.get("reasoningTokens")
                        ),
                    }
                    for r in report["sessions"]
                )
            elif "inputTokens" in report:
                rows.append(report)
    aliases = {
        "input": "inputTokens",
        "output": "outputTokens",
        "cache_read": "cacheReadTokens",
        "cache_write": "cacheCreationTokens",
        "reasoning": "reasoningTokens",
    }
    return {
        key: measure(
            sum(r[field] for r in rows if r.get(field) is not None),
            "ccusage@20.0.19." + field,
        )
        for key, field in aliases.items()
        if rows and any(r.get(field) is not None for r in rows)
    }


def session_steps(raw, start, end, events, turns, waits, assigned=None):
    steps = []
    prior = start
    name = raw[0].get("from_step_id") if raw else None
    for row in [*raw, {"created_at": end, "to_step_id": None}]:
        at = row["created_at"]
        if timestamp(at) < timestamp(start):
            name = row.get("to_step_id")
            continue
        stop = min(timestamp(at), timestamp(end))
        if stop > timestamp(prior):
            matched_turns = [
                t
                for t in turns
                if t.get("started_at")
                and t.get("completed_at")
                and timestamp(prior) <= timestamp(t["started_at"]) < stop
                and timestamp(t["completed_at"]) <= stop
            ]
            ids = {t["id"] for t in matched_turns}
            observed = [
                e
                for e in events
                if e.get("turn_id") in ids
                or (
                    not e.get("turn_id")
                    and timestamp(prior) <= timestamp(e["occurred_at"]) < stop
                )
            ]
            if assigned is not None:
                assigned.update(id(e) for e in observed)
            steps.append(
                {
                    "step": name,
                    "start": prior,
                    "end": at if stop == timestamp(at) else end,
                    "wall_seconds": stop - timestamp(prior),
                    "turns": len(matched_turns),
                    "preceding_pacer_wait": next(
                        (w["wait"] for w in waits if timestamp(prior) <= w["t"] < stop),
                        None,
                    ),
                    "totals": usage_totals(observed),
                }
            )
        prior, name = at, row.get("to_step_id")
        if stop >= timestamp(end):
            break
    return steps


def collect_task(db_path, task_id, home, timeout=45):
    expires = time.monotonic() + 3 * timeout
    with deadline(timeout), contextlib.closing(read_db(db_path)) as db:
        db.execute("BEGIN")
        task = db.execute(
            "SELECT t.*, w.name AS workflow FROM tasks t "
            "LEFT JOIN workflows w ON w.id=t.workflow_id WHERE t.id=?",
            (task_id,),
        ).fetchone()
        if not task or not task["archived_at"]:
            raise ValueError("task is unknown or not archived")
        record = {
            k: task[k]
            for k in ("title", "workflow", "archived_at", "archived_by_cascade_id")
        }
        record.update(schema=1, task=task_id, sessions=[], notes=[], diffs=[])
        record["blockers"] = {
            k: v for k, v in decode(task["metadata"]).items() if "block" in k.lower()
        }
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT s.*, a.name AS agent_name, p.model AS profile_model FROM task_sessions s "
                "LEFT JOIN agent_profiles p ON p.id=s.agent_profile_id "
                "LEFT JOIN agents a ON a.id=p.agent_id "
                "WHERE s.task_id=? ORDER BY s.started_at,s.id",
                (task_id,),
            )
        ]
        all_sessions = [
            dict(r)
            for r in db.execute(
                "SELECT id, workspace_path, started_at, completed_at FROM task_sessions"
            )
        ]
        data = {}
        for row in rows:
            sid = row["id"]
            data[sid] = {}
            for key, query in {
                "history": "SELECT * FROM session_step_history WHERE session_id=? "
                "ORDER BY created_at,id",
                "usage": "SELECT * FROM task_usage_events WHERE session_id=? "
                "ORDER BY occurred_at,id",
                "turns": "SELECT * FROM task_session_turns WHERE task_session_id=? "
                "ORDER BY started_at,id",
                "commits": "SELECT * FROM task_session_commits WHERE session_id=?",
            }.items():
                data[sid][key] = [dict(r) for r in db.execute(query, (sid,))]
    seen_commits = set()
    for row in rows:
        snap, metadata = (
            decode(row.get("agent_profile_snapshot")),
            decode(row.get("metadata")),
        )
        agent = (row["agent_name"] or snap.get("agent_name") or "unknown").removesuffix(
            "-acp"
        )
        start = row["started_at"]
        end = min(
            (row.get("completed_at") or record["archived_at"], record["archived_at"]),
            key=timestamp,
        )
        if not start or timestamp(start) > timestamp(end):
            record["notes"].append(f"no data for {agent}: session time window")
            continue
        session: dict[str, Any] = {
            "id": row["id"],
            "agent": agent,
            "models": sorted(
                {m for m in (snap.get("model") or row.get("profile_model"),) if m}
            ),
            "workspace_path": row.get("workspace_path") or "",
            "start": start,
            "end": end,
            "transcript_id": row.get("downstream_acp_session_id")
            or metadata.get("acp", {}).get("session_id"),
        }
        session["ambiguous_window"] = any(
            other["id"] != row["id"]
            and other["workspace_path"] == session["workspace_path"]
            and other["started_at"]
            and timestamp(other["started_at"]) < timestamp(end)
            and timestamp(other["completed_at"] or record["archived_at"])
            > timestamp(start)
            for other in all_sessions
        )
        evidence: dict[str, Any]
        remaining = expires - time.monotonic()
        if remaining > 0:
            evidence = collect_transcripts(home, session, min(timeout, remaining / 2))
        else:
            evidence = {
                "tools": [],
                "ids": [],
                "models": [],
                "requests": [],
                "pi_usage": [],
                "notes": [
                    f"no data for {agent}: task collection timeout",
                    "no transcript",
                ],
            }
        session["models"] = sorted(set(session["models"] + evidence["models"]))
        session.update(
            transcript_ids=evidence["ids"],
            notes=evidence["notes"],
            tools=evidence["tools"],
            analyzer_sha256=evidence.get("analyzer_sha256"),
            ccusage=evidence.get("ccusage", []),
        )
        source = data[row["id"]]
        events: list[dict[str, Any]] = list(
            {
                (e["usage_event_id"] or e["id"]): e
                for e in source["usage"]
                if timestamp(start) <= timestamp(e["occurred_at"]) <= timestamp(end)
            }.values()
        )
        if agent == "pi":
            events = [
                {
                    "occurred_at": e["at"],
                    "turn_id": None,
                    "tokens_in": e.get("input"),
                    "tokens_cached_read": e.get("cacheRead"),
                    "tokens_cached_write": e.get("cacheWrite"),
                    "tokens_out": e.get("output"),
                    "tokens_thought": e.get("reasoning"),
                    "estimated": 0,
                    "cost_source": "provider_reported",
                    "cost_subcents": e["cost"]["total"] * 10000
                    if isinstance(e.get("cost"), dict)
                    and e["cost"].get("total") is not None
                    else None,
                }
                for e in evidence["pi_usage"]
            ]
        session["models"] = sorted(
            set(session["models"]) | {e["model"] for e in events if e.get("model")}
        )
        totals = usage_totals(events)
        aggregate = ccusage_totals(session.pop("ccusage"), start, end)
        if not events:
            for key, field in {
                "input": "tokens_in",
                "output": "tokens_out",
                "cache_read": "tokens_cached_in",
            }.items():
                totals[key] = measure(row.get(field), "task_sessions." + field)
        totals.update(aggregate)
        session["usage_coverage"] = {
            "event_rows": len(events),
            "actual_cost_rows": sum(
                not e.get("estimated") and e.get("cost_source") == "provider_reported"
                for e in events
            ),
            "ccusage_totals": aggregate,
            "token_event_source": "pi.message.usage"
            if agent == "pi"
            else "task_usage_events",
            "cost_coverage": "recorded rows only; unrecorded turns have unknown cost",
        }
        if agent == "pi" and evidence["pi_usage"]:
            for key, field in {
                "input": "input",
                "output": "output",
                "cache_read": "cacheRead",
                "cache_write": "cacheWrite",
            }.items():
                totals[key] = measure(
                    sum(e.get(field, 0) for e in evidence["pi_usage"]),
                    "pi.message.usage." + field,
                )
            costs = [
                e["cost"]["total"]
                for e in evidence["pi_usage"]
                if isinstance(e.get("cost"), dict)
                and e["cost"].get("total") is not None
            ]
            totals["cost_usd"] = measure(
                sum(costs) if costs else None, "pi.message.usage.cost.total"
            )
        if agent == "hermes":
            totals["cost_usd"] = measure(
                evidence.get("hermes", {}).get("actual_cost_usd"),
                "hermes.sessions.actual_cost_usd",
            )
        session["totals"] = totals
        session["tool_calls"] = measure(
            evidence.get("hermes", {}).get("tool_call_count")
            if agent == "hermes"
            and evidence.get("hermes", {}).get("tool_call_count") is not None
            else sum(bool(t.get("called")) for t in evidence["tools"])
            if evidence["ids"] or evidence.get("hermes")
            else None,
            "hermes.sessions.tool_call_count"
            if agent == "hermes"
            and evidence.get("hermes", {}).get("tool_call_count") is not None
            else "transcript.call_id",
        )
        session["waits"], session["gaps"] = [], []
        turns = source["turns"]
        try:
            completed = [
                (
                    timestamp(t["started_at"]) if t["started_at"] else None,
                    timestamp(t["completed_at"]),
                )
                for t in turns
                if t["completed_at"]
            ]
            remaining = expires - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("task collection timeout")
            if not (home / ".local/state/kandev-pacer/pacer.jsonl").exists():
                session["notes"].append("no data for pacer")
            session["waits"], session["gaps"] = pacer_observations(
                home,
                session,
                completed,
                evidence["requests"] or {},
                min(timeout, remaining),
            )
        except (OSError, ValueError, TimeoutError) as exc:
            session["notes"].append(f"no data for pacer: {type(exc).__name__}")
        assigned = set()
        session["steps"] = session_steps(
            source["history"], start, end, events, turns, session["waits"], assigned
        )
        if agent == "pi":
            for step in session["steps"]:
                for key in METRICS:
                    step["totals"][key]["source"] = "pi.message.usage"
        event_totals = usage_totals(events)
        unassigned_totals = usage_totals([e for e in events if id(e) not in assigned])
        session["unattributed_usage"] = {
            key: measure(
                unassigned_totals[key]["value"] or 0
                if event_totals[key]["value"] is not None and events
                else None,
                "pi.message.usage" if agent == "pi" else event_totals[key]["source"],
            )
            for key in METRICS
        }
        session["signals"] = [
            {"at": h["created_at"], "step": h["to_step_id"], **decode(h["metadata"])}
            for h in source["history"]
            if timestamp(h["created_at"]) <= timestamp(end)
        ]
        session["blockers"] = {
            k: v for k, v in metadata.items() if "block" in k.lower()
        }
        if row.get("error_message"):
            session["blockers"]["error_message"] = row["error_message"]
        for commit in source["commits"]:
            identity = (row.get("repository_id"), commit["commit_sha"])
            if identity not in seen_commits and timestamp(
                commit["committed_at"]
            ) <= timestamp(end):
                seen_commits.add(identity)
                record["diffs"].append(
                    {
                        "repository": row.get("repository_id"),
                        "commit": commit["commit_sha"],
                        **{
                            k: measure(commit[k], "task_session_commits." + k)
                            for k in ("files_changed", "insertions", "deletions")
                        },
                    }
                )
        record["sessions"].append(session)
    commands = Counter(
        cmd
        for session in record["sessions"]
        for t in session["tools"]
        if isinstance(t.get("input"), dict) and t.get("called")
        for cmd in [t["input"].get("command", t["input"].get("cmd"))]
        if isinstance(cmd, str)
    )
    for session in record["sessions"]:
        for tool in session["tools"]:
            arguments = tool.pop("input", {})
            cmd = (
                arguments.get("command", arguments.get("cmd"))
                if isinstance(arguments, dict)
                else None
            )
            if isinstance(cmd, str) and commands[cmd] > 1:
                tool["command"] = cmd
            if not tool.get("error"):
                tool.pop("excerpt", None)
    record["totals"] = {
        key: measure(
            sum(
                s["totals"][key]["value"]
                for s in record["sessions"]
                if s["totals"][key]["value"] is not None
            )
            if any(s["totals"][key]["value"] is not None for s in record["sessions"])
            else None,
            sorted({s["totals"][key]["source"] for s in record["sessions"]}),
        )
        for key in METRICS
    }
    if not record["sessions"]:
        record["notes"].append("no transcript")
    return record


def render_record(record):
    # JSON is a YAML 1.2 document; no extra parser dependency is needed.
    sections = [
        "---\n" + json.dumps(record, ensure_ascii=False, indent=2) + "\n---\n",
        "# Session observations",
        dedent("""\
        Durations are seconds. Output sizes are UTF-8 bytes. Costs are recorded USD.
        Token categories retain source semantics; reasoning can be included in output.
        Cache gap rows show elapsed time and cache counts, not a confirmed miss cause.
        Tables derive from the source-labeled measurements in the frontmatter.
        """),
    ]
    sections.extend(record["notes"])
    sections.extend(
        [
            "## Session totals",
            table(
                ["session", *METRICS, "availability"],
                [
                    [s["id"], *[s["totals"][k] for k in METRICS], s["notes"]]
                    for s in record["sessions"]
                ],
            ),
        ]
    )
    sections.extend(
        [
            "## Steps",
            table(
                [
                    "session",
                    "step",
                    "wall seconds",
                    "preceding pacer wait",
                    "turns",
                    *METRICS,
                ],
                [
                    [
                        s["id"],
                        r["step"],
                        r["wall_seconds"],
                        r["preceding_pacer_wait"],
                        r["turns"],
                        *[r["totals"][k]["value"] for k in METRICS],
                    ]
                    for s in record["sessions"]
                    for r in s["steps"]
                ],
            ),
        ]
    )
    tools = [{**t, "session": s["id"]} for s in record["sessions"] for t in s["tools"]]
    sections.extend(
        [
            "## Tool calls",
            table(
                ["tool", "calls"],
                sorted(Counter(t["name"] for t in tools if t.get("called")).items()),
            ),
            "## Errored calls",
            table(
                ["session", "call", "tool", "first 120 characters"],
                [
                    [t["session"], t["id"], t["name"], t.get("excerpt")]
                    for t in tools
                    if t.get("error")
                ],
            ),
        ]
    )
    commands = Counter(
        t["command"] for t in tools if t.get("command") and t.get("called")
    )
    sections.extend(
        [
            "## Identical commands",
            table(["command", "calls"], [[c, n] for c, n in commands.items() if n > 1]),
            "## Ten largest tool outputs",
            table(
                ["session", "call", "tool", "bytes"],
                [
                    [t["session"], t["id"], t["name"], t["output_bytes"]]
                    for t in sorted(
                        [t for t in tools if "output_bytes" in t],
                        key=lambda t: (-t["output_bytes"], t["id"]),
                    )[:10]
                ],
            ),
        ]
    )
    sections.extend(
        [
            "## Cache gaps",
            table(
                [
                    "session",
                    "start",
                    "end",
                    "seconds",
                    "TTL seconds",
                    "pacer withheld",
                    "usage after",
                ],
                [
                    [
                        s["id"],
                        *[
                            g[k]
                            for k in (
                                "start",
                                "end",
                                "seconds",
                                "ttl",
                                "pacer_withheld",
                                "usage_after",
                            )
                        ],
                    ]
                    for s in record["sessions"]
                    for g in s["gaps"]
                ],
            ),
            "## Step signal summaries and blockers",
            table(
                ["session", "time", "step", "verbatim metadata"],
                [
                    [s["id"], r.get("at"), r.get("step"), r]
                    for s in record["sessions"]
                    for r in s["signals"]
                ]
                + [[s["id"], None, None, s["blockers"]] for s in record["sessions"]]
                + [[None, None, None, record.get("blockers", {})]],
            ),
        ]
    )
    repositories = {}
    for row in record["diffs"]:
        values = repositories.setdefault(row["repository"], [0, 0, 0])
        for i, key in enumerate(("files_changed", "insertions", "deletions")):
            values[i] += row[key]["value"] or 0
    sections.extend(
        [
            "## Diff size per repository",
            table(
                ["repository", "file changes", "insertions", "deletions"],
                [[key, *values] for key, values in repositories.items()],
            ),
        ]
    )
    return "\n\n".join(sections) + "\n"


def record_path(vault, task_id, archived_at):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", task_id):
        raise ValueError("invalid task ID")
    date = re.sub(r"[^a-zA-Z0-9.+-]", "-", archived_at)
    path = vault / OBSERVATIONS / archived_at[:7] / f"{date} {task_id}.md"
    if not path.resolve().is_relative_to(vault.resolve()):
        raise ValueError("observation path leaves vault")
    return path


def publish(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write(content.encode())
        file.flush()
        os.fsync(file.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        temporary.unlink()


def read_record(path):
    return json.loads(path.read_text().split("\n---\n", 1)[0].removeprefix("---\n"))


def write_task(args, task_id, expected=None):
    with contextlib.closing(read_db(args.db)) as db:
        row = db.execute(
            "SELECT archived_at FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    if not row or not row[0]:
        if expected:
            return None
        raise ValueError("task is unknown or not archived")
    if expected and expected != row[0]:
        return None
    path = record_path(args.vault, task_id, row[0])
    if path.exists():
        record = read_record(path)
        if (record["task"], record["archived_at"]) != (task_id, row[0]):
            raise ValueError("observation identity conflict")
        return path
    record = collect_task(args.db, task_id, args.home, args.source_timeout)
    if record["archived_at"] != row[0]:
        return None
    with contextlib.closing(read_db(args.db)) as db:
        current = db.execute(
            "SELECT archived_at FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    if not current or current[0] != record["archived_at"]:
        return None
    publish(path, render_record(record))
    return path


def save_state(path, state):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as file:
        json.dump(state, file)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield


def commit_records(vault, paths, journal=None):
    if not paths:
        return
    with (
        locked(vault / ".git/kandev-learn.lock"),
        tempfile.TemporaryDirectory() as temporary,
    ):
        relative = [str(p.relative_to(vault)) for p in paths]
        base = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(vault)]

        def git(*args, env=None):
            return command([*base, *args], env=env).strip()

        head = git("rev-parse", "HEAD")
        journal = journal or vault / ".git/kandev-learn-commit.json"
        if journal.exists():
            pending = json.loads(journal.read_text())
            if pending["paths"] != relative:
                raise RuntimeError("pending observation commit paths differ")
            published = git("merge-base", head, pending["commit"]) == pending["commit"]
            if published:
                if git("diff", "--name-only", pending["commit"], head, "--", *relative):
                    raise RuntimeError(
                        "published observations changed in a later commit"
                    )
                indexed = git("ls-files", "--stage", "--", *relative)
                if indexed != pending["index_before"]:
                    # Reset may already have completed before the process stopped.
                    if git("diff", "--cached", "--name-only", "--", *relative):
                        raise RuntimeError("pending observation index changed")
                else:
                    git("reset", "-q", head, "--", *relative)
                journal.unlink()
                return
            if git("ls-files", "--stage", "--", *relative) != pending["index_before"]:
                raise RuntimeError("unpublished observation index changed")
            # A failed compare-and-swap left this commit unreachable. Rebuild on HEAD.
            journal.unlink()
        if git("diff", "--cached", "--name-only", "--", *relative):
            raise RuntimeError("observation paths have staged changes")
        env = {
            **os.environ,
            "GIT_INDEX_FILE": str(Path(temporary) / "index"),
            "GIT_LITERAL_PATHSPECS": "1",
        }
        git("read-tree", head, env=env)
        git("add", "--", *relative, env=env)
        tree = git("write-tree", env=env)
        if tree == git("rev-parse", "HEAD^{tree}"):
            return
        commit = git(
            "commit-tree",
            tree,
            "-p",
            head,
            "-m",
            "docs: record archived Kandev sessions",
            env=env,
        )
        save_state(
            journal,
            {
                "head": head,
                "commit": commit,
                "paths": relative,
                "index_before": git("ls-files", "--stage", "--", *relative),
            },
        )
        git("update-ref", "HEAD", commit, head)
        git("reset", "-q", commit, "--", *relative)
        journal.unlink()


def sweep(args):
    state_path = args.state / "state.json"
    state: dict[str, Any] | None = (
        json.loads(state_path.read_text()) if state_path.exists() else None
    )
    with contextlib.closing(read_db(args.db)) as db:
        archived = [
            list(r)
            for r in db.execute(
                "SELECT id,archived_at FROM tasks WHERE archived_at IS NOT NULL "
                "ORDER BY archived_at,id"
            )
        ]
    if state is None:
        state = {
            "watermark": max((r[1] for r in archived), default=""),
            "boundary": [
                r[0]
                for r in archived
                if r[1] == max((a[1] for a in archived), default="")
            ],
            "pending": [],
        }
        if not args.backfill:
            print("Initial archive boundary:", state["watermark"])
            print("Tasks: []")
            if not args.dry_run:
                save_state(state_path, state)
            return
    key = "backfill_pending" if args.backfill else "pending"
    watermark = state["watermark"]
    if args.backfill:
        done = state.get("backfill_done", [])
        candidates = [r for r in archived if r[1] <= watermark and r not in done]
    else:
        candidates = [
            r
            for r in archived
            if r[1] > watermark or (r[1] == watermark and r[0] not in state["boundary"])
        ]
    selected = state.get(key) or candidates[: args.batch_size]
    print(table(["task", "archived_at"], selected))
    if args.dry_run:
        return
    state[key] = selected
    save_state(state_path, state)
    paths = []
    for task_id, at in selected:
        path = record_path(args.vault, task_id, at)
        if path.exists():
            record = read_record(path)
            if (record.get("task"), record.get("archived_at")) != (task_id, at):
                raise ValueError("observation identity conflict")
        else:
            path = write_task(args, task_id, at)
        if path:
            paths.append(path)
    commit_records(args.vault, paths, args.state / "commit.json")
    if args.backfill:
        state["backfill_done"] = [*state.get("backfill_done", []), *selected]
    elif selected:
        latest = max(r[1] for r in selected)
        state["boundary"] = list(
            set(
                (state["boundary"] if latest == watermark else [])
                + [r[0] for r in selected if r[1] == latest]
            )
        )
        state["watermark"] = latest
    state[key] = []
    save_state(state_path, state)


def digest(args):
    since = timestamp(args.since)
    fields = ("wall_seconds", "turns", "tool_calls", "errored_calls", *METRICS)
    rows, sums, known = [], dict.fromkeys(fields, 0), dict.fromkeys(fields, 0)
    for path in sorted((args.vault / OBSERVATIONS).glob("*/*.md")):
        record = read_record(path)
        if timestamp(record["archived_at"]) < since:
            continue
        for session in record["sessions"]:
            values = [
                measure(
                    timestamp(session["end"]) - timestamp(session["start"]),
                    "task_sessions",
                ),
                measure(
                    sum(s["turns"] for s in session["steps"]),
                    "task_session_turns.assigned",
                ),
                session["tool_calls"],
                measure(
                    sum(bool(t.get("error")) for t in session["tools"])
                    if session["transcript_ids"]
                    else None,
                    "transcript.explicit_error",
                ),
                *[session["totals"][k] for k in METRICS],
            ]
            rows.append(
                [
                    record["task"],
                    record["archived_at"],
                    session["id"],
                    *values,
                    session["notes"],
                ]
            )
            for key, value in zip(fields, values, strict=True):
                if value["value"] is not None:
                    sums[key] += value["value"]
                    known[key] += 1
    rows.append(
        [
            "total",
            None,
            len(rows),
            *[
                measure(sums[k] if known[k] else None, f"{known[k]} sessions with data")
                for k in fields
            ],
            None,
        ]
    )
    print(table(["task", "archived_at", "session", *fields, "availability"], rows))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", help="task ID, sweep, or digest")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--db", type=Path)
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--source-timeout", type=float, default=45)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--since")
    args = parser.parse_args(argv)
    args.db = args.db or args.home / ".kandev/data/kandev.db"
    args.vault = args.vault or args.home / "my-knowledge"
    args.state = args.state or args.home / ".local/state/kandev-learn"
    if args.batch_size < 1 or args.source_timeout <= 0:
        parser.error("batch size and source timeout must be positive")
    if args.action == "digest" and not args.since:
        parser.error("digest requires --since")
    try:
        if args.action == "digest":
            digest(args)
        elif args.action == "sweep" and args.dry_run:
            sweep(args)
        else:
            with locked(args.state / "collector.lock"):
                if args.action == "sweep":
                    sweep(args)
                else:
                    print(write_task(args, args.action))
    except (OSError, ValueError, RuntimeError, sqlite3.Error, TimeoutError) as exc:
        print(f"kandev-learn: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
