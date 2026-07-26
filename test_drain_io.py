#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets", "websocket-client"]
# ///
"""Queued-prompt release tests against a fake kandev (loopback HTTP + WS). Run: uv run test_drain_io.py

Covers what the pure tests in test_pace.py cannot: the WebSocket layer's wire handling, and
tick()'s drain branch end to end -- selection, the drain call itself, booking, and the
no-turn-spent paths. No real kandev, no agent turn, no quota.
"""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import websockets

HTTP_PORT, WS_PORT = 38997, 38998
# Point the pacer at the fakes before importing it -- config is read at import time.
os.environ.update(KANDEV_URL=f"http://127.0.0.1:{HTTP_PORT}",
                  KANDEV_WS_URL=f"ws://127.0.0.1:{WS_PORT}/ws",
                  PACED_WORKFLOW_ID="wf", PACER_INTERVAL="120", PACER_CONCURRENCY="1",
                  PACER_ORDER="board")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STEPS = [
    {"id": "bk", "name": "Backlog", "is_start_step": True, "position": 0, "events": {}},
    {"id": "ip", "name": "In Progress", "position": 1, "stage_type": "work",
     "events": {"on_turn_complete": [{"type": "move_to_step"}]}},
    {"id": "rv", "name": "Review", "position": 2,
     "events": {"on_turn_start": [{"type": "move_to_step"}]}},
]
# 10% consumed against a half-elapsed window -> headroom positive, gate open.
USAGE_PCT = 10
# Mutable fake-server state, rewritten per scenario. Kept as separate containers rather than
# one dict so each has a single value type.
TASKS: list = []
SESSIONS: dict = {}
QUEUES: dict = {}
WS_LOG: list = []


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass  # keep the fake server quiet

    def do_GET(self):
        if self.path.endswith("/agents/usage"):
            reset = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 9000))
            body = {"agents": [{"usage": {"provider": "anthropic", "windows": [
                {"label": "5-hour", "utilization_pct": USAGE_PCT, "reset_at": reset}]}}]}
        elif self.path == "/api/v1/workflows/wf":
            body = {"name": "Fake Paced"}
        elif self.path.endswith("/workflow/steps"):
            body = {"steps": STEPS}
        elif self.path == "/api/v1/workflows/wf/tasks":
            body = {"tasks": TASKS}
        elif "/sessions" in self.path:
            body = {"sessions": SESSIONS.get(self.path.split("/")[-2], [])}
        else:
            body = {}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


async def _ws_handler(conn):
    async for raw in conn:
        m = json.loads(raw)
        rid, action, p = m["id"], m["action"], m["payload"]
        WS_LOG.append((action, p, conn.request.headers.get("Origin")))
        # Push an unrelated event first: the client must skip it and keep waiting for `rid`.
        await conn.send(json.dumps({"id": "push-xyz", "type": "event", "action": "session.updated"}))
        sid = p.get("session_id")
        if action == "message.queue.get":
            if sid == "sBoom":
                await conn.send(json.dumps({"id": rid, "type": "error", "action": action,
                                            "payload": {"message": "boom"}}))
                continue
            count = 1 if sid == "sStale" else QUEUES.get(sid, 0)
            body = {"entries": [], "count": count, "max": 10}
        elif action == "message.queue.drain":
            if sid == "sBusy":
                await conn.send(json.dumps({"id": rid, "type": "error", "action": action,
                                            "payload": {"message": "Session is busy"}}))
                continue
            took = sid != "sStale" and QUEUES.get(sid, 0) > 0
            if took:
                QUEUES[sid] -= 1
            body = {"session_id": sid, "drained": took}
        elif action == "task.state" and str(p.get("id", "")).startswith("boomflag"):
            await conn.send(json.dumps({"id": rid, "type": "error", "action": action,
                                        "payload": {"message": "no"}}))
            continue
        elif action == "session.launch" and str(p.get("task_id", "")).startswith("boom"):
            # What kandev answers for a CREATED session with no executors_running row: the
            # launch is refused outright, and no retry of the same call will ever work.
            await conn.send(json.dumps({"id": rid, "type": "error", "action": action,
                                        "payload": {"message": "session is not resumable: "
                                                               "no executor record"}}))
            continue
        else:
            body = {"success": True, "state": "STARTING"}
        await conn.send(json.dumps({"id": rid, "type": "response", "action": action, "payload": body}))


async def _serve_ws():
    async with websockets.serve(_ws_handler, "127.0.0.1", WS_PORT):
        await asyncio.Future()


def _start_fakes():
    threading.Thread(target=lambda: HTTPServer(("127.0.0.1", HTTP_PORT), _Handler).serve_forever(),
                     daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(_serve_ws()), daemon=True).start()
    time.sleep(1.2)  # let both listeners bind before the first request


_start_fakes()
import pacer  # noqa: E402  -- must follow the env setup above

