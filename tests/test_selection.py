"""Tests for kandev_pacer/selection.py: launch order, eligibility, and the spend records.

Everything under test here is a pure function over plain dictionaries. No network.
"""

from conftest import CLEAN
from conftest import sess as _sess
from conftest import task as _task

from kandev_pacer.selection import (
    Booking,
    action_key,
    backed_off,
    drain_session,
    effective_sidebar_view,
    fail_backoff,
    has_custom_sort_view,
    is_drainable,
    is_launchable,
    launch_target,
    newest_session,
    ordered_tasks,
    pick_intent,
    priority_rank,
    prune_booked,
    prune_failures,
    select_action,
    task_agent_profile,
    task_order_key,
    visible_task_order,
)
from kandev_pacer.workflow import Board, step_ranks

# --- launch ordering ---

RANKS = step_ranks(CLEAN)   # bk=0, ip=1, rv=2, dn=3

def _ord(tasks):
    """Fixtures in launch-priority order, board mode.

    Routed through the real ordering pipeline rather than hand-sorted, so a test
    cannot accidentally assert against an order the pacer would never produce.
    """
    return ordered_tasks(tasks, None, RANKS)

def _order(tasks, ranks=RANKS):
    return [t["id"] for t in sorted(tasks, key=lambda t: task_order_key(t, ranks))]

def test_step_ranks():
    assert RANKS == {"bk": 0, "ip": 1, "rv": 2, "dn": 3}
    assert step_ranks([{"id": "x"}]) == {"x": 0}      # missing position -> leftmost column

def test_priority_rank():
    assert priority_rank({"priority": "critical"}) < priority_rank({"priority": "high"})
    assert priority_rank({"priority": "high"}) < priority_rank({"priority": "medium"})
    assert priority_rank({"priority": "medium"}) < priority_rank({"priority": "low"})
    assert priority_rank({"priority": "LOW  "}) == priority_rank({"priority": "low"})  # case/space
    # kandev's query ranks 'none' and anything unrecognized identically (ELSE 4).
    assert priority_rank({"priority": "none"}) == priority_rank({"priority": "weird"})
    assert (
        priority_rank({})
        == priority_rank({"priority": None})
        == priority_rank({"priority": "none"})
    )


def test_order_by_position_within_a_step():
    """The headline rule: the board's manual drag order decides, lowest position first."""
    assert _order([_task("c", pos=2), _task("a", pos=0), _task("b", pos=1)]) == ["a", "b", "c"]

def test_position_beats_priority_and_age():
    """position outranks both, as in kandev's pull query: a drag is explicit."""
    assert _order([_task("dragged_down", pos=5, prio="critical", created="2020-01-01T00:00:00Z"),
                   _task("dragged_up", pos=0, prio="low", created="2026-06-01T00:00:00Z")]) \
        == ["dragged_up", "dragged_down"]

def test_priority_breaks_position_ties():
    assert _order([_task("lo", pos=0, prio="low"),
                   _task("hi", pos=0, prio="high"),
                   _task("crit", pos=0, prio="critical")]) == ["crit", "hi", "lo"]

def test_created_at_breaks_priority_ties():
    assert _order([_task("new", pos=0, created="2026-06-01T00:00:00Z"),
                   _task("old", pos=0, created="2026-01-01T00:00:00Z")]) == ["old", "new"]

def test_id_makes_the_order_total():
    """Fully tied tasks must still order deterministically, or the pick flaps tick to tick."""
    same = {"pos": 0, "prio": "medium", "created": "2026-01-01T00:00:00Z"}
    assert _order([_task("b", **same), _task("a", **same)]) == ["a", "b"]

