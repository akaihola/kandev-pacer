"""Tests for kandev_pacer/client.py: the WebSocket wire handling, against a fake kandev.

Covers what the pure tests cannot -- request/response matching past unrelated pushes, the
Origin header /ws insists on, and error frames. No real kandev, no agent turn, no quota.
"""
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
