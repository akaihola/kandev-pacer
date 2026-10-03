"""Read archived session evidence without invoking a model."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import cache_waste
from .vendor import claude_session, pi_session

CCUSAGE_VERSION = "20.0.19"
METRICS = ("input", "cache_read", "cache_write", "output", "reasoning", "cost_usd")


def timestamp(value):
    if isinstance(value, (float, int)):
        return value / 1000 if value > 1e11 else value
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()


def claude_slug(path):
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def pi_slug(path):
    return "--" + re.sub(r"[/\\:]", "-", re.sub(r"^[/\\]", "", path)) + "--"


def read_db(path):
    db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    expires = time.monotonic() + 45
    # Python callbacks also deliver an enclosing source deadline during SQLite work.
    db.set_progress_handler(lambda: time.monotonic() >= expires, 1000)
    return db


@contextlib.contextmanager
def deadline(seconds):
    def expired(*_):
        raise TimeoutError("source timeout")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def command(args, timeout=45, env=None, cwd=None):
    # A regular file also avoids exporters dropping buffered output on pipe exit.
    with (
        tempfile.TemporaryFile(mode="w+") as output,
        subprocess.Popen(
            args,
            stdout=output,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=cwd,
            start_new_session=True,
        ) as process,
    ):
        try:
            process.communicate(timeout=timeout)
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        if process.returncode:
            raise RuntimeError(f"{Path(args[0]).name} exit {process.returncode}")
        output.seek(0)
        return output.read()


def measure(value, source):
    return {"value": value, "source": source}


def content_text(content):
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def message_text(agent, entry):
    message = entry.get("payload", {}) if agent == "codex" else entry.get("message", {})
    if not isinstance(message, dict):
        return None, ""
    role = message.get("role") or entry.get("type")
    if agent == "codex":
        if message.get("type") == "output_text":
            role = "assistant"
        elif message.get("type") != "message":
            return None, ""
    if role not in {"user", "assistant"} or message.get("channel") not in {
        None,
        "final",
    }:
        return None, ""
    content = message.get("content", message.get("text", ""))
    if isinstance(content, str):
        return role, content
    if not isinstance(content, list):
        return role, ""
    return role, "\n".join(
        block if isinstance(block, str) else block.get("text", "")
        for block in content
        if isinstance(block, str)
        or (
            isinstance(block, dict)
            and block.get("type") in {"text", "input_text", "output_text"}
            and isinstance(block.get("text"), str)
        )
    )


def tool_events(agent, entries):
    """Normalize call/result records; never infer an error from prose."""
    calls = {}
    for entry in entries:
        at = entry.get("timestamp")
        if agent == "codex":
            payload = entry.get("payload", {})
            kind = payload.get("type")
            if entry.get("type") != "response_item":
                continue
            if kind in ("function_call", "custom_tool_call"):
                value = payload.get("arguments", payload.get("input", {}))
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        value = {"input": value}
                blocks = [
                    {
                        "type": "tool_use",
                        "id": payload.get("call_id"),
                        "name": payload.get("name"),
                        "input": value,
                    }
                ]
            elif kind in ("function_call_output", "custom_tool_call_output"):
                output = payload.get("output", "")
                # Codex records command exit status in a structured output envelope.
                envelope = output
                if isinstance(envelope, str):
                    with contextlib.suppress(ValueError):
                        envelope = json.loads(envelope)
                status = (
                    envelope.get("exit_code") if isinstance(envelope, dict) else None
                )
                if isinstance(envelope, str):
                    header = envelope.split("\nOutput:", 1)[0]
                    match = re.search(
                        r"(?m)^Process (?:exited with|exit) code:? (\d+)$", header
                    )
                    status = int(match[1]) if match else None
                blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": payload.get("call_id"),
                        "content": output,
                        "is_error": bool(status),
                    }
                ]
            else:
                continue
        elif agent == "pi":
            msg = entry.get("message", {})
            if msg.get("role") == "toolResult":
                blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.get("toolCallId"),
                        "content": msg.get("content", ""),
                        "is_error": msg.get("isError", False),
                    }
                ]
            else:
                blocks = [
                    {**b, "type": "tool_use", "input": b.get("arguments", {})}
                    for b in pi_session._content_blocks(entry)
                    if b.get("type") == "toolCall"
                ]
        else:
            blocks = claude_session._content_blocks(entry)
        for block in blocks:
            kind = block.get("type")
            call_id = (
                block.get("id") if kind == "tool_use" else block.get("tool_use_id")
            )
            if not call_id:
                continue
            item = calls.setdefault(
                call_id, {"id": call_id, "name": "unknown", "at": at}
            )
            if kind == "tool_use":
                item.update(
                    name=block.get("name", "unknown"),
                    at=at,
                    input=block.get("input", {}),
                    called=True,
                )
            elif kind == "tool_result":
                output = content_text(block.get("content", ""))
                item.update(
                    output_bytes=len(output.encode("utf-8")),
                    excerpt=output[:120],
                    error=bool(block.get("is_error")),
                    result_at=at,
                )
    return list(calls.values())


def opencode_entries(export):
    entries = []
    models = set()
    for message in export.get("messages", []):
        info = message.get("info", {})
        if info.get("modelID"):
            models.add(info["modelID"])
        entries.append(
            {
                "timestamp": info.get("time", {}).get("created"),
                "message": {
                    "role": info.get("role"),
                    "content": [
                        part
                        for part in message.get("parts", [])
                        if part.get("type") == "text"
                    ],
                },
            }
        )
        for part in message.get("parts", []):
            if part.get("type") != "tool":
                continue
            state = part.get("state", {})
            timing = state.get("time", {})
            at = timing.get("start", info.get("time", {}).get("created"))
            entries.append(
                {
                    "timestamp": at,
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": part.get("callID"),
                                "name": part.get("tool"),
                                "input": state.get("input", {}),
                            }
                        ]
                    },
                }
            )
            if state.get("status") in ("completed", "error"):
                entries.append(
                    {
                        "timestamp": timing.get("end", at),
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": part.get("callID"),
                                    "content": state.get(
                                        "output", state.get("error", "")
                                    ),
                                    "is_error": state.get("status") == "error",
                                }
                            ]
                        },
                    }
                )
    return entries, sorted(models)


def ccusage(home, ids, timeout, agent="claude", since=None, files=()):
    if not ids:
        return []
    # Execute only a prepared, exact package. Never let npx install during a sweep.
    for package in sorted(
        (home / ".npm/_npx").glob("*/node_modules/ccusage/package.json")
    ):
        if json.loads(package.read_text()).get("version") != CCUSAGE_VERSION:
            continue
        base = ["node", str(package.parent / "src/cli.js")]
        options = ["--json", "--offline", "--no-cost"]
        if agent == "claude":
            return [
                report
                for identity in ids
                if (
                    report := json.loads(
                        command([*base, "session", *options, "--id", identity], timeout)
                    )
                )
                is not None
            ]
        with tempfile.TemporaryDirectory(prefix="kandev-learn-usage-") as temporary:
            env = None
            if agent == "codex":
                # ccusage otherwise rereads unrelated rollouts before filtering dates.
                for file in files:
                    target = Path(temporary) / file.relative_to(home / ".codex")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(file, target)
                env = {**os.environ, "CODEX_HOME": temporary}
            report = json.loads(
                command(
                    [
                        *base,
                        agent,
                        "session",
                        *options,
                        *(["--since", since[:10]] if since else []),
                    ],
                    timeout,
                    env,
                )
            )
        selected = [
            r
            for r in report.get("sessions", [])
            if any(
                r.get("sessionId", "") == identity
                or (
                    agent == "codex" and r.get("sessionId", "").endswith("-" + identity)
                )
                for identity in ids
            )
        ]
        return [{"sessions": selected}] if selected else []
    raise FileNotFoundError("ccusage 20.0.19 is not prepared")


def export_session(home, agent, session_id, path, timeout):
    """Export a snapshot so CLI migrations cannot write to an original store."""
    relative = (
        ".local/share/opencode/opencode.db"
        if agent == "opencode"
        else ".hermes/profiles/tyko/state.db"
    )
    with tempfile.TemporaryDirectory(prefix="kandev-learn-export-") as temporary:
        root = Path(temporary)
        destination = root / (
            "data/opencode/opencode.db" if agent == "opencode" else "hermes/state.db"
        )
        destination.parent.mkdir(parents=True)
        with (
            contextlib.closing(read_db(home / relative)) as source,
            sqlite3.connect(destination) as target,
        ):
            source.backup(target)
        env = {
            **os.environ,
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_STATE_HOME": str(root / "state"),
            "HERMES_HOME": str(root / "hermes"),
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
        }
        if agent == "opencode":
            binary = next(
                (
                    p
                    for p in (
                        home / ".bun/bin/opencode",
                        home / ".npm-global/bin/opencode",
                    )
                    if p.exists()
                ),
                home / ".bun/bin/opencode",
            )
            return command(
                [str(binary) if binary.exists() else "opencode", "export", session_id],
                timeout,
                env,
                root,
            )
        return command(
            [
                "hermes",
                "sessions",
                "export",
                "--cwd",
                path,
                "--session-id",
                session_id,
                "--format",
                "trace",
                "--no-redact",
            ],
            timeout,
            env,
            root,
        )


def transcript_window(file, agent, path, start, end):
    if file.stat().st_mtime < start:
        return None
    identity, entries, full_window = file.stem, [], True
    owned = agent != "codex"
    with file.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if not owned and entry.get("type") == "session_meta":
                metadata = entry.get("payload", {})
                if metadata.get("cwd") != path:
                    return None
                identity = metadata.get("id") or identity
                owned = True
            at = entry.get("timestamp")
            if at is None:
                continue
            inside = start <= timestamp(at) <= end
            if at and not inside:
                full_window = False
            if inside:
                entries.append(entry)
    return (identity, entries, full_window) if owned else None


def collect_transcripts(home, session, timeout=45):
    agent = session["agent"]
    path = session["workspace_path"]
    start, end = timestamp(session["start"]), timestamp(session["end"])
    expected = session.get("transcript_id") if session.get("ambiguous_window") else None
    result: dict[str, Any] = {
        "tools": [],
        "ids": [],
        "models": [],
        "notes": [],
        "requests": {},
        "pi_usage": [],
        "aggregate_ids": [],
    }
    if session.get("ambiguous_window") and not expected:
        result["notes"].append(f"no data for {agent}: ambiguous session window")
        result["notes"].append("no transcript")
        return result
    if not path:
        result["notes"].append(f"no data for {agent}")
        return result
    messages = []
    try:
        with deadline(timeout):
            candidates = []
            if agent in ("claude", "pi", "codex"):
                if agent == "claude":
                    files = (home / ".claude/projects" / claude_slug(path)).glob(
                        "*.jsonl"
                    )
                elif agent == "pi":
                    files = (home / ".pi/agent/sessions" / pi_slug(path)).glob(
                        "*.jsonl"
                    )
                else:
                    files = (home / ".codex/sessions").rglob("*.jsonl")
                for file in files:
                    if expected and expected not in file.stem:
                        continue
                    window = transcript_window(file, agent, path, start, end)
                    if window is None:
                        continue
                    identity, entries, full_window = window
                    if entries:
                        candidates.append((identity, entries, file))
                        if full_window or agent == "claude":
                            result["aggregate_ids"].append(identity)
            elif agent in ("opencode", "hermes"):
                relative = (
                    ".local/share/opencode/opencode.db"
                    if agent == "opencode"
                    else ".hermes/profiles/tyko/state.db"
                )
                with contextlib.closing(read_db(home / relative)) as db:
                    table, column = (
                        ("session", "directory")
                        if agent == "opencode"
                        else ("sessions", "cwd")
                    )
                    rows = [
                        dict(r)
                        for r in db.execute(
                            f"SELECT * FROM {table} WHERE {column}=?", (path,)
                        )
                    ]
                rows.sort(
                    key=lambda row: (
                        timestamp(
                            row["time_created"]
                            if agent == "opencode"
                            else row["started_at"]
                        ),
                        row["id"],
                    )
                )
                for row in rows:
                    begin = timestamp(
                        row["time_created"]
                        if agent == "opencode"
                        else row["started_at"]
                    )
                    finish = timestamp(
                        row.get("time_updated")
                        or row.get("ended_at")
                        or row.get("last_activity_at")
                        or begin
                    )
                    if (
                        (expected and row["id"] != expected)
                        or finish < start
                        or begin > end
                    ):
                        continue
                    full_window = begin >= start and finish <= end
                    if agent == "hermes":
                        full_window = full_window and row.get("ended_at") is not None
                        if not full_window:
                            result["notes"].append(
                                "no transcript: session is not bounded by time window"
                            )
                            continue
                        metadata = result.setdefault("hermes", {})
                        for key in ("actual_cost_usd", "tool_call_count"):
                            value = row.get(key)
                            if value is not None:
                                metadata[key] = metadata.get(key, 0) + value
                        if row.get("model"):
                            result["models"].append(row["model"])
                    try:
                        exported = export_session(home, agent, row["id"], path, timeout)
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        result["notes"].append(
                            f"no data for {agent}: {type(exc).__name__}"
                        )
                        continue
                    if agent == "opencode":
                        exported_data = json.loads(exported)
                        message_times = [
                            m.get("info", {}).get("time", {}).get("created")
                            for m in exported_data.get("messages", [])
                            if m.get("info", {}).get("role") == "assistant"
                        ]
                        full_window = bool(message_times) and all(
                            at is not None and start <= timestamp(at) <= end
                            for at in message_times
                        )
                        entries, models = opencode_entries(exported_data)
                        result["models"].extend(models)
                    else:
                        entries = [
                            json.loads(line)
                            for line in exported.splitlines()
                            if line.strip()
                        ]
                    if agent == "hermes":
                        if begin < start or finish > end:
                            result["notes"].append(
                                "no transcript: exported session extends outside time window"
                            )
                            continue
                        for entry in entries:
                            entry["timestamp"] = None
                        result["notes"].append(
                            "no data for Hermes message timing: exporter uses export time"
                        )
                    else:
                        entries = [
                            e
                            for e in entries
                            if e.get("timestamp") is not None
                            and start <= timestamp(e["timestamp"]) <= end
                        ]
                    if entries:
                        candidates.append((row["id"], entries, None))
                        if full_window:
                            result["aggregate_ids"].append(row["id"])
            for identity, entries, file in (
                sorted(candidates, key=lambda candidate: candidate[0])
                if agent != "hermes"
                else candidates
            ):
                result["ids"].append(identity)
                for tool in tool_events(agent, entries):
                    tool["id"] = identity + ":" + tool["id"]
                    result["tools"].append(tool)
                for e in entries:
                    msg = e.get("message")
                    if not isinstance(msg, dict):
                        msg = {}
                    if msg.get("model"):
                        result["models"].append(msg["model"])
                    if (
                        agent == "pi"
                        and msg.get("role") == "assistant"
                        and msg.get("usage")
                    ):
                        result["pi_usage"].append(
                            {"at": e["timestamp"], **msg["usage"]}
                        )
                    role, text = message_text(agent, e)
                    if text.strip():
                        messages.append(
                            (
                                timestamp(e["timestamp"]) if e.get("timestamp") else 0,
                                role,
                                text,
                            )
                        )
                if file and agent in ("claude", "codex"):
                    requests = (
                        cache_waste.claude_requests(file)
                        if agent == "claude"
                        else cache_waste.codex_session(file)[1]
                    )
                    result["requests"][identity] = sorted(
                        (r for r in requests if start <= r[0] <= end),
                        key=lambda r: r[0],
                    )
    except (
        OSError,
        ValueError,
        RuntimeError,
        sqlite3.Error,
        TimeoutError,
        subprocess.TimeoutExpired,
    ) as exc:
        result["notes"].append(f"no data for {agent}: {type(exc).__name__}")
    result["messages"] = [
        (role, text) for _, role, text in sorted(messages, key=lambda item: item[0])
    ]
    if not result["ids"]:
        result["notes"].append("no transcript")
    else:
        try:
            with deadline(timeout):
                result["ccusage"] = ccusage(
                    home,
                    result["aggregate_ids"],
                    timeout,
                    agent,
                    session["start"],
                    [
                        file
                        for identity, _, file in candidates
                        if file and identity in result["aggregate_ids"]
                    ],
                )
        except (
            OSError,
            ValueError,
            RuntimeError,
            TimeoutError,
            subprocess.TimeoutExpired,
        ) as exc:
            result["notes"].append(f"no data for ccusage: {type(exc).__name__}")
    if result.get("ids") and not result.get("ccusage"):
        result["notes"].append("no data for ccusage")
    for report in result.get("ccusage", []):
        for row in report.get("sessions", []):
            result["models"].extend(row.get("modelsUsed", []))
            models = row.get("models", {})
            if isinstance(models, dict):
                result["models"].extend(models.keys())
            elif isinstance(models, list):
                result["models"].extend(m for m in models if isinstance(m, str))
    result["models"] = sorted(set(result["models"]))
    result["analyzer_sha256"] = hashlib.sha256(
        Path((pi_session if agent == "pi" else claude_session).__file__).read_bytes()
    ).hexdigest()
    return result


def pacer_observations(home, session, turns, requests, timeout):
    with deadline(timeout):
        cache_waste.PACER_LOG = home / ".local/state/kandev-pacer/pacer.jsonl"
        log_available = True
        try:
            launches, withheld, ticks = cache_waste.pacer_launches()
        except FileNotFoundError:
            launches, withheld, ticks = [], {}, {}
            log_available = False
        sid, path = session["id"], session["workspace_path"]
        launches = [
            r
            for r in launches
            if r[3] == sid
            and timestamp(session["start"]) <= r[0] <= timestamp(session["end"])
        ]
        claude = (
            {cache_waste.slug(path): list(requests.values())}
            if session["agent"] == "claude"
            else {}
        )
        codex = {path: list(requests.values())} if session["agent"] == "codex" else {}
        rows = cache_waste.attribute(
            launches,
            withheld,
            ticks,
            {sid: turns},
            {sid: path},
            {sid: next(iter(session["models"]), "")},
            claude,
            codex,
            measurements_only=True,
        )
        waits = [{k: r[k] for k in ("t", "wait", "held", "matched")} for r in rows]
        gaps = []
        for identity, group in requests.items():
            for previous, current in zip(group, group[1:], strict=False):
                ttl = (
                    cache_waste.claude_ttl(current[1])
                    if session["agent"] == "claude"
                    else cache_waste.TTL_CODEX
                )
                if current[0] - previous[0] > ttl:
                    gaps.append(
                        {
                            "transcript_id": identity,
                            "start": previous[0],
                            "end": current[0],
                            "seconds": current[0] - previous[0],
                            "ttl": ttl,
                            "ttl_source": "cache_waste",
                            "usage_after": current[1],
                            "pacer_withheld": None
                            if not log_available
                            else any(
                                r["held"] and previous[0] < r["t"] <= current[0]
                                for r in waits
                            ),
                        }
                    )
        return waits, gaps
