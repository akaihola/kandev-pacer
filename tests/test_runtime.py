#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets", "websocket-client"]
# ///
"""Tests for kandev_pacer/runtime.py: the event log, and tick() end to end against a fake kandev.

Covers what the pure tests cannot: tick()'s drain and launch branches -- selection, the call
itself, booking, the no-turn-spent paths, and what a launch kandev refuses does to the board.
No real kandev, no agent turn, no quota. Run: uv run tests/test_runtime.py
"""
import io
import json
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

from fake_kandev import STEPS, WS_LOG, board_task as _task, load, one_session as _sess
from support import run_tests

from kandev_pacer import runtime  # noqa: E402  -- the fakes must be up before config is read
from kandev_pacer import client
from kandev_pacer.config import BOOKING_TTL, FAIL_BACKOFF, MAX_ATTEMPTS
from kandev_pacer.workflow import fail_backoff

# Wire the loop's scope for the fake board the way startup does, rather than assigning the
# globals by hand -- so what tick() runs against is what resolve_scope() would really produce.
runtime.resolve_globals([({"id": "wf", "name": "Paced"}, STEPS)])
assert (runtime._SCOPE, runtime._WORK_STEPS, runtime._ADMISSION) == (["wf"], {"ip"}, {"wf": "ip"})

# These tests drive runtime.tick() with fake IO, so their records are fixtures, not
# observations -- writing them to the real log would poison the very dataset the
# log exists to collect.
runtime.LOG_PATH = ""

LAST = {}  # what the last _tick() returned, for the tests that care whether the gate stayed open


def _tick(tasks, sessions, queues):
    """Run one tick against the fakes; return the actions it took (queue reads excluded)."""
    load(tasks, sessions, queues)
    WS_LOG.clear()
    runtime._BOOKED, runtime._FAILED = {}, {}
    # Fresh snapshots per tick, or one test's fake board leaks into the next one's session
    # diff. LOG_PATH is cleared at import; this only keeps the in-memory state honest.
    runtime._PREV_SESSIONS, runtime._PREV_WINDOWS = None, []
    LAST["open"] = runtime.tick(was_open=True)
    return [a for a, _, _ in WS_LOG if a != "message.queue.get"]


# --- the event log ---

def test_log_event_roundtrip_and_never_raises():
    with tempfile.TemporaryDirectory() as d:
        original = runtime.LOG_PATH
        try:
            runtime.LOG_PATH = str(Path(d) / "sub" / "pacer.jsonl")   # parent is created on demand
            runtime.log_event({"kind": "tick", "gate": "open"})
            runtime.log_event({"kind": "session", "to": "RUNNING"})
            lines = Path(runtime.LOG_PATH).read_text().splitlines()
            assert [json.loads(x)["kind"] for x in lines] == ["tick", "session"]
            # A log we cannot write must not take the pacing loop down with it: here the
            # parent of the log path is an existing *file*, so mkdir raises NotADirectoryError.
            blocker = Path(d) / "blocker"
            blocker.write_text("")
            runtime.LOG_PATH = str(blocker / "pacer.jsonl")
            runtime._log_failed = False
            runtime.log_event({"kind": "tick"})
            assert runtime._log_failed                               # warned once, did not raise
            runtime.LOG_PATH = ""                                    # disabled -> no-op
            runtime.log_event({"kind": "tick"})
        finally:
            runtime.LOG_PATH, runtime._log_failed = original, False


# --- tick(): queued-prompt release ---

def test_drains_prompt_parked_at_review():
    """The case that was impossible before: Review is never launchable, so only a drain serves it."""
    acts = _tick([_task("t1", "rv", "Feedback")], {"t1": _sess("s1", "WAITING_FOR_INPUT")}, {"s1": 1})
    assert acts == ["message.queue.drain"], acts
    assert ("t1", "rv", "s1") in runtime._BOOKED, "a spent drain must be booked against concurrency"