def test_missing_and_zero_position_tie_at_the_top():
    """A task with no/null position is treated as position 0 -- top of its column, not last."""
    explicit_zero = _task("zero", pos=0, created="2026-01-02T00:00:00Z")
    absent = {"id": "absent", "workflow_step_id": "ip", "priority": "medium",
              "created_at": "2026-01-01T00:00:00Z"}
    null = dict(_task("null", created="2026-01-03T00:00:00Z"), position=None)
    # All three collapse to 0, so the created_at tiebreak -- not the missing key -- decides.
    assert _order([null, explicit_zero, absent]) == ["absent", "zero", "null"]
    # And they all still beat a genuinely dragged-down task.
    assert _order([_task("down", pos=1), absent]) == ["absent", "down"]

def test_downstream_column_wins_across_steps():
    """position is per-column, so columns are ranked first: rightmost (most downstream) first.

    Both tasks sit at position 0 -- comparing those numbers directly would be meaningless.
    Finishing the turn already in flight beats admitting new work from the parked queue.
    """
    assert _order([_task("queued", step="bk", pos=0),
                   _task("in_flight", step="ip", pos=0)]) == ["in_flight", "queued"]
    # Even a queue task dragged to the very top, made critical and years older stays behind:
    # the column ordering is applied before any of it.
    assert _order(
        [
            _task(
                "queued",
                step="bk",
                pos=0,
                prio="critical",
                created="2020-01-01T00:00:00Z",
            ),
            _task("in_flight", step="ip", pos=9, prio="low"),
        ]
    ) == ["in_flight", "queued"]


def test_multi_work_step_drains_right_to_left():
    """With several work steps, tasks deepest in the pipeline advance before new ones enter."""
    assert _order([_task("q", step="bk"), _task("early", step="ip"), _task("late", step="rv")]) \
        == ["late", "early", "q"]

def test_positions_only_compared_within_a_column():
    """Within each column position decides; the columns themselves never interleave."""
    tasks = [_task("q_top", step="bk", pos=0), _task("q_next", step="bk", pos=1),
             _task("ip_top", step="ip", pos=0), _task("ip_next", step="ip", pos=1)]
    assert _order(tasks) == ["ip_top", "ip_next", "q_top", "q_next"]

def test_unknown_step_sorts_last():
    """A step that vanished from the workflow must not silently rank as the leftmost column."""
    assert _order([_task("ghost", step="deleted"), _task("real", step="bk")]) == ["real", "ghost"]

# --- launchability ---

# Sets, not single ids: with several workflows paced at once there is one queue step per
# board, and admission is per workflow. The fixture tasks below live in workflow "wf".
BOARD = Board(work_steps={"ip"}, queue_steps={"bk"}, admission={"wf": "ip"})

def test_is_launchable_work_step():
    booked = set()
    at_ip = {"id": "t", "workflow_step_id": "ip"}
    assert is_launchable(at_ip, [_sess("s", "WAITING_FOR_INPUT")], BOARD, booked)
    assert not is_launchable(at_ip, [_sess("s", "RUNNING")], BOARD, booked)   # in flight
    # A sessionless task on a work step is launchable, not stuck: kandev moves the card and
    # sets SCHEDULING before it validates the launch, so a refused admission strands it here
    # with no session. Requiring one made that state permanently unlaunchable -- the card was
    # skipped every tick while the log said "nothing to launch".
    assert is_launchable(at_ip, [], BOARD, booked)

def test_is_launchable_queue_admission():
    booked = set()
    at_bk = {"id": "t", "workflow_step_id": "bk"}
    assert is_launchable(at_bk, [], BOARD, booked)                            # new task
    assert is_launchable(at_bk, [_sess("s", "WAITING_FOR_INPUT")], BOARD, booked)  # re-queued
    assert not is_launchable(at_bk, [_sess("s", "RUNNING")], BOARD, booked)

