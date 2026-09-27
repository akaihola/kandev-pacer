"""Two subscriptions from one pacer: attribution, the second source, and the per-provider gate.

The single-provider path is covered everywhere else and must stay byte-identical; these tests
are only the machinery that engages once PACER_PROVIDERS names more than one. They run against
conftest's fake board, which now answers GET /api/v1/agents and fakes the Codex source at
provider.codex_usage the way it already fakes the Anthropic one at provider.usage.
"""

import base64
import json

import pytest
from conftest import CLEAN
from conftest import board_task as _task
from conftest import one_session as _sess

from kandev_pacer import client, provider
from kandev_pacer.config import API_KEY, Settings, agent_provider, settings
from kandev_pacer.provider import (
    QuotaUnavailable,
    codex_access_token,
    codex_windows,
    read_codex_credentials,
)
from kandev_pacer.quota import (
    parse_windows,
    quota_verdict,
    windows_by_provider,
)
from kandev_pacer.runtime import Pacer, QuotaUnreadable
from kandev_pacer.selection import task_provider
from kandev_pacer.workflow import Board

NOW = 1_754_000_000.0


# --- config: how the plural provider list is read, and how agents map to it ---


def test_providers_defaults_to_the_single_provider():
    assert Settings().providers == ["anthropic"]
    assert Settings(PACER_PROVIDER="openai").providers == ["openai"]


def test_fallback_settings_default_and_parse_profile_names():
    assert Settings().fallback_after == 0
    assert Settings().fallback_profiles == {}
    config = Settings(
        PACER_FALLBACK_AFTER="30",
        PACER_FALLBACK_PROFILES="Luna low=Sonnet low, Codex=Claude",
    )
    assert config.fallback_after == 30
    assert config.fallback_profiles == {
        "Luna low": "Sonnet low",
        "Codex": "Claude",
    }


def test_providers_parses_a_list_and_dedupes_keeping_order():
    assert Settings(PACER_PROVIDERS="anthropic,openai").providers == [
        "anthropic",
        "openai",
    ]
    assert Settings(PACER_PROVIDERS="anthropic openai").providers == [
        "anthropic",
        "openai",
    ]
    assert Settings(PACER_PROVIDERS="openai, anthropic, openai").providers == [
        "openai",
        "anthropic",
    ]


def test_agent_provider_maps_by_agent_not_by_model():
    """hermes-acp runs an OpenAI model on an API key: mapping by model would gate it wrongly."""
    assert agent_provider("claude-acp") == "anthropic"
    assert agent_provider("codex-acp") == "openai"
    assert (
        agent_provider("hermes-acp") is None
    )  # billing_type catches it; the map stays out
    assert agent_provider(None) is None


def test_apikey_policy_is_validated():
    assert Settings(PACER_APIKEY_POLICY="cap").apikey_policy == "cap"
    with pytest.raises(ValueError, match="PACER_APIKEY_POLICY"):
        Settings(PACER_APIKEY_POLICY="spend-freely")


# --- client.agents(): profile id -> agent and billing ---


def test_agents_maps_profiles_to_their_agent_and_billing(board):
    board.agents_map = {
        "p-claude": {
            "agent": "claude-acp",
            "billing_type": "subscription",
            "model": "opus",
        },
        "p-hermes": {"agent": "hermes-acp", "billing_type": "api_key"},
    }
    mapped = client.agents()
    assert mapped["p-claude"] == {
        "agent": "claude-acp",
        "billing_type": "subscription",
        "model": "opus",
        "fallback_model": None,
        "auto_fallback": None,
    }
    assert mapped["p-hermes"]["billing_type"] == "api_key"


def test_agents_unavailable_degrades_to_empty(board, monkeypatch):
    def boom(path):
        raise client.ReadFailed("GET /api/v1/agents: HTTP Error 500")

    monkeypatch.setattr(client, "get", boom)
    assert (
        client.agents() == {}
    )  # cannot attribute -> the resolver withholds, tick lives


# --- selection.task_provider: which subscription a launch burns ---


AGENTS = {
    "p-claude": {"agent": "claude-acp", "billing_type": "subscription"},
    "p-codex": {"agent": "codex-acp", "billing_type": "subscription"},
    "p-hermes": {"agent": "hermes-acp", "billing_type": "api_key"},
}


