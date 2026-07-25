#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Pure pacing + classification tests (no network). Run: uv run test_pace.py"""
from pacer import (
    clamp01, elapsed_frac, headroom, worst_headroom, gate_open, ceiling_breached,
    parse_windows, usage_providers, provider_missing,
    is_work_step, work_step_ids, queue_step_id, admission_target,
    lint_workflow, newest_session, is_launchable, launch_target, pick_intent, prune_booked,
    step_names, named,
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

# Clean (post-reconfig) Paced Kanban: In Progress routes away, no auto-start.
CLEAN = [
    _step("bk", "Backlog", start=True, pos=0),
    _step("ip", "In Progress", otc=["move_to_step"], pos=1),
    _step("rv", "Review", ots=["move_to_step"], pos=2),
    _step("dn", "Done", ots=["move_to_step"], pos=3),
]

def test_work_step_ids():
    assert work_step_ids(CLEAN) == {"ip"}                       # only In Progress routes on complete
    assert is_work_step(_step("x", "X", otc=["move_to_next"]))      # every transition kind counts
    assert is_work_step(_step("x", "X", otc=["move_to_previous"]))
    assert is_work_step(_step("x", "X", otc=["move_to_step"]))
    assert not is_work_step(_step("x", "X", ots=["move_to_step"]))  # on_turn_start is not routing-away

def test_step_names():
    names = step_names(CLEAN)
    assert names["ip"] == "In Progress"
    assert named("ip", names) == "In Progress"
    assert named("gone", names) == "gone"            # unknown id stays identifiable in the log
    assert named(None, names) is None                # no queue/admission step -> prints None
    assert step_names([{"id": "x"}])["x"] == "x"     # nameless step falls back to its id

def test_queue_and_admission_target():
    assert queue_step_id(CLEAN) == "bk"
    assert admission_target(CLEAN) == "ip"                      # lowest-position work step
    assert admission_target([_step("a", "A", otc=["move_to_next"], pos=5),
                             _step("b", "B", otc=["move_to_next"], pos=2)]) == "b"
    assert admission_target(CLEAN, override="forced") == "forced"
    assert admission_target([_step("bk", "Backlog", start=True)]) is None  # no work step

# --- launchability ---

def _sess(id, state, started="1"):
    # `started_at` is the real field name in GET /tasks/:id/sessions -- there is no `created_at`.
    return {"id": id, "state": state, "started_at": started}

WORK = {"ip"}

def test_is_launchable_work_step():
    booked = set()
    at_ip = {"id": "t", "workflow_step_id": "ip"}
    assert is_launchable(at_ip, [_sess("s", "WAITING_FOR_INPUT")], WORK, "bk", booked)
    assert not is_launchable(at_ip, [_sess("s", "RUNNING")], WORK, "bk", booked)   # in flight
    assert not is_launchable(at_ip, [], WORK, "bk", booked)                        # no session

def test_is_launchable_queue_admission():
    booked = set()
    at_bk = {"id": "t", "workflow_step_id": "bk"}
    assert is_launchable(at_bk, [], WORK, "bk", booked)                            # new task
    assert is_launchable(at_bk, [_sess("s", "WAITING_FOR_INPUT")], WORK, "bk", booked)  # re-queued
    assert not is_launchable(at_bk, [_sess("s", "RUNNING")], WORK, "bk", booked)

def test_is_launchable_created_session():
    """Kandev prepares a session at task creation, so a never-run task arrives CREATED.

    That means "workspace prepared, agent never started" -- launchable, exactly like the
    no-session case. Missing this is why two Backlog tasks sat forever while the pacer
    reported "nothing to launch". intent:workflow_step handles it: kandev's
    ensureSessionRunning starts the agent on the already-prepared workspace.
    """
    booked = set()
    at_bk = {"id": "t", "workflow_step_id": "bk"}
    assert is_launchable(at_bk, [_sess("s", "CREATED")], WORK, "bk", booked)
    # Same story one step over: a new task dragged straight to a work step is prepared,
    # not running, and would otherwise be ignored forever.
    at_ip = {"id": "t", "workflow_step_id": "ip"}
    assert is_launchable(at_ip, [_sess("s", "CREATED")], WORK, "bk", booked)
    # The dup-guard still applies to it.
    assert not is_launchable(at_bk, [_sess("s", "CREATED")], WORK, "bk", {("t", "bk", "s")})

def test_is_launchable_parked_and_booked():
    at_rv = {"id": "t", "workflow_step_id": "rv"}
    assert not is_launchable(at_rv, [_sess("s", "WAITING_FOR_INPUT")], WORK, "bk", set())  # terminal
    at_ip = {"id": "t", "workflow_step_id": "ip"}
    booked = {("t", "ip", "s")}
    assert not is_launchable(at_ip, [_sess("s", "WAITING_FOR_INPUT")], WORK, "bk", booked)  # dup-guard

def test_launch_target():
    assert launch_target({"workflow_step_id": "ip"}, WORK, "bk", "ip") == "ip"    # in place
    assert launch_target({"workflow_step_id": "bk"}, WORK, "bk", "ip") == "ip"    # admit to target

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

# --- lint ---

def test_lint_workflow():
    # Live (pre-reconfig) In Progress still auto-starts -> flagged.
    dirty = [_step("ip", "In Progress", oe=["auto_start_agent"], otc=["move_to_step"], pos=0)]
    assert any("auto_start_agent" in w for w in lint_workflow(dirty))
    # profile switch + prompt on a work step -> bypass.
    ps = [_step("ip", "In Progress", otc=["move_to_step"], profile="p", prompt="do it")]
    assert any("agent-profile" in w for w in lint_workflow(ps))
    # tagged work but parked (no transition) -> mismatch.
    assert any("stage_type='work'" in w for w in lint_workflow([_step("x", "X", stage="work")]))
    # routes on complete but tagged review -> mismatch.
    assert any("tagged stage_type" in w for w in lint_workflow(
        [_step("x", "X", otc=["move_to_step"], stage="review")]))
    # signal-gated work step -> flagged.
    assert any("step_complete" in w for w in lint_workflow(
        [_step("x", "X", otc=["move_to_step"], signal=True)]))
    # P2: a PARKED step (no on_turn_complete) leaks too -- auto_start_agent / profile+prompt
    # auto-launch on entry regardless of classification, so both must be flagged there.
    assert any("auto_start_agent" in w for w in lint_workflow(
        [_step("rv", "Review", oe=["auto_start_agent"], ots=["move_to_step"])]))
    assert any("agent-profile" in w for w in lint_workflow(
        [_step("rv", "Review", ots=["move_to_step"], profile="p", prompt="review it")]))
    # clean workflow -> no warnings.
    assert lint_workflow(CLEAN) == []

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"ok {name}")
    print(f"\n{len(tests)} passed")
