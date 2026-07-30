#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets", "websocket-client"]
# ///
"""Tests for kandev_pacer/client.py: the WebSocket wire handling, against a fake kandev.

Covers what the pure tests cannot -- request/response matching past unrelated pushes, the
Origin header /ws insists on, error frames, and which sessions are worth asking about.
No real kandev, no agent turn, no quota. Run: uv run tests/test_client.py
"""
import time

import fake_kandev
from fake_kandev import WS_LOG, load, one_session as _sess
from support import run_tests

from kandev_pacer import client  # noqa: E402  -- fakes must be up first
from kandev_pacer.client import queued_counts, ws_drain


def test_queued_counts_only_asks_about_drainable_sessions():
    load([], {}, {"sReview": 2, "sIdle": 0})
    WS_LOG.clear()
    counts = queued_counts({
        "a": _sess("sReview", "WAITING_FOR_INPUT"),
        "b": _sess("sIdle", "WAITING_FOR_INPUT"),
        "c": _sess("sRun", "RUNNING"),      # turn in flight
        "d": _sess("sNew", "CREATED"),      # prepared but not promptable
    })
    assert counts == {"sReview": 2, "sIdle": 0}, counts
    asked = sorted(p["session_id"] for a, p, _ in WS_LOG if a == "message.queue.get")
    assert asked == ["sIdle", "sReview"], asked
    # /ws rejects a foreign Origin, so the pacer must present the server's own URL.
    assert WS_LOG[0][2] == f"http://127.0.0.1:{fake_kandev.HTTP_PORT}"


def test_drain_reports_whether_it_took_anything():
    load([], {}, {"sReview": 1})
    assert ws_drain("sReview")["drained"] is True
    # Queue now empty -> kandev answers drained:false, which means no turn was spent.
    assert ws_drain("sReview")["drained"] is False


def test_ws_errors_surface_with_kandev_reason():
    try:
        ws_drain("sBusy")
    except RuntimeError as e:
        assert "Session is busy" in str(e), e
    else:
        raise AssertionError("a type:'error' frame must raise")


def test_a_launch_waits_only_for_a_refusal():
    """kandev answers an accepted launch minutes later; the pacer must not wait for that.

    The plain request deadline is what a launch used to run on, and it froze the loop --
    no headroom line, no status, nothing -- for as long as kandev took to answer.
    """
    old_window, old_timeout = client.REFUSAL_WINDOW, client.WS_REQUEST_TIMEOUT
    client.REFUSAL_WINDOW, client.WS_REQUEST_TIMEOUT = .08, 30.0
    started = time.monotonic()
    try:
        try:
            client.ws_launch("lost-window", "start", None, "ip")
        except client.RequestOutcomeUnknown:
            pass
        else:
            raise AssertionError("an unanswered launch must give up, not block")
        assert time.monotonic() - started < .5, (
            "a launch must run on the refusal window, not the request deadline"
        )
    finally:
        client.REFUSAL_WINDOW, client.WS_REQUEST_TIMEOUT = old_window, old_timeout


def test_ws_request_deadline_is_absolute_across_push_events():
    old_timeout = client.REFUSAL_WINDOW
    client.REFUSAL_WINDOW = .08
    started = time.monotonic()
    try:
        try:
            client.ws_launch("lost-direct", "start", None, "ip")
        except client.RequestOutcomeUnknown as error:
            assert "outcome unknown" in str(error), error
        else:
            raise AssertionError(
                "a missing response must reach the absolute request deadline"
            )
        assert time.monotonic() - started < .5, (
            "unrelated pushes must not refresh the deadline"
        )
    finally:
        client.REFUSAL_WINDOW = old_timeout


def test_unreadable_queue_degrades_to_empty():
    """A failing queue read must not kill the tick -- it falls back to launch-only pacing."""
    assert queued_counts({"t": _sess("sBoom", "WAITING_FOR_INPUT")}) == {}


if __name__ == "__main__":
    run_tests(globals())