pacer._SCOPE = ["wf"]
pacer._WORK_STEPS = pacer.paced_step_ids(STEPS)
pacer._QUEUE_STEPS = {pacer.queue_step_id(STEPS)}
pacer._ADMISSION = {"wf": pacer.admission_target(STEPS)}
pacer._WF_NAMES = {"wf": "Paced"}
pacer._STEP_NAMES = pacer.step_names(STEPS)
pacer._STEP_RANKS = pacer.step_ranks(STEPS)


def _sess(sid, state):
    return [{"id": sid, "state": state, "started_at": "1"}]


def _task(tid, step, title="T", created="1"):
    return {"id": tid, "title": title, "workflow_step_id": step, "created_at": created,
            "workflow_id": "wf"}


def _load(tasks, sessions, queues):
    TASKS[:] = tasks
    SESSIONS.clear()
    SESSIONS.update(sessions)
    QUEUES.clear()
    QUEUES.update(queues)


# These tests drive pacer.tick() with fake IO, so their records are fixtures, not
# observations -- writing them to the real log would poison the very dataset the
# log exists to collect.
pacer.LOG_PATH = ""

LAST = {}  # what the last _tick() returned, for the tests that care whether the gate stayed open


def _tick(tasks, sessions, queues):
    """Run one tick against the fakes; return the actions it took (queue reads excluded)."""
    _load(tasks, sessions, queues)
    WS_LOG.clear()
    pacer._BOOKED, pacer._FAILED = {}, {}
    # Fresh snapshots per tick, or one test's fake board leaks into the next one's session
    # diff. LOG_PATH is cleared at import; this only keeps the in-memory state honest.
    pacer._PREV_SESSIONS, pacer._PREV_WINDOWS = None, []
    LAST["open"] = pacer.tick(was_open=True)
    return [a for a, _, _ in WS_LOG if a != "message.queue.get"]


# --- WS layer ---

def test_queued_counts_only_asks_about_drainable_sessions():
    _load([], {}, {"sReview": 2, "sIdle": 0})
    WS_LOG.clear()
    counts = pacer.queued_counts({
        "a": _sess("sReview", "WAITING_FOR_INPUT"),
        "b": _sess("sIdle", "WAITING_FOR_INPUT"),
        "c": _sess("sRun", "RUNNING"),      # turn in flight
        "d": _sess("sNew", "CREATED"),      # prepared but not promptable
    })
    assert counts == {"sReview": 2, "sIdle": 0}, counts
    asked = sorted(p["session_id"] for a, p, _ in WS_LOG if a == "message.queue.get")
    assert asked == ["sIdle", "sReview"], asked
    # /ws rejects a foreign Origin, so the pacer must present the server's own URL.
    assert WS_LOG[0][2] == f"http://127.0.0.1:{HTTP_PORT}"


def test_drain_reports_whether_it_took_anything():
    _load([], {}, {"sReview": 1})
    assert pacer.ws_drain("sReview")["drained"] is True
    # Queue now empty -> kandev answers drained:false, which means no turn was spent.
    assert pacer.ws_drain("sReview")["drained"] is False


def test_ws_errors_surface_with_kandev_reason():
    try:
        pacer.ws_drain("sBusy")
    except RuntimeError as e:
        assert "Session is busy" in str(e), e
    else:
        raise AssertionError("a type:'error' frame must raise")


def test_unreadable_queue_degrades_to_empty():
    """A failing queue read must not kill the tick -- it falls back to launch-only pacing."""
    assert pacer.queued_counts({"t": _sess("sBoom", "WAITING_FOR_INPUT")}) == {}


# --- tick() integration ---

def test_drains_prompt_parked_at_review():
    """The case that was impossible before: Review is never launchable, so only a drain serves it."""
    acts = _tick([_task("t1", "rv", "Feedback")], {"t1": _sess("s1", "WAITING_FOR_INPUT")}, {"s1": 1})
    assert acts == ["message.queue.drain"], acts
    assert ("t1", "rv", "s1") in pacer._BOOKED, "a spent drain must be booked against concurrency"


def test_parked_with_empty_queue_still_does_nothing():
    acts = _tick([_task("t1", "rv", "Feedback")], {"t1": _sess("s1", "WAITING_FOR_INPUT")}, {})
    assert acts == [], acts


def test_drain_outranks_launch_on_a_work_step():
    """Launching first would spend the turn on the step prompt and leak the queued entry."""
    acts = _tick([_task("t2", "ip", "Work")], {"t2": _sess("s2", "WAITING_FOR_INPUT")}, {"s2": 1})
    assert acts == ["message.queue.drain"], acts


def test_empty_queue_leaves_launch_behaviour_unchanged():
    acts = _tick([_task("t2", "ip", "Work")], {"t2": _sess("s2", "WAITING_FOR_INPUT")}, {})
    assert acts == ["session.launch"], acts