def test_drains_the_session_that_holds_the_queue_not_the_newest():
    """The bug this guards: a task parked at Review with two idle sessions, the prompt on the
    older (primary) one -- kandev queues it on whichever session the user had open. Reading and
    draining only the newest saw an empty queue, so the card stayed parked while the pacer spent
    its slots on tasks further down the order. The booking must name the drained session too, or
    the next tick prunes a reservation that matches no live key and drains the prompt twice.
    """
    sessions = {"t1": [{"id": "old", "state": "WAITING_FOR_INPUT", "started_at": "1"},
                       {"id": "new", "state": "WAITING_FOR_INPUT", "started_at": "2"}]}
    acts = _tick([_task("t1", "rv", "Feedback")], sessions, {"old": 1})
    assert acts == ["message.queue.drain"], acts
    drained = [p["session_id"] for a, p, _ in WS_LOG if a == "message.queue.drain"]
    assert drained == ["old"], drained
    assert ("t1", "rv", "old") in runtime._BOOKED, "the booking must name the drained session"


def test_parked_with_empty_queue_still_does_nothing():
    acts = _tick([_task("t1", "rv", "Feedback")], {"t1": _sess("s1", "WAITING_FOR_INPUT")}, {})
    assert acts == [], acts


def test_drain_outranks_launch_on_a_work_step():
    """Launching first would spend the turn on the step prompt and leak the queued entry."""
    acts = _tick([_task("t2", "ip", "Work")], {"t2": _sess("s2", "WAITING_FOR_INPUT")}, {"s2": 1})
    assert acts == ["message.queue.drain"], acts


def test_empty_queue_leaves_launch_behaviour_unchanged():
    acts = _tick([_task("t2", "ip", "Work")], {"t2": _sess("s2", "WAITING_FOR_INPUT")}, {})
    assert acts == ["session.launch"], acts


def test_stale_count_spends_no_turn():
    """Queue emptied between the count and the take (the UI's Send now) -> nothing booked.

    The fake reports one pending entry for sStale but takes nothing, which is exactly the race:
    kandev answers drained:false and no turn is spent.
    """
    acts = _tick([_task("t1", "rv", "Feedback")], {"t1": _sess("sStale", "WAITING_FOR_INPUT")}, {})
    assert acts == ["message.queue.drain"], acts
    assert runtime._BOOKED == {}, "drained:false spends no turn, so it must not book"


def test_never_run_task_is_launched_not_drained():
    """CREATED is not promptable; its queued prompt waits for the admission turn."""
    acts = _tick([_task("t3", "bk", "New")], {"t3": _sess("s3", "CREATED")}, {"s3": 1})
    assert acts == ["session.launch"], acts
    assert not any(a == "message.queue.get" and p["session_id"] == "s3" for a, p, _ in WS_LOG), \
        "a CREATED session should not even be asked about"


def test_an_in_place_turn_does_not_carry_an_agent_profile():
    """Only intent:start gets the profile. intent:workflow_step must stay profile-free.

    That is the path a profile-switch step uses to spawn its own session. kandev ignores the
    field there today, but sending one on every turn would pin the task to whichever profile it
    started with the moment that changed -- kandev owns routing, the pacer owns timing.
    """
    acts = _tick([_task("inplace", "ip", "Running on", profile="prof-1")],
                 {"inplace": _sess("s-ip", "WAITING_FOR_INPUT")}, {})
    assert acts == ["session.launch"], acts
    launch = next(p for a, p, _ in WS_LOG if a == "session.launch")
    assert launch["intent"] == "workflow_step", launch
    assert "agent_profile_id" not in launch, launch


def test_concurrency_cap_short_circuits_before_any_queue_read():
    acts = _tick([_task("t4", "ip", "Busy")], {"t4": _sess("s4", "RUNNING")}, {"s4": 1})
    assert acts == [] and WS_LOG == [], WS_LOG


# --- tick(): a launch kandev refuses ---

