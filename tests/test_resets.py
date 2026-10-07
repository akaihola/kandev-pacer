"""Reset planning and redemption, with no network or real subscription credentials."""

import copy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kandev_pacer import provider, resets
from kandev_pacer.config import Settings
from kandev_pacer.quota import parse_windows, quota_verdict, reset_plan

WEEK = 7 * 86400
NOW = WEEK / 2


def envelope(name="openai", used=100, count=2, expiry=WEEK - 1, roll=WEEK):
    return {
        "agents": [
            {
                "usage": {
                    "provider": name,
                    "windows": [
                        {
                            "label": "7-day",
                            "utilization_pct": used,
                            "reset_at": datetime.fromtimestamp(roll, UTC).isoformat(),
                        }
                    ],
                    "reset_credits": [
                        {"id": "credit_a", "count": count, "expires_at": expiry}
                    ]
                    if count
                    else [],
                }
            }
        ]
    }


@pytest.mark.parametrize("name", ["openai", "anthropic"])
def test_reset_budget_opens_weekly_gate_without_spending_future_allowance(name):
    usage = envelope(name, used=90)
    windows = parse_windows(usage, name)
    config = Settings(PACER_PROVIDER=name)
    assert quota_verdict(usage, windows, NOW, config).is_open
    assert quota_verdict(envelope(name, count=0), windows, NOW, config).gate == "paused"
    assert (
        quota_verdict(
            envelope(name), parse_windows(envelope(name), name), NOW, config
        ).gate
        == "ceiling"
    )
    assert windows[0].get("pace_resets_at") is None


def test_codex_reserves_long_lived_credits_but_claude_can_use_them_this_week():
    for name, expected in [("openai", 0), ("anthropic", 2)]:
        usage = envelope(name, used=90, expiry=2 * WEEK)
        planned, count = reset_plan(usage, parse_windows(usage, name), NOW, name)
        assert count == expected
        assert ("pace_resets_at" in planned[0]) == bool(expected)


def test_expired_credits_and_session_only_limits_do_not_accelerate():
    usage = envelope(expiry=NOW)
    assert reset_plan(usage, parse_windows(usage, "openai"), NOW, "openai")[1] == 0
    usage = envelope()
    usage["agents"][0]["usage"]["windows"][0]["label"] = "5-hour"
    assert reset_plan(usage, parse_windows(usage, "openai"), NOW, "openai")[1] == 0


def test_expiry_shortens_the_pace_and_resume_estimate():
    usage = envelope(used=40, count=1, expiry=WEEK / 2)
    windows = parse_windows(usage, "openai")
    verdict = quota_verdict(
        usage, windows, WEEK / 20, Settings(PACER_PROVIDER="openai")
    )
    assert verdict.gate == "paused"
    assert verdict.resumes_at == pytest.approx(WEEK / 10)


def test_five_hour_and_model_scope_gates_still_bind():
    usage = envelope(used=90)
    windows = parse_windows(usage, "openai")
    windows.append({"duration_s": 18000, "resets_at": NOW + 9000, "consumed_frac": 0.8})
    assert (
        quota_verdict(usage, windows, NOW, Settings(PACER_PROVIDER="openai")).gate
        == "paused"
    )
    windows[-1] = {
        "duration_s": WEEK,
        "resets_at": WEEK,
        "consumed_frac": 1,
        "scope": "Fable",
    }
    assert (
        quota_verdict(usage, windows, NOW, Settings(PACER_PROVIDER="openai")).gate
        == "ceiling"
    )


def test_claude_rollover_stays_fixed_and_codex_new_window_is_paced_from_new_start():
    claude = envelope("anthropic", used=0, count=1, roll=WEEK)
    codex = envelope("openai", used=0, count=1, roll=NOW + WEEK)
    claude_plan, _ = reset_plan(
        claude, parse_windows(claude, "anthropic"), NOW, "anthropic"
    )
    codex_plan, _ = reset_plan(codex, parse_windows(codex, "openai"), NOW, "openai")
    assert claude_plan[0]["resets_at"] == WEEK
    assert codex_plan[0]["resets_at"] == NOW + WEEK
    assert claude_plan[0]["pace_resets_at"] < codex_plan[0]["pace_resets_at"]


