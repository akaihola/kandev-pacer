#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Pure pacing + classification tests (no network). Run: uv run test_pace.py"""
from pacer import (
    clamp01, elapsed_frac, headroom, worst_headroom, gate_open, ceiling_breached,
    parse_windows, usage_providers, provider_missing,
    is_work_step, is_paced_step, paced_step_ids, queue_step_id, admission_target, resolve_scope,
    lint_workflow, newest_session, is_launchable, launch_target, pick_intent, prune_booked,
    step_names, named, step_ranks, priority_rank, task_order_key,
    effective_sidebar_view, has_custom_sort_view, visible_task_order, ordered_tasks,
    is_drainable, select_action,
)

# --- pacing math (unchanged) ---

def test_elapsed():
    now = 1000.0
    assert elapsed_frac(now, resets_at=now + 3600, duration_s=3600) == 0.0   # just reset
    assert elapsed_frac(now, resets_at=now + 1800, duration_s=3600) == 0.5   # halfway
    assert elapsed_frac(now, resets_at=now, duration_s=3600) == 1.0          # at reset
    assert clamp01(elapsed_frac(now, resets_at=now - 100, duration_s=3600)) == 1.0

def test_steady_burn_never_pauses():
    for e in (0.1, 0.5, 0.9):
        assert headroom(consumed_frac=e, elapsed_frac_=e) == 0.0
        assert gate_open(headroom(e, e), was_open=True)

def test_overspend_pauses():
    assert headroom(0.8, 0.25) < 0
    assert not gate_open(headroom(0.8, 0.25), was_open=True)

def test_underspend_open():
    assert headroom(0.1, 0.5) > 0
    assert gate_open(headroom(0.1, 0.5), was_open=False)

def test_self_heal():
    frozen = 0.8
    assert not gate_open(headroom(frozen, 0.5), was_open=False)   # still behind
    assert gate_open(headroom(frozen, 0.85), was_open=False)      # time caught up -> open

def test_worst_window_wins():
    now = 0.0
    windows = [
        {"consumed_frac": 0.2, "resets_at": 1800, "duration_s": 3600},  # e=0.5, h=+0.3
        {"consumed_frac": 0.9, "resets_at": 1800, "duration_s": 3600},  # e=0.5, h=-0.4  <- worst
    ]
    assert worst_headroom(windows, now) == -0.4
    assert not gate_open(worst_headroom(windows, now), was_open=True)

def test_no_windows_open():
    assert worst_headroom([], now=0.0) == 1.0

def test_alpha_reserve():
    assert headroom(0.48, 0.5, alpha=0.9) < 0
    assert headroom(0.48, 0.5, alpha=1.0) > 0

def test_ceiling_breached():
    assert ceiling_breached([{"consumed_frac": 0.98}], 0.97)        # over cutoff
    assert not ceiling_breached([{"consumed_frac": 0.5}], 0.97)     # under
    assert not ceiling_breached([{"consumed_frac": 0.99}], 1.0)     # >=1.0 disables
    assert not ceiling_breached([], 0.97)

# --- usage parsing (real payload shape) ---

USAGE = {"agents": [
    {"agent_id": "claude-acp", "usage": {"provider": "anthropic", "windows": [
        {"label": "5-hour", "utilization_pct": 9, "reset_at": "2026-07-23T19:39:59.5Z"},
        {"label": "7-day", "utilization_pct": 21, "reset_at": "2026-07-27T12:59:59.5Z"},
        {"label": "7-day (Fable)", "utilization_pct": 9, "reset_at": "2026-07-27T12:59:59.5Z"}]}},
    {"agent_id": "codex-acp", "usage": {"provider": "openai", "windows": [
        {"label": "7-day", "utilization_pct": 10, "reset_at": "2026-07-29T18:51:33+03:00"}]}}]}

def test_parse_windows_real_shape():
    ws = parse_windows(USAGE, provider="anthropic")
    assert len(ws) == 3                                   # all 3 anthropic windows, codex dropped
    assert [round(w["consumed_frac"], 2) for w in ws] == [0.09, 0.21, 0.09]
    assert [w["duration_s"] for w in ws] == [18000, 604800, 604800]

def test_parse_windows_provider_filter():
    assert len(parse_windows(USAGE, provider="openai")) == 1   # only codex survives
    assert parse_windows(USAGE, provider="nobody") == []

