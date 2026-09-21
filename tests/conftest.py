"""The fake kandev the tests run against, and the payload builders they feed it.

The pacer talks to kandev two ways, and this fakes each at its own seam:

- REST reads go through `client.get(path)`, so `FakeBoard.get`
  replaces that one function and answers from the board held here.
- Quota no longer comes from kandev at all (see provider.py), so it is faked at its own
  seam: `provider.usage`, which the board still owns because the tests that shut the gate
  do it by moving a number on the board.
- Actions go over a WebSocket, and the framing is the pacer's own code (matching a
  reply to its request id past unrelated pushes, spotting error frames). So the fake
  goes in one layer lower -- at `websocket.create_connection` -- and speaks real
  frames to a real `_ws_call`. That keeps the wire handling under test.
"""
import json
import os
import time
from collections import deque
from pathlib import Path

import pytest
import websocket

_operator_settings = pytest.MonkeyPatch()


def _load_test_modules():
    # Settings load during import, before fixtures can isolate the offline suite.
    if not os.environ.get("PACER_LIVE_KANDEV"):
        for key in tuple(os.environ):
            if key.startswith(("PACER_", "KANDEV_")):
                _operator_settings.delenv(key)
        default_config = Path.home() / ".config" / "kandev-pacer" / "config.toml"
        is_file = Path.is_file
        _operator_settings.setattr(
            Path, "is_file", lambda path: path != default_config and is_file(path)
        )

    from kandev_pacer import client, provider
    from kandev_pacer.config import settings

    return client, provider, settings


client, provider, settings = _load_test_modules()


def pytest_unconfigure():
    _operator_settings.undo()

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


def sess(id, state, started="1", incarnation=None):
    # `started_at` is the real field name in GET /tasks/:id/sessions -- there is no `created_at`.
    return {
        "id": id,
        "state": state,
        "started_at": started,
        "queue_incarnation_id": incarnation or f"inc-{id}",
    }


def task(id, step="ip", pos=0, prio="medium", created="2026-01-01T00:00:00Z", parent=None):
    t = {"id": id, "workflow_step_id": step, "position": pos,
         "priority": prio, "created_at": created}
    if parent:
        t["parent_id"] = parent
    return t


def one_session(sid, state):
    """The list GET /tasks/:id/sessions returns for a task with a single session."""
    return [sess(sid, state)]


def board_task(tid, step_id, title="T", created="1", profile=None):
    """A task as the fake board serves it: titled, and owned by workflow "wf".

    `profile` sets metadata.agent_profile_id, which is how a two-provider run attributes the
    task to a subscription.
    """
    t = {**task(tid, step_id, created=created), "title": title, "workflow_id": "wf"}
    if profile:
        t["metadata"] = {"agent_profile_id": profile}
    return t


# --- workflow fixtures: the boards these tests run against ---

# Enrolled (post-make_paced) Paced Kanban: In Progress routes away, tagged work, no auto-start.
CLEAN = [
    step("bk", "Backlog", start=True, pos=0),
    step("ip", "In Progress", otc=["move_to_step"], stage="work", pos=1),
    step("rv", "Review", ots=["move_to_step"], stage="review", pos=2),
    step("dn", "Done", ots=["move_to_step"], pos=3),
]


# The three live workflows on gogo, which between them cover every scope case.
# Feature Dev: enrolled, several work steps. Priority Kanban: never run through make_paced --
# its Backlog even routes on turn complete, so routing alone would wrongly pace a parked queue.
FEATURE_DEV = [
    step("fd-todo", "Todo", start=True, pos=0),
    step("fd-spec", "Spec", otc=["move_to_next"], stage="work", pos=1),
    step("fd-work", "Work", otc=["move_to_next"], stage="work", pos=2),
    step("fd-done", "Done", pos=3),
]
PRIORITY_KANBAN = [
    step("pk-bk", "Backlog", otc=["move_to_step"], ots=["move_to_next"], pos=0),
    step("pk-ip", "In Progress", oe=["auto_start_agent"], otc=["move_to_step"], start=True, pos=1),
    step("pk-dn", "Done", ots=["move_to_step"], pos=2),
]
ENTRIES = [({"id": "wf-pk", "name": "Priority Kanban"}, PRIORITY_KANBAN),
           ({"id": "wf-ck", "name": "Paced Kanban"}, CLEAN),
           ({"id": "wf-fd", "name": "Feature Dev"}, FEATURE_DEV)]