def test_a_refused_launch_does_not_wedge_the_board():
    """The bug: the refused task is the oldest, so it is re-picked every tick and nothing runs.

    The refusal costs no quota, so the tick still owes the board a turn -- it must move on to
    the next candidate instead of ending, and must leave the gate open.
    """
    acts = _tick([_task("boom1", "ip", "Broken", created="1"),
                  _task("t9", "ip", "Fine", created="2")],
                 {"boom1": _sess("sa", "CREATED"), "t9": _sess("sb", "WAITING_FOR_INPUT")}, {})
    assert acts == ["session.launch", "session.launch"], acts
    assert [p["task_id"] for a, p, _ in WS_LOG if a == "session.launch"] == ["boom1", "t9"]
    assert LAST["open"] is True, "one task's problem must not pause the whole gate"
    assert ("t9", "ip", "sb") in runtime._BOOKED
    assert not any(k[0] == "boom1" for k in runtime._BOOKED), "a refused launch spends no turn"
    assert runtime._FAILED[("boom1", "ip", "sa")]["attempts"] == 1


def test_a_timed_out_launch_reserves_capacity_instead_of_oversubscribing():
    old_window = client.REFUSAL_WINDOW
    client.REFUSAL_WINDOW = .08
    try:
        load(
            [
                _task("lost1", "ip", "Unknown", created="1"),
                _task("t9", "ip", "Fine", created="2"),
            ],
            {
                "lost1": _sess("sa", "CREATED"),
                "t9": _sess("sb", "WAITING_FOR_INPUT"),
            },
            {},
        )
        runtime._BOOKED, runtime._FAILED = {}, {}
        WS_LOG.clear()
        assert runtime.tick(was_open=True) is True
        launches = [
            payload["task_id"]
            for action, payload, _ in WS_LOG
            if action == "session.launch"
        ]
        assert launches == ["lost1"], launches
        booking = runtime._BOOKED[("lost1", "ip", "sa")]
        assert booking["uncertain"] is True
        assert runtime._FAILED == {}

        WS_LOG.clear()
        assert runtime.tick(was_open=True) is True
        assert not any(action == "session.launch" for action, _, _ in WS_LOG)
    finally:
        client.REFUSAL_WINDOW = old_window


def test_a_tick_names_the_launch_before_it_waits_for_kandev():
    """The silence bug: the launch line was printed only once kandev answered.

    kandev answers a launch long after it accepts one, so a pacer that had just been restarted
    into a free slot printed nothing at all -- no headroom, no status -- and read as hung. The
    decision has to reach the terminal before the wait, whatever the wait turns into.
    """
    old_window = client.REFUSAL_WINDOW
    client.REFUSAL_WINDOW, printed = .08, io.StringIO()
    try:
        load([_task("lost-line", "ip", "Unconfirmed")],
             {"lost-line": _sess("sl", "WAITING_FOR_INPUT")}, {})
        runtime._BOOKED, runtime._FAILED = {}, {}
        runtime._PREV_SESSIONS, runtime._PREV_WINDOWS = None, []
        with redirect_stdout(printed):
            runtime.tick(was_open=True)
    finally:
        client.REFUSAL_WINDOW = old_window
    out = printed.getvalue()
    announced = out.find("open -> launching 'Unconfirmed' intent=workflow_step")
    assert announced >= 0, out
    assert announced < out.find("is in kandev's hands, unconfirmed"), out


def test_a_refused_launch_is_left_alone_next_tick():
    load([_task("boom2", "ip", "Broken")], {"boom2": _sess("sb2", "CREATED")}, {})
    runtime._BOOKED, runtime._FAILED = {}, {}
    runtime.tick(was_open=True)
    WS_LOG.clear()
    runtime.tick(was_open=True)
    assert [a for a, _, _ in WS_LOG if a == "session.launch"] == [], WS_LOG


def test_a_task_stranded_on_a_work_step_is_still_launched():
    """A work-step task with *no* session must be launched, not skipped forever.

    Kandev moves a task to the requested step before it validates the launch (startTask: state
    -> SCHEDULING, moveTaskToWorkflowStep, then the agent-profile check), so an admission it
    rejects leaves the card on the work step with no session ever created. Nothing creates one
    afterwards, so skipping this state is permanent: the pacer reported "nothing to launch"
    while four Heartbeat subtasks sat on the board for ~7.75h with the gate open and no turns
    running.
    """
    acts = _tick([_task("stranded", "ip", "Stranded", profile="prof-s")], {}, {})
    assert acts.count("session.launch") == 1, acts
    launch = next(p for a, p, _ in WS_LOG if a == "session.launch")
    # No session to reuse, so a fresh one is created -- in place, on the step it is already on.
    assert launch["intent"] == "start", launch
    assert "session_id" not in launch, launch
    assert launch["workflow_step_id"] == "ip", launch
    # And it is a real recovery, not just an attempt: the card runs again from where it stuck.
    assert runtime._FAILED == {}, runtime._FAILED


