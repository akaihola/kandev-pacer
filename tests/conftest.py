"""The fake kandev the tests run against, and the payload builders they feed it.

The pacer talks to kandev two ways, and this fakes each at its own seam:

- REST reads go through `client.request(method, path)`, so `FakeBoard.request`
  replaces that one function and answers from the board held here.
- Actions go over a WebSocket, and the framing is the pacer's own code (matching a
  reply to its request id past unrelated pushes, spotting error frames). So the fake
  goes in one layer lower -- at `websocket.create_connection` -- and speaks real
  frames to a real `_ws_call`. That keeps the wire handling under test.
"""
import json
import time
from collections import deque

import pytest
import websocket

from kandev_pacer import client
from kandev_pacer.config import settings

STEPS = [
    {"id": "bk", "name": "Backlog", "is_start_step": True, "position": 0, "events": {}},
    {"id": "ip", "name": "In Progress", "position": 1, "stage_type": "work",
     "events": {"on_turn_complete": [{"type": "move_to_step"}]}},
    {"id": "rv", "name": "Review", "position": 2,
     "events": {"on_turn_start": [{"type": "move_to_step"}]}},
]
# 10% consumed against a half-elapsed window -> headroom positive, gate open.
USAGE_PCT = 10


# --- payload builders, shaped like the kandev API responses ---
# Plain functions, not fixtures: a test usually wants several with different fields,
# which reads better as a call than as a fixture per shape.

def step(id, name, otc=None, oe=None, ots=None, start=False, stage="custom",
         pos=0, profile="", prompt="", signal=False):
    events = {}
    if oe:
        events["on_enter"] = [{"type": k} for k in oe]
    if otc:
        events["on_turn_complete"] = [{"type": k} for k in otc]
    if ots:
        events["on_turn_start"] = [{"type": k} for k in ots]
    return {"id": id, "name": name, "events": events, "is_start_step": start,
            "stage_type": stage, "position": pos, "agent_profile_id": profile,
            "prompt": prompt, "auto_advance_requires_signal": signal}


def sess(id, state, started="1"):
    # `started_at` is the real field name in GET /tasks/:id/sessions -- there is no `created_at`.
    return {"id": id, "state": state, "started_at": started}


def task(id, step="ip", pos=0, prio="medium", created="2026-01-01T00:00:00Z", parent=None):
    t = {"id": id, "workflow_step_id": step, "position": pos,
         "priority": prio, "created_at": created}
    if parent:
        t["parent_id"] = parent
    return t


def one_session(sid, state):
    """The list GET /tasks/:id/sessions returns for a task with a single session."""
    return [sess(sid, state)]


def board_task(tid, step_id, title="T", created="1"):
    """A task as the fake board serves it: titled, and owned by workflow "wf"."""
    return {**task(tid, step_id, created=created), "title": title, "workflow_id": "wf"}


# --- the fake kandev ---


class _FakeConnection:
    """A websocket-client connection that answers from the board, in real frames."""

    def __init__(self, board, origin):
        self.board = board
        self.origin = origin
        self.inbox = deque()
        self.closed = False

    def send(self, raw):
        self.inbox.extend(self.board.reply(json.loads(raw)))

    def recv(self):
        return self.inbox.popleft()

    def close(self):
        self.closed = True


class FakeBoard:
    """The kandev the tests run against: tasks, sessions, message queues, actions taken."""

    def __init__(self):
        self.tasks = []
        self.sessions = {}
        self.queues = {}
        self.ws_log = []       # (action, payload) per action the pacer sent
        self.origins = []      # the Origin header each connection presented
        self.paths = []        # every REST path fetched, for counting round trips

    def load(self, tasks, sessions, queues):
        self.tasks, self.sessions, self.queues = tasks, dict(sessions), dict(queues)

    # --- REST ---

    def request(self, method, path, body=None):
        self.paths.append(path)
        if path.endswith("/agents/usage"):
            # A reset 2.5h out on a 5h window: half elapsed, 10% consumed.
            return {"agents": [{"usage": {"provider": "anthropic", "windows": [
                {"label": "5-hour", "utilization_pct": USAGE_PCT,
                 "reset_at": _iso_in(9000)}]}}]}
        if path == "/api/v1/workflows":
            return {"workflows": [{"id": "wf", "name": "Fake Paced"}]}
        if path.endswith("/workflow/steps"):
            return {"steps": STEPS}
        if path == "/api/v1/workflows/wf/tasks":
            return {"tasks": self.tasks}
        if "/sessions" in path:
            return {"sessions": self.sessions.get(path.split("/")[-2], [])}
        return {}

    # --- WebSocket ---

    def connect(self, url, timeout=None, origin=None):
        self.origins.append(origin)
        return _FakeConnection(self, origin)

    def reply(self, message):
        """The frames kandev sends back for one request frame."""
        rid, action, payload = message["id"], message["action"], message["payload"]
        self.ws_log.append((action, payload))
        # An unrelated push first: the client must skip it and keep waiting for `rid`.
        frames = [_frame("push-xyz", "event", "session.updated", {})]
        session_id = payload.get("session_id")

        def fail(reason):
            return frames + [_frame(rid, "error", action, {"message": reason})]

        if action == "message.queue.get":
            if session_id == "sBoom":
                return fail("boom")
            count = 1 if session_id == "sStale" else self.queues.get(session_id, 0)
            body = {"entries": [], "count": count, "max": 10}
        elif action == "message.queue.drain":
            if session_id == "sBusy":
                return fail("Session is busy")
            # sStale: counted as pending, gone by the time we take it -- the real race.
            took = session_id != "sStale" and self.queues.get(session_id, 0) > 0
            if took:
                self.queues[session_id] -= 1
            body = {"session_id": session_id, "drained": took}
        elif action == "task.state" and str(payload.get("id", "")).startswith("boomflag"):
            return fail("no")
        elif action == "session.launch" and str(payload.get("task_id", "")).startswith("boom"):
            # What kandev answers for a CREATED session with no executors_running row:
            # refused outright, and no retry of the same call will ever work.
            return fail("session is not resumable: no executor record")
        else:
            body = {"success": True, "state": "STARTING"}
        return frames + [_frame(rid, "response", action, body)]


def _frame(rid, kind, action, payload):
    return json.dumps({"id": rid, "type": kind, "action": action, "payload": payload})


def _iso_in(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))


@pytest.fixture
def backoff_100(monkeypatch):
    """A round PACER_FAIL_BACKOFF, so the doubling is readable in the assertions."""
    monkeypatch.setattr(settings, "fail_backoff", 100)


@pytest.fixture
def board(monkeypatch):
    """A fake kandev, with `client`'s two seams pointed at it."""
    fake = FakeBoard()
    monkeypatch.setattr(client, "request", fake.request)
    monkeypatch.setattr(websocket, "create_connection", fake.connect)
    return fake
