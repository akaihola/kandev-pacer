"""Tests for kandev_pacer/client.py: the WebSocket wire handling, against a fake kandev.

Covers what the pure tests cannot -- request/response matching past unrelated pushes, the
Origin header /ws insists on, error frames, and which sessions are worth asking about.
No real kandev, no agent turn, no quota.
"""
from conftest import one_session as _sess

from kandev_pacer.client import URL, queued_counts, ws_drain


def test_queued_counts_only_asks_about_drainable_sessions(board):
    board.load([], {}, {"sReview": 2, "sIdle": 0})
    counts = queued_counts({
        "a": _sess("sReview", "WAITING_FOR_INPUT"),
        "b": _sess("sIdle", "WAITING_FOR_INPUT"),
        "c": _sess("sRun", "RUNNING"),      # turn in flight
        "d": _sess("sNew", "CREATED"),      # prepared but not promptable
    })
    assert counts == {"sReview": 2, "sIdle": 0}
    asked = sorted(p["session_id"] for a, p in board.ws_log if a == "message.queue.get")
    assert asked == ["sIdle", "sReview"]


def test_connections_present_the_servers_own_origin(board):
    """/ws rejects a foreign Origin, so the pacer must send the server's own URL."""
    ws_drain("sAny")
    assert board.origins == [URL]


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


def test_unreadable_queue_degrades_to_empty(board):
    """A failing queue read must not kill the tick -- it falls back to launch-only pacing."""
    assert queued_counts({"t": _sess("sBoom", "WAITING_FOR_INPUT")}) == {}
