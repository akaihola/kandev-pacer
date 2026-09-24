"""Pacing the Fable weekly window: scope classes, attribution by model, per-class gates.

The rule under test (docs/fable-window.md): gate each task on exactly the windows its
launch can spend. A Fable task gates on the 5-hour, 7-day, and 7-day (Fable) windows; any
other Claude task on the shared windows only. Unknown gates on all windows -- the strict
direction. The classes share the provider's cap and its one launch per tick.
"""

import pytest
from conftest import CLEAN, _iso_in
from conftest import board_task as _task
from conftest import one_session as _sess

from kandev_pacer import provider
from kandev_pacer.config import settings
from kandev_pacer.provider import claude_windows
from kandev_pacer.quota import class_windows, parse_windows, scope_names
from kandev_pacer.runtime import Pacer
from kandev_pacer.selection import matched_scopes, task_models
from kandev_pacer.workflow import Board

NOW = 1_754_000_000.0


@pytest.fixture(autouse=True)
def _clean_cache():
    provider.reset_cache()
    yield
    provider.reset_cache()


# --- provider and quota: the scope rides along with the window ---


def test_claude_windows_carries_the_scope_model_name():
    payload = {"limits": [
        {"kind": "session", "percent": 68,
         "resets_at": "2026-08-03T22:59:59+00:00", "scope": None},
        {"kind": "weekly_all", "percent": 7,
         "resets_at": "2026-08-10T12:59:59+00:00", "scope": None},
        {"kind": "weekly_scoped", "percent": 40,
         "resets_at": "2026-08-10T12:59:59+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}}},
    ]}
    windows = claude_windows(payload, NOW)
    assert [w.get("scope") for w in windows] == [None, None, "Fable"]
    # And the scope survives into the internal windows the gate reads.
    parsed = parse_windows(
        {"agents": [{"usage": {"provider": "anthropic", "windows": windows}}]},
        provider="anthropic",
    )
    assert scope_names(parsed) == ("Fable",)


def test_class_windows_selects_the_subset_a_class_gates_on():
    windows = [{"label": "5-hour"}, {"label": "7-day"},
               {"label": "7-day (Fable)", "scope": "Fable"}]
    assert [w["label"] for w in class_windows(windows, ())] == ["5-hour", "7-day"]
    assert [w["label"] for w in class_windows(windows, ("Fable",))] == [
        "5-hour", "7-day", "7-day (Fable)"]


# --- selection: which models a task can run, and which scopes those spend ---


AGENTS = {
    "p-fable": {"agent": "claude-acp", "model": "claude-fable-5[1m]"},
    "p-opus": {"agent": "claude-acp", "model": "opus[1m]"},
    "p-default": {"agent": "claude-acp", "model": ""},
    "p-fallback": {"agent": "claude-acp", "model": "claude-fable-5[1m]",
                   "fallback_model": "opus[1m]", "auto_fallback": True},
    "p-fallback-off": {"agent": "claude-acp", "model": "opus[1m]",
                       "fallback_model": "claude-fable-5[1m]", "auto_fallback": False},
}


def test_task_models_reads_the_profile_and_its_enabled_fallback():
    assert task_models(_task("t", "ip", profile="p-opus"), Board(), AGENTS) == ["opus[1m]"]
    # auto_fallback widens the answer to both models the launch can run.
    assert task_models(_task("t", "ip", profile="p-fallback"), Board(), AGENTS) == [
        "claude-fable-5[1m]", "opus[1m]"]
    assert task_models(
        _task("t", "ip", profile="p-fallback-off"), Board(), AGENTS
    ) == ["opus[1m]"]


def test_task_models_is_none_when_the_model_is_unknown():
    assert task_models(_task("t", "ip"), Board(), AGENTS) is None           # no profile
    assert task_models(_task("t", "ip", profile="p-gone"), Board(), AGENTS) is None
    # "" is the agent default, which the pacer cannot see.
    assert task_models(_task("t", "ip", profile="p-default"), Board(), AGENTS) is None
    # An unreadable agents payload answers {}.
    assert task_models(_task("t", "ip", profile="p-opus"), Board(), {}) is None


def test_matched_scopes_is_a_case_insensitive_substring_test():
    assert matched_scopes(["claude-fable-5[1m]"], ("Fable",)) == ("Fable",)
    assert matched_scopes(["opus[1m]"], ("Fable",)) == ()
    assert matched_scopes(["claude-fable-5[1m]", "opus[1m]"], ("Fable",)) == ("Fable",)


def test_unknown_models_match_every_scope():
    """Unknown wins upward: the all-windows class, the strict direction."""
    assert matched_scopes(None, ("Fable",)) == ("Fable",)
    assert matched_scopes(None, ()) == ()


# --- runtime: the per-class gate, end to end ---


def launches(board):
    return [
        payload["task_id"]
        for action, payload in board.ws_log
        if action == "session.launch"
    ]


def _fable_board(board, fable_pct=83, general_pct=30):
    """The 08-14 starvation shape: the Fable weekly window past its half-elapsed burn
    line, the shared 5-hour and 7-day windows open."""
    board.usage_windows = [
        {"label": "5-hour", "utilization_pct": 10, "reset_at": _iso_in(9000)},
        {"label": "7-day", "utilization_pct": general_pct,
         "reset_at": _iso_in(302400)},
        {"label": "7-day (Fable)", "utilization_pct": fable_pct,
         "reset_at": _iso_in(302400), "scope": "Fable"},
    ]


@pytest.fixture
def solo(board, monkeypatch):
    """A single-subscription Pacer; the fake agents endpoint knows a Fable and an Opus
    profile, so the scope class comes from the model string alone."""
    monkeypatch.setattr(settings, "log_path", "")
    board.agents_map = {
        "p-fable": {"agent": "claude-acp", "billing_type": "subscription",
                    "model": "claude-fable-5[1m]"},
        "p-opus": {"agent": "claude-acp", "billing_type": "subscription",
                   "model": "opus[1m]"},
    }
    return Pacer(Board.scan([({"id": "wf", "name": "Paced"}, CLEAN)]))


def run(pacer, board, tasks, sessions, queues):
    board.load(tasks, sessions, queues)
    board.ws_log.clear()
    return pacer.tick()


def _ready(tid, profile):
    return _task(tid, "ip", tid, profile=profile), (
        tid,
        _sess(f"s-{tid}", "WAITING_FOR_INPUT"),
    )


@pytest.mark.parametrize("providers", ["anthropic", "anthropic openai"])
@pytest.mark.parametrize(
    "fable_pct, outcome", [(10, "open, nothing to launch"), (83, "PAUSED")]
)
def test_a_sole_fable_class_is_named_in_text_logs(
    solo, board, monkeypatch, caplog, providers, fable_pct, outcome
):
    monkeypatch.setattr(settings, "providers_raw", providers)
    _fable_board(board, fable_pct=fable_pct)
    with caplog.at_level("INFO", logger="kandev-pacer"):
        run(solo, board, [], {}, {})
    assert any(
        message.startswith("[anthropic (Fable)] headroom=") and outcome in message
        for message in caplog.messages
    )
    if "openai" in providers:
        assert any(
            message.startswith("[openai] headroom=") for message in caplog.messages
        )
    assert launches(board) == []


def test_a_sole_open_fable_class_launches(solo, board, caplog):
    _fable_board(board, fable_pct=10)
    task, (_, sessions) = _ready("tf", "p-fable")
    with caplog.at_level("INFO", logger="kandev-pacer"):
        run(solo, board, [task], {"tf": sessions}, {})
    assert launches(board) == ["tf"]
    assert any(
        message.startswith("[anthropic (Fable)] headroom=")
        for message in caplog.messages
    )


@pytest.mark.parametrize("queued", [0, 1])
def test_waiting_fable_on_unpaced_step_only_drains_explicit_prompt(
    solo, board, caplog, queued
):
    _fable_board(board, fable_pct=10)
    task = _task("tf", "rv", "Waiting Fable", profile="p-fable")
    with caplog.at_level("INFO", logger="kandev-pacer"):
        run(
            solo,
            board,
            [task],
            {"tf": _sess("sf", "WAITING_FOR_INPUT")},
            {"sf": queued},
        )
    actions = [
        action
        for action, _ in board.ws_log
        if action in {"session.launch", "message.queue.drain"}
    ]
    assert actions == (["message.queue.drain"] if queued else [])
    outcome = "drained queued prompt" if queued else "nothing to launch"
    assert any(
        message.startswith("[anthropic (Fable)] headroom=") and outcome in message
        for message in caplog.messages
    )


def test_waiting_fable_with_unreadable_queue_names_the_hold(solo, board, caplog):
    """The 09-22 records: open, nothing, launchable=1 -- a failed queue read held it."""
    _fable_board(board, fable_pct=10)
    task = _task("tf", "ip", "Waiting Fable", profile="p-fable")
    with caplog.at_level("INFO", logger="kandev-pacer"):
        run(solo, board, [task], {"tf": _sess("sBoom", "WAITING_FOR_INPUT")}, {})
    assert launches(board) == []
    assert any(
        message.startswith("[anthropic (Fable)] headroom=")
        and "nothing to launch" in message
        and "1 held: queue unreadable" in message
        for message in caplog.messages
    )


def test_a_bound_fable_window_does_not_withhold_general_work(solo, board):
    """The 08-14 regression: Fable bound, general open -> a non-Fable task must launch."""
    _fable_board(board)
    tf, (_, sf) = _ready("tf", "p-fable")
    to, (_, so) = _ready("to", "p-opus")
    was_open = run(solo, board, [tf, to], {"tf": sf, "to": so}, {})
    assert launches(board) == ["to"], "Opus work runs while only the Fable window binds"
    assert was_open is True


def test_a_fable_task_pauses_when_its_window_binds(solo, board):
    _fable_board(board)
    tf, (_, sf) = _ready("tf", "p-fable")
    run(solo, board, [tf], {"tf": sf}, {})
    assert launches(board) == []


def test_an_unknown_model_gates_on_all_windows(solo, board):
    """Unknown wins upward: no model information waits with the Fable class -- stricter,
    never looser, and exactly what every task did before scopes existed."""
    _fable_board(board)
    plain = _task("t", "ip", "No profile")  # no metadata.agent_profile_id
    run(solo, board, [plain], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert launches(board) == []


def test_a_binding_shared_window_pauses_both_classes(solo, board):
    """The shared windows sit in both gate sets, so nothing overspends them."""
    _fable_board(board, fable_pct=10, general_pct=80)
    tf, (_, sf) = _ready("tf", "p-fable")
    to, (_, so) = _ready("to", "p-opus")
    was_open = run(solo, board, [tf, to], {"tf": sf, "to": so}, {})
    assert launches(board) == []
    assert was_open is False


def test_a_fable_window_past_the_backstop_stops_only_fable(solo, board):
    """The ceiling applies per class: the breached window is only in the Fable subset."""
    _fable_board(board, fable_pct=98)  # 0.98 > the 0.97 backstop
    tf, (_, sf) = _ready("tf", "p-fable")
    to, (_, so) = _ready("to", "p-opus")
    run(solo, board, [tf, to], {"tf": sf, "to": so}, {})
    assert launches(board) == ["to"]


def test_queue_order_picks_among_open_classes_one_launch_per_tick(
    solo, board, monkeypatch
):
    """Both classes open: the queue-first task launches, whatever its class, and the
    provider still spends one turn per tick -- the classes share the machine."""
    written = []
    monkeypatch.setattr(solo, "log_event", written.append)
    _fable_board(board, fable_pct=10)
    tf, (_, sf) = _ready("tf", "p-fable")
    to, (_, so) = _ready("to", "p-opus")
    run(solo, board, [tf, to], {"tf": sf, "to": so}, {})
    assert launches(board) == ["tf"], "queue order (tf < to) picks the class"
    actions = {r["scope"]: r["action"] for r in written if r["kind"] == "tick"}
    assert actions == {"Fable": "launched", None: "yielded"}


def test_a_tick_writes_one_record_per_class(solo, board, monkeypatch):
    written = []
    monkeypatch.setattr(solo, "log_event", written.append)
    _fable_board(board)
    tf, (_, sf) = _ready("tf", "p-fable")
    to, (_, so) = _ready("to", "p-opus")
    run(solo, board, [tf, to], {"tf": sf, "to": so}, {})
    ticks = {r["scope"]: r for r in written if r["kind"] == "tick"}
    assert set(ticks) == {None, "Fable"}
    assert ticks["Fable"]["gate"] == "paused"
    assert ticks[None]["gate"] == "open"
    assert [w["label"] for w in ticks["Fable"]["windows"]] == [
        "5-hour", "7-day", "7-day (Fable)"]
    assert [w["label"] for w in ticks[None]["windows"]] == ["5-hour", "7-day"]


def test_the_agents_call_is_made_only_when_a_scoped_window_appears(solo, board):
    """A single-subscription run pays for attribution exactly when a scoped window makes
    the model matter -- one cached call, no change to the pre-scope path."""
    run(solo, board, [], {}, {})
    assert "/api/v1/agents" not in board.paths
    _fable_board(board)
    run(solo, board, [], {}, {})
    assert board.paths.count("/api/v1/agents") == 1


def test_without_a_scoped_window_nothing_splits(solo, board, monkeypatch, caplog):
    """Backward compatibility falls out: no scoped window -> one class, today's path."""
    written = []
    monkeypatch.setattr(solo, "log_event", written.append)
    t, (_, s) = _ready("t", "p-opus")
    with caplog.at_level("INFO", logger="kandev-pacer"):
        run(solo, board, [t], {"t": s}, {})
    ticks = [r for r in written if r["kind"] == "tick"]
    assert len(ticks) == 1 and ticks[0]["scope"] is None
    assert launches(board) == ["t"]
    assert any(message.startswith("headroom=") for message in caplog.messages)


def test_scope_classes_compose_with_a_second_provider(board, monkeypatch):
    """A bound Fable window withholds only Fable tasks; Opus and Codex work both run."""
    monkeypatch.setattr(settings, "log_path", "")
    monkeypatch.setattr(settings, "providers_raw", "anthropic openai")
    board.agents_map = {
        "p-fable": {"agent": "claude-acp", "billing_type": "subscription",
                    "model": "claude-fable-5[1m]"},
        "p-opus": {"agent": "claude-acp", "billing_type": "subscription",
                   "model": "opus[1m]"},
        "p-codex": {"agent": "codex-acp", "billing_type": "subscription",
                    "model": "gpt-5.6-sol"},
    }
    pacer = Pacer(Board.scan([({"id": "wf", "name": "Paced"}, CLEAN)]))
    _fable_board(board)
    tf, (_, sf) = _ready("tf", "p-fable")
    to, (_, so) = _ready("to", "p-opus")
    tc, (_, sc) = _ready("tc", "p-codex")
    run(pacer, board, [tf, to, tc], {"tf": sf, "to": so, "tc": sc}, {})
    assert sorted(launches(board)) == ["tc", "to"]