def test_stale_count_spends_no_turn():
    """Queue emptied between the count and the take (the UI's Send now) -> nothing booked.

    The fake reports one pending entry for sStale but takes nothing, which is exactly the race:
    kandev answers drained:false and no turn is spent.
    """
    acts = _tick([_task("t1", "rv", "Feedback")], {"t1": _sess("sStale", "WAITING_FOR_INPUT")}, {})
    assert acts == ["message.queue.drain"], acts
    assert pacer._BOOKED == {}, "drained:false spends no turn, so it must not book"


def test_never_run_task_is_launched_not_drained():
    """CREATED is not promptable; its queued prompt waits for the admission turn."""
    acts = _tick([_task("t3", "bk", "New")], {"t3": _sess("s3", "CREATED")}, {"s3": 1})
    assert acts == ["session.launch"], acts
    assert not any(a == "message.queue.get" and p["session_id"] == "s3" for a, p, _ in WS_LOG), \
        "a CREATED session should not even be asked about"


def test_concurrency_cap_short_circuits_before_any_queue_read():
    acts = _tick([_task("t4", "ip", "Busy")], {"t4": _sess("s4", "RUNNING")}, {"s4": 1})
    assert acts == [] and WS_LOG == [], WS_LOG


# --- a launch kandev refuses ---

def test_a_refused_launch_does_not_wedge_the_board():
    """The bug: the refused task is the oldest, so it is re-picked every tick and nothing runs.

    The refusal costs no quota, so the tick still owes the board a turn -- it must move on to
    the next candidate instead of ending, and must leave the gate open.
    """
    acts = _tick([_task("boom1", "ip", "Broken", created="1"),
                  _task("t9", "ip", "Fine", created="2")],
                 {"boom1": _sess("sa", "CREATED"), "t9": _sess("sb", "WAITING_FOR_INPUT")}, {})
    assert acts == ["session.launch", "session.launch"], acts
    assert [p["task_id"] for a, p, _ in WS_LOG if a == "session.launch"] == ["boom1", "t9"]
    assert LAST["open"] is True, "one task's problem must not pause the whole gate"
    assert ("t9", "ip", "sb") in pacer._BOOKED
    assert not any(k[0] == "boom1" for k in pacer._BOOKED), "a refused launch spends no turn"
    assert pacer._FAILED[("boom1", "ip", "sa")]["attempts"] == 1


def test_a_refused_launch_is_left_alone_next_tick():
    _load([_task("boom2", "ip", "Broken")], {"boom2": _sess("sb2", "CREATED")}, {})
    pacer._BOOKED, pacer._FAILED = {}, {}
    pacer.tick(was_open=True)
    WS_LOG.clear()
    pacer.tick(was_open=True)
    assert [a for a, _, _ in WS_LOG if a == "session.launch"] == [], WS_LOG


def test_repeated_refusals_flag_the_task_blocked_on_the_board():
    """A card the pacer has given up on has to be visible somewhere other than stderr."""
    _load([_task("boom3", "ip", "Broken")], {"boom3": _sess("sb3", "CREATED")}, {})
    pacer._BOOKED, pacer._FAILED = {}, {}
    pacer.tick(was_open=True)
    assert not any(a == "task.state" for a, _, _ in WS_LOG), "one hiccup must not flag a card"
    # Jump to the far side of the backoff instead of waiting it out.
    for f in pacer._FAILED.values():
        f["at"] -= pacer.FAIL_BACKOFF + 1
    WS_LOG.clear()
    assert pacer.tick(was_open=True) is True
    assert [p for a, p, _ in WS_LOG if a == "task.state"] == [{"id": "boom3", "state": "BLOCKED"}]
    # Flagged once, not once per retry.
    for f in pacer._FAILED.values():
        f["at"] -= pacer.fail_backoff(f["attempts"]) + 1
    WS_LOG.clear()
    pacer.tick(was_open=True)
    assert not any(a == "task.state" for a, _, _ in WS_LOG), WS_LOG


def test_a_failed_flag_does_not_stop_pacing():
    """Reporting the problem must not become a second problem -- the state write can fail too."""
    _load([_task("boomflag", "ip", "Broken")], {"boomflag": _sess("sf", "CREATED")}, {})
    pacer._BOOKED, pacer._FAILED = {}, {}
    pacer.tick(was_open=True)
    for f in pacer._FAILED.values():
        f["at"] -= pacer.FAIL_BACKOFF + 1
    assert pacer.tick(was_open=True) is True
    assert pacer._FAILED[("boomflag", "ip", "sf")]["flagged"] is False, \
        "a flag that didn't stick must be tried again, not remembered as done"


def test_a_tick_gives_up_after_max_attempts():
    """A board of broken tasks must not turn one tick into a walk of the whole backlog."""
    acts = _tick([_task(f"boom{i}", "ip", "Broken", created=str(i)) for i in range(5)],
                 {f"boom{i}": _sess(f"sm{i}", "CREATED") for i in range(5)}, {})
    assert acts.count("session.launch") == pacer.MAX_ATTEMPTS, acts
    assert LAST["open"] is True


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"ok {name}")
    print(f"\n{len(tests)} passed")