def test_is_launchable_created_session():
    """Kandev prepares a session at task creation, so a never-run task arrives CREATED.

    That means "workspace prepared, agent never started" -- launchable, exactly like the
    no-session case. Missing this is why two Backlog tasks sat forever while the pacer
    reported "nothing to launch". intent:workflow_step handles it: kandev's
    ensureSessionRunning starts the agent on the already-prepared workspace.
    """
    booked = set()
    at_bk = {"id": "t", "workflow_step_id": "bk"}
    assert is_launchable(at_bk, [_sess("s", "CREATED")], BOARD, booked)
    # Same story one step over: a new task dragged straight to a work step is prepared,
    # not running, and would otherwise be ignored forever.
    at_ip = {"id": "t", "workflow_step_id": "ip"}
    assert is_launchable(at_ip, [_sess("s", "CREATED")], BOARD, booked)
    # The dup-guard still applies to it.
    assert not is_launchable(at_bk, [_sess("s", "CREATED")], BOARD, {("t", "bk", "s")})

def test_is_launchable_parked_and_booked():
    at_rv = {"id": "t", "workflow_step_id": "rv"}
    assert not is_launchable(at_rv, [_sess("s", "WAITING_FOR_INPUT")], BOARD, set())  # terminal
    at_ip = {"id": "t", "workflow_step_id": "ip"}
    booked = {("t", "ip", "s")}
    assert not is_launchable(at_ip, [_sess("s", "WAITING_FOR_INPUT")], BOARD, booked)  # dup-guard

def test_launch_target():
    at_ip = {"workflow_step_id": "ip", "workflow_id": "wf"}
    at_bk = {"workflow_step_id": "bk", "workflow_id": "wf"}
    assert launch_target(at_ip, BOARD) == "ip"    # in place
    assert launch_target(at_bk, BOARD) == "ip"    # admit to its own workflow's target

def test_launch_target_is_per_workflow():
    """Admission must resolve through the task's OWN workflow. One global target would drag a
    queued task onto another workflow's board."""
    two_boards = Board(work_steps={"ip"}, admission={"wf1": "ip1", "wf2": "ip2"})
    assert launch_target({"workflow_step_id": "bk", "workflow_id": "wf2"}, two_boards) == "ip2"
    # A task whose workflow has no admission target must yield None -- the caller skips it
    # rather than launching in place, which would start a turn on a parked queue step.
    assert launch_target({"workflow_step_id": "bk", "workflow_id": "wf3"}, two_boards) is None

# --- queued-prompt release ---

def test_is_drainable_parked_step():
    """The whole point: a prompt queued at Review/Done must be releasable.

    Those steps are not launchable, and kandev fires no event on them either, so without this
    a queued prompt there is never delivered at all.
    """
    at_rv = {"id": "t", "workflow_step_id": "rv"}
    assert is_drainable(at_rv, [_sess("s", "WAITING_FOR_INPUT")], {"s": 1}, set())
    at_dn = {"id": "t", "workflow_step_id": "dn"}
    assert is_drainable(at_dn, [_sess("s", "WAITING_FOR_INPUT")], {"s": 3}, set())

def test_is_drainable_requires_queue_and_idle_session():
    at_rv = {"id": "t", "workflow_step_id": "rv"}
    # empty queue
    assert not is_drainable(at_rv, [_sess("s", "WAITING_FOR_INPUT")], {}, set())
    # counted zero
    assert not is_drainable(at_rv, [_sess("s", "WAITING_FOR_INPUT")], {"s": 0}, set())
    # turn in flight
    assert not is_drainable(at_rv, [_sess("s", "RUNNING")], {"s": 1}, set())
    # no session
    assert not is_drainable(at_rv, [], {"s": 1}, set())
    # CREATED is prepared-but-never-run: launchable, but NOT promptable -- kandev answers
    # session_not_promptable, so its queued prompt must wait for the admission turn.
    assert not is_drainable(at_rv, [_sess("s", "CREATED")], {"s": 1}, set())
    # Counts are keyed by session, so a queue on an older session doesn't license a drain.
    # The queue lives on whichever session the user had open -- often the primary one, not
    # the newest. Looking only at the newest made this task read as having no prompt, so the
    # card sat parked while the pacer launched others.
    two = [_sess("old", "WAITING_FOR_INPUT", "1"), _sess("new", "WAITING_FOR_INPUT", "2")]
    assert is_drainable(at_rv, two, {"old": 1}, set())
    assert drain_session(at_rv, two, {"old": 1}, set())["id"] == "old"
    # Among idle sessions that do hold a queue, the newest wins.
    assert drain_session(at_rv, two, {"old": 1, "new": 1}, set())["id"] == "new"
    # Dup-guard, same shape as the launch path.
    assert not is_drainable(at_rv, [_sess("s", "WAITING_FOR_INPUT")], {"s": 1}, {("t", "rv", "s")})