def test_parse_windows_skips_unknown_label():
    u = {"agents": [{"usage": {"provider": "anthropic", "windows": [
        {"label": "1-minute", "utilization_pct": 5, "reset_at": "2026-07-23T19:39:59Z"}]}}]}
    assert parse_windows(u, provider="anthropic") == []

def test_provider_guard():
    """A PACER_PROVIDER that matches nothing must be caught, not silently fail open."""
    assert usage_providers(USAGE) == {"anthropic", "openai"}
    assert usage_providers({"agents": []}) == set()
    assert provider_missing(USAGE, "claude")            # typo -> present providers, ours absent -> guard fires
    assert not provider_missing(USAGE, "anthropic")     # ours is present -> fine
    assert not provider_missing({"agents": []}, "anthropic")  # empty payload -> can't tell -> don't withhold

# --- workflow step fixtures ---

def _step(id, name, otc=None, oe=None, ots=None, start=False, stage="custom",
          pos=0, profile="", prompt="", signal=False):
    events = {}
    if oe:
        events["on_enter"] = [{"type": k} for k in oe]
    if otc:
        events["on_turn_complete"] = [{"type": k} for k in otc]
    if ots:
        events["on_turn_start"] = [{"type": k} for k in ots]
    return {"id": id, "name": name, "events": events, "is_start_step": start,
            "stage_type": stage, "position": pos, "agent_profile_id": profile,
            "prompt": prompt, "auto_advance_requires_signal": signal}

# Enrolled (post-make_paced) Paced Kanban: In Progress routes away, tagged work, no auto-start.
CLEAN = [
    _step("bk", "Backlog", start=True, pos=0),
    _step("ip", "In Progress", otc=["move_to_step"], stage="work", pos=1),
    _step("rv", "Review", ots=["move_to_step"], stage="review", pos=2),
    _step("dn", "Done", ots=["move_to_step"], pos=3),
]

def test_paced_step_ids():
    assert paced_step_ids(CLEAN) == {"ip"}                      # only In Progress routes on complete
    assert is_work_step(_step("x", "X", otc=["move_to_next"]))      # every transition kind counts
    assert is_work_step(_step("x", "X", otc=["move_to_previous"]))
    assert is_work_step(_step("x", "X", otc=["move_to_step"]))
    assert not is_work_step(_step("x", "X", ots=["move_to_step"]))  # on_turn_start is not routing-away

def test_is_paced_step_needs_both_halves():
    """The tag is the enrollment marker; the routing is the safety property. Neither alone.

    Tag without routing would relaunch the same task every tick forever (the step never routes
    away, so it reads idle after each turn) -- an infinite prompt loop billed to the quota.
    Routing without the tag is an un-enrolled workflow the pacer merely discovered.
    """
    assert is_paced_step(_step("x", "X", otc=["move_to_next"], stage="work"))
    assert not is_paced_step(_step("x", "X", stage="work"))                    # tagged, parked
    assert not is_paced_step(_step("x", "X", otc=["move_to_next"]))            # routes, untagged
    assert not is_paced_step(_step("x", "X", otc=["move_to_next"], stage="review"))
    assert is_paced_step(_step("x", "X", otc=["move_to_next"], stage="WORK"))  # tag is case-insensitive

def test_step_names():
    names = step_names(CLEAN)
    assert names["ip"] == "In Progress"
    assert named("ip", names) == "In Progress"
    assert named("gone", names) == "gone"            # unknown id stays identifiable in the log
    assert named(None, names) is None                # no queue/admission step -> prints None
    assert step_names([{"id": "x"}])["x"] == "x"     # nameless step falls back to its id

