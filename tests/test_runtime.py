"""Tests for kandev_pacer/runtime.py: the event log, and tick() end to end against a fake kandev.

Covers what the pure tests cannot: tick()'s drain and launch branches -- selection, the call
itself, booking, the no-turn-spent paths, and what a launch kandev refuses does to the board.
No real kandev, no agent turn, no quota.
"""
import json
import logging
import re
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from conftest import CLEAN
from conftest import board_task as _task
from conftest import one_session as _sess

from kandev_pacer import client, provider, runtime
from kandev_pacer.config import MAX_ATTEMPTS, MAX_INTERVAL, MIN_INTERVAL, settings
from kandev_pacer.runtime import (
    ERROR_RESTATE,
    Pacer,
    QuotaUnreadable,
    dump_order,
    queued_counts,
)
from kandev_pacer.selection import Booking, fail_backoff
from kandev_pacer.workflow import Board


@pytest.fixture
def pacer(board, monkeypatch):
    """A Pacer wired for the fake board, with logging off.

    Scope comes from Board.scan() rather than being assembled by hand, so what tick()
    runs against is what startup would really produce. The log is disabled because
    these records are fixtures, not observations -- writing them to the real log would
    poison the very dataset the log exists to collect.
    """
    monkeypatch.setattr(settings, "log_path", "")
    scanned = Board.scan([({"id": "wf", "name": "Paced"}, CLEAN)])
    assert (scanned.scope, scanned.work_steps, scanned.admission) == \
        (["wf"], {"ip"}, {"wf": "ip"})
    return Pacer(scanned)


def actions(board):
    """What the pacer did this tick, queue reads excluded."""
    return [action for action, _ in board.ws_log if action != "message.queue.get"]


# --- which sessions a tick asks about before it decides ---

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


def test_unreadable_queue_degrades_to_empty(board):
    """A failing queue read must not kill the tick -- it falls back to launch-only pacing."""
    assert queued_counts({"t": _sess("sBoom", "WAITING_FOR_INPUT")}) == {}


def tick(pacer, board, tasks, sessions, queues):
    board.load(tasks, sessions, queues)
    board.ws_log.clear()
    return pacer.tick()


# --- the event log ---

def test_log_event_roundtrip_and_never_raises(tmp_path, monkeypatch):
    logger = Pacer(Board.scan([]))
    monkeypatch.setattr(settings, "log_path", str(tmp_path / "sub" / "pacer.jsonl"))
    logger.log_event({"kind": "tick", "gate": "open"})
    logger.log_event({"kind": "failure", "task": "t1"})      # parent created on demand
    lines = (tmp_path / "sub" / "pacer.jsonl").read_text().splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["tick", "failure"]

    # A log we cannot write must not take the pacing loop down with it: here the parent of
    # the log path is an existing *file*, so mkdir raises NotADirectoryError.
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    monkeypatch.setattr(settings, "log_path", str(blocker / "pacer.jsonl"))
    logger.log_event({"kind": "tick"})
    assert logger._log_failed                                # warned once, did not raise

    monkeypatch.setattr(settings, "log_path", "")            # disabled -> no-op
    logger.log_event({"kind": "tick"})


# --- a failure that repeats every tick ---

def _errors(pacer, tmp_path, monkeypatch):
    """The pacer's error reporting, writing to a real log we can count."""
    monkeypatch.setattr(settings, "log_path", str(tmp_path / "pacer.jsonl"))
    return lambda: [json.loads(line) for line in
                    (tmp_path / "pacer.jsonl").read_text().splitlines()]


def test_a_standing_failure_is_said_once_then_restated_on_a_slow_clock(
    pacer, tmp_path, monkeypatch, caplog
):
    """One line per tick is how the event log reached 30.7 MB. Say it, then be quiet."""
    records = _errors(pacer, tmp_path, monkeypatch)
    error = RuntimeError("GET /api/v1/agents/usage: HTTP Error 404: Not Found")
    with caplog.at_level(logging.WARNING, logger="kandev-pacer"):
        for tick in range(100):                              # 100 ticks, 5s apart
            pacer.note_tick_error(error, 1_000 + tick * 5)
    lines = [r.message for r in caplog.records]
    # 100 ticks span 495s, short of one restate -- so exactly the first occurrence.
    assert len(lines) == 1 and "/api/v1/agents/usage" in lines[0]
    assert len(records()) == 1

    # Past the restate window the outage is stated again, with what it cost meanwhile.
    with caplog.at_level(logging.WARNING, logger="kandev-pacer"):
        pacer.note_tick_error(error, 1_000 + 100 * ERROR_RESTATE)
    restated = caplog.records[-1].message
    assert "unchanged for" in restated and "101 ticks" in restated
    assert records()[-1]["ticks"] == 101


def test_a_different_failure_is_never_suppressed(pacer, tmp_path, monkeypatch, caplog):
    """Suppression is per-message: a new symptom is news even inside the quiet window."""
    records = _errors(pacer, tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="kandev-pacer"):
        pacer.note_tick_error(RuntimeError("GET /api/v1/workflows: HTTP Error 500"), 1_000)
        pacer.note_tick_error(RuntimeError("GET /api/v1/tasks/t1: HTTP Error 404"), 1_005)
    assert len(records()) == 2


def test_an_unreadable_quota_says_the_gate_is_shut(pacer, tmp_path, monkeypatch, caplog):
    """Not an anonymous tick error: no quota means nothing starts at all."""
    records = _errors(pacer, tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="kandev-pacer"):
        pacer.note_tick_error(QuotaUnreadable("GET /api/v1/agents/usage: HTTP 404"), 1_000)
    line = caplog.records[-1].message
    assert "gate is shut" in line and "/api/v1/agents/usage" in line
    assert records()[-1]["where"] == "quota"