def test_task_provider_reads_the_metadata_profile():
    task = _task("t", "ip", profile="p-codex")
    assert task_provider(task, Board(), AGENTS) == "openai"


def test_task_provider_prefers_a_step_override_over_metadata():
    """Attribution is per launch: a profile-switch step's own profile wins over the task's."""
    board = Board(step_profiles={"ip": "p-codex"})
    task = _task("t", "ip", profile="p-claude")
    assert task_provider(task, board, AGENTS) == "openai"


def test_task_provider_flags_an_api_key_profile():
    assert (
        task_provider(_task("t", "ip", profile="p-hermes"), Board(), AGENTS) == API_KEY
    )


def test_task_provider_is_none_when_unresolvable():
    assert task_provider(_task("t", "ip"), Board(), AGENTS) is None  # no profile
    assert (
        task_provider(_task("t", "ip", profile="p-gone"), Board(), AGENTS) is None
    )  # unknown


def test_an_agent_override_wins_over_kandev_billing_type(monkeypatch):
    """Hermes runs gpt-5.6-sol on the Codex OAuth subscription, but kandev labels it api_key.

    The operator override says which subscription it really burns, and is honoured before the
    billing_type short-circuit -- so the api-key Hermes profile paces on the Codex/openai gate
    instead of being withheld (or, under `cap`, launched ungated against a window it *does* spend).
    """
    monkeypatch.setattr(settings, "agent_providers_raw", "hermes-acp=openai")
    assert (
        task_provider(_task("t", "ip", profile="p-hermes"), Board(), AGENTS) == "openai"
    )
    # Untouched agents still resolve as before.
    assert (
        task_provider(_task("t", "ip", profile="p-claude"), Board(), AGENTS)
        == "anthropic"
    )


def test_agent_provider_overrides_parse():
    assert Settings(
        PACER_AGENT_PROVIDERS="hermes-acp=openai"
    ).agent_provider_overrides == {"hermes-acp": "openai"}
    assert Settings(
        PACER_AGENT_PROVIDERS="hermes-acp=openai, pi-acp=anthropic"
    ).agent_provider_overrides == {"hermes-acp": "openai", "pi-acp": "anthropic"}
    assert Settings().agent_provider_overrides == {}


# --- quota: grouping, the stated duration, and the per-provider blind guard ---


TWO = {
    "agents": [
        {
            "agent_id": "claude-acp",
            "usage": {
                "provider": "anthropic",
                "windows": [
                    {
                        "label": "5-hour",
                        "utilization_pct": 9,
                        "reset_at": "2026-07-23T19:39:59.5Z",
                    }
                ],
            },
        },
        {
            "agent_id": "codex-acp",
            "usage": {
                "provider": "openai",
                "windows": [
                    {
                        "label": "weekly",
                        "utilization_pct": 50,
                        "reset_at": "2026-07-29T18:51:33+03:00",
                        "duration_s": 604800,
                    }
                ],
            },
        },
    ]
}


def test_windows_by_provider_groups_each_provider():
    grouped = windows_by_provider(TWO)
    assert set(grouped) == {"anthropic", "openai"}
    assert [w["duration_s"] for w in grouped["anthropic"]] == [18000]
    assert [w["duration_s"] for w in grouped["openai"]] == [604800]


def test_parse_windows_takes_the_provider_it_is_asked_for():
    assert [w["label"] for w in parse_windows(TWO, provider="openai")] == ["weekly"]
    assert [w["label"] for w in parse_windows(TWO, provider="anthropic")] == ["5-hour"]


def test_a_stated_duration_is_preferred_over_the_label_lookup():
    """The Codex win: a window whose label matches no prefix still reaches the gate, because
    the payload carries its length. Under label-only matching it would be silently dropped."""
    usage = {
        "agents": [
            {
                "usage": {
                    "provider": "openai",
                    "windows": [
                        {
                            "label": "primary",
                            "utilization_pct": 30,
                            "reset_at": "2026-07-29T18:51:33Z",
                            "duration_s": 3600,
                        }
                    ],
                }
            }
        ]
    }
    windows = parse_windows(usage, provider="openai")
    assert [w["duration_s"] for w in windows] == [3600]  # kept, not dropped