# The three live workflows on gogo, which between them cover every scope case.
# Feature Dev: enrolled, several work steps. Priority Kanban: never run through make_paced --
# its Backlog even routes on turn complete, so routing alone would wrongly pace a parked queue.
FEATURE_DEV = [
    _step("fd-todo", "Todo", start=True, pos=0),
    _step("fd-spec", "Spec", otc=["move_to_next"], stage="work", pos=1),
    _step("fd-work", "Work", otc=["move_to_next"], stage="work", pos=2),
    _step("fd-done", "Done", pos=3),
]
PRIORITY_KANBAN = [
    _step("pk-bk", "Backlog", otc=["move_to_step"], ots=["move_to_next"], pos=0),
    _step("pk-ip", "In Progress", oe=["auto_start_agent"], otc=["move_to_step"], start=True, pos=1),
    _step("pk-dn", "Done", ots=["move_to_step"], pos=2),
]
ENTRIES = [({"id": "wf-pk", "name": "Priority Kanban"}, PRIORITY_KANBAN),
           ({"id": "wf-ck", "name": "Paced Kanban"}, CLEAN),
           ({"id": "wf-fd", "name": "Feature Dev"}, FEATURE_DEV)]

def test_resolve_scope_enrolls_only_tagged_workflows():
    scope, work, queues, admission, skipped = resolve_scope(ENTRIES)
    assert scope == ["wf-ck", "wf-fd"]                 # Priority Kanban is not enrolled
    assert work == {"ip", "fd-spec", "fd-work"}        # unioned; step ids are globally unique
    assert queues == {"bk", "fd-todo"}                 # one start step per scoped workflow
    assert admission == {"wf-ck": "ip", "wf-fd": "fd-spec"}   # lowest-position paced step, per board
    assert [wf["name"] for wf, _ in skipped] == ["Priority Kanban"]
    assert "make_paced" in skipped[0][1]               # the reason is actionable, not just a drop

def test_resolve_scope_never_paces_an_untagged_queue():
    """Priority Kanban's Backlog routes on turn complete, so the routing-only classifier would
    treat that parked queue as a work step and start turns on tasks merely sitting there."""
    _, work, _, _, _ = resolve_scope(ENTRIES)
    assert "pk-bk" not in work
    assert "pk-ip" not in work

def test_resolve_scope_restrict_by_id_or_name():
    scope, _, _, _, skipped = resolve_scope(ENTRIES, restrict=["Feature Dev"])
    assert scope == ["wf-fd"]                          # matched by name, case-insensitively
    assert {wf["name"] for wf, _ in skipped} == {"Priority Kanban", "Paced Kanban"}
    assert resolve_scope(ENTRIES, restrict=["wf-ck"])[0] == ["wf-ck"]          # by id
    assert resolve_scope(ENTRIES, restrict=["feature dev"])[0] == ["wf-fd"]    # case-insensitive
    # A restriction naming an unenrolled workflow still yields nothing to pace.
    assert resolve_scope(ENTRIES, restrict=["Priority Kanban"])[0] == []

def test_resolve_scope_override_applies_only_to_its_own_workflow():
    """An override names a step in one workflow; applying it everywhere would admit other
    workflows' tasks into a step they don't own."""
    _, _, _, admission, _ = resolve_scope(ENTRIES, override="fd-work")
    assert admission == {"wf-ck": "ip", "wf-fd": "fd-work"}   # Paced Kanban keeps its own target

def test_queue_and_admission_target():
    assert queue_step_id(CLEAN) == "bk"
    assert admission_target(CLEAN) == "ip"                      # lowest-position paced step
    assert admission_target([_step("a", "A", otc=["move_to_next"], stage="work", pos=5),
                             _step("b", "B", otc=["move_to_next"], stage="work", pos=2)]) == "b"
    assert admission_target(CLEAN, override="forced") == "forced"
    assert admission_target([_step("bk", "Backlog", start=True)]) is None  # no paced step
    # Work-shaped but untagged is not an admission target either -- admitting a task into an
    # un-enrolled step would start turns the pacer then cannot track as paced work.
    assert admission_target([_step("a", "A", otc=["move_to_next"])]) is None

# --- launch ordering ---

RANKS = step_ranks(CLEAN)   # bk=0, ip=1, rv=2, dn=3

def _task(id, step="ip", pos=0, prio="medium", created="2026-01-01T00:00:00Z", parent=None):
    t = {"id": id, "workflow_step_id": step, "position": pos,
         "priority": prio, "created_at": created}
    if parent:
        t["parent_id"] = parent
    return t

def _ord(tasks):
    """Fixtures in launch-priority order, board mode -- select_action no longer sorts itself,
    so route them through the real ordering pipeline rather than hand-sorting here."""
    return ordered_tasks(tasks, None, "board", RANKS)

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
    assert priority_rank({}) == priority_rank({"priority": None}) == priority_rank({"priority": "none"})

