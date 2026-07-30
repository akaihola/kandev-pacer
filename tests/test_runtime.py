"""Tests for kandev_pacer/runtime.py: the event log, and tick() end to end against a fake kandev.

Covers what the pure tests cannot: tick()'s drain and launch branches -- selection, the call
itself, booking, the no-turn-spent paths, and what a launch kandev refuses does to the board.
No real kandev, no agent turn, no quota.
"""
import json
import logging
import re
from dataclasses import replace

import pytest
from conftest import CLEAN
from conftest import board_task as _task
from conftest import one_session as _sess

from kandev_pacer import client
from kandev_pacer.config import MAX_ATTEMPTS, settings
from kandev_pacer.runtime import Pacer, dump_order, queued_counts
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
        r"headroom=-0\.300 PAUSED \(0 running\), resumes in ~1h (29|30)m \d\ds \(~\d\d:\d\d\)",
        line,
    ), line


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
        r"resumes in ~2h (29|30)m \d\ds \(~\d\d:\d\d\)",
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