def test_quota_verdict_is_blind_per_provider():
    """A configured provider missing from the merged envelope withholds only its own tasks."""
    verdict = quota_verdict(TWO, [], NOW, Settings(), provider="gemini")
    assert verdict.gate == "blind"
    # The provider that is present forms a normal verdict off its own windows.
    present = quota_verdict(
        TWO, windows_by_provider(TWO)["openai"], NOW, Settings(), provider="openai"
    )
    assert present.gate in ("open", "paused", "ceiling")


# --- provider: the Codex source ---


def _jwt(exp):
    """A minimal JWT whose payload carries `exp` -- the shape codex_access_token decodes."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.sig"


def _codex_auth(tmp_path, exp_in_h=48.0, **overrides):
    path = tmp_path / "auth.json"
    tokens = {
        "access_token": _jwt(int(NOW + exp_in_h * 3600)),
        "refresh_token": "refresh",
        "account_id": "acct-1",
    }
    path.write_text(json.dumps({"tokens": {**tokens, **overrides}}))
    return path


def test_codex_access_token_reads_expiry_from_the_jwt_claim(tmp_path):
    tokens = read_codex_credentials(_codex_auth(tmp_path, exp_in_h=48))
    assert codex_access_token(tokens, NOW) == tokens["access_token"]


def test_an_expired_codex_token_fails_closed(tmp_path):
    tokens = read_codex_credentials(_codex_auth(tmp_path, exp_in_h=-1))
    with pytest.raises(QuotaUnavailable, match="Codex credentials expired"):
        codex_access_token(tokens, NOW)


def test_an_api_key_codex_login_has_no_quota(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"tokens": {}, "OPENAI_API_KEY": "sk-x"}))
    with pytest.raises(QuotaUnavailable, match="no subscription token"):
        read_codex_credentials(path)


def test_an_apikey_auth_mode_is_rejected_even_with_a_token(tmp_path):
    """auth_mode is authoritative: an api-key Codex login has metered spend, not a window."""
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps({"auth_mode": "apikey", "tokens": {"access_token": "x"}})
    )
    with pytest.raises(QuotaUnavailable, match="api-key"):
        read_codex_credentials(path)


def test_the_codex_read_presents_the_codex_user_agent(monkeypatch):
    """The endpoint is Cloudflare-fronted and 403s a bare urllib UA -- this is the regression
    guard for the fix that made the second source read at all (verified live 2026-08-08)."""
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"rate_limit": {"primary_window": null, "secondary_window": null}}'

    def fake_urlopen(request, timeout=None):
        captured["ua"] = request.get_header("User-agent")
        return _Resp()

    monkeypatch.setattr(provider.urllib.request, "urlopen", fake_urlopen)
    provider._codex_get("tok", "acct")
    assert captured["ua"] == provider.CODEX_USER_AGENT


def test_codex_windows_carries_the_duration_and_an_epoch_reset():
    payload = {
        "rate_limit": {
            "primary_window": {
                "used_percent": 54,
                "limit_window_seconds": 604800,
                "reset_at": 1786168839,
            }
        }
    }
    windows = codex_windows(payload, NOW)
    assert windows[0]["duration_s"] == 604800
    assert windows[0]["utilization_pct"] == 54
    # The internal envelope carries an ISO reset, as parse_windows expects; the payload's epoch
    # is converted, so the resulting window parses.
    parsed = parse_windows(
        {"agents": [{"usage": {"provider": "openai", "windows": windows}}]},
        provider="openai",
    )
    assert parsed[0]["duration_s"] == 604800


def test_codex_windows_falls_back_to_reset_after_seconds(monkeypatch):
    payload = {
        "rate_limit": {
            "primary_window": {
                "used_percent": 10,
                "limit_window_seconds": 3600,
                "reset_after_seconds": 1800,
            }
        }
    }
    windows = codex_windows(payload, NOW)
    assert windows and windows[0]["duration_s"] == 3600


# --- provider.read: merging two sources, and failing one without the other ---


@pytest.fixture(autouse=True)
def _clean_cache():
    provider.reset_cache()
    yield
    provider.reset_cache()


def test_read_merges_both_sources(board):
    envelope, errors = provider.read(["anthropic", "openai"], now=NOW)
    providers = {a["usage"]["provider"] for a in envelope["agents"]}
    assert providers == {"anthropic", "openai"} and errors == {}


def test_read_fails_one_source_without_the_other(board):
    board.codex_error = "Codex usage read failed: HTTP Error 502"
    envelope, errors = provider.read(["anthropic", "openai"], now=NOW)
    assert list(errors) == ["openai"]
    assert {a["usage"]["provider"] for a in envelope["agents"]} == {"anthropic"}


def test_read_reports_a_source_with_no_reader():
    envelope, errors = provider.read(["nobody"], now=NOW)
    assert envelope["agents"] == [] and "nobody" in errors


# --- runtime: the per-provider gate, end to end ---


def actions(board):
    return [action for action, _ in board.ws_log if action != "message.queue.get"]


def launches(board):
    return [
        payload["task_id"]
        for action, payload in board.ws_log
        if action == "session.launch"
    ]


@pytest.fixture
def duo(board, monkeypatch):
    """A Pacer pacing anthropic + openai against the fake board, logging off."""
    monkeypatch.setattr(settings, "log_path", "")
    monkeypatch.setattr(settings, "providers_raw", "anthropic openai")
    board.agents_map = {
        "p-claude": {"agent": "claude-acp", "billing_type": "subscription"},
        "p-codex": {"agent": "codex-acp", "billing_type": "subscription"},
        "p-hermes": {"agent": "hermes-acp", "billing_type": "api_key"},
    }
    return Pacer(Board.scan([({"id": "wf", "name": "Paced"}, CLEAN)]))


def run(duo, board, tasks, sessions, queues):
    board.load(tasks, sessions, queues)
    board.ws_log.clear()
    return duo.tick()


def _ready(tid, profile):
    return _task(tid, "ip", tid, profile=profile), (
        tid,
        _sess(f"s-{tid}", "WAITING_FOR_INPUT"),
    )


def test_each_open_provider_launches_its_own_task_in_one_tick(duo, board):
    """One launch per provider per tick: the two subscriptions do not compete for one turn."""
    ta, (_, sa) = _ready("ta", "p-claude")
    tc, (_, sc) = _ready("tc", "p-codex")
    was_open = run(duo, board, [ta, tc], {"ta": sa, "tc": sc}, {})
    assert was_open is True
    assert sorted(launches(board)) == ["ta", "tc"]


def test_a_shut_gate_does_not_hide_the_other_providers_work(duo, board):
    """The bug two subscriptions exist to fix: a full Claude window withholding Codex work."""
    board.usage_pct = 80  # anthropic paused
    ta, (_, sa) = _ready("ta", "p-claude")
    tc, (_, sc) = _ready("tc", "p-codex")
    was_open = run(duo, board, [ta, tc], {"ta": sa, "tc": sc}, {})
    assert launches(board) == ["tc"], "Codex work must run while Claude is paused"
    assert was_open is True


def test_a_tick_writes_one_record_per_provider(duo, board, monkeypatch):
    written = []
    monkeypatch.setattr(duo, "log_event", written.append)
    board.usage_pct = 80  # anthropic paused, openai open
    ta, (_, sa) = _ready("ta", "p-claude")
    tc, (_, sc) = _ready("tc", "p-codex")
    run(duo, board, [ta, tc], {"ta": sa, "tc": sc}, {})
    ticks = [r for r in written if r["kind"] == "tick"]
    by_provider = {r["provider"]: r for r in ticks}
    assert set(by_provider) == {"anthropic", "openai"}
    assert by_provider["anthropic"]["gate"] == "paused"
    assert by_provider["openai"]["gate"] == "open"


def test_the_interval_is_the_earliest_reopening_across_providers(duo, board):
    """A wide-open Codex must not stop the loop meeting a Claude reopening due in seconds.

    The loop paces on the *earliest* reopening -- the min across providers -- so a converging
    Claude pause (its 5-hour window heals in ~9s) is met even while Codex would tick at 120.
    """
    board.usage_pct = 50.1  # anthropic just under the line: ~18s of healing left
    board.codex_pct = 5  # openai wide open -> would tick at the configured 120
    run(duo, board, [], {}, {})
    assert duo.interval == pytest.approx(9, abs=1)  # the min, not the 120 openai wants


def test_an_unresolvable_task_is_withheld_when_two_are_paced(duo, board):
    """With one subscription every task burns it; with two, an unattributable task is withheld."""
    plain = _task("t", "ip", "No profile")  # no metadata.agent_profile_id
    was_open = run(duo, board, [plain], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert actions(board) == [], (
        "nothing says which gate it burns -> withhold, do not guess"
    )
    assert was_open is True


def test_a_withheld_task_is_named_once(duo, board, caplog):
    """Silence would leave an operator asking why the card never runs; named once, not per tick."""
    plain = _task("t", "ip", "No profile")
    with caplog.at_level("WARNING", logger="kandev-pacer"):
        run(duo, board, [plain], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
        run(duo, board, [plain], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    named = [r.message for r in caplog.records if "withheld" in r.message]
    assert len(named) == 1 and "'No profile'" in named[0]


def test_an_api_key_task_launches_by_default(duo, board):
    """billing_type=api_key has no subscription window; the pacer launches it gated on
    the concurrency cap alone rather than withholding work it never paces."""
    task = _task("t", "ip", "Hermes", profile="p-hermes")
    was_open = run(duo, board, [task], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert launches(board) == ["t"]
    assert was_open is True


def test_an_api_key_task_is_withheld_under_the_withhold_policy(
    duo, board, monkeypatch, caplog
):
    """`withhold` opts back into fail-closed: no launch, and the line names the specific
    case -- an api-key profile, with the knob that changes it."""
    monkeypatch.setattr(settings, "apikey_policy", "withhold")
    task = _task("t", "ip", "Hermes", profile="p-hermes")
    with caplog.at_level("WARNING", logger="kandev-pacer"):
        run(duo, board, [task], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert actions(board) == []
    line = next(r.message for r in caplog.records if "withheld" in r.message)
    assert "api-key profile" in line and "PACER_APIKEY_POLICY=cap" in line


def test_an_unmapped_profile_is_named_as_unresolvable_not_api_key(duo, board, caplog):
    """The other withheld case, kept distinct: a profile the agents endpoint does not carry."""
    task = _task("t", "ip", "Mystery", profile="p-unknown")
    with caplog.at_level("WARNING", logger="kandev-pacer"):
        run(duo, board, [task], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    line = next(r.message for r in caplog.records if "withheld" in r.message)
    assert "not in GET /api/v1/agents" in line and "p-unknown" in line


def test_an_overridden_api_key_agent_paces_on_its_real_subscription(
    duo, board, monkeypatch
):
    """The Hermes fix end to end: overridden to openai, the api-key task launches under the
    Codex gate rather than being withheld -- and is gated by that window, not launched ungated."""
    monkeypatch.setattr(settings, "agent_providers_raw", "hermes-acp=openai")
    task = _task("t", "ip", "Hermes", profile="p-hermes")
    was_open = run(duo, board, [task], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert launches(board) == ["t"]
    assert was_open is True
    # And it obeys that gate: shut the openai window and the same task is withheld.
    board.ws_log.clear()
    board.codex_pct = 95  # openai now over its burn line
    run(duo, board, [task], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert launches(board) == [], (
        "an overridden task is paced by its window, not launched blind"
    )


def test_the_api_key_policy_can_launch_on_the_cap_alone(duo, board, monkeypatch):
    monkeypatch.setattr(settings, "apikey_policy", "cap")
    task = _task("t", "ip", "Hermes", profile="p-hermes")
    was_open = run(duo, board, [task], {"t": _sess("s", "WAITING_FOR_INPUT")}, {})
    assert launches(board) == ["t"]
    assert was_open is True


def test_one_provider_going_blind_does_not_shut_the_other(duo, board):
    """An unreachable Codex endpoint must not withhold Claude work -- the whole reason for one
    process reading each source apart."""
    board.codex_error = "Codex usage read failed: HTTP Error 502"
    ta, (_, sa) = _ready("ta", "p-claude")
    tc, (_, sc) = _ready("tc", "p-codex")
    was_open = run(duo, board, [ta, tc], {"ta": sa, "tc": sc}, {})
    assert launches(board) == ["ta"], (
        "Claude runs; Codex is blind and withholds only its own"
    )
    assert was_open is True


def test_a_standing_source_outage_is_said_once_then_recovered(duo, board, caplog):
    """A down Codex endpoint must not print a line every tick while Claude runs on; and its
    recovery needs a visible end, so a live tail can tell the outage stopped."""
    board.codex_error = "Codex usage read failed: HTTP Error 502"
    with caplog.at_level("INFO", logger="kandev-pacer"):
        for _ in range(5):
            run(duo, board, [], {}, {})
    outage = [
        r.message for r in caplog.records if "Codex usage read failed" in r.message
    ]
    assert len(outage) == 1, "five failing ticks, one line"

    caplog.clear()
    board.codex_error = None  # the endpoint comes back
    with caplog.at_level("WARNING", logger="kandev-pacer"):
        run(duo, board, [], {}, {})
    assert any("recovered" in r.message for r in caplog.records)


def test_every_source_failing_shuts_the_whole_gate(duo, board, monkeypatch):
    """When no quota can be read at all, the tick withholds exactly as a single provider does."""
    board.codex_error = "Codex down"
    monkeypatch.setattr(
        provider,
        "usage",
        lambda fresh=False, now=None: (_ for _ in ()).throw(
            QuotaUnavailable("Anthropic down")
        ),
    )
    board.load([], {}, {})
    with pytest.raises(QuotaUnreadable):
        duo.tick()


def test_the_shared_total_cap_limits_the_sum_across_providers(duo, board, monkeypatch):
    """Per-provider caps do not bound the machine; PACER_CONCURRENCY_TOTAL does."""
    monkeypatch.setattr(settings, "concurrency_total", 1)
    ta, (_, sa) = _ready("ta", "p-claude")
    tc, (_, sc) = _ready("tc", "p-codex")
    run(duo, board, [ta, tc], {"ta": sa, "tc": sc}, {})
    assert len(launches(board)) == 1, (
        "one turn total this tick, though each provider had room"
    )


def test_running_is_counted_per_provider(duo, board):
    """Claude at its cap must not stop Codex, and Codex's running turn must not free Claude."""
    # A running Claude turn (its cap is 1) and an idle Codex task: only Codex may launch.
    ta = _task("ta", "ip", "Claude busy", profile="p-claude")
    tc, (_, sc) = _ready("tc", "p-codex")
    was_open = run(duo, board, [ta, tc], {"ta": _sess("sa", "RUNNING"), "tc": sc}, {})
    assert launches(board) == ["tc"]
    assert was_open is True