def test_order_by_position_within_a_step():
    """The headline rule: the board's manual drag order decides, lowest position first."""
    assert _order([_task("c", pos=2), _task("a", pos=0), _task("b", pos=1)]) == ["a", "b", "c"]

def test_position_beats_priority_and_age():
    """position outranks both, exactly as in kandev's pull query -- a drag is an explicit decision."""
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
    same = dict(pos=0, prio="medium", created="2026-01-01T00:00:00Z")
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
    assert _order([_task("queued", step="bk", pos=0, prio="critical", created="2020-01-01T00:00:00Z"),
                   _task("in_flight", step="ip", pos=9, prio="low")]) == ["in_flight", "queued"]

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

def _sess(id, state, started="1"):
    # `started_at` is the real field name in GET /tasks/:id/sessions -- there is no `created_at`.
    return {"id": id, "state": state, "started_at": started}

WORK = {"ip"}
# Sets, not single ids: with several workflows paced at once there is one queue step per board.
QUEUE = {"bk"}
# workflow id -> admission step. The fixture tasks below live in workflow "wf".
ADMIT = {"wf": "ip"}

def test_is_launchable_work_step():
    booked = set()
    at_ip = {"id": "t", "workflow_step_id": "ip"}
    assert is_launchable(at_ip, [_sess("s", "WAITING_FOR_INPUT")], WORK, QUEUE,booked)
    assert not is_launchable(at_ip, [_sess("s", "RUNNING")], WORK, QUEUE,booked)   # in flight
    assert not is_launchable(at_ip, [], WORK, QUEUE,booked)                        # no session

def test_is_launchable_queue_admission():
    booked = set()
    at_bk = {"id": "t", "workflow_step_id": "bk"}
    assert is_launchable(at_bk, [], WORK, QUEUE,booked)                            # new task
    assert is_launchable(at_bk, [_sess("s", "WAITING_FOR_INPUT")], WORK, QUEUE,booked)  # re-queued
    assert not is_launchable(at_bk, [_sess("s", "RUNNING")], WORK, QUEUE,booked)

def test_is_launchable_created_session():
    """Kandev prepares a session at task creation, so a never-run task arrives CREATED.

    That means "workspace prepared, agent never started" -- launchable, exactly like the
    no-session case. Missing this is why two Backlog tasks sat forever while the pacer
    reported "nothing to launch". intent:workflow_step handles it: kandev's
    ensureSessionRunning starts the agent on the already-prepared workspace.
    """
    booked = set()
    at_bk = {"id": "t", "workflow_step_id": "bk"}
    assert is_launchable(at_bk, [_sess("s", "CREATED")], WORK, QUEUE,booked)
    # Same story one step over: a new task dragged straight to a work step is prepared,
    # not running, and would otherwise be ignored forever.
    at_ip = {"id": "t", "workflow_step_id": "ip"}
    assert is_launchable(at_ip, [_sess("s", "CREATED")], WORK, QUEUE,booked)
    # The dup-guard still applies to it.
    assert not is_launchable(at_bk, [_sess("s", "CREATED")], WORK, QUEUE,{("t", "bk", "s")})

def test_is_launchable_parked_and_booked():
    at_rv = {"id": "t", "workflow_step_id": "rv"}
    assert not is_launchable(at_rv, [_sess("s", "WAITING_FOR_INPUT")], WORK, QUEUE,set())  # terminal
    at_ip = {"id": "t", "workflow_step_id": "ip"}
    booked = {("t", "ip", "s")}
    assert not is_launchable(at_ip, [_sess("s", "WAITING_FOR_INPUT")], WORK, QUEUE,booked)  # dup-guard

def test_launch_target():
    at_ip = {"workflow_step_id": "ip", "workflow_id": "wf"}
    at_bk = {"workflow_step_id": "bk", "workflow_id": "wf"}
    assert launch_target(at_ip, WORK, ADMIT) == "ip"    # in place
    assert launch_target(at_bk, WORK, ADMIT) == "ip"    # admit to its own workflow's target

