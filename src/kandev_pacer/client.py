"""Kandev HTTP and WebSocket access."""

import json
import logging
import time
import urllib.request

import websocket

from .config import WS_TIMEOUT, settings

log = logging.getLogger("kandev-pacer")


class ReadFailed(RuntimeError):
    """A GET that failed, named by the path it failed on.

    urllib's own message says "HTTP Error 404: Not Found" and nothing else -- the URL
    is on the exception object but never reaches the text, so a 404 from any of the
    seven endpoints the pacer reads looks identical in the log. A RuntimeError so the
    `except Exception` handlers that already guard these calls keep catching it.
    """


def get(path):
    """Read one `/api/v1` endpoint.

    The pacer's loop only ever reads over HTTP: everything that changes kandev while
    pacing -- launching, draining, flagging -- goes over the WebSocket. The one HTTP write
    is `put`, which `make-paced` uses to enrol a workflow.
    """
    try:
        with urllib.request.urlopen(settings.url + path, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except Exception as error:
        raise ReadFailed(f"GET {path}: {error}") from error


class WriteFailed(RuntimeError):
    """A PUT kandev refused or never answered, named by its path. See ReadFailed."""


def put(path, body):
    """Send `body` as JSON to one `/api/v1` endpoint and decode the answer."""
    request = urllib.request.Request(
        settings.url + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except Exception as error:
        raise WriteFailed(f"PUT {path}: {error}") from error


def steps_of(workflow_id):
    return (get(f"/api/v1/workflows/{workflow_id}/workflow/steps") or {}).get("steps", [])


def workflow_entries():
    """Every workflow kandev knows, each paired with its steps. The startup scan's input."""
    workflows = (get("/api/v1/workflows") or {}).get("workflows") or []
    return [(workflow, steps_of(workflow["id"])) for workflow in workflows]


def tasks_of(workflow_id):
    response = get(f"/api/v1/workflows/{workflow_id}/tasks") or {}
    return response.get("tasks", [])


def paced_tasks(scope):
    return [task for workflow_id in scope for task in tasks_of(workflow_id)]


def agents():
    """Every agent profile kandev knows, as `profile id -> {agent, billing_type, model}`.

    The one call that says which subscription a task will burn: a task carries an
    `agent_profile_id`, and this maps it to the agent that runs it and how that agent is
    billed. Fetched when more than one provider is paced, and when a model-scoped window
    (the Fable weekly limit) makes the profile's model matter even with one -- otherwise
    every task burns the one subscription and no attribution is needed.

    Unreadable answers `{}`, which the resolver reads as "cannot say", so a hiccup here
    withholds the tasks it cannot attribute rather than stopping the tick.
    """
    try:
        response = get("/api/v1/agents") or {}
    except Exception as error:
        log.warning(
            f"agents unavailable ({error}) -> cannot attribute tasks to a provider"
        )
        return {}
    return {
        profile["id"]: {
            "agent": agent.get("name"),
            "billing_type": profile.get("billing_type"),
            "model": profile.get("model"),
            "fallback_model": profile.get("fallback_model"),
            "auto_fallback": profile.get("auto_fallback"),
        }
        for agent in response.get("agents") or []
        for profile in agent.get("profiles") or []
        if profile.get("id")
    }


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


def task_of(task_id):
    """One task by id, or None if kandev no longer has it.

    The endpoint answers either the task itself or a `{"task": …}` envelope, depending
    on the kandev build, so both are accepted.
    """
    response = get(f"/api/v1/tasks/{task_id}") or {}
    return response.get("task") or (response if response.get("id") else None)


def _ws_connect():
    # /ws rejects a foreign Origin, so present the server's own URL.
    return websocket.create_connection(
        settings.ws_url, timeout=WS_TIMEOUT, origin=settings.url
    )


class RequestOutcomeUnknown(RuntimeError):
    """A sent request got no matching response before the deadline or a disconnect.

    Distinct from a refusal on purpose. A refusal means kandev did nothing, so the slot is
    free. This means kandev may have acted and only the answer was lost -- spending the
    slot again could start the same turn twice.
    """


def _ws_call(connection, action, payload, request_id, timeout=None):
    """One request and its answer, matched on `id`, under an absolute deadline."""
    timeout = WS_TIMEOUT if timeout is None else timeout
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