def test_a_never_run_task_is_started_with_its_own_agent_profile():
    """The Heartbeat-subtask case end to end: no session, so intent:start, which needs a profile.

    kandev's start path won't resolve the agent profile from task metadata itself and refuses the
    launch without one — so the pacer passes the task's own back. The fake refuses a profile-less
    start exactly as kandev does, which is what makes this a test of the fix and not just of the
    payload shape.
    """
    acts = _tick([_task("newtask", "bk", "Fresh", profile="prof-1")], {}, {})
    assert acts.count("session.launch") == 1, acts
    launch = next(p for a, p, _ in WS_LOG if a == "session.launch")
    assert launch["intent"] == "start", launch
    assert launch["agent_profile_id"] == "prof-1", launch
    # Admitted from the parked queue to the work step in the same call.
    assert launch["workflow_step_id"] == "ip", launch
    # Accepted, so it books rather than recording a failure.
    assert runtime._FAILED == {}, runtime._FAILED
    assert LAST["open"] is True


def test_a_task_with_no_profile_anywhere_is_flagged_not_retried_silently():
    """Without a profile to pass, the launch still fails — but visibly, not silently.

    The pacer does not invent a profile; that is kandev's business. What it must not do is let
    the refusal disappear, which is what happened when a task in this state wasn't a candidate
    at all: nothing retried it and nothing flagged it.
    """
    load([_task("noprof", "bk", "No profile")], {}, {})
    runtime._BOOKED, runtime._FAILED = {}, {}
    runtime.tick(was_open=True)
    assert runtime._FAILED[("noprof", "bk", None)]["attempts"] == 1, runtime._FAILED
    for f in runtime._FAILED.values():
        f["at"] -= FAIL_BACKOFF + 1
    WS_LOG.clear()
    assert runtime.tick(was_open=True) is True
    assert [p for a, p, _ in WS_LOG if a == "task.state"] == \
        [{"id": "noprof", "state": "BLOCKED"}], WS_LOG


def test_a_stranded_task_kandev_keeps_refusing_gets_flagged():
    """The stranded card must end up visible, not silently retried forever.

    This is the other half of the hours-long stall: while the pacer never even attempted the
    launch, the failure count never climbed, so BLOCK_AFTER never fired and nothing on the
    board said anything was wrong. Retrying is what makes the refusal reportable.
    """
    load([_task("boomStranded", "ip", "Broken")], {}, {})
    runtime._BOOKED, runtime._FAILED = {}, {}
    runtime.tick(was_open=True)
    assert runtime._FAILED[("boomStranded", "ip", None)]["attempts"] == 1, runtime._FAILED
    assert not any(a == "task.state" for a, _, _ in WS_LOG), "one hiccup must not flag a card"
    # Jump to the far side of the backoff instead of waiting it out.
    for f in runtime._FAILED.values():
        f["at"] -= FAIL_BACKOFF + 1
    WS_LOG.clear()
    assert runtime.tick(was_open=True) is True
    assert [p for a, p, _ in WS_LOG if a == "task.state"] == \
        [{"id": "boomStranded", "state": "BLOCKED"}], WS_LOG