def test_a_recovered_tick_closes_out_the_outage(pacer, tmp_path, monkeypatch, caplog):
    """The quiet stretch needs a visible end, or a live tail cannot tell it ended."""
    records = _errors(pacer, tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="kandev-pacer"):
        pacer.note_tick_error(RuntimeError("boom"), 1_000)
        pacer.note_tick_error(RuntimeError("boom"), 1_005)
        pacer.note_tick_recovered(1_010)
        pacer.note_tick_recovered(1_015)                     # nothing to close out
    assert [r["kind"] for r in records()] == ["error", "recovered"]
    assert records()[-1]["ticks"] == 2
    assert pacer._tick_error is None


def test_an_unreadable_quota_names_what_failed(pacer, board, monkeypatch):
    """The two halves together: read() raises, and the message says what could not be read.

    The quota comes from the provider now, not from kandev (see provider.py), so what a
    tick names here is that read. The shape is what matters either way: no quota is not one
    more anonymous read failure, it is the tick's whole verdict gone.
    """
    def boom(fresh=False, now=None):
        raise provider.QuotaUnavailable(
            "provider usage read failed: HTTP Error 401: Unauthorized"
        )

    board.load([], {}, {})
    monkeypatch.setattr(provider, "usage", boom)
    try:
        pacer.read()
    except QuotaUnreadable as error:
        assert "provider usage read failed" in str(error)
    else:
        raise AssertionError("an unreadable quota must stop the tick")


# --- tick(): queued-prompt release ---

def test_drains_prompt_parked_at_review(pacer, board):
    """Impossible before: Review is never launchable, so only a drain serves it."""
    tick(pacer, board, [_task("t1", "rv", "Feedback")], {"t1": _sess("s1", "WAITING_FOR_INPUT")},
         {"s1": 1})
    assert actions(board) == ["message.queue.drain"]
    assert ("t1", "rv", "s1") in pacer.booked, "a spent drain must be booked"


def test_parked_with_empty_queue_still_does_nothing(pacer, board):
    tick(
        pacer,
        board,
        [_task("t1", "rv", "Feedback")],
        {"t1": _sess("s1", "WAITING_FOR_INPUT")},
        {},
    )
    assert actions(board) == []


def test_drain_outranks_launch_on_a_work_step(pacer, board):
    """Launching first would spend the turn on the step prompt and leak the queued entry."""
    tick(pacer, board, [_task("t2", "ip", "Work")], {"t2": _sess("s2", "WAITING_FOR_INPUT")},
         {"s2": 1})
    assert actions(board) == ["message.queue.drain"]


def test_empty_queue_leaves_launch_behaviour_unchanged(pacer, board):
    tick(pacer, board, [_task("t2", "ip", "Work")], {"t2": _sess("s2", "WAITING_FOR_INPUT")}, {})
    assert actions(board) == ["session.launch"]


def test_stale_count_spends_no_turn(pacer, board):
    """Queue emptied between the count and the take (the UI's Send now) -> nothing booked.

    The fake reports one pending entry for sStale but takes nothing, which is exactly the
    race: kandev answers drained:false and no turn is spent.
    """
    tick(
        pacer,
        board,
        [_task("t1", "rv", "Feedback")],
        {"t1": _sess("sStale", "WAITING_FOR_INPUT")},
        {},
    )
    assert actions(board) == ["message.queue.drain"]
    assert pacer.booked == {}, "drained:false spends no turn, so it must not book"


def test_never_run_task_is_launched_not_drained(pacer, board):
    """CREATED is not promptable; its queued prompt waits for the admission turn."""
    tick(pacer, board, [_task("t3", "bk", "New")], {"t3": _sess("s3", "CREATED")}, {"s3": 1})
    assert actions(board) == ["session.launch"]
    assert not any(a == "message.queue.get" and p["session_id"] == "s3"
                   for a, p in board.ws_log), "a CREATED session should not even be asked about"


def test_concurrency_cap_short_circuits_before_any_queue_read(pacer, board):
    tick(pacer, board, [_task("t4", "ip", "Busy")], {"t4": _sess("s4", "RUNNING")}, {"s4": 1})
    assert board.ws_log == []


def test_sidebar_order_is_fetched_once_per_tick(pacer, board):
    """Not once per candidate: three refused launches must not mean three settings reads.

    The drag order cannot change inside a tick, so a board of broken tasks must not
    multiply the fetch by MAX_ATTEMPTS.
    """
    tick(pacer, board,
         [_task(f"boom{i}", "ip", "Broken", created=str(i)) for i in range(5)],
         {f"boom{i}": _sess(f"sm{i}", "CREATED") for i in range(5)}, {})
    assert actions(board).count("session.launch") == MAX_ATTEMPTS   # several attempts...
    assert board.paths.count("/api/v1/user/settings") == 1          # ...one settings read


# --- tick(): a launch kandev refuses ---

def test_a_refused_launch_does_not_wedge_the_board(pacer, board):
    """The bug: the refused task is the oldest, so it is re-picked every tick, and nothing runs.

    The refusal costs no quota, so the tick still owes the board a turn -- it must move on to
    the next candidate instead of ending, and must leave the gate open.
    """
    was_open = tick(pacer, board,
                    [_task("boom1", "ip", "Broken", created="1"),
                     _task("t9", "ip", "Fine", created="2")],
                    {"boom1": _sess("sa", "CREATED"), "t9": _sess("sb", "WAITING_FOR_INPUT")},
                    {})
    assert actions(board) == ["session.launch", "session.launch"]
    assert [p["task_id"] for a, p in board.ws_log if a == "session.launch"] == ["boom1", "t9"]
    assert was_open is True, "one task's problem must not pause the whole gate"
    assert ("t9", "ip", "sb") in pacer.booked
    assert not any(k[0] == "boom1" for k in pacer.booked), "a refusal spends no turn"
    assert pacer.failed[("boom1", "ip", "sa")]["attempts"] == 1


def test_a_refused_launch_is_left_alone_next_tick(pacer, board):
    tick(pacer, board, [_task("boom2", "ip", "Broken")], {"boom2": _sess("sb2", "CREATED")}, {})
    board.ws_log.clear()
    pacer.tick()
    assert actions(board) == []


