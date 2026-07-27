"""Tests for kandev_pacer/runtime.py: the event log, and tick() end to end against a fake kandev.

Covers what the pure tests cannot: tick()'s drain and launch branches -- selection, the call
itself, booking, the no-turn-spent paths, and what a launch kandev refuses does to the board.
No real kandev, no agent turn, no quota.
"""
import json

import pytest
from conftest import STEPS, board_task as _task, one_session as _sess

from kandev_pacer.config import MAX_ATTEMPTS, settings
from kandev_pacer.runtime import Pacer
from kandev_pacer.workflow import Board, fail_backoff


@pytest.fixture
def pacer(board, monkeypatch):
    """A Pacer wired for the fake board, with logging off.

    Scope comes from Board.scan() rather than being assembled by hand, so what tick()
    runs against is what startup would really produce. The log is disabled because
    these records are fixtures, not observations -- writing them to the real log would
    poison the very dataset the log exists to collect.
    """
    monkeypatch.setattr(settings, "log_path", "")
    scanned = Board.scan([({"id": "wf", "name": "Paced"}, STEPS)])
    assert (scanned.scope, scanned.work_steps, scanned.admission) == \
        (["wf"], {"ip"}, {"wf": "ip"})
    return Pacer(scanned)


def actions(board):
    """What the pacer did this tick, queue reads excluded."""
    return [action for action, _ in board.ws_log if action != "message.queue.get"]


def tick(pacer, board, tasks, sessions, queues):
    board.load(tasks, sessions, queues)
    board.ws_log.clear()
    return pacer.tick(was_open=True)


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


# --- tick(): queued-prompt release ---

def test_drains_prompt_parked_at_review(pacer, board):
    """Impossible before: Review is never launchable, so only a drain serves it."""
    tick(pacer, board, [_task("t1", "rv", "Feedback")], {"t1": _sess("s1", "WAITING_FOR_INPUT")},
         {"s1": 1})
    assert actions(board) == ["message.queue.drain"]
    assert ("t1", "rv", "s1") in pacer.booked, "a spent drain must be booked"


def test_parked_with_empty_queue_still_does_nothing(pacer, board):
    tick(pacer, board, [_task("t1", "rv", "Feedback")], {"t1": _sess("s1", "WAITING_FOR_INPUT")}, {})
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
    tick(pacer, board, [_task("t1", "rv", "Feedback")], {"t1": _sess("sStale", "WAITING_FOR_INPUT")},
         {})
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

    The drag order cannot change inside a tick, and the fetch used to sit in the
    candidate loop, so a board of broken tasks multiplied it by MAX_ATTEMPTS.
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
    pacer.tick(was_open=True)
    assert actions(board) == []


def test_repeated_refusals_flag_the_task_blocked_on_the_board(pacer, board):
    """A card the pacer has given up on has to be visible somewhere other than stderr."""
    tick(pacer, board, [_task("boom3", "ip", "Broken")], {"boom3": _sess("sb3", "CREATED")}, {})
    assert "task.state" not in actions(board), "one hiccup must not flag a card"

    _expire_backoff(pacer)
    board.ws_log.clear()
    assert pacer.tick(was_open=True) is True
    assert [p for a, p in board.ws_log if a == "task.state"] == \
        [{"id": "boom3", "state": "BLOCKED"}]

    _expire_backoff(pacer)                       # flagged once, not once per retry
    board.ws_log.clear()
    pacer.tick(was_open=True)
    assert "task.state" not in actions(board)


def test_a_failed_flag_does_not_stop_pacing(pacer, board):
    """Reporting the problem must not become a second problem -- the state write can fail too."""
    tick(pacer, board, [_task("boomflag", "ip", "Broken")], {"boomflag": _sess("sf", "CREATED")}, {})
    _expire_backoff(pacer)
    assert pacer.tick(was_open=True) is True
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