def test_claude_grants_filter_unusable_expired_and_malformed_entries():
    grant = {
        "id": "grant_a",
        "resets_left": 2,
        "usable_now": True,
        "ends_at": "1970-01-08T00:00:00Z",
    }
    payload = {
        "cedar_ember": {
            "eligible": True,
            "next_grant_id": "grant_a",
            "grants": [
                grant,
                {**grant, "paused": True},
                {**grant, "usable_now": False},
                {**grant, "ends_at": "bad"},
                {**grant, "resets_left": True},
                {**grant, "ends_at": "1970-01-02T00:00:00Z"},
                {**grant, "id": "../invalid"},
            ],
        }
    }
    assert provider.claude_reset_credits(payload, NOW) == [
        {"id": "grant_a", "count": 2, "expires_at": WEEK}
    ]
    payload["cedar_ember"]["next_grant_id"] = "missing"
    assert provider.claude_reset_credits(payload, NOW) == []


def test_codex_credits_are_filtered_and_sorted():
    credit = {
        "id": "a",
        "status": "available",
        "reset_type": "codex_rate_limits",
        "expires_at": "1970-01-08T00:00:00Z",
    }
    payload = {
        "credits": [
            credit,
            {**credit, "id": "b", "expires_at": "1970-01-07T00:00:00Z"},
            {**credit, "status": "used"},
            {**credit, "reset_type": "other"},
            {**credit, "expires_at": "1970-01-02T00:00:00Z"},
            {**credit, "expires_at": "bad"},
        ]
    }
    assert [c["id"] for c in provider.codex_reset_credits(payload, NOW)] == ["b", "a"]


@pytest.fixture
def redemption(tmp_path, monkeypatch):
    config = Settings(
        PACER_RESET_STATE_DIR=str(tmp_path / "resets"), PACER_PROVIDER="openai"
    )
    monkeypatch.setattr(resets, "_identity", lambda *a: ("account", "token"))
    return config


@pytest.mark.parametrize(
    "name,field", [("openai", "redeem_request_id"), ("anthropic", "request_id")]
)
def test_lost_response_and_restart_keep_one_request_id(
    redemption, monkeypatch, name, field
):
    before = envelope(name)
    after = envelope(
        name, used=0, count=1, roll=NOW + WEEK if name == "openai" else WEEK
    )
    reads = iter([before, before, after])
    monkeypatch.setattr(resets, "_fresh", lambda *a: copy.deepcopy(next(reads)))
    bodies = []

    def post(url, token, body, account=None):
        bodies.append(body)
        if len(bodies) == 1:
            raise provider.QuotaUnavailable("response lost")
        return {"code": "already_redeemed", "result": "already_used"}

    monkeypatch.setattr(resets, "_post", post)
    with pytest.raises(provider.QuotaUnavailable):
        resets.redeem(name, redemption, NOW)
    result = resets.redeem(name, redemption, NOW)
    assert result == after
    assert bodies[0][field] == bodies[1][field]
    assert not list(Path(redemption.reset_state_dir).glob("*.json"))


def test_success_without_confirmed_quota_withholds_then_recovers(
    redemption, monkeypatch
):
    before = envelope()
    after = envelope(used=0, count=1)
    monkeypatch.setattr(resets, "_fresh", lambda *a: copy.deepcopy(before))
    calls = []
    monkeypatch.setattr(
        resets, "_post", lambda *a: calls.append(a) or {"code": "reset"}
    )
    _, errors = resets.apply(before, {}, {"openai"}, redemption, NOW)
    assert "openai" in errors
    monkeypatch.setattr(resets, "_fresh", lambda *a: copy.deepcopy(after))
    result, errors = resets.apply(after, {}, {"openai"}, redemption, NOW)
    assert not errors
    assert result == after
    assert len(calls) == 1


def test_missing_optional_credit_read_cannot_confirm_redemption(
    redemption, monkeypatch
):
    before = envelope()
    after = envelope(used=0, count=0)
    del after["agents"][0]["usage"]["reset_credits"]
    reads = iter([before, after])
    monkeypatch.setattr(resets, "_fresh", lambda *a: next(reads))
    monkeypatch.setattr(resets, "_post", lambda *a: {"code": "reset"})
    with pytest.raises(provider.QuotaUnavailable, match="confirmation"):
        resets.redeem("openai", redemption, NOW)