def test_repeated_refusals_flag_the_task_blocked_on_the_board(pacer, board):
    """A card the pacer has given up on has to be visible somewhere other than stderr."""
    tick(pacer, board, [_task("boom3", "ip", "Broken")], {"boom3": _sess("sb3", "CREATED")}, {})
    assert "task.state" not in actions(board), "one hiccup must not flag a card"

    _expire_backoff(pacer)
    board.ws_log.clear()
    assert pacer.tick() is True
    assert [p for a, p in board.ws_log if a == "task.state"] == \
        [{"id": "boom3", "state": "BLOCKED"}]

    _expire_backoff(pacer)                       # flagged once, not once per retry
    board.ws_log.clear()
    pacer.tick()
    assert "task.state" not in actions(board)


def test_a_failed_flag_does_not_stop_pacing(pacer, board):
    """Reporting the problem must not become a second problem -- the state write can fail too."""
    tick(
        pacer,
        board,
        [_task("boomflag", "ip", "Broken")],
        {"boomflag": _sess("sf", "CREATED")},
        {},
    )
    _expire_backoff(pacer)
    assert pacer.tick() is True
    assert pacer.failed[("boomflag", "ip", "sf")]["flagged"] is False, \
        "a flag that didn't stick must be tried again, not remembered as done"


def test_a_tick_gives_up_after_max_attempts(pacer, board):
    """A board of broken tasks must not turn one tick into a walk of the whole backlog."""
    was_open = tick(pacer, board,
                    [_task(f"boom{i}", "ip", "Broken", created=str(i)) for i in range(5)],
                    {f"boom{i}": _sess(f"sm{i}", "CREATED") for i in range(5)}, {})
    assert actions(board).count("session.launch") == MAX_ATTEMPTS
    assert was_open is True


def _expire_backoff(pacer):
    """Jump to the far side of every recorded backoff instead of waiting it out."""
    for failure in pacer.failed.values():
        failure["at"] -= max(settings.fail_backoff, fail_backoff(failure["attempts"])) + 1


# --- --dump-order, the report you check a drag against ---

def test_dump_order_lists_the_queue_in_launch_order(pacer, board, capsys):
    board.load([_task("b", "ip", "Second", created="2"),
                _task("a", "ip", "First", created="1")], {}, {})
    dump_order(pacer)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("order: ")
    assert [line.split(". ", 1)[1] for line in lines if ". " in line] == ["First", "Second"]


def test_dump_order_names_a_dragged_task_that_will_never_launch(pacer, board, capsys):
    """The failure this report exists for: dragged to the top, in an unpaced workflow.

    The sidebar shows it first either way, so nothing on screen says it cannot run.
    """
    unpaced = ({"id": "wf-off", "name": "Not Enrolled"}, "not enrolled: ...")
    pacer.board = replace(pacer.board, skipped=[unpaced])
    stranded = {"id": "t-off", "title": "Stranded", "workflow_step_id": "ip"}
    board.load([_task("a", "ip", "In Scope")], {}, {})
    board.tasks_elsewhere = [stranded]
    board.sidebar = {"sidebar_views": [{"id": "v", "sort": {"key": "custom"}}],
                     "sidebar_task_prefs": {"ordered_task_ids": ["t-off", "a"]}}

    dump_order(pacer)
    out = capsys.readouterr().out
    assert "not paced: 1 workflow(s) out of scope" in out
    assert "Not Enrolled: not enrolled" in out
    assert "! placed by hand but not paced: 'Stranded' (in 'Not Enrolled')" in out


# --- the gate shut: what the pacer exists to do ---

def records_of(pacer, monkeypatch):
    """Collect the JSONL records a tick writes, without touching the real log."""
    written = []
    monkeypatch.setattr(pacer, "log_event", written.append)
    return written


