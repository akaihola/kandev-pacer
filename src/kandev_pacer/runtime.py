# ruff: noqa: F401  # compatibility exports for code that imported the old pacer.py
"""Stateful pacing loop and command-line interface."""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from .client import (
    paced_tasks as _paced_tasks,
    queued_counts,
    request as _req,
    sessions_of,
    tasks_of,
    user_settings as _user_settings,
    workflow_entries,
    ws_drain,
    ws_launch,
    ws_set_state,
)
from .config import (
    ADMISSION_OVERRIDE,
    ALPHA,
    BACKSTOP,
    BLOCK_AFTER,
    BLOCKED_STATE,
    BOOKING_TTL,
    CONCURRENCY,
    DRAINABLE_STATES,
    FAIL_BACKOFF,
    INTERVAL,
    LOG_PATH,
    MAX_ATTEMPTS,
    ORDER,
    PACED_WORKFLOWS,
    PROVIDER,
    RESUME_MARGIN,
    RUNNING_STATES,
    URL,
)
from .quota import (
    binding_label,
    ceiling_breached,
    clamp01,
    detect_resets,
    diff_sessions,
    elapsed_frac,
    gate_open,
    headroom,
    parse_windows,
    provider_missing,
    sessions_snapshot,
    tick_record,
    usage_providers,
    window_records,
    worst_headroom,
)
from .workflow import (
    action_key,
    admission_target,
    backed_off,
    effective_sidebar_view,
    fail_backoff,
    has_custom_sort_view,
    is_drainable,
    is_launchable,
    is_paced_step,
    is_work_step,
    launch_target,
    lint_workflow,
    named,
    newest_session,
    ordered_tasks,
    paced_step_ids,
    pick_intent,
    priority_rank,
    prune_booked,
    prune_failures,
    queue_step_id,
    resolve_scope,
    select_action,
    step_names,
    step_ranks,
    task_order_key,
    visible_task_order,
)

_log_failed = False
_BOOKED = {}
_FAILED = {}
_SCOPE = []
_WORK_STEPS = set()
_QUEUE_STEPS = set()
_ADMISSION = {}
_STEP_NAMES = {}
_WF_NAMES = {}
_STEP_RANKS = {}
_PREV_SESSIONS = None
_PREV_WINDOWS = []