def test_launch_target_is_per_workflow():
    """Admission must resolve through the task's OWN workflow. One global target would drag a
    queued task onto another workflow's board."""
    admit = {"wf1": "ip1", "wf2": "ip2"}
    assert launch_target({"workflow_step_id": "bk", "workflow_id": "wf2"}, WORK, admit) == "ip2"
    # A task whose workflow has no admission target must yield None -- the caller skips it
    # rather than launching in place, which would start a turn on a parked queue step.
    assert launch_target({"workflow_step_id": "bk", "workflow_id": "wf3"}, WORK, admit) is None

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
    assert not is_drainable(at_rv, [_sess("s", "WAITING_FOR_INPUT")], {}, set())          # empty queue
    assert not is_drainable(at_rv, [_sess("s", "WAITING_FOR_INPUT")], {"s": 0}, set())    # counted zero
    assert not is_drainable(at_rv, [_sess("s", "RUNNING")], {"s": 1}, set())              # turn in flight
    assert not is_drainable(at_rv, [], {"s": 1}, set())                                   # no session
    # CREATED is prepared-but-never-run: launchable, but NOT promptable -- kandev answers
    # session_not_promptable, so its queued prompt must wait for the admission turn.
    assert not is_drainable(at_rv, [_sess("s", "CREATED")], {"s": 1}, set())
    # Counts are keyed by session, so a queue on an older session doesn't license a drain.
    assert not is_drainable(at_rv, [_sess("old", "WAITING_FOR_INPUT", "1"),
                                    _sess("new", "WAITING_FOR_INPUT", "2")], {"old": 1}, set())
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
    kind, chosen = select_action(_ord([at_ip]), sessions, {"s": 1}, WORK, QUEUE, set())
    assert (kind, chosen["id"]) == ("drain", "t")
    # Empty queue -> falls through to the launch path unchanged.
    assert select_action(_ord([at_ip]), sessions, {}, WORK, QUEUE, set())[0] == "launch"
    # Kind beats column: the drainable task sits in the LEFTMOST column, which pull ordering
    # would rank last, and it still wins over a launchable task further downstream.
    at_bk = {"id": "u", "workflow_step_id": "bk", "created_at": "9"}
    kind, chosen = select_action(_ord([at_ip, at_bk]), {"t": [_sess("a", "WAITING_FOR_INPUT")],
                                                 "u": [_sess("b", "WAITING_FOR_INPUT")]},
                                {"b": 1}, WORK, QUEUE, set())
    assert (kind, chosen["id"]) == ("drain", "u")

def test_select_action_uses_board_pull_order():
    """Within a kind, selection follows task_order_key -- not insertion or creation order."""
    old = {"id": "old", "workflow_step_id": "ip", "created_at": "1"}
    new = {"id": "new", "workflow_step_id": "ip", "created_at": "2"}
    sessions = {"old": [_sess("a", "WAITING_FOR_INPUT")], "new": [_sess("b", "WAITING_FOR_INPUT")]}
    # Same column and position -> falls through to created_at, so the older one goes first.
    assert select_action(_ord([new, old]), sessions, {}, WORK, QUEUE, set())[1]["id"] == "old"
    assert select_action(_ord([new, old]), sessions, {"a": 1, "b": 1}, WORK, QUEUE, set())[1]["id"] == "old"
    # A drag outranks age: position 0 wins even though it was created later.
    dragged = dict(new, position=0)
    aged = dict(old, position=1)
    assert select_action(_ord([aged, dragged]), sessions, {"a": 1, "b": 1},
                         WORK, QUEUE, set())[1]["id"] == "new"
    # Rightmost column first: a queued prompt at Review beats one still in Backlog.
    at_rv = {"id": "rv1", "workflow_step_id": "rv", "created_at": "5"}
    at_bk = {"id": "bk1", "workflow_step_id": "bk", "created_at": "1"}
    both = {"rv1": [_sess("x", "WAITING_FOR_INPUT")], "bk1": [_sess("y", "WAITING_FOR_INPUT")]}
    assert select_action(_ord([at_bk, at_rv]), both, {"x": 1, "y": 1},
                         WORK, QUEUE, set())[1]["id"] == "rv1"
    # Nothing to do at all.
    assert select_action([], {}, {}, WORK, QUEUE, set()) == (None, None)
    # Parked, nothing queued -> not actionable (today's behaviour, unchanged).
    assert select_action([at_rv], {"rv1": [_sess("s", "WAITING_FOR_INPUT")]},
                         {}, WORK, QUEUE, set()) == (None, None)