def test_unknown_queue_does_not_block_other_provider(duo, board):
    run(
        duo,
        board,
        [_task("t1", "ip", profile="p-claude"), _task("t2", "ip", profile="p-codex")],
        {
            "t1": _sess("sBoom", "WAITING_FOR_INPUT"),
            "t2": _sess("s2", "WAITING_FOR_INPUT"),
        },
        {},
    )
    assert launches(board) == ["t2"]
    assert duo.failed == {}


@pytest.mark.parametrize("total", [None, 1])
def test_capacity_deferral_reserves_provider_and_shared_slots(
    duo, board, monkeypatch, capacity_launch, total
):
    monkeypatch.setattr(settings, "concurrency_total", total)
    ta, (_, sa) = _ready("t1", "p-claude")
    tc, (_, sc) = _ready("tc", "p-codex")
    capacity_launch("legacy")
    board.load([ta, tc], {"t1": sa, "tc": sc}, {})
    duo.tick()
    assert launches(board) == (["t1", "tc"] if total is None else ["t1"])
    board.ws_log.clear()
    # A persisted hold also consumes capacity on later ticks.
    board.tasks.append(_task("ta2", "ip", profile="p-claude"))
    duo.tick()
    assert not launches(board)
    assert not duo.failed


def test_unknown_provider_deferral_still_uses_total_capacity(duo, board, monkeypatch):
    monkeypatch.setattr(settings, "concurrency_total", 1)
    unknown = _task("t1", "ip", profile="missing")
    unknown["metadata"]["deferred_launch"] = {"ceiling_deferred": True}
    tc, (_, sc) = _ready("tc", "p-codex")
    run(duo, board, [unknown, tc], {"tc": sc}, {})
    assert not launches(board)