def test_repeated_refusals_flag_the_task_blocked_on_the_board():
    """A card the pacer has given up on has to be visible somewhere other than stderr."""
    load([_task("boom3", "ip", "Broken")], {"boom3": _sess("sb3", "CREATED")}, {})
    runtime._BOOKED, runtime._FAILED = {}, {}
    runtime.tick(was_open=True)
    assert not any(a == "task.state" for a, _, _ in WS_LOG), "one hiccup must not flag a card"
    # Jump to the far side of the backoff instead of waiting it out.
    for f in runtime._FAILED.values():
        f["at"] -= FAIL_BACKOFF + 1
    WS_LOG.clear()
    assert runtime.tick(was_open=True) is True
    assert [p for a, p, _ in WS_LOG if a == "task.state"] == [{"id": "boom3", "state": "BLOCKED"}]
    # Flagged once, not once per retry.
    for f in runtime._FAILED.values():
        f["at"] -= fail_backoff(f["attempts"]) + 1
    WS_LOG.clear()
    runtime.tick(was_open=True)
    assert not any(a == "task.state" for a, _, _ in WS_LOG), WS_LOG


def test_a_failed_flag_does_not_stop_pacing():
    """Reporting the problem must not become a second problem -- the state write can fail too."""
    load([_task("boomflag", "ip", "Broken")], {"boomflag": _sess("sf", "CREATED")}, {})
    runtime._BOOKED, runtime._FAILED = {}, {}
    runtime.tick(was_open=True)
    for f in runtime._FAILED.values():
        f["at"] -= FAIL_BACKOFF + 1
    assert runtime.tick(was_open=True) is True
    assert runtime._FAILED[("boomflag", "ip", "sf")]["flagged"] is False, \
        "a flag that didn't stick must be tried again, not remembered as done"


def test_a_launch_that_spent_no_turn_stops_reserving_its_slot():
    """A launch kandev accepts but never runs must not hold its slot for good.

    The session stays WAITING_FOR_INPUT under the same (task, step, session) key, so the
    booking had nothing to clear it: with the gate open and 0 running, the pacer reported
    "nothing to launch" for hours while the reserved card was the only launchable one.
    """
    acts = _tick([_task("wedged", "ip", "Wedged")], {"wedged": _sess("sw", "WAITING_FOR_INPUT")}, {})
    assert acts == ["session.launch"], acts
    key = ("wedged", "ip", "sw")
    assert key in runtime._BOOKED, runtime._BOOKED
    # Still within the TTL: the reservation stands, so no second launch doubles the turn.
    WS_LOG.clear()
    runtime.tick(was_open=True)
    assert [a for a, _, _ in WS_LOG if a == "session.launch"] == [], WS_LOG
    assert key in runtime._BOOKED, "a launch may still be landing -- do not release it early"
    # Past the TTL, with the session still parked where the launch left it: the slot comes back
    # and the card is a candidate again instead of being skipped on every later tick.
    runtime._BOOKED[key] = time.time() - BOOKING_TTL - 1
    WS_LOG.clear()
    assert runtime.tick(was_open=True) is True
    assert [a for a, _, _ in WS_LOG if a == "session.launch"] == ["session.launch"], WS_LOG


def test_the_nothing_to_launch_line_accounts_for_reserved_slots():
    """`0/2 running` alone reads as an idle fleet, which is what hid the wedged booking."""
    _tick([_task("held", "ip", "Held")], {"held": _sess("sh", "WAITING_FOR_INPUT")}, {})
    assert ("held", "ip", "sh") in runtime._BOOKED, runtime._BOOKED
    printed, cap = io.StringIO(), runtime.CONCURRENCY
    try:
        runtime.CONCURRENCY = 2      # room to spare, so the tick reaches the nothing-to-launch line
        with redirect_stdout(printed):
            runtime.tick(was_open=True)
    finally:
        runtime.CONCURRENCY = cap
    assert "nothing to launch (0/2 running, 1 pending)" in printed.getvalue(), printed.getvalue()


def test_a_tick_gives_up_after_max_attempts():
    """A board of broken tasks must not turn one tick into a walk of the whole backlog."""
    acts = _tick([_task(f"boom{i}", "ip", "Broken", created=str(i)) for i in range(5)],
                 {f"boom{i}": _sess(f"sm{i}", "CREATED") for i in range(5)}, {})
    assert acts.count("session.launch") == MAX_ATTEMPTS, acts
    assert LAST["open"] is True


if __name__ == "__main__":
    run_tests(globals())