def test_select_action_prefers_drain():
    """Draining first is what keeps a queued prompt to one turn.

    Launch the step turn instead and the entry survives it, then kandev's on-step-entry drain
    flushes it on arrival at the next step -- a second turn that never passed the gate.
    """
    at_ip = {"id": "t", "workflow_step_id": "ip", "created_at": "1"}
    sessions = {"t": [_sess("s", "WAITING_FOR_INPUT")]}
    # Same task is both launchable (work step, idle) and drainable (queue non-empty) -> drain.
    kind, chosen, _session = select_action(_ord([at_ip]), sessions, {"s": 1}, BOARD, set())
    assert (kind, chosen["id"]) == ("drain", "t")
    # Empty queue -> falls through to the launch path unchanged.
    assert select_action(_ord([at_ip]), sessions, {}, BOARD,  set())[0] == "launch"
    # Kind beats column: the drainable task sits in the LEFTMOST column, which pull ordering
    # would rank last, and it still wins over a launchable task further downstream.
    at_bk = {"id": "u", "workflow_step_id": "bk", "created_at": "9"}
    both = {"t": [_sess("a", "WAITING_FOR_INPUT")], "u": [_sess("b", "WAITING_FOR_INPUT")]}
    kind, chosen, _session = select_action(_ord([at_ip, at_bk]), both, {"b": 1}, BOARD, set())
    assert (kind, chosen["id"]) == ("drain", "u")

def test_select_action_uses_board_pull_order():
    """Within a kind, selection follows task_order_key -- not insertion or creation order."""
    old = {"id": "old", "workflow_step_id": "ip", "created_at": "1"}
    new = {"id": "new", "workflow_step_id": "ip", "created_at": "2"}
    sessions = {"old": [_sess("a", "WAITING_FOR_INPUT")], "new": [_sess("b", "WAITING_FOR_INPUT")]}
    # Same column and position -> falls through to created_at, so the older one goes first.
    assert select_action(_ord([new, old]), sessions, {}, BOARD,  set())[1]["id"] == "old"
    picked = select_action(_ord([new, old]), sessions, {"a": 1, "b": 1}, BOARD, set())
    assert picked[1]["id"] == "old"
    # A drag outranks age: position 0 wins even though it was created later.
    dragged = dict(new, position=0)
    aged = dict(old, position=1)
    assert select_action(_ord([aged, dragged]), sessions, {"a": 1, "b": 1},
                         BOARD, set())[1]["id"] == "new"
    # Rightmost column first: a queued prompt at Review beats one still in Backlog.
    at_rv = {"id": "rv1", "workflow_step_id": "rv", "created_at": "5"}
    at_bk = {"id": "bk1", "workflow_step_id": "bk", "created_at": "1"}
    both = {"rv1": [_sess("x", "WAITING_FOR_INPUT")], "bk1": [_sess("y", "WAITING_FOR_INPUT")]}
    assert select_action(_ord([at_bk, at_rv]), both, {"x": 1, "y": 1},
                         BOARD, set())[1]["id"] == "rv1"
    # Nothing to do at all.
    assert select_action([], {}, {}, BOARD, set()) is None
    # Parked, nothing queued -> not actionable (today's behaviour, unchanged).
    assert select_action([at_rv], {"rv1": [_sess("s", "WAITING_FOR_INPUT")]},
                         {}, BOARD, set()) is None