def test_fallback_deferral_is_attributed_to_destination(
    duo, board, monkeypatch, capacity_launch
):
    monkeypatch.setattr(settings, "fallback_after", 1)
    monkeypatch.setattr(settings, "fallback_profiles_raw", "Claude=Codex")
    board.agents_map["p-claude"]["name"] = "Claude"
    board.agents_map["p-codex"]["name"] = "Codex"
    board.usage_pct = 95
    duo._gate_closed_at["anthropic"] = 0
    source = _task("t1", "bk", profile="p-claude")
    other, (_, session) = _ready("tc", "p-codex")
    board.load([source, other], {"tc": session}, {})
    capacity_launch("legacy")
    duo.tick()
    payload = next(p for action, p in board.ws_log if action == "session.launch")
    assert payload["agent_profile_id"] == "p-codex"
    assert payload["workflow_step_id"] == "ip"
    assert duo.deferred["t1"].group.provider == "openai"
    assert launches(board) == ["t1"]
    board.tasks.append(_task("t2", "bk", profile="p-claude"))
    duo.tick()
    assert launches(board) == ["t1"]


def test_heartbeat_can_refill_unaffected_provider_with_capacity_hold(
    duo, board, monkeypatch
):
    monkeypatch.setattr(settings, "heartbeat_min_launchable", 1)
    monkeypatch.setattr(settings, "heartbeat_webhook_url", "http://kandev/hook")
    monkeypatch.setattr(settings, "heartbeat_webhook_secret", "test")
    posted = []
    monkeypatch.setattr(
        client, "post_webhook", lambda url, secret, body: posted.append(body) or 200
    )
    task = _task("t1", "ip", profile="p-claude")
    task["metadata"]["deferred_launch"] = {"ceiling_deferred": True}
    board.load([task], {}, {})
    duo.tick()
    assert [body["provider"] for body in posted] == ["openai"]