def log_event(record):
    """Append a JSONL record without letting log failure stop the pacing loop."""
    global _log_failed
    if not LOG_PATH:
        return
    try:
        path = Path(LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as file:
            file.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as error:
        if not _log_failed:
            print(f"log: {error} (continuing unlogged)", file=sys.stderr, flush=True)
            _log_failed = True


def paced_tasks(scope=None):
    return _paced_tasks(_SCOPE if scope is None else scope)


def user_settings():
    return _user_settings(ORDER)


def label(task):
    return task.get("title") or task["id"]


def flag_blocked(task, reason):
    try:
        ws_set_state(task["id"], BLOCKED_STATE, "pace-block")
    except Exception as error:
        print(
            f"could not flag {label(task)!r} {BLOCKED_STATE} ({error})",
            file=sys.stderr,
            flush=True,
        )
        return False
    print(
        f"flagged {label(task)!r} {BLOCKED_STATE} on the board: {reason}",
        file=sys.stderr,
        flush=True,
    )
    return True


def note_failure(key, task, reason, now):
    previous = _FAILED.get(key) or {}
    attempts = previous.get("attempts", 0) + 1
    _FAILED[key] = {
        "at": now,
        "attempts": attempts,
        "flagged": previous.get("flagged", False),
    }
    print(
        f"kandev refused to start {label(task)!r} ({reason}) -- failure "
        f"{attempts}, holding it for {fail_backoff(attempts)}s",
        file=sys.stderr,
        flush=True,
    )
    log_event(
        {
            "t": round(now, 3),
            "kind": "failure",
            "task": task["id"],
            "reason": reason,
            "attempts": attempts,
        }
    )
    if BLOCK_AFTER and attempts >= BLOCK_AFTER and not _FAILED[key]["flagged"]:
        _FAILED[key]["flagged"] = flag_blocked(task, reason)


def attempt(kind, task, key, wh, running, now, emit):
    """Try one action; False means rejection spent no quota, so try another task."""
    fresh = next(
        (candidate for candidate in paced_tasks() if candidate["id"] == task["id"]),
        None,
    )
    if (
        fresh is None
        or fresh.get("workflow_step_id", "")
        != task.get("workflow_step_id", "")
    ):
        print(
            f"headroom={wh:+.3f} open, {label(task)!r} moved since selection -> skip",
            flush=True,
        )
        emit("open", "skipped")
        return True
    step = fresh.get("workflow_step_id", "")
    sessions = sessions_of(task["id"])

    if kind == "drain":
        newest = newest_session(sessions)
        if newest is None or newest.get("state") not in DRAINABLE_STATES:
            print(
                f"headroom={wh:+.3f} open, {label(fresh)!r} no longer idle "
                "-> skip drain",
                flush=True,
            )
            emit("open", "skipped")
            return True
        try:
            drained = bool((ws_drain(newest["id"]) or {}).get("drained"))
        except Exception as error:
            note_failure(key, fresh, f"drain rejected: {error}", now)
            return False
        if not drained:
            print(
                f"headroom={wh:+.3f} open, queue for {label(fresh)!r} already "
                "empty -> no turn spent",
                flush=True,
            )
            emit("open", "empty")
            return True
        _BOOKED[(task["id"], step, newest["id"])] = now
        _clear_failures(task["id"])
        print(
            f"headroom={wh:+.3f} open -> drained queued prompt for "
            f"{label(fresh)!r} at {named(step, _STEP_NAMES)!r} "
            f"({running + 1}/{CONCURRENCY})",
            flush=True,
        )
        emit(
            "open",
            "drained",
            {"task": task["id"], "session": newest["id"], "step": step},
        )
        return True

    target = launch_target(fresh, _WORK_STEPS, _ADMISSION)
    if target is None:
        note_failure(
            key,
            fresh,
            "no admission target in workflow "
            f"{named(fresh.get('workflow_id'), _WF_NAMES)!r}",
            now,
        )
        return False
    intent, session_id = pick_intent(sessions)
    try:
        ws_launch(task["id"], intent, session_id, target)
    except Exception as error:
        note_failure(key, fresh, str(error), now)
        return False
    _BOOKED[(task["id"], step, session_id)] = now
    _clear_failures(task["id"])
    print(
        f"headroom={wh:+.3f} open -> launched {label(fresh)!r} intent={intent} "
        f"workflow={named(fresh.get('workflow_id'), _WF_NAMES)!r} "
        f"target={named(target, _STEP_NAMES)!r} "
        f"({running + 1}/{CONCURRENCY})",
        flush=True,
    )
    emit(
        "open",
        "launched",
        {
            "task": task["id"],
            "intent": intent,
            "session": session_id,
            "target": target,
        },
    )
    return True


def _clear_failures(task_id):
    for key in [key for key in _FAILED if key[0] == task_id]:
        del _FAILED[key]


def tick(was_open):
    global _BOOKED, _FAILED, _PREV_SESSIONS, _PREV_WINDOWS
    usage = _req("GET", "/api/v1/agents/usage")
    now = time.time()
    windows = parse_windows(usage)
    tasks = paced_tasks()
    task_sessions = {task["id"]: sessions_of(task["id"]) for task in tasks}
    snapshot = sessions_snapshot(task_sessions)
    for event in diff_sessions(_PREV_SESSIONS, snapshot, now):
        log_event(event)
    for event in detect_resets(_PREV_WINDOWS, windows, now):
        log_event(event)
    _PREV_SESSIONS, _PREV_WINDOWS = snapshot, windows

    _BOOKED = prune_booked(_BOOKED, tasks, task_sessions, now, BOOKING_TTL)
    _FAILED = prune_failures(_FAILED, tasks, task_sessions)
    running = sum(
        1
        for task in tasks
        for session in task_sessions[task["id"]]
        if session.get("state") in RUNNING_STATES
    )
    skip = set(_BOOKED) | backed_off(_FAILED, now)
    launchable = sum(
        1
        for task in tasks
        if is_launchable(
            task,
            task_sessions[task["id"]],
            _WORK_STEPS,
            _QUEUE_STEPS,
            skip,
        )
    )
    records = window_records(windows, now, ALPHA)

    def emit(gate, action, launched=None):
        log_event(
            tick_record(
                now,
                gate,
                action,
                records,
                running,
                launchable,
                CONCURRENCY,
                len(_BOOKED),
                launched,
            )
        )

    if not windows and provider_missing(usage, PROVIDER):
        print(
            f"provider {PROVIDER!r} not in usage "
            f"{sorted(usage_providers(usage))} -> WITHHOLDING "
            "(check PACER_PROVIDER)",
            flush=True,
        )
        emit("blind", "withheld")
        return False
    worst = worst_headroom(windows, now, ALPHA)
    if ceiling_breached(windows, BACKSTOP):
        print(
            f"headroom={worst:+.3f} CEILING (consumed>{BACKSTOP:.2f}) "
            "-> withholding",
            flush=True,
        )
        emit("ceiling", "withheld")
        return False
    is_open = gate_open(worst, was_open, RESUME_MARGIN)
    if not is_open:
        print(f"headroom={worst:+.3f} PAUSED ({running} running)", flush=True)
        emit("paused", "withheld")
        return is_open
    if running >= CONCURRENCY:
        print(
            f"headroom={worst:+.3f} open, full ({running}/{CONCURRENCY})",
            flush=True,
        )
        emit("open", "full")
        return is_open

    ordered = ordered_tasks(tasks, user_settings(), ORDER, _STEP_RANKS)
    queued = queued_counts(task_sessions)
    for _ in range(MAX_ATTEMPTS):
        skip = set(_BOOKED) | backed_off(_FAILED, now)
        kind, task = select_action(
            ordered,
            task_sessions,
            queued,
            _WORK_STEPS,
            _QUEUE_STEPS,
            skip,
        )
        if kind is None:
            held = (
                f", {len(_FAILED)} held after a failed launch" if _FAILED else ""
            )
            print(
                f"headroom={worst:+.3f} open, nothing to launch "
                f"({running}/{CONCURRENCY} running{held})",
                flush=True,
            )
            emit("open", "nothing")
            return is_open
        if attempt(
            kind,
            task,
            action_key(task, task_sessions.get(task["id"], [])),
            worst,
            running,
            now,
            emit,
        ):
            return is_open
    print(
        f"headroom={worst:+.3f} open, {MAX_ATTEMPTS} candidates rejected "
        "this tick -> waiting for the next one",
        file=sys.stderr,
        flush=True,
    )
    emit("open", "exhausted")
    return is_open


def resolve_globals(entries):
    global _SCOPE, _WORK_STEPS, _QUEUE_STEPS, _ADMISSION
    global _STEP_NAMES, _WF_NAMES, _STEP_RANKS
    _SCOPE, _WORK_STEPS, _QUEUE_STEPS, _ADMISSION, skipped = resolve_scope(
        entries, PACED_WORKFLOWS, ADMISSION_OVERRIDE
    )
    _STEP_NAMES = {
        step_id: name
        for _, steps in entries
        for step_id, name in step_names(steps).items()
    }
    _STEP_RANKS = {
        step_id: rank
        for _, steps in entries
        for step_id, rank in step_ranks(steps).items()
    }
    _WF_NAMES = {
        workflow["id"]: workflow.get("name") or workflow["id"]
        for workflow, _ in entries
    }
    return skipped


def _dump_order(entries, skipped):
    settings = user_settings()
    manual = bool(settings) and (
        ORDER == "custom" or has_custom_sort_view(settings)
    )
    placed = (
        ((settings or {}).get("sidebar_task_prefs") or {}).get(
            "ordered_task_ids"
        )
        or []
    )
    description = (
        "manual sidebar order over board pull order "
        f"({len(placed)} task(s) placed by hand)"
        if manual
        else "board pull order (sidebar order not consulted)"
    )
    print(f"order={ORDER} -> {description}")
    print(f"scope: {[named(workflow, _WF_NAMES) for workflow in _SCOPE]}")
    tasks = paced_tasks()
    for index, task in enumerate(
        ordered_tasks(tasks, settings, ORDER, _STEP_RANKS), start=1
    ):
        print(f"{index:3}. {task.get('title') or task['id']}")
    if not skipped:
        return
    in_scope = {task["id"] for task in tasks}
    elsewhere = {
        task["id"]: (task, workflow)
        for workflow, _ in skipped
        for task in tasks_of(workflow["id"])
    }
    missing = [
        task_id
        for task_id in placed
        if task_id not in in_scope and task_id in elsewhere
    ]
    print(f"\nnot paced: {len(skipped)} workflow(s) out of scope")
    for workflow, reason in skipped:
        print(f"  - {workflow.get('name') or workflow['id']}: {reason}")
    for task_id in missing:
        task, workflow = elsewhere[task_id]
        print(
            f"  ! placed by hand but not paced: "
            f"{task.get('title') or task_id!r} "
            f"(in {workflow.get('name') or workflow['id']!r})"
        )


def watch_usage(interval):
    print(
        f"watching {URL} usage every {interval}s -> "
        f"{LOG_PATH or '(logging disabled)'}",
        flush=True,
    )
    while True:
        now = time.time()
        try:
            records = window_records(
                parse_windows(_req("GET", "/api/v1/agents/usage")), now, ALPHA
            )
            log_event(
                {
                    "t": round(now, 3),
                    "kind": "usage",
                    "windows": records,
                }
            )
            print(
                f"{datetime.fromtimestamp(now):%H:%M:%S} "
                + " ".join(
                    f"{record['label']}={record['c']:.2f}"
                    for record in records
                ),
                flush=True,
            )
        except Exception as error:
            log_event(
                {
                    "t": round(now, 3),
                    "kind": "error",
                    "where": "watch_usage",
                    "msg": str(error),
                }
            )
            print(f"watch error: {error}", file=sys.stderr, flush=True)
        time.sleep(interval)


def main():
    if "--dump-usage" in sys.argv:
        print(json.dumps(_req("GET", "/api/v1/agents/usage"), indent=2))
        return None
    if "--watch-usage" in sys.argv:
        index = sys.argv.index("--watch-usage") + 1
        watch_usage(int(sys.argv[index]) if len(sys.argv) > index else 30)
        return None
    if ORDER not in ("auto", "custom", "board"):
        print(
            f"lint: PACER_ORDER={ORDER!r} unknown -> treating it as 'auto'",
            file=sys.stderr,
            flush=True,
        )

    entries = workflow_entries()
    skipped = resolve_globals(entries)
    if "--dump-order" in sys.argv:
        _dump_order(entries, skipped)
        return None
    if not _SCOPE:
        print(
            "no workflow is enrolled for pacing (no step tagged stage_type=work "
            "that routes on turn complete). Run: "
            "./make_paced.py '<workflow>' --apply",
            file=sys.stderr,
            flush=True,
        )
        return 1
    for workflow, steps in entries:
        if workflow["id"] not in _SCOPE:
            continue
        for warning in lint_workflow(steps):
            print(
                f"lint: [{workflow.get('name') or workflow['id']}] {warning}",
                file=sys.stderr,
                flush=True,
            )
    print(
        f"pacing {URL} every {INTERVAL}s "
        f"(alpha={ALPHA}, concurrency={CONCURRENCY}, provider={PROVIDER}, "
        f"order={ORDER})\n  log={LOG_PATH or '(disabled)'}",
        flush=True,
    )
    log_event(
        {
            "t": round(time.time(), 3),
            "kind": "start",
            "url": URL,
            "scope": list(_SCOPE),
            "interval": INTERVAL,
            "alpha": ALPHA,
            "cap": CONCURRENCY,
            "order": ORDER,
            "backstop": BACKSTOP,
            "resume_margin": RESUME_MARGIN,
            "provider": PROVIDER,
        }
    )
    steps_by_workflow = {
        workflow["id"]: steps for workflow, steps in entries
    }
    for workflow_id in _SCOPE:
        steps = steps_by_workflow[workflow_id]
        works = sorted(
            named(step["id"], _STEP_NAMES)
            for step in steps
            if step["id"] in _WORK_STEPS
        )
        print(
            f"  {named(workflow_id, _WF_NAMES)!r}: work_steps={works} "
            f"queue={named(queue_step_id(steps), _STEP_NAMES)!r} "
            f"admission={named(_ADMISSION.get(workflow_id), _STEP_NAMES)!r}",
            flush=True,
        )
    for workflow, reason in skipped:
        print(
            f"  not paced: {workflow.get('name') or workflow['id']!r} "
            f"-- {reason}",
            flush=True,
        )

    was_open = True
    while True:
        try:
            was_open = tick(was_open)
        except Exception as error:
            print(
                f"tick error: {error} -> pausing this tick",
                file=sys.stderr,
                flush=True,
            )
            log_event(
                {
                    "t": round(time.time(), 3),
                    "kind": "error",
                    "where": "tick",
                    "msg": str(error),
                }
            )
            was_open = False
        time.sleep(INTERVAL)
