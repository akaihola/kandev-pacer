"""Tests for kandev_pacer/client.py: the WebSocket wire handling, against a fake kandev.

Covers what the pure tests cannot -- request/response matching past unrelated pushes, the
Origin header /ws insists on, and error frames. No real kandev, no agent turn, no quota.
"""
import urllib.error
from email.message import Message

import pytest
import websocket

from kandev_pacer import client
from kandev_pacer.client import ws_drain
from kandev_pacer.config import settings


def test_connections_present_the_servers_own_origin(board):
    """/ws rejects a foreign Origin, so the pacer must send the server's own URL."""
    ws_drain("sAny")
    assert board.origins == [settings.url]


def test_drain_reports_whether_it_took_anything(board):
    board.load([], {}, {"sReview": 1})
    assert ws_drain("sReview")["drained"] is True
    # Queue now empty -> kandev answers drained:false, which means no turn was spent.
    assert ws_drain("sReview")["drained"] is False


def test_ws_errors_surface_with_kandev_reason(board):
    try:
        ws_drain("sBusy")
    except RuntimeError as error:
        assert "Session is busy" in str(error)
    else:
        raise AssertionError("a type:'error' frame must raise")


# --- the HTTP layer itself, which every other test replaces ---

def test_get_builds_the_url_and_decodes_the_body(monkeypatch):
    """The one place a path becomes a URL. Every other test patches it away."""
    calls = []

    class _Response:
        def __init__(self, raw):
            self.raw = raw

        def read(self):
            return self.raw

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(url, timeout=None):
        calls.append((url, timeout))
        return _Response(b'{"tasks": []}')

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    assert client.get("/api/v1/workflows") == {"tasks": []}
    assert calls == [(f"{settings.url}/api/v1/workflows", 15)]


def test_get_reads_an_empty_body_as_nothing(monkeypatch):
    """kandev answers some endpoints with no body at all; that is None, not a crash."""

    class _Empty:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(client.urllib.request, "urlopen", lambda url, timeout=None: _Empty())
    assert client.get("/api/v1/anything") is None


def test_a_failed_get_names_the_path_it_failed_on(monkeypatch):
    """A bare urllib 404 says only "HTTP Error 404: Not Found" -- which of the seven
    endpoints answered it is on the exception object and never in the message."""

    def fake_urlopen(url, timeout=None):
        raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    try:
        client.get("/api/v1/agents/usage")
    except RuntimeError as error:
        assert "/api/v1/agents/usage" in str(error)
        assert "404" in str(error)
    else:
        raise AssertionError("a 404 must raise")


# --- a request whose answer never comes ---

def test_a_request_that_never_answers_raises_outcome_unknown(board, monkeypatch):
    """Not a refusal: kandev may have acted and only the answer was lost.

    The distinction is what stops the pacer spending the same slot twice.
    """
    monkeypatch.setattr(client, "WS_TIMEOUT", 0.05)
    fake = board.connect("ws://x", origin=None)
    monkeypatch.setattr(websocket, "create_connection", lambda *a, **k: fake)
    fake.inbox.clear()
    monkeypatch.setattr(fake, "send", lambda raw: None)      # sent, never answered
    try:
        client.ws_drain("s1")
    except client.RequestOutcomeUnknown as error:
        assert "outcome unknown" in str(error)
    else:
        raise AssertionError("a silent request must not read as success or refusal")


def test_unrelated_pushes_spend_the_same_deadline(board, monkeypatch):
    """A busy event stream must not keep one request alive forever.

    Every push makes recv() return, and a per-recv socket timeout would restart on each
    one. The deadline is absolute, so the budget shrinks instead of resetting.
    """
    board.load([], {}, {"sReview": 1})
    client.ws_drain("sReview")
    connection = board.connections[-1]
    assert connection.timeouts == sorted(connection.timeouts, reverse=True), \
        f"deadlines must shrink, got {connection.timeouts}"


def test_a_launch_waits_only_for_a_refusal(board, monkeypatch):
    """An accepted launch is answered minutes later; waiting for it froze the loop."""
    monkeypatch.setattr(settings, "refusal_window", 7)
    client.ws_launch("t", "workflow_step", "s", "ip")
    assert max(board.connections[-1].timeouts) <= 7, "launch must use the refusal window"


def test_a_launch_can_carry_the_tasks_agent_profile(board):
    """intent:start needs it -- kandev's start path will not resolve one itself."""
    client.ws_launch("t", "start", None, "ip", agent_profile_id="prof-1")
    action, payload = board.ws_log[-1]
    assert payload["agent_profile_id"] == "prof-1"
    client.ws_launch("t", "workflow_step", "s", "ip")
    assert "agent_profile_id" not in board.ws_log[-1][1], "only start carries it"


def test_put_sends_json_and_decodes_the_answer(monkeypatch):
    """The one HTTP write: make-paced's step update. Method, header and body all matter."""
    seen = []

    class _Response:
        def read(self):
            return b'{"step": {"id": "ip"}}'

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        return _Response()

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    assert client.put("/api/v1/workflow/steps/ip", {"stage_type": "work"}) == {
        "step": {"id": "ip"}
    }
    [request] = seen
    assert request.get_method() == "PUT"
    assert request.full_url == f"{settings.url}/api/v1/workflow/steps/ip"
    assert request.get_header("Content-type") == "application/json"
    assert request.data == b'{"stage_type": "work"}'


def test_a_failed_put_names_the_path_it_failed_on(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 409, "Conflict", Message(), None)

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    try:
        client.put("/api/v1/workflow/steps/ip", {})
    except client.WriteFailed as error:
        assert "/api/v1/workflow/steps/ip" in str(error) and "409" in str(error)
    else:
        raise AssertionError("a refused PUT must raise")


def test_post_webhook_sends_the_secret_as_a_header(monkeypatch):
    seen = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_urlopen(request, timeout=None):
        seen.append((request, timeout))
        return _Response()

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    assert client.post_webhook("http://kandev/hook", "s3cret", {"provider": "openai"}) == 200
    [(request, timeout)] = seen
    assert (request.get_method(), request.full_url, timeout) == ("POST", "http://kandev/hook", 10)
    assert request.get_header("X-webhook-secret") == "s3cret"
    assert request.data == b'{"provider": "openai"}'


def test_post_webhook_returns_an_http_error_status_and_raises_on_no_answer(monkeypatch):
    def refused(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 409, "Conflict", Message(), None)

    monkeypatch.setattr(client.urllib.request, "urlopen", refused)
    assert client.post_webhook("http://kandev/hook", "s", {}) == 409

    def down(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(client.urllib.request, "urlopen", down)
    with pytest.raises(client.WriteFailed, match="POST http://kandev/hook"):
        client.post_webhook("http://kandev/hook", "s3cret", {})