def test_pick_intent():
    assert pick_intent([]) == ("start", None)
    assert pick_intent([_sess("a", "WAITING_FOR_INPUT", "1"),
                        _sess("b", "WAITING_FOR_INPUT", "2")]) == ("workflow_step", "b")

def test_newest_session():
    assert newest_session([]) is None
    # Must sort on the real payload field. Keying on a field the API doesn't have makes
    # every key equal, so max() silently returns the FIRST session -- and the pacer then
    # hands a stale session_id to session.launch once a step spawns a second session.
    sessions = [_sess("a", "X", "1"), _sess("b", "X", "3"), _sess("c", "X", "2")]
    assert newest_session(sessions)["id"] == "b"

def test_prune_booked():
    now, ttl = 1000.0, 360.0
    tasks = [{"id": "t", "workflow_step_id": "ip"}]
    # still WAITING at same (step, session) -> booking retained (launch not yet taken effect)
    ts_waiting = {"t": [_sess("s", "WAITING_FOR_INPUT")]}
    booked = {("t", "ip", "s"): Booking(990.0)}
    assert prune_booked(booked, tasks, ts_waiting, now, ttl) == booked
    # turn started (RUNNING) -> booking cleared
    ts_running = {"t": [_sess("s", "RUNNING")]}
    assert prune_booked({("t", "ip", "s"): Booking(990.0)}, tasks, ts_running, now, ttl) == {}

def test_prune_booked_never_run_admission():
    """W1: a never-run admission booking (sid None) must survive while its session is invisible."""
    now, ttl = 1000.0, 360.0
    at_bk = [{"id": "t", "workflow_step_id": "bk"}]
    # session not yet surfaced (detached goroutine)
    no_sess = {"t": []}
    # within TTL -> retained, so is_launchable stays guarded and no second intent:start fires
    fresh = {("t", "bk", None): Booking(900.0)}
    assert prune_booked(fresh, at_bk, no_sess, now, ttl) == fresh
    # older than TTL -> expired, so a silently-failed launch can retry instead of wedging
    assert prune_booked({("t", "bk", None): Booking(500.0)}, at_bk, no_sess, now, ttl) == {}
    # session surfaced -> the None key no longer matches the task's key -> dropped naturally
    surfaced = {"t": [_sess("s2", "RUNNING")]}
    assert prune_booked({("t", "bk", None): Booking(900.0)}, at_bk, surfaced, now, ttl) == {}

def test_prune_booked_created_admission():
    """A CREATED booking must survive under TTL, or accepting CREATED double-launches.

    The launched session doesn't flip out of CREATED instantly, so on the next tick the
    task still reads (same step, same session, CREATED) and would look launchable again.
    Unlike the other states, nothing in the key changes when the launch silently fails --
    so the TTL, not the key, is what eventually lets it retry.
    """
    now, ttl = 1000.0, 360.0
    at_bk = [{"id": "t", "workflow_step_id": "bk"}]
    created = {"t": [_sess("s", "CREATED")]}
    fresh = {("t", "bk", "s"): Booking(900.0)}
    assert prune_booked(fresh, at_bk, created, now, ttl) == fresh
    assert prune_booked({("t", "bk", "s"): Booking(500.0)}, at_bk, created, now, ttl) == {}
    # Turn actually started -> state leaves CREATED -> guard no longer needed.
    running = {"t": [_sess("s", "RUNNING")]}
    assert prune_booked({("t", "bk", "s"): Booking(900.0)}, at_bk, running, now, ttl) == {}

# --- rejected launches: backoff and moving on ---

def _fail(at, attempts=1, flagged=False):
    return {"at": at, "attempts": attempts, "flagged": flagged}