def test_no_demand_or_unexhausted_budget_never_redeems(redemption, monkeypatch):
    monkeypatch.setattr(resets, "redeem", lambda *a: pytest.fail("unexpected reset"))
    for usage, names in [(envelope(), set()), (envelope(used=90), {"openai"})]:
        assert resets.apply(usage, {}, names, redemption, NOW) == (usage, {})


def test_fresh_read_clears_cached_quota(monkeypatch):
    provider._codex_cache.usage = envelope()
    monkeypatch.setattr(provider, "direct_read", lambda *a: {"agents": []})
    resets._fresh("openai", NOW)
    assert provider._codex_cache.usage is None


def test_pending_confirmation_stays_closed_without_waiting_work(
    redemption, monkeypatch
):
    before = envelope()
    monkeypatch.setattr(resets, "_fresh", lambda *a: before)
    monkeypatch.setattr(resets, "_post", lambda *a: {"code": "reset"})
    resets.apply(before, {}, {"openai"}, redemption, NOW)
    after = envelope(used=0, count=0)
    monkeypatch.setattr(
        resets, "redeem", lambda *a: pytest.fail("no work should retry a reset")
    )
    _, errors = resets.apply(after, {}, set(), redemption, NOW)
    assert "openai" in errors


@pytest.mark.parametrize(
    "payload",
    [None, [], {"credits": 1}, {"cedar_ember": {"eligible": True, "grants": 1}}],
)
def test_malformed_optional_credit_payloads_do_not_hide_quota(payload):
    assert provider.codex_reset_credits(payload, NOW) == []
    assert provider.claude_reset_credits(payload, NOW) == []


def test_codex_credit_outage_retains_usage(monkeypatch):
    monkeypatch.setattr(
        provider,
        "read_codex_credentials",
        lambda *a: {"access_token": "token", "account_id": "account"},
    )
    calls = []

    def get(token, account, url=provider.CODEX_USAGE_URL):
        calls.append(url)
        if url == provider.CODEX_CREDITS_URL:
            raise provider.QuotaUnavailable("credit outage")
        return {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 30,
                    "limit_window_seconds": WEEK,
                    "reset_at": WEEK,
                }
            }
        }

    monkeypatch.setattr(provider, "_codex_get", get)
    usage = provider.codex_fetch(NOW)
    assert parse_windows(usage, "openai")[0]["consumed_frac"] == 0.3
    assert "reset_credits" not in usage["agents"][0]["usage"]
    assert calls == [provider.CODEX_USAGE_URL, provider.CODEX_CREDITS_URL]


def test_reset_post_does_not_expose_tokens_on_bad_responses(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"[]"

    captured = []
    monkeypatch.setattr(
        resets.urllib.request,
        "urlopen",
        lambda request, **kw: captured.append(request) or Response(),
    )
    with pytest.raises(provider.QuotaUnavailable, match="unconfirmed") as caught:
        resets._post(
            "https://example.test/consume", "private-token", {"request_id": "same"}
        )
    assert "private-token" not in str(caught.value)
    assert captured[0].get_method() == "POST"


@pytest.mark.parametrize("name", ["anthropic", "openai"])
def test_pebble_uses_live_reset_budget_and_not_stale_snapshot(name, monkeypatch):
    before = envelope(name)
    after = envelope(name, used=0, count=1)
    monkeypatch.setattr(provider.settings, "quota_source", "pebble")
    monkeypatch.setattr(provider, "pebble_fetch", lambda *a: before)
    monkeypatch.setattr(provider, "direct_read", lambda *a: after)
    assert provider._read_source(name, False, NOW) == after


def test_pebble_survives_optional_live_reset_read_failure(monkeypatch):
    before = envelope(count=0)
    monkeypatch.setattr(provider.settings, "quota_source", "pebble")
    monkeypatch.setattr(provider, "pebble_fetch", lambda *a: before)

    def fail(*args):
        raise provider.QuotaUnavailable("optional read failed")

    monkeypatch.setattr(provider, "direct_read", fail)
    assert provider._read_source("openai", False, NOW) == before