@pytest.mark.parametrize("signal_check", [False, True])
@pytest.mark.parametrize("deferred", [False, True])
def test_fresh_admission_reserves_capacity_before_other_provider(
    duo, board, monkeypatch, signal_check, deferred
):
    from kandev_pacer.selection import ActionKey

    monkeypatch.setattr(settings, "concurrency_total", 1)
    task = _task("t1", "bk", profile="p-claude")
    other, (_, other_sessions) = _ready("tc", "p-codex")
    board.load(
        [task, other],
        {"t1": _sess("s1", "WAITING_FOR_INPUT"), "tc": other_sessions},
        {},
    )
    read = client.task_of

    def admitted(task_id):
        fresh = read(task_id)
        if task_id != "t1":
            return fresh
        metadata = dict(fresh["metadata"])
        if deferred:
            metadata["deferred_launch"] = {"ceiling_deferred": True}
        return {**fresh, "workflow_step_id": "ip", "metadata": metadata}

    monkeypatch.setattr(client, "task_of", admitted)
    if signal_check:
        # The closed source gate prevents a later launch check from hiding a
        # missing reservation in the pending-signal reconciliation path.
        board.usage_pct = 95
        duo.pending_signals[ActionKey("t1", "bk", "s1")] = (0, 5)
    duo.tick()
    assert launches(board) == ([] if deferred else ["tc"])
    assert not duo.failed
    assert not duo.pending_signals


def test_fresh_capacity_hold_uses_admitted_profile(duo, board, monkeypatch):
    task = _task("t1", "bk", profile="p-claude")
    other, (_, sessions) = _ready("tc", "p-codex")
    board.load([task, other], {"tc": sessions}, {})
    read = client.task_of

    def admitted(task_id):
        fresh = read(task_id)
        if task_id != "t1":
            return fresh
        return {
            **fresh,
            "workflow_step_id": "ip",
            "metadata": {
                "agent_profile_id": "p-codex",
                "deferred_launch": {"ceiling_deferred": True},
            },
        }

    monkeypatch.setattr(client, "task_of", admitted)
    duo.tick()
    assert launches(board) == []
    assert duo.deferred["t1"].group.provider == "openai"