def test_action_key_is_the_same_shape_as_a_booking():
    assert action_key({"id": "t", "workflow_step_id": "ip"},
                      [_sess("a", "WAITING_FOR_INPUT", "1"),
                       _sess("b", "WAITING_FOR_INPUT", "2")]) == ("t", "ip", "b")
    # No session at all is a real, distinct key -- that is the admission case.
    assert action_key({"id": "t"}, []) == ("t", "", None)

def test_fail_backoff_doubles_then_flattens(backoff_100):
    assert fail_backoff(1) == 100
    assert fail_backoff(2) == 200
    assert fail_backoff(3) == 400
    # Flattens instead of growing forever: a task that stays broken should keep costing an
    # occasional retry, not drift out to one attempt a week.
    assert fail_backoff(99) == 100 * 8

def test_backed_off_holds_a_task_only_until_its_wait_is_up(backoff_100):
    failures = {("t", "ip", "s"): _fail(1000.0)}
    assert backed_off(failures, 1050.0) == {("t", "ip", "s")}
    assert backed_off(failures, 1150.0) == set()
    # A second failure buys a longer wait, so a hopeless task stops costing round trips.
    assert backed_off({("t", "ip", "s"): _fail(1000.0, attempts=2)}, 1150.0)

def test_a_backed_off_task_stops_being_selected(backoff_100):
    """The wedge, in one assertion: the broken task IS the top candidate, every tick.

    Sorted oldest-first, a task kandev refuses to launch is picked again and again, so nothing
    else on the board ever gets the turn. Excluding it is what lets the next one through.
    """
    broken = {"id": "b", "workflow_step_id": "ip", "created_at": "1"}
    ok = {"id": "g", "workflow_step_id": "ip", "created_at": "2"}
    sessions = {"b": [_sess("s1", "CREATED")], "g": [_sess("s2", "WAITING_FOR_INPUT")]}
    assert select_action(_ord([broken, ok]), sessions, {}, BOARD,  set())[1]["id"] == "b"
    held = backed_off({("b", "ip", "s1"): _fail(1000.0)}, 1050.0)
    assert select_action(_ord([broken, ok]), sessions, {}, BOARD,  held)[1]["id"] == "g"

def test_prune_failures_keeps_the_count_while_the_situation_holds():
    tasks = [{"id": "t", "workflow_step_id": "ip"}]
    sessions = {"t": [_sess("s", "CREATED")]}
    failures = {("t", "ip", "s"): _fail(1000.0, attempts=3, flagged=True)}
    # Nothing changed -> keep it, attempt count and all. Expiring it would reset the backoff
    # to its shortest wait and retry a hopeless task at that pace forever.
    assert prune_failures(failures, tasks, sessions) == failures
    # The task moved, a new session appeared, the task is gone: each is a new situation that
    # deserves a fresh attempt, and each stops the key from matching.
    assert prune_failures(failures, [{"id": "t", "workflow_step_id": "rv"}], sessions) == {}
    assert prune_failures(failures, tasks, {"t": [_sess("s2", "CREATED", "9")]}) == {}
    assert prune_failures(failures, [], {}) == {}

# --- launch order from the sidebar TASKS list ---

def _settings(sort_key="custom", ordered=(), pinned=(), sub=None, draft=None, views=None):
    s = {"sidebar_views": views or [{"id": "v1", "name": "All",
                                     "sort": {"key": sort_key, "direction": "asc"},
                                     "group": "none"}],
         "sidebar_active_view_id": "v1",
         "sidebar_task_prefs": {"pinned_task_ids": list(pinned),
                                "ordered_task_ids": list(ordered),
                                "subtask_order_by_parent_id": sub or {}}}
    if draft:
        s["sidebar_draft"] = draft
    return s

