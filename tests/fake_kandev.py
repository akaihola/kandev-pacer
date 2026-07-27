"""A fake kandev on loopback: HTTP for the REST reads, WebSocket for the actions.

Importing this module starts both servers on ephemeral ports and points the pacer's
configuration at them. That has to happen *before* `kandev_pacer.config` is imported,
since the config is read from the environment once at import time -- so every test
module that talks to the fakes imports this one first.

Used by test_client.py (the wire handling) and test_runtime.py (tick() end to end).
"""
import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import websockets  # ty: ignore[unresolved-import]  -- a test-only dep, see the script header

import support  # noqa: F401  -- sets up sys.path for the importing test module

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
HTTP_PORT = 0   # filled in by _start_fakes()
WS_PORT = 0


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


def _serve_ws(bound):
    async def run():
        global WS_PORT
        async with websockets.serve(_ws_handler, "127.0.0.1", 0) as server:
            WS_PORT = server.sockets[0].getsockname()[1]
            bound.set()
            await asyncio.Future()

    asyncio.run(run())


def _start_fakes():
    """Bind both fakes on free ports and point the pacer's environment at them."""
    global HTTP_PORT
    http = HTTPServer(("127.0.0.1", 0), _Handler)   # port 0 -> the OS picks a free one, so
    HTTP_PORT = http.server_address[1]              # two test modules never collide
    threading.Thread(target=http.serve_forever, daemon=True).start()
    bound = threading.Event()
    threading.Thread(target=_serve_ws, args=(bound,), daemon=True).start()
    bound.wait(10)
    os.environ.update(KANDEV_URL=f"http://127.0.0.1:{HTTP_PORT}",
                      KANDEV_WS_URL=f"ws://127.0.0.1:{WS_PORT}/ws",
                      PACED_WORKFLOW_ID="wf", PACER_INTERVAL="120", PACER_CONCURRENCY="1",
                      PACER_ORDER="board")


_start_fakes()


def one_session(sid, state):
    """The sessions list GET /tasks/:id/sessions returns for a task with a single session."""
    return [{"id": sid, "state": state, "started_at": "1"}]


def board_task(tid, step, title="T", created="1"):
    return {"id": tid, "title": title, "workflow_step_id": step, "created_at": created,
            "workflow_id": "wf"}


def load(tasks, sessions, queues):
    """Replace the fake board: what the HTTP reads and the queue actions will see."""
    TASKS[:] = tasks
    SESSIONS.clear()
    SESSIONS.update(sessions)
    QUEUES.clear()
    QUEUES.update(queues)
