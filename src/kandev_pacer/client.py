"""Kandev HTTP and WebSocket access."""

import json
import logging
import urllib.request

import websocket

from .config import DRAINABLE_STATES, settings
from .selection import newest_session

log = logging.getLogger("kandev-pacer")


def request(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        settings.url + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def workflow_entries():
    """Every workflow kandev knows, each paired with its steps. The startup scan's input."""
    workflows = (request("GET", "/api/v1/workflows") or {}).get("workflows") or []
    return [
        (
            workflow,
            (request("GET", f"/api/v1/workflows/{workflow['id']}/workflow/steps")
             or {}).get("steps", []),
        )
        for workflow in workflows
    ]


def tasks_of(workflow_id):
    response = request("GET", f"/api/v1/workflows/{workflow_id}/tasks") or {}
    return response.get("tasks", [])


def paced_tasks(scope):
    return [task for workflow_id in scope for task in tasks_of(workflow_id)]


def user_settings(order):
    if order == "board":
        return None
    try:
        return (request("GET", "/api/v1/user/settings") or {}).get("settings") or None
    except Exception as error:
        log.warning(f"user settings unavailable ({error}) -> FIFO order this tick")
        return None


def sessions_of(task_id):
    response = request("GET", f"/api/v1/tasks/{task_id}/sessions") or {}
    return response.get("sessions", [])


def _ws_connect():
    # /ws rejects a foreign Origin, so present the server's own URL.
    return websocket.create_connection(settings.ws_url, timeout=30, origin=settings.url)


def _ws_call(connection, action, payload, request_id):
    connection.send(
        json.dumps(
            {
                "id": request_id,
                "type": "request",
                "action": action,
                "payload": payload,
            }
        )
    )
    while True:
        raw = connection.recv()
        if not raw:
            continue
        message = json.loads(raw)
        if message.get("id") != request_id:
            continue
        response = message.get("payload") or {}
        if message.get("type") == "error" or response.get("success") is False:
            raise RuntimeError(
                f"{action} failed: {response.get('message') or message}"
            )
        return response


def ws_request(action, payload, request_id="pace-1"):
    """One action over its own connection.

    The request id only has to be unique within a connection, and this opens a fresh
    one per call with a single request in flight -- so one constant is enough.
    """
    connection = _ws_connect()
    try:
        return _ws_call(connection, action, payload, request_id)
    finally:
        connection.close()


def ws_launch(task_id, intent, session_id, workflow_step_id):
    payload = {
        "task_id": task_id,
        "intent": intent,
        "workflow_step_id": workflow_step_id,
    }
    if session_id:
        payload["session_id"] = session_id
    return ws_request("session.launch", payload)


def ws_set_state(task_id, state):
    return ws_request("task.state", {"id": task_id, "state": state})


def ws_drain(session_id):
    return ws_request("message.queue.drain", {"session_id": session_id})


def ws_queue_count(session_id):
    """How many prompts are parked on this session's queue."""
    return int((ws_request("message.queue.get", {"session_id": session_id})
                or {}).get("count") or 0)


def queued_counts(task_sessions):
    """Pending prompt counts, for the idle sessions that could actually take one.

    A session we cannot ask about is left out, which reads downstream as "empty" --
    a queue-status hiccup must degrade to launch-only pacing, never stop the tick.
    """
    counts = {}
    for sessions in task_sessions.values():
        newest = newest_session(sessions)
        if newest is None or newest.get("state") not in DRAINABLE_STATES:
            continue
        try:
            counts[newest["id"]] = ws_queue_count(newest["id"])
        except Exception as error:
            log.warning(f"queue status failed for session {newest['id']} ({error}) "
                        "-> treating as empty")
    return counts