def test_effective_sidebar_view_prefers_draft():
    """A drag flips the sort to custom in the DRAFT first -- reading only the saved view
    would miss exactly the state the user is looking at."""
    s = _settings(sort_key="updatedAt", draft={"base_view_id": "v1", "filters": [],
                                               "sort": {"key": "custom", "direction": "asc"},
                                               "group": "none"})
    assert effective_sidebar_view(s)["sort"]["key"] == "custom"
    assert has_custom_sort_view(s)
    # A draft for some other view doesn't apply.
    stale = _settings(sort_key="updatedAt", draft={"base_view_id": "other", "filters": [],
                                                   "sort": {"key": "custom", "direction": "asc"},
                                                   "group": "none"})
    assert not has_custom_sort_view(stale)

def test_effective_sidebar_view_fallbacks():
    assert effective_sidebar_view({}) is None
    assert not has_custom_sort_view({})
    # Unknown active id falls back to the first view, like the frontend does.
    s = _settings()
    s["sidebar_active_view_id"] = "gone"
    assert effective_sidebar_view(s)["id"] == "v1"

def test_has_custom_sort_view_is_not_tied_to_the_active_view():
    """The manual order is stored per USER, not per view, so a saved 'Prioritize' view still
    expresses the intended order while the user works in some other view. Keying on the
    active view would flip the pacer back to FIFO on an unrelated view switch."""
    s = _settings(views=[{"id": "v1", "sort": {"key": "state", "direction": "asc"},
                          "group": "repository"},
                         {"id": "v2", "name": "Prioritize",
                          "sort": {"key": "custom", "direction": "asc"}, "group": "none"}])
    assert effective_sidebar_view(s)["id"] == "v1"   # active view is NOT the custom one
    assert has_custom_sort_view(s)
    # No custom view anywhere -> no manual ordering.
    assert not has_custom_sort_view(_settings(sort_key="state"))

def test_visible_task_order_manual_first():
    tasks = [_task("a"), _task("b"), _task("c")]
    assert [t["id"] for t in visible_task_order(tasks, [], ["c", "a"], {})] == ["c", "a", "b"]

def test_visible_task_order_keeps_unplaced_behind_in_fifo():
    """"Not placed" means "not prioritized": those tasks keep the FIFO order they came in
    with, behind everything the user did place. Copying the sidebar's own newest-first
    fallback here would let merely creating a task jump a deliberately-set order."""
    fifo = [
        _task("old", created="2026-01-01T00:00:00Z"),
        _task("new", created="2026-02-01T00:00:00Z"),
        _task("placed", created="2026-03-01T00:00:00Z"),
    ]
    assert [t["id"] for t in visible_task_order(fifo, [], ["placed"], {})] == [
        "placed",
        "old",
        "new",
    ]


def test_visible_task_order_pinned_float_to_top():
    tasks = [_task("a"), _task("b"), _task("c")]
    order = [t["id"] for t in visible_task_order(tasks, ["c"], ["a", "b", "c"], {})]
    assert order == ["c", "a", "b"]

def test_visible_task_order_subtasks_follow_parent():
    tasks = [_task("p1"), _task("p2"), _task("s2", parent="p1"), _task("s1", parent="p1")]
    order = [t["id"] for t in visible_task_order(tasks, [], ["p2", "p1"], {"p1": ["s1", "s2"]})]
    assert order == ["p2", "p1", "s1", "s2"]

def test_visible_task_order_orphans_and_cycles_still_appear():
    # Parent outside the paced workflow -> the task is a root here, not dropped.
    outside = [_task("t", parent="elsewhere")]
    assert [t["id"] for t in visible_task_order(outside, [], [], {})] == ["t"]
    # A parent cycle is unreachable from any root; those tasks must still be launchable, last.
    cyc = [_task("x", parent="y"), _task("y", parent="x"), _task("r")]
    assert sorted(t["id"] for t in visible_task_order(cyc, [], ["r"], {})) == ["r", "x", "y"]
    assert visible_task_order(cyc, [], ["r"], {})[0]["id"] == "r"

