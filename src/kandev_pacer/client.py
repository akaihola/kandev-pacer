"""Kandev HTTP and WebSocket access."""

import json
import logging
import time
import urllib.request

import websocket

from .config import DRAINABLE_STATES, settings

log = logging.getLogger("kandev-pacer")


def get(path):
    """Read one `/api/v1` endpoint. GET is the only HTTP verb the pacer uses.

    Everything that changes kandev -- launching, draining, flagging -- goes over the
    WebSocket instead, so there is no body to send and no other method to pick.
    """
    with urllib.request.urlopen(settings.url + path, timeout=15) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def workflow_entries():
    """Every workflow kandev knows, each paired with its steps. The startup scan's input."""
    workflows = (get("/api/v1/workflows") or {}).get("workflows") or []
    return [
        (
            workflow,
            (get(f"/api/v1/workflows/{workflow['id']}/workflow/steps")
             or {}).get("steps", []),
        )
        for workflow in workflows
    ]


def tasks_of(workflow_id):
    response = get(f"/api/v1/workflows/{workflow_id}/tasks") or {}
    return response.get("tasks", [])


def paced_tasks(scope):
    return [task for workflow_id in scope for task in tasks_of(workflow_id)]


def user_settings():
    """The sidebar preferences, or None if they cannot be read.

    Unreadable settings must not stop a tick -- the pacer falls back to board pull
    order, which is the base ordering anyway.
    """
    try:
        return (get("/api/v1/user/settings") or {}).get("settings") or None
    except Exception as error:
        log.warning(f"user settings unavailable ({error}) -> FIFO order this tick")
        return None


def sessions_of(task_id):
    response = get(f"/api/v1/tasks/{task_id}/sessions") or {}
    return response.get("sessions", [])


def _ws_connect():
    # /ws rejects a foreign Origin, so present the server's own URL.
    return websocket.create_connection(settings.ws_url, timeout=30, origin=settings.url)


class RequestOutcomeUnknown(RuntimeError):
    """A sent request got no matching response before the deadline or a disconnect.

    Distinct from a refusal on purpose. A refusal means kandev did nothing, so the slot is
    free. This means kandev may have acted and only the answer was lost -- spending the
    slot again could start the same turn twice.
    """


def _ws_call(connection, action, payload, request_id, timeout=None):
    """One request and its answer, matched on `id`, under an absolute deadline."""
    timeout = settings.ws_timeout if timeout is None else timeout
    deadline = time.monotonic() + timeout
    try:
        connection.settimeout(max(deadline - time.monotonic(), 0))
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
    except Exception as error:
        raise RequestOutcomeUnknown(
            f"{action} outcome unknown: connection failed while sending"
        ) from error
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RequestOutcomeUnknown(
                f"{action} outcome unknown: no response within {timeout:g}s"
            )
        try:
            # Recomputed every pass, so unrelated pushed events spend this request's
            # budget instead of refreshing a per-recv socket timeout.
            connection.settimeout(remaining)
            raw = connection.recv()
        except Exception as error:
            raise RequestOutcomeUnknown(
                f"{action} outcome unknown: no response within {timeout:g}s"
            ) from error
        if not raw:
            # recv() returns "" for any non-text frame, so this skips ping/pong.
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


def ws_request(action, payload, request_id="pace-1", timeout=None):
    """One action over its own connection.

    The request id only has to be unique within a connection, and this opens a fresh
    one per call with a single request in flight -- so one constant is enough.
    """
    connection = _ws_connect()
    try:
        return _ws_call(connection, action, payload, request_id, timeout)
    finally:
        connection.close()


def ws_launch(task_id, intent, session_id, workflow_step_id, agent_profile_id=None):
    payload = {
        "task_id": task_id,
        "intent": intent,
        "workflow_step_id": workflow_step_id,
    }
    if session_id:
        payload["session_id"] = session_id
    # Only intent:start needs this, and only because kandev's start path will not look it
    # up itself. The caller decides when to pass one.
    if agent_profile_id:
        payload["agent_profile_id"] = agent_profile_id
    # Waits only long enough to catch a refusal. An accepted launch is answered minutes
    # later -- kandev waits for the session -- and waiting for that freezes the loop. What
    # arrives after the window is not news: the board shows the session.
    return ws_request("session.launch", payload, timeout=settings.refusal_window)


def ws_set_state(task_id, state):
    return ws_request("task.state", {"id": task_id, "state": state})


def ws_drain(session_id):
    return ws_request("message.queue.drain", {"session_id": session_id})


def ws_queue_count(session_id):
    """How many prompts are parked on this session's queue."""
    return int((ws_request("message.queue.get", {"session_id": session_id})
                or {}).get("count") or 0)


def queued_counts(task_sessions):
    """Pending prompt counts, for every idle session that could hold one.

    Every idle session, not just each task's newest: kandev queues a prompt on whichever
    session the user had open, which is regularly the primary one created with the task.
    Asking only the newest reported an empty queue for a task that had a prompt waiting,
    so the drain never fired and the card sat parked indefinitely.

    A session we cannot ask about is left out, which reads downstream as "empty" -- a
    queue-status hiccup must degrade to launch-only pacing, never stop the tick.
    """
    counts = {}
    for sessions in task_sessions.values():
        for session in sessions:
            if session.get("state") not in DRAINABLE_STATES:
                continue
            try:
                counts[session["id"]] = ws_queue_count(session["id"])
            except Exception as error:
                log.warning(f"queue status failed for session {session['id']} ({error}) "
                            "-> treating as empty")
    return counts