def test_pick_intent():
    assert pick_intent([]) == ("start", None)
    assert pick_intent([_sess("a", "WAITING_FOR_INPUT", "1"),
                        _sess("b", "WAITING_FOR_INPUT", "2")]) == ("workflow_step", "b")

def test_newest_session():
    assert newest_session([]) is None
    # Must sort on the real payload field. Keying on a field the API doesn't have makes
    # every key equal, so max() silently returns the FIRST session -- and the pacer then
    # hands a stale session_id to session.launch once a step spawns a second session.
    assert newest_session([_sess("a", "X", "1"), _sess("b", "X", "3"), _sess("c", "X", "2")])["id"] == "b"

def test_prune_booked():
    now, ttl = 1000.0, 360.0
    tasks = [{"id": "t", "workflow_step_id": "ip"}]
    # still WAITING at same (step, session) -> booking retained (launch not yet taken effect)
    ts_waiting = {"t": [_sess("s", "WAITING_FOR_INPUT")]}
    assert prune_booked({("t", "ip", "s"): 990.0}, tasks, ts_waiting, now, ttl) == {("t", "ip", "s"): 990.0}
    # turn started (RUNNING) -> booking cleared
    ts_running = {"t": [_sess("s", "RUNNING")]}
    assert prune_booked({("t", "ip", "s"): 990.0}, tasks, ts_running, now, ttl) == {}

def test_prune_booked_never_run_admission():
    """W1: a never-run admission booking (sid None) must survive while its session is invisible."""
    now, ttl = 1000.0, 360.0
    at_bk = [{"id": "t", "workflow_step_id": "bk"}]
    no_sess = {"t": []}                                     # session not yet surfaced (detached goroutine)
    # within TTL -> retained, so is_launchable stays guarded and no second intent:start fires
    assert prune_booked({("t", "bk", None): 900.0}, at_bk, no_sess, now, ttl) == {("t", "bk", None): 900.0}
    # older than TTL -> expired, so a silently-failed launch can retry instead of wedging
    assert prune_booked({("t", "bk", None): 500.0}, at_bk, no_sess, now, ttl) == {}
    # session surfaced -> the None key no longer matches the task's key -> dropped naturally
    surfaced = {"t": [_sess("s2", "RUNNING")]}
    assert prune_booked({("t", "bk", None): 900.0}, at_bk, surfaced, now, ttl) == {}

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
    assert prune_booked({("t", "bk", "s"): 900.0}, at_bk, created, now, ttl) == {("t", "bk", "s"): 900.0}
    assert prune_booked({("t", "bk", "s"): 500.0}, at_bk, created, now, ttl) == {}
    # Turn actually started -> state leaves CREATED -> guard no longer needed.
    running = {"t": [_sess("s", "RUNNING")]}
    assert prune_booked({("t", "bk", "s"): 900.0}, at_bk, running, now, ttl) == {}

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
    fifo = [_task("old", created="2026-01-01T00:00:00Z"), _task("new", created="2026-02-01T00:00:00Z"),
            _task("placed", created="2026-03-01T00:00:00Z")]
    assert [t["id"] for t in visible_task_order(fifo, [], ["placed"], {})] == \
        ["placed", "old", "new"]

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
    ordered = ordered_tasks([a, b], dragged, "auto", RANKS)
    kind, chosen = select_action(ordered, sessions, queued, WORK, QUEUE, set())
    assert (kind, chosen["id"]) == ("drain", "b")
    # Without the drag, pull order decides (equal here -> id tiebreak keeps it total).
    kind, chosen = select_action(_ord([a, b]), sessions, queued, WORK, QUEUE, set())
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
    assert [t["id"] for t in ordered_tasks(tasks, _settings(), "auto", ranks)] == ["wip", "qa", "qb"]
    # Dragging a queued task to the top outranks the pull preference -- an explicit
    # instruction beats a heuristic, and the unplaced rest keeps pull order behind it.
    dragged = _settings(ordered=["qb"])
    assert [t["id"] for t in ordered_tasks(tasks, dragged, "auto", ranks)] == ["qb", "wip", "qa"]
    # PACER_ORDER=board ignores the drag entirely and restores strict pull order.
    assert [t["id"] for t in ordered_tasks(tasks, dragged, "board", ranks)] == ["wip", "qa", "qb"]