def test_sidebar_order_reaches_drain_selection():
    """The overlay must order drains, not just launches. select_action takes a pre-ordered list
    precisely so one ordering governs both; sorting inside it again would ignore the sidebar."""
    a = _task("a", step="ip")
    b = _task("b", step="ip")
    sessions = {"a": [_sess("sa", "WAITING_FOR_INPUT")], "b": [_sess("sb", "WAITING_FOR_INPUT")]}
    queued = {"sa": 1, "sb": 1}                      # both have a prompt parked
    dragged = _settings(ordered=["b"])               # user put b on top
    ordered = ordered_tasks([a, b], dragged, RANKS)
    kind, chosen, _session = select_action(ordered, sessions, queued, BOARD, set())
    assert (kind, chosen["id"]) == ("drain", "b")
    # Without the drag, pull order decides (equal here -> id tiebreak keeps it total).
    kind, chosen, _session = select_action(_ord([a, b]), sessions, queued, BOARD, set())
    assert (kind, chosen["id"]) == ("drain", "a")

def test_ordered_tasks_layers_manual_over_board_pull_order():
    """The two orderings compose: hand-placed tasks first in the user's order, everything
    else behind them in the board's pull order (downstream column first, then position).

    Neither branch could cover this alone -- it is the merge of the sidebar drag order with
    the kanban pull order, and a textual merge that kept only one of them would still pass
    both original suites.
    """
    ranks = {"bk": 0, "ip": 1}          # In Progress is downstream of Backlog
    queued_a = _task("qa", step="bk", pos=0)
    queued_b = _task("qb", step="bk", pos=1)
    in_flight = _task("wip", step="ip", pos=0)
    tasks = [queued_a, queued_b, in_flight]
    # Nothing placed by hand -> pure pull order: the in-flight column drains first.
    ordered = ordered_tasks(tasks, _settings(), ranks)
    assert [t["id"] for t in ordered] == ["wip", "qa", "qb"]
    # Dragging a queued task to the top outranks the pull preference -- an explicit
    # instruction beats a heuristic, and the unplaced rest keeps pull order behind it.
    dragged = _settings(ordered=["qb"])
    assert [t["id"] for t in ordered_tasks(tasks, dragged, ranks)] == ["qb", "wip", "qa"]

def test_ordered_tasks_follows_the_drag_only_when_a_view_sorts_by_hand():
    # Passed in newest-first to prove the sidebar, not the input order, decides the result.
    tasks = [
        _task("new", created="2026-02-01T00:00:00Z"),
        _task("old", created="2026-01-01T00:00:00Z"),
    ]
    fifo = ["old", "new"]  # equal step/position/priority -> created_at decides
    manual = _settings(ordered=["new", "old"])
    assert [t["id"] for t in ordered_tasks(tasks, manual)] == ["new", "old"]
    # A view sorted by something else is not a drag order, so the board decides.
    other = _settings(sort_key="updatedAt", ordered=["new", "old"])
    assert [t["id"] for t in ordered_tasks(tasks, other)] == fifo
    # Settings unreadable, or nothing dragged yet -> FIFO, never an arbitrary order.
    assert [t["id"] for t in ordered_tasks(tasks, None)] == fifo
    assert [t["id"] for t in ordered_tasks(tasks, {})] == fifo
    assert [t["id"] for t in ordered_tasks(tasks, _settings())] == fifo


def test_task_agent_profile_reads_launch_metadata():
    """intent:start is refused outright without one, and kandev will not look it up itself."""
    assert task_agent_profile({"metadata": {"agent_profile_id": "prof-1"}}) == "prof-1"
    # Nothing usable -> None, and the pacer never invents a profile for a task without one.
    assert task_agent_profile({}) is None
    assert task_agent_profile({"metadata": {}}) is None
    assert task_agent_profile({"metadata": {"agent_profile_id": ""}}) is None
    assert task_agent_profile({"metadata": {"agent_profile_id": 7}}) is None