# --- the fake kandev ---


class _FakeConnection:
    """A websocket-client connection that answers from the board, in real frames."""

    def __init__(self, board, origin):
        self.board = board
        self.origin = origin
        self.inbox = deque()
        self.closed = False
        self.timeouts = []     # every deadline _ws_call set, for the timeout tests

    def settimeout(self, seconds):
        self.timeouts.append(seconds)

    def send(self, raw):
        self.inbox.extend(self.board.reply(json.loads(raw)))

    def recv(self):
        return self.inbox.popleft()

    def close(self):
        self.closed = True


class FakeBoard:
    """The kandev the tests run against: tasks, sessions, message queues, actions taken."""

    def __init__(self):
        self.steps = CLEAN
        self.signal_target = None
        self.tasks = []
        self.sessions = {}
        self.queues = {}
        self.ws_log = []       # (action, payload) per action the pacer sent
        self.origins = []      # the Origin header each connection presented
        self.connections = []  # every connection opened, for the deadline tests
        self.paths = []        # every REST path fetched, for counting round trips
        self.sidebar = {}      # what GET /user/settings answers
        # Quota the board reports. The defaults leave the gate open; a test that wants
        # it shut moves these instead of reaching into the pacer.
        self.usage_pct = USAGE_PCT
        self.provider = "anthropic"
        # When set, the exact windows `usage` answers -- for tests that need more than
        # the default single 5-hour window, e.g. a model-scoped Fable window.
        self.usage_windows = None
        self.usage_reads = []  # the `fresh` flag of every quota read, for counting them
        self.tasks_elsewhere = []   # tasks belonging to workflows outside the scope
        # The Codex (openai) source, for two-subscription tests. Default: 5% against a
        # half-elapsed 7-day window -> headroom positive, its gate open. Only read when a
        # provider list includes "openai". `codex_error`, when set, makes its read fail.
        self.codex_pct = 5
        self.codex_reset_in = 302400  # half of 604800: e = 0.5
        self.codex_error = None
        self.codex_reads = []
        # profile id -> {"agent", "billing_type"[, "model"]}, what GET /api/v1/agents answers.
        self.agents_map = {}

    def load(self, tasks, sessions, queues):
        self.tasks, self.sessions, self.queues = tasks, dict(sessions), dict(queues)

    # --- quota ---

    def usage(self, fresh=False, now=None):
        # A reset 2.5h out on a 5h window: half elapsed, 10% consumed.
        self.usage_reads.append(fresh)
        windows = self.usage_windows or [
            {"label": "5-hour", "utilization_pct": self.usage_pct,
             "reset_at": _iso_in(9000)}]
        return {"agents": [{"usage": {"provider": self.provider, "windows": windows}}]}

    def codex_usage(self, fresh=False, now=None):
        """The second source, faked at provider.codex_usage the way `usage` fakes the first."""
        self.codex_reads.append(fresh)
        if self.codex_error is not None:
            raise provider.QuotaUnavailable(self.codex_error)
        return {
            "agents": [
                {
                    "agent_id": "codex-acp",
                    "usage": {
                        "provider": "openai",
                        "windows": [
                            {
                                "label": "7-day",
                                "utilization_pct": self.codex_pct,
                                "reset_at": _iso_in(self.codex_reset_in),
                                "duration_s": 604800,
                            }
                        ],
                    },
                }
            ]
        }

    # --- REST ---

    def get(self, path):
        self.paths.append(path)
        if path == "/api/v1/workflows":
            return {"workflows": [{"id": "wf", "name": "Fake Paced"}]}
        if path.endswith("/workflow/steps"):
            return {"steps": self.steps}
        if path == "/api/v1/workflows/wf/tasks":
            return {"tasks": self.tasks}
        if path.endswith("/tasks"):        # any other workflow: the out-of-scope ones
            return {"tasks": self.tasks_elsewhere}
        if path == "/api/v1/agents":
            return {
                "agents": [
                    {
                        "name": info["agent"],
                        "profiles": [
                            {
                                "id": pid,
                                "name": info.get("name"),
                                "billing_type": info.get("billing_type"),
                                "model": info.get("model"),
                                "fallback_model": info.get("fallback_model"),
                                "auto_fallback": info.get("auto_fallback"),
                            }
                        ],
                    }
                    for pid, info in self.agents_map.items()
                ]
            }
        if path == "/api/v1/user/settings":
            return {"settings": self.sidebar}
        if "/sessions" in path:
            return {"sessions": self.sessions.get(path.split("/")[-2], [])}
        if path.startswith("/api/v1/tasks/"):
            # One task by id, in the envelope kandev answers with. Unknown ids answer
            # `{}`, which the client reads as "gone" -- the moved-since-selection path.
            task_id = path.rsplit("/", 1)[-1]
            return {"task": next((t for t in self.tasks if t["id"] == task_id), None)}
        return {}

    # --- WebSocket ---

    def connect(self, url, timeout=None, origin=None):
        self.origins.append(origin)
        connection = _FakeConnection(self, origin)
        self.connections.append(connection)
        return connection

    def reply(self, message):
        """The frames kandev sends back for one request frame."""
        rid, action, payload = message["id"], message["action"], message["payload"]
        self.ws_log.append((action, payload))
        # An unrelated push first: the client must skip it and keep waiting for `rid`.
        frames = [_frame("push-xyz", "event", "session.updated", {})]
        session_id = payload.get("session_id")

        def fail(reason):
            return frames + [_frame(rid, "error", action, {"message": reason})]

        if action in {"message.queue.get", "message.queue.drain"}:
            if not all(
                payload.get(key)
                for key in ("task_id", "session_id", "session_incarnation_id")
            ):
                return fail(
                    "task_id, session_id, and session_incarnation_id are required"
                )
            holder = next(
                (
                    s
                    for s in self.sessions.get(payload["task_id"], [])
                    if s["id"] == session_id
                ),
                None,
            )
            if (
                holder is None
                or holder.get("queue_incarnation_id")
                != payload["session_incarnation_id"]
            ):
                return fail("Session not found")
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
        elif action == "mcp.step_complete":
            if not all(
                payload.get(key) for key in ("task_id", "session_id", "summary")
            ):
                return fail("task_id, session_id, and summary are required")
            if self.signal_target:
                for task in self.tasks:
                    if task["id"] == payload["task_id"]:
                        task["workflow_step_id"] = self.signal_target
            body = {"success": True}
        elif action == "task.state" and str(payload.get("id", "")).startswith("boomflag"):
            return fail("no")
        elif action == "session.launch" and str(payload.get("task_id", "")).startswith("silent"):
            # Sent, never answered: kandev may have acted and only the reply was lost.
            return []
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
    monkeypatch.setattr(settings, "fail_backoff_override", 100)


@pytest.fixture
def board(monkeypatch):
    """A fake kandev, with `client`'s two seams pointed at it."""
    fake = FakeBoard()
    monkeypatch.setattr(client, "get", fake.get)
    monkeypatch.setattr(provider, "usage", fake.usage)
    monkeypatch.setattr(provider, "codex_usage", fake.codex_usage)
    monkeypatch.setattr(websocket, "create_connection", fake.connect)
    return fake