def test_a_window_burning_too_fast_pauses_the_gate(pacer, board, monkeypatch):
    """80% consumed against a half-elapsed window: ahead of the burn line, so withhold."""
    written = records_of(pacer, monkeypatch)
    board.usage_pct = 80
    was_open = tick(pacer, board, [_task("t", "ip", "Ready")],
                    {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert was_open is False
    assert actions(board) == [], "a shut gate must spend nothing"
    assert (written[0]["gate"], written[0]["action"]) == ("paused", "withheld")


def _withholding_line(caplog, word):
    return next(r.message for r in caplog.records if word in r.message)


def test_a_paused_tick_says_how_long_it_is_paused_for(pacer, board, caplog):
    """The estimate belongs after the running count, on the one line the operator reads."""
    board.usage_pct = 80          # h=-0.30 against the fake's half-elapsed 5-hour window
    with caplog.at_level(logging.INFO, logger="kandev-pacer"):
        tick(pacer, board, [_task("t", "ip", "Ready")],
             {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    # e reaches 0.80 an hour before the reset the fake puts 9000s out -- 5400s from now,
    # give or take the second the fake truncates off its reset stamp.
    line = _withholding_line(caplog, "PAUSED")
    assert re.fullmatch(
        r"headroom=-0\.300 PAUSED \(0 running\), resumes in ~1h (29|30)m \d\ds \(~\d\d:\d\d\)"
        r", next tick in 120s",
        line,
    ), line


def test_a_far_off_reopening_waits_the_ceiling(pacer, board, monkeypatch):
    """Half of 90 minutes is well past the ceiling: nothing is gained by looking sooner."""
    written = records_of(pacer, monkeypatch)
    board.usage_pct = 80
    tick(pacer, board, [_task("t", "ip", "Ready")], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert pacer.interval == MAX_INTERVAL
    assert written[0]["interval"] == MAX_INTERVAL, "the log has to explain the gaps"


def test_an_eager_interval_still_waits_out_a_long_pause(pacer, board, monkeypatch):
    """PACER_INTERVAL=5 buys a responsive gate; it must not buy a busy one.

    The band does not move with the knob, so the wait here is paced *slower* than the
    configured tick -- and snaps back to it the moment the gate is open again.
    """
    monkeypatch.setattr(settings, "interval", 5)
    board.usage_pct = 80
    tick(pacer, board, [_task("t", "ip", "Ready")], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert pacer.interval == MAX_INTERVAL

    board.usage_pct = 10                      # back under the burn line
    tick(pacer, board, [_task("t", "ip", "Ready")], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert pacer.interval == 5


def test_a_pause_about_to_end_is_checked_sooner(pacer, board, caplog):
    """The point of the estimate: meet the reopening instead of sleeping through it."""
    board.usage_pct = 50.1        # h=-0.001 against the half-elapsed window: 18s of healing
    with caplog.at_level(logging.INFO, logger="kandev-pacer"):
        tick(pacer, board, [_task("t", "ip", "Ready")],
             {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert pacer.interval == pytest.approx(9, abs=1)
    assert "next tick in 9s" in _withholding_line(caplog, "PAUSED")


class _StopTheLoop(Exception):
    """Breaks out of run()'s `while True` from inside sleep, which run() does not catch."""


def _sleeps_of(pacer, monkeypatch):
    """Run one pass of the loop, returning what it asked to sleep for."""
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        raise _StopTheLoop

    monkeypatch.setattr(runtime.time, "sleep", sleep)
    with pytest.raises(_StopTheLoop):
        pacer.run()
    return slept


def test_the_loop_waits_for_however_long_the_tick_decided(pacer, monkeypatch):
    """The whole point: the sleep is the tick's own estimate, not the configured period."""
    def converged():
        pacer.interval = 17
    monkeypatch.setattr(pacer, "tick", converged)
    assert _sleeps_of(pacer, monkeypatch) == [17]


def test_a_failed_tick_backs_off_to_the_full_interval(pacer, monkeypatch):
    """A converged interval was derived from a reading this tick never got.

    Keeping it would poll an unreachable kandev every few seconds for as long as it
    stays down; the estimate is re-derived from scratch by the next tick that works.
    """
    def boom():
        raise OSError("kandev is down")
    pacer.interval = 5                       # as a tick near the reopening would leave it
    monkeypatch.setattr(pacer, "tick", boom)
    monkeypatch.setattr(pacer, "log_event", lambda record: None)
    assert _sleeps_of(pacer, monkeypatch) == [settings.interval]


def test_the_loop_meets_a_reopening_it_would_otherwise_sleep_through(pacer, board, monkeypatch):
    """The feature, end to end: 90 minutes of pause, on a fake clock, through run().

    Neither half proves this alone -- one tick's arithmetic could be right while the loop
    still slept past the moment it computed. What matters is the whole approach: the long
    wait costs no more polling than a fixed interval, and the gate is still met inside the
    floor rather than up to a full interval late.
    """
    clock = {"t": 0.0}
    # 80% consumed against a 5-hour window resetting at t=9000: h=-0.30 now, healed at 5400.
    reset_at = datetime.fromtimestamp(9000.0, UTC).isoformat()
    monkeypatch.setattr(provider, "usage", lambda fresh=False, now=None: {"agents": [{"usage": {
        "provider": "anthropic",
        "windows": [{"label": "5-hour", "utilization_pct": 80, "reset_at": reset_at}]}}]})
    monkeypatch.setattr(runtime.time, "time", lambda: clock["t"])
    board.load([], {}, {})

    slept, opened = [], []

    def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds
        if clock["t"] > 6000:                # well past the reopening
            raise _StopTheLoop
    monkeypatch.setattr(runtime.time, "sleep", sleep)
    ticked = pacer.tick
    monkeypatch.setattr(pacer, "tick", lambda: opened.append((clock["t"], ticked())))

    with pytest.raises(_StopTheLoop):
        pacer.run()

    reopened_at = next(t for t, is_open in opened if is_open)
    assert reopened_at - 5400.0 <= MIN_INTERVAL, "the reopening must be met inside the floor"
    assert max(slept) == MAX_INTERVAL, "a distant wait must not poll faster than the ceiling"
    assert min(slept) >= MIN_INTERVAL, "and the approach must never spin below the floor"
    # Halving means the cost of meeting it is a handful of extra ticks, not a faster rate:
    # a flat 120s would have spent 45 to cover the same 5400s, and slept past the end.
    waiting = [t for t, is_open in opened if t <= reopened_at]
    assert len(waiting) <= 5400 / MAX_INTERVAL + 8


def test_an_open_gate_ticks_at_the_configured_interval(pacer, board):
    """Nothing to converge on: PACER_INTERVAL is the pacing rate, not a poll ceiling."""
    assert tick(pacer, board, [_task("t", "ip", "Ready")],
                {"t": _sess("s", "WAITING_FOR_INPUT")}, {}) is True
    assert pacer.interval == settings.interval


def test_consumption_past_the_backstop_withholds_whatever_the_burn_line_says(
    pacer, board, monkeypatch, caplog
):
    written = records_of(pacer, monkeypatch)
    board.usage_pct = 99                      # past PACER_BACKSTOP, and past elapsed
    with caplog.at_level(logging.INFO, logger="kandev-pacer"):
        assert tick(pacer, board, [_task("t", "ip", "Ready")],
                    {"t": _sess("s", "WAITING_FOR_INPUT")}, {}) is False
    assert actions(board) == []
    assert (written[0]["gate"], written[0]["action"]) == ("ceiling", "withheld")
    # The ceiling waits for the roll the fake puts 9000s out, not for the burn line, which
    # would clear 180s earlier.
    line = _withholding_line(caplog, "CEILING")
    assert re.fullmatch(
        r"headroom=-0\.490 CEILING \(consumed>0\.97\) -> withholding, "
        r"resumes in ~2h (29|30)m \d\ds \(~\d\d:\d\d\), next tick in 120s",
        line,
    ), line


def test_an_unknown_provider_withholds_rather_than_pacing_blind(pacer, board, monkeypatch):
    """A PACER_PROVIDER typo leaves zero windows -- which must not read as 'plenty of room'."""
    written = records_of(pacer, monkeypatch)
    board.provider = "openai"                 # settings.provider is anthropic
    assert tick(pacer, board, [_task("t", "ip", "Ready")],
                {"t": _sess("s", "WAITING_FOR_INPUT")}, {}) is False
    assert actions(board) == []
    assert (written[0]["gate"], written[0]["action"]) == ("blind", "withheld")


def test_a_single_provider_run_launches_an_api_key_task_while_paused(
    pacer, board, monkeypatch
):
    """An api-key task has no subscription window: a shut single gate must not hold it."""
    board.agents_map = {
        "p-spark": {"agent": "opencode-acp", "billing_type": "api_key",
                    "model": "muse-spark-1.3"},
    }
    board.usage_pct = 99                      # past PACER_BACKSTOP: the gate is shut
    task = _task("t", "ip", "ApiKey", profile="p-spark")
    assert tick(pacer, board, [task],
                {"t": _sess("s", "WAITING_FOR_INPUT")}, {}) is True
    assert [p["task_id"] for a, p in board.ws_log if a == "session.launch"] == ["t"]


def test_a_shut_single_gate_withholds_subscription_work_but_not_api_key_work(
    pacer, board, monkeypatch
):
    """Queue order still applies, but the paused subscription task must not hide the
    api-key one behind it."""
    board.agents_map = {
        "p-claude": {"agent": "claude-acp", "billing_type": "subscription"},
        "p-spark": {"agent": "opencode-acp", "billing_type": "api_key",
                    "model": "muse-spark-1.3"},
    }
    board.usage_pct = 99                      # past PACER_BACKSTOP: the gate is shut
    sub = _task("s", "ip", "Sub", profile="p-claude")
    key = _task("k", "ip", "Key", profile="p-spark")
    tick(pacer, board, [sub, key],
         {"s": _sess("ss", "WAITING_FOR_INPUT"),
          "k": _sess("sk", "WAITING_FOR_INPUT")}, {})
    assert [p["task_id"] for a, p in board.ws_log if a == "session.launch"] == ["k"]


def test_an_expired_credential_shuts_the_gate_and_says_what_to_do(
    pacer, board, tmp_path, monkeypatch, caplog
):
    """The one quota failure an operator can actually fix, end to end through the loop.

    The provider's sentence has to survive the whole trip: `QuotaUnavailable` carries it,
    `QuotaUnreadable` passes it on, and `note_tick_error` puts it under the headline about
    the gate. So what broke and what to do about it arrive on the same line.
    """
    records = _errors(pacer, tmp_path, monkeypatch)

    def unavailable(fresh=False, now=None):
        raise provider.QuotaUnavailable(
            "quota credentials expired -- re-auth Claude Code (`claude` then `/login`)"
        )

    board.load([_task("t", "ip", "Ready")], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    board.ws_log.clear()
    monkeypatch.setattr(provider, "usage", unavailable)
    with caplog.at_level(logging.WARNING, logger="kandev-pacer"):
        with pytest.raises(QuotaUnreadable) as caught:
            pacer.tick()
        pacer.note_tick_error(caught.value, 1_000)

    line = caplog.records[-1].message
    assert "gate is shut" in line and "re-auth Claude Code" in line
    assert records()[-1]["where"] == "quota"
    assert actions(board) == []      # nothing was launched on the way to failing


def test_the_quota_is_only_read_fresh_once_the_estimate_has_come_due(pacer, board):
    """The cache bypass a converging pause is allowed, and no more than that.

    `poll_interval` halves the wait down to a 5s floor, so a tick that always asked for a
    fresh reading would poll the provider 60x harder than kandev ever did. Asking only
    once the previous tick's own estimate says the gate is back costs one extra call per
    pause instead of one per tick.
    """
    board.usage_pct = 99                      # shut, with an estimate ~9000s out
    tick(pacer, board, [], {}, {})
    assert board.usage_reads == [False]       # nothing due yet: cache is good enough
    tick(pacer, board, [], {}, {})
    assert board.usage_reads == [False, False]

    pacer.resumes_at = 0.0                    # as a tick that reached its estimate leaves it
    tick(pacer, board, [], {}, {})
    assert board.usage_reads[-1] is True


def test_a_shut_gate_still_records_the_board(pacer, board, monkeypatch):
    """The paused ticks are exactly the ones the log exists to explain."""
    written = records_of(pacer, monkeypatch)
    board.usage_pct = 80
    tick(pacer, board, [_task("t", "ip", "Ready")], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert written[0]["launchable"] == 1, "quota going unspent while work waits"
    assert written[0]["running"] == 0
    assert written[0]["windows"][0]["label"] == "5-hour"


# --- refusals that are one task's problem, not the board's ---

def test_a_refused_drain_holds_that_task_and_frees_the_tick(pacer, board):
    """kandev can refuse a drain (session busy). That spends no quota either."""
    tick(pacer, board, [_task("t1", "rv", "Feedback", created="1"),
                        _task("t2", "ip", "Fine", created="2")],
         {"t1": _sess("sBusy", "WAITING_FOR_INPUT"),
          "t2": _sess("s2", "WAITING_FOR_INPUT")}, {"sBusy": 1})
    assert actions(board) == ["message.queue.drain", "session.launch"], \
        "the refused drain must not end the tick"
    assert pacer.failed[("t1", "rv", "sBusy")]["attempts"] == 1
    assert not any(key.task == "t1" for key in pacer.booked)


def test_a_workflow_with_no_admission_target_does_not_starve_the_board(pacer, board):
    """A task parked in a queue whose board has no work step can never be admitted.

    Left selected it would be the top pick every tick, so it has to be recorded as a
    failure and stepped over, exactly like a launch kandev refuses.
    """
    pacer.board = replace(pacer.board, admission={})
    was_open = tick(pacer, board, [_task("q", "bk", "Queued")],
                    {"q": _sess("sq", "CREATED")}, {})
    assert actions(board) == [], "nothing to launch it into, so no turn is spent"
    assert pacer.failed[("q", "bk", "sq")]["attempts"] == 1
    assert pacer.booked == {}
    assert was_open is True, "one unlaunchable task must not shut the gate"


def test_a_task_that_moved_since_selection_is_skipped_not_launched(pacer, board, monkeypatch):
    """Selection ran against a snapshot; a turn is expensive enough to re-check.

    attempt() re-reads the board, so a task a human dragged between the snapshot and
    the launch is skipped rather than started at a step it has already left.
    """
    board.load([_task("t", "ip", "Ready")], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})

    def moving(path):
        # The single-task re-read inside attempt() sees that it moved to Review.
        if path == "/api/v1/tasks/t":
            return {"task": _task("t", "rv", "Ready")}
        return board.get(path)

    monkeypatch.setattr(client, "get", moving)
    assert pacer.tick() is True
    assert actions(board) == [], "a task that moved spends nothing"
    assert pacer.booked == {}


def test_drains_the_session_holding_the_queue_not_the_newest(pacer, board):
    """A card parked at Review with two idle sessions, the prompt on the older one.

    kandev queues a prompt on whichever session the user had open, routinely the primary
    one created with the task. Reading only the newest saw an empty queue, so the drain
    never fired and the card sat parked while the pacer spent its slots further down the
    order -- with nothing in the log to say a prompt was being skipped.
    """
    sessions = {"t1": [{"id": "old", "state": "WAITING_FOR_INPUT", "started_at": "1"},
                       {"id": "new", "state": "WAITING_FOR_INPUT", "started_at": "2"}]}
    tick(pacer, board, [_task("t1", "rv", "Feedback")], sessions, {"old": 1})

    assert actions(board) == ["message.queue.drain"]
    drained = [p["session_id"] for a, p in board.ws_log if a == "message.queue.drain"]
    assert drained == ["old"], "the drain must target the session that holds the queue"
    # The booking has to name that session too: one naming a session the board never lists
    # is pruned on the next tick, and the same prompt gets drained a second time.
    assert ("t1", "rv", "old") in pacer.booked


def test_a_never_run_task_is_started_with_its_own_agent_profile(pacer, board):
    """kandev's start path resolves the executor but not the profile, and refuses without one.

    That refusal is expensive rather than noisy: kandev sets SCHEDULING and moves the card
    before the check, so it strands the task on a work step with no session.
    """
    task = _task("t", "ip", "Fresh")
    task["metadata"] = {"agent_profile_id": "prof-1"}
    tick(pacer, board, [task], {"t": []}, {})           # no session at all -> intent:start

    launch = next(p for a, p in board.ws_log if a == "session.launch")
    assert launch["intent"] == "start"
    assert launch["agent_profile_id"] == "prof-1"


def test_an_in_place_turn_carries_no_agent_profile(pacer, board):
    """intent:workflow_step is how a profile-switch step spawns its own session.

    Sending a profile on every turn would pin the task to whichever one it started with.
    """
    task = _task("t", "ip", "Running on")
    task["metadata"] = {"agent_profile_id": "prof-1"}
    tick(pacer, board, [task], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})

    launch = next(p for a, p in board.ws_log if a == "session.launch")
    assert launch["intent"] == "workflow_step"
    assert "agent_profile_id" not in launch


def test_an_unconfirmed_launch_reserves_the_slot_instead_of_freeing_it(pacer, board,
                                                                       monkeypatch):
    """kandev may have started the turn and only the answer was lost.

    Treating that like a refusal frees the slot for the next candidate, which is how the
    same turn gets started twice. The reservation waits for the board to say.
    """
    monkeypatch.setattr(settings, "refusal_window", 0.05)
    was_open = tick(pacer, board, [_task("silent1", "ip", "Unanswered")],
                    {"silent1": _sess("s1", "WAITING_FOR_INPUT")}, {})

    assert was_open is True
    booking = pacer.booked[("silent1", "ip", "s1")]
    assert booking.uncertain is True, "the slot must stay reserved, not be freed"
    assert pacer.failed == {}, "an unknown outcome is not a refusal"


def test_a_reservation_expires_so_a_lost_launch_is_retried(pacer, board):
    """Bounded by the TTL: if the turn never appears, the card has to become launchable."""
    tasks = [_task("t", "ip", "Ready")]
    sessions = {"t": _sess("s", "WAITING_FOR_INPUT")}
    board.load(tasks, sessions, {})
    pacer.booked[("t", "ip", "s")] = Booking(0.0, uncertain=True)     # long expired

    pacer.tick()
    assert actions(board) == ["session.launch"], "an expired reservation must not wedge it"


def test_a_launch_that_never_became_a_turn_stops_reserving_forever(pacer, board):
    """A launch kandev accepted but never ran leaves the session at READY under the same key.

    Nothing else clears that booking, so without the TTL the card -- launchable in every
    other respect -- was skipped on every later tick.
    """
    board.load([_task("t", "ip", "Ready")], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    pacer.booked[("t", "ip", "s")] = Booking(0.0)                     # plain, long expired
    pacer.tick()
    assert actions(board) == ["session.launch"]


# --- a finished task is not launched forever ---
#
# The fake board's sessions carry no worktree, so progress.fingerprint reads `{}` on every
# tick: a task that never changes anything, which is exactly the case under test. Each
# tick clears the booking first, so what these measure is the stall check and nothing else.


def _relaunch(pacer, board, times):
    """Drive `times` consecutive ticks on one unchanging card, as the real loop would."""
    launched = 0
    for _ in range(times):
        pacer.booked.clear()                 # the previous turn finished; the slot is free
        board.ws_log.clear()
        pacer.tick()
        launched += actions(board).count("session.launch")
    return launched


def test_a_task_that_changes_nothing_stops_being_launched(pacer, board):
    """The whole bug: 509 turns of "the task is complete; stopping", one every two minutes."""
    board.load([_task("done", "ip", "Finished")], {"done": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert _relaunch(pacer, board, 20) == settings.stall_after, \
        "one launch per no-op turn up to the limit, then never again"


def test_the_card_is_flagged_so_a_person_can_see_it(pacer, board):
    """Giving up silently would trade a loud waste for a quiet one."""
    board.load([_task("done", "ip", "Finished")], {"done": _sess("s", "WAITING_FOR_INPUT")}, {})
    _relaunch(pacer, board, 20)
    assert pacer.stalled[("done", "ip", "s")]["flagged"] is True
    assert [p for a, p in board.ws_log if a == "task.state"] == [], \
        "flagged on the tick it gave up, not again on every tick after it"


def test_giving_up_is_not_a_failure(pacer, board):
    """Nothing refused the launch, so the backoff machinery must not own this card."""
    board.load([_task("done", "ip", "Finished")], {"done": _sess("s", "WAITING_FOR_INPUT")}, {})
    _relaunch(pacer, board, 20)
    assert pacer.failed == {}


def test_work_keeps_the_card_launchable(pacer, board, monkeypatch):
    """A task whose turns commit must never be stopped, however long it runs."""
    trees = iter(range(100))
    monkeypatch.setattr(runtime.progress, "fingerprint",
                        lambda sessions: {"/w": (str(next(trees)), False)})
    board.load([_task("busy", "ip", "Working")], {"busy": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert _relaunch(pacer, board, 20) == 20


def test_an_unreadable_worktree_never_stops_a_task(pacer, board, monkeypatch):
    """git being briefly unavailable is not evidence, and must not hold up a live card."""
    monkeypatch.setattr(runtime.progress, "fingerprint", lambda sessions: None)
    board.load([_task("t", "ip", "Ready")], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert _relaunch(pacer, board, 20) == 20


def test_the_limit_can_be_switched_off(pacer, board, monkeypatch):
    """PACER_STALL_AFTER=0 restores the old behaviour, for an operator who wants it."""
    monkeypatch.setattr(settings, "stall_after", 0)
    board.load([_task("done", "ip", "Finished")], {"done": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert _relaunch(pacer, board, 20) == 20


def test_moving_the_card_makes_it_launchable_again(pacer, board):
    """The recovery path a person actually uses, end to end through real ticks."""
    board.load([_task("done", "ip", "Finished")], {"done": _sess("s", "WAITING_FOR_INPUT")}, {})
    _relaunch(pacer, board, 20)
    # Same task, same session, different step: a different key, so the stall does not follow.
    board.load([_task("done", "bk", "Finished")], {"done": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert _relaunch(pacer, board, 1) == 1


# --- the Heartbeat webhook: refilling the board ---

@pytest.fixture
def heartbeat(monkeypatch):
    """The webhook configured and faked; returns the bodies posted, most recent last."""
    monkeypatch.setattr(settings, "heartbeat_webhook_url", "http://kandev/hook")
    monkeypatch.setattr(settings, "heartbeat_webhook_secret", "s3cret")
    monkeypatch.setattr(settings, "heartbeat_min_launchable", 2)
    monkeypatch.setattr(settings, "heartbeat_min_interval", 1800)
    class Posted(list):
        status = 200          # what the fake webhook answers; a test moves it

    posted = Posted()

    def fake_post(url, secret, body):
        posted.append((url, secret, body))
        return posted.status

    monkeypatch.setattr(client, "post_webhook", fake_post)
    return posted


def _ready(n):
    """n launchable tasks, all on the work step, each with its own idle session."""
    tasks = [_task(f"t{i}", "ip", f"Work {i}", created=str(i)) for i in range(n)]
    sessions = {f"t{i}": _sess(f"s{i}", "WAITING_FOR_INPUT") for i in range(n)}
    return tasks, sessions


def test_heartbeat_fires_when_launchable_falls_below_the_threshold(
    pacer, board, heartbeat, monkeypatch
):
    """Two ready, one launched this tick: one left, under the threshold of two."""
    written = records_of(pacer, monkeypatch)
    tasks, sessions = _ready(2)
    tick(pacer, board, tasks, sessions, {})
    assert actions(board) == ["session.launch"]
    [(url, secret, body)] = heartbeat
    assert (url, secret) == ("http://kandev/hook", "s3cret")
    assert body == {"source": "kandev-pacer", "provider": "anthropic", "launchable": 1}
    [record] = [r for r in written if r["kind"] == "heartbeat"]
    assert (record["provider"], record["launchable"], record["status"]) == ("anthropic", 1, 200)
    assert "s3cret" not in json.dumps(record)


def test_heartbeat_stays_quiet_at_the_threshold(pacer, board, heartbeat):
    """Three ready, one launched: two left, which is the threshold, not under it."""
    tasks, sessions = _ready(3)
    tick(pacer, board, tasks, sessions, {})
    assert actions(board) == ["session.launch"]
    assert heartbeat == []


def test_heartbeat_stays_quiet_while_the_gate_is_closed(pacer, board, heartbeat):
    """A closed gate with no work needs no new work: the paused case is excluded."""
    board.usage_pct = 80
    tick(pacer, board, [], {}, {})
    assert heartbeat == []


def test_heartbeat_holds_for_the_minimum_interval(pacer, board, heartbeat, monkeypatch):
    tick(pacer, board, [], {}, {})
    tick(pacer, board, [], {}, {})
    assert len(heartbeat) == 1
    monkeypatch.setattr(pacer, "_last_heartbeat_at", pacer._last_heartbeat_at - 1800)
    tick(pacer, board, [], {}, {})
    assert len(heartbeat) == 2


def test_a_heartbeat_whose_work_was_launched_skips_the_cooldown(pacer, board, heartbeat):
    """Empty board: one call. Its task arrives and is launched: the board is dry again,
    and the next tick calls without waiting out the interval. Then nothing arrives,
    so the cooldown holds -- it throttles fruitless attempts only."""
    tick(pacer, board, [], {}, {})
    assert len(heartbeat) == 1
    tasks, sessions = _ready(1)
    tick(pacer, board, tasks, sessions, {})
    assert actions(board) == ["session.launch"]
    assert len(heartbeat) == 2
    tick(pacer, board, [], {}, {})
    assert len(heartbeat) == 2


def test_a_drain_does_not_lift_the_heartbeat_cooldown(pacer, board, heartbeat):
    """A released prompt is the user's queued work on an existing task, not a refill."""
    tick(pacer, board, [], {}, {})
    assert len(heartbeat) == 1
    tick(pacer, board, [_task("t1", "rv", "Feedback")], {"t1": _sess("s1", "WAITING_FOR_INPUT")},
         {"s1": 1})
    assert actions(board) == ["message.queue.drain"]
    tick(pacer, board, [], {}, {})
    assert len(heartbeat) == 1


def test_heartbeat_skips_while_a_heartbeat_run_is_looking(pacer, board, heartbeat):
    tick(pacer, board, [_task("hb", "ip", "[Auto] Heartbeat")], {"hb": _sess("sh", "RUNNING")}, {})
    assert heartbeat == []
    # Once that run is over the board is dry again, and the call goes out.
    tick(pacer, board, [_task("hb", "dn", "[Auto] Heartbeat")],
         {"hb": _sess("sh", "WAITING_FOR_INPUT")}, {})
    assert len(heartbeat) == 1


def test_a_refused_heartbeat_counts_as_an_attempt(pacer, board, heartbeat, monkeypatch, caplog):
    """409 means a run is active on kandev's side: wait the interval out, no retry per tick."""
    written = records_of(pacer, monkeypatch)
    heartbeat.status = 409
    with caplog.at_level(logging.WARNING, logger="kandev-pacer"):
        tick(pacer, board, [], {}, {})
        tick(pacer, board, [], {}, {})
    assert len(heartbeat) == 1
    assert [r["status"] for r in written if r["kind"] == "heartbeat"] == [409]
    assert any("409" in r.message for r in caplog.records)


def test_a_failed_heartbeat_is_a_warning_not_a_tick_error(pacer, board, heartbeat, monkeypatch):
    written = records_of(pacer, monkeypatch)
    monkeypatch.setattr(client, "post_webhook",
                        lambda *_: (_ for _ in ()).throw(client.WriteFailed("POST x: refused")))
    assert tick(pacer, board, [], {}, {}) is True
    [record] = [r for r in written if r["kind"] == "heartbeat"]
    assert record["status"] == "POST x: refused"
    assert pacer._last_heartbeat_at is not None


def test_heartbeat_is_off_without_a_url(pacer, board, heartbeat, monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_webhook_url", "")
    tick(pacer, board, [], {}, {})
    assert heartbeat == [] and pacer._last_heartbeat_at is None


def test_a_heartbeat_url_without_a_secret_fails_at_startup():
    from kandev_pacer.config import Settings

    assert Settings(PACER_HEARTBEAT_WEBHOOK_URL="").heartbeat_webhook_url == ""
    with pytest.raises(
        ValueError, match="PACER_HEARTBEAT_WEBHOOK_URL.*PACER_HEARTBEAT_WEBHOOK_SECRET"
    ):
        Settings(
            PACER_HEARTBEAT_WEBHOOK_URL="http://kandev/hook",
            PACER_HEARTBEAT_WEBHOOK_SECRET="",
        )


def test_heartbeat_empty_board_calls_once_for_two_providers(
    pacer, board, heartbeat, monkeypatch, caplog
):
    now = runtime.time.time()
    monkeypatch.setattr(runtime.time, "time", lambda: now)
    monkeypatch.setattr(settings, "providers_raw", "anthropic openai")
    written = records_of(pacer, monkeypatch)
    with caplog.at_level(logging.INFO, logger="kandev-pacer"):
        tick(pacer, board, [], {}, {})
    idle = [r.message for r in caplog.records if "nothing to launch" in r.message]
    assert len(idle) == 2
    assert idle[0].startswith("[anthropic]")
    assert idle[1].startswith("[openai]")
    assert len(heartbeat) == 1
    assert heartbeat[0][2] == {
        "source": "kandev-pacer",
        "provider": "anthropic",
        "launchable": 0,
    }
    assert len([r for r in written if r["kind"] == "heartbeat"]) == 1
    assert "webhook 200" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="kandev-pacer"):
        tick(pacer, board, [], {}, {})
    assert len(heartbeat) == 1
    assert "heartbeat: cooldown, retry in 1800s" in caplog.text
    assert pacer._last_heartbeat_at == now
    assert len([r for r in written if r["kind"] == "heartbeat"]) == 1
    assert "http://kandev/hook" not in caplog.text
    assert "s3cret" not in caplog.text


@pytest.mark.parametrize("reason", ["disabled", "already running"])
def test_heartbeat_skip_explains_without_recording_attempt(
    pacer, board, heartbeat, monkeypatch, caplog, reason
):
    monkeypatch.setattr(settings, "providers_raw", "anthropic openai")
    written = records_of(pacer, monkeypatch)
    tasks, sessions = [], {}
    if reason == "disabled":
        monkeypatch.setattr(settings, "heartbeat_webhook_url", "")
    else:
        tasks = [_task("hb", "ip", "[Auto] Heartbeat")]
        sessions = {"hb": _sess("sh", "RUNNING")}
    with caplog.at_level(logging.INFO, logger="kandev-pacer"):
        tick(pacer, board, tasks, sessions, {})
    messages = [r.message for r in caplog.records if r.message.startswith("heartbeat:")]
    assert len(messages) == 1
    assert reason in messages[0]
    if reason == "disabled":
        assert "PACER_HEARTBEAT_WEBHOOK_URL" in messages[0]
        assert "PACER_HEARTBEAT_WEBHOOK_SECRET" in messages[0]
    assert "http://kandev/hook" not in messages[0]
    assert "s3cret" not in messages[0]
    assert heartbeat == []
    assert pacer._last_heartbeat_at is None
    assert not any(r["kind"] == "heartbeat" for r in written)


@pytest.mark.parametrize("condition", ["closed", "threshold", "zero"])
def test_heartbeat_no_refill_opportunity_stays_quiet(
    pacer, board, heartbeat, monkeypatch, caplog, condition
):
    tasks, sessions = [], {}
    if condition == "closed":
        board.usage_pct = 80
    elif condition == "threshold":
        tasks, sessions = _ready(3)
    else:
        monkeypatch.setattr(settings, "heartbeat_min_launchable", 0)
    with caplog.at_level(logging.INFO, logger="kandev-pacer"):
        tick(pacer, board, tasks, sessions, {})
    assert heartbeat == []
    assert not any(r.message.startswith("heartbeat:") for r in caplog.records)
