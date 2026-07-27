"""Kandev HTTP and WebSocket access."""

import json
import sys
import urllib.request

from .config import DRAINABLE_STATES, ORDER, URL, WS_URL
from .workflow import newest_session


def request(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def workflows():
    return (request("GET", "/api/v1/workflows") or {}).get("workflows") or []


def workflow_steps(workflow_id):
    response = request(
        "GET", f"/api/v1/workflows/{workflow_id}/workflow/steps"
    ) or {}
    return response.get("steps", [])


def workflow_entries():
    return [(workflow, workflow_steps(workflow["id"])) for workflow in workflows()]


def tasks_of(workflow_id):
    response = request("GET", f"/api/v1/workflows/{workflow_id}/tasks") or {}
    return response.get("tasks", [])


def paced_tasks(scope):
    return [task for workflow_id in scope for task in tasks_of(workflow_id)]


def user_settings(order=ORDER):
    if order == "board":
        return None
    try:
        return (request("GET", "/api/v1/user/settings") or {}).get("settings") or None
    except Exception as error:
        print(
            f"user settings unavailable ({error}) -> FIFO order this tick",
            file=sys.stderr,
            flush=True,
        )
        return None


def sessions_of(task_id):
    response = request("GET", f"/api/v1/tasks/{task_id}/sessions") or {}
    return response.get("sessions", [])


def _ws_connect():
    import websocket

    return websocket.create_connection(WS_URL, timeout=30, origin=URL)


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
    connection = _ws_connect()
    try:
        return _ws_call(connection, action, payload, request_id)
    finally:
        connection.close()


def ws_launch(
    task_id, intent, session_id, workflow_step_id, request_id="pace-1"
):
    payload = {
        "task_id": task_id,
        "intent": intent,
        "workflow_step_id": workflow_step_id,
    }
    if session_id:
        payload["session_id"] = session_id
    return ws_request("session.launch", payload, request_id)


def ws_set_state(task_id, state, request_id="state-1"):
    return ws_request(
        "task.state", {"id": task_id, "state": state}, request_id
    )


def ws_drain(session_id, request_id="drain-1"):
    return ws_request(
        "message.queue.drain", {"session_id": session_id}, request_id
    )


def queued_counts(task_sessions):
    targets = []
    for sessions in task_sessions.values():
        newest = newest_session(sessions)
        if newest is not None and newest.get("state") in DRAINABLE_STATES:
            targets.append(newest["id"])
    if not targets:
        return {}
    try:
        connection = _ws_connect()
    except Exception as error:
        print(
            f"queue status unavailable ({error}) -> treating every queue as empty",
            file=sys.stderr,
            flush=True,
        )
        return {}
    counts = {}
    try:
        for index, session_id in enumerate(targets):
            try:
                response = (
                    _ws_call(
                        connection,
                        "message.queue.get",
                        {"session_id": session_id},
                        f"q{index}",
                    )
                    or {}
                )
                counts[session_id] = int(response.get("count") or 0)
            except Exception as error:
                print(
                    f"queue status failed for session {session_id} ({error}) "
                    "-> treating as empty",
                    file=sys.stderr,
                    flush=True,
                )
    finally:
        connection.close()
    return counts