def test_ordered_tasks_modes():
    # Passed in newest-first to prove the mode, not the input order, decides the result.
    tasks = [_task("new", created="2026-02-01T00:00:00Z"), _task("old", created="2026-01-01T00:00:00Z")]
    fifo = ["old", "new"]   # equal step/position/priority -> created_at decides
    manual = _settings(ordered=["new", "old"])
    assert [t["id"] for t in ordered_tasks(tasks, manual, "auto")] == ["new", "old"]
    assert [t["id"] for t in ordered_tasks(tasks, manual, "custom")] == ["new", "old"]
    assert [t["id"] for t in ordered_tasks(tasks, manual, "board")] == fifo
    # auto follows the list only once a custom-sorted view exists...
    other = _settings(sort_key="updatedAt", ordered=["new", "old"])
    assert [t["id"] for t in ordered_tasks(tasks, other, "auto")] == fifo
    # ...whereas 'custom' is the explicit opt-in and ignores the sidebar's sort entirely.
    assert [t["id"] for t in ordered_tasks(tasks, other, "custom")] == ["new", "old"]
    # Settings unreadable, or nothing dragged yet -> FIFO, never an arbitrary order.
    assert [t["id"] for t in ordered_tasks(tasks, None, "auto")] == fifo
    assert [t["id"] for t in ordered_tasks(tasks, {}, "custom")] == fifo
    assert [t["id"] for t in ordered_tasks(tasks, _settings(), "auto")] == fifo

# --- lint ---

def test_lint_workflow():
    # Live (pre-reconfig) In Progress still auto-starts -> flagged.
    dirty = [_step("ip", "In Progress", oe=["auto_start_agent"], otc=["move_to_step"],
                   stage="work", pos=0)]
    assert any("auto_start_agent" in w for w in lint_workflow(dirty))
    # profile switch + prompt on a work step -> bypass.
    ps = [_step("ip", "In Progress", otc=["move_to_step"], stage="work", profile="p", prompt="do it")]
    assert any("agent-profile" in w for w in lint_workflow(ps))
    # tagged work but parked (no transition) -> mismatch.
    assert any("stage_type='work'" in w for w in lint_workflow([_step("x", "X", stage="work")]))
    # routes on complete but tagged review -> mismatch.
    assert any("tagged stage_type" in w for w in lint_workflow(
        [_step("x", "X", otc=["move_to_step"], stage="review")]))
    # signal-gated work step -> flagged.
    assert any("step_complete" in w for w in lint_workflow(
        [_step("x", "X", otc=["move_to_step"], stage="work", signal=True)]))
    # Work-shaped but untagged -> NOT paced. This is the warning that explains a task sitting
    # on such a step forever; it names the fix rather than just reporting the state.
    untagged = lint_workflow([_step("x", "X", otc=["move_to_step"])])
    assert any("NOT paced" in w and "make_paced" in w for w in untagged)
    # P2: a PARKED step (no on_turn_complete) leaks too -- auto_start_agent / profile+prompt
    # auto-launch on entry regardless of classification, so both must be flagged there.
    assert any("auto_start_agent" in w for w in lint_workflow(
        [_step("rv", "Review", oe=["auto_start_agent"], ots=["move_to_step"])]))
    assert any("agent-profile" in w for w in lint_workflow(
        [_step("rv", "Review", ots=["move_to_step"], profile="p", prompt="review it")]))
    # A start step that is itself pace-eligible has no holding area, so the backlog executes
    # instead of queueing. This is the shape Priority Kanban takes once make_paced tags it --
    # every other check passes, so without this one the hazard ships silently.
    executing_queue = [_step("bk", "Backlog", otc=["move_to_step"], stage="work", start=True, pos=0),
                       _step("ip", "In Progress", otc=["move_to_step"], stage="work", pos=1)]
    assert any("parked queue will execute" in w for w in lint_workflow(executing_queue))
    # clean workflow -> no warnings.
    assert lint_workflow(CLEAN) == []
    assert lint_workflow(FEATURE_DEV) == []

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"ok {name}")
    print(f"\n{len(tests)} passed")
