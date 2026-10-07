"""Reading quota from the provider: the payload shapes, the credential, and the cache.

Plain dictionaries throughout, taken from a real recorded response -- the point of keeping
the parsing pure is that none of this needs a server, a token, or a clock.
"""
import json

import pytest

from kandev_pacer import provider
from kandev_pacer.config import Settings, settings, window_duration
from kandev_pacer.provider import (
    QuotaUnavailable,
    access_token,
    claude_windows,
    limit_label,
    read_credentials,
    usage_envelope,
)
from kandev_pacer.quota import parse_windows

NOW = 1_754_000_000.0

# The live payload, trimmed to the fields anything here reads. Note `weekly_scoped` with a
# null resets_at: that is what the endpoint really answers for a scope nothing has spent
# against yet, and a window with no end cannot be placed on a burn line.
PAYLOAD = {
    "five_hour": {"utilization": 68.0, "resets_at": "2026-08-03T22:59:59.781884+00:00"},
    "seven_day": {"utilization": 7.0, "resets_at": "2026-08-10T12:59:59.781907+00:00"},
    "limits": [
        {"kind": "session", "percent": 68,
         "resets_at": "2026-08-03T22:59:59.781884+00:00", "scope": None},
        {"kind": "weekly_all", "percent": 7,
         "resets_at": "2026-08-10T12:59:59.781907+00:00", "scope": None},
        {"kind": "weekly_scoped", "percent": 40,
         "resets_at": "2026-08-10T12:59:59.781907+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}}},
    ],
}


@pytest.fixture(autouse=True)
def _clean_cache():
    provider.reset_cache()
    yield
    provider.reset_cache()


# --- labels ---

def test_limit_labels():
    assert limit_label({"kind": "session"}) == "5-hour"
    assert limit_label({"kind": "weekly_all"}) == "7-day"
    scoped = {"kind": "weekly_scoped", "scope": {"model": {"display_name": "Opus 5"}}}
    assert limit_label(scoped) == "7-day (Opus 5)"


def test_scoped_limit_without_a_named_model_still_gets_a_label():
    """A window has to be labelled to be gated on, and the scope is not always populated."""
    assert limit_label({"kind": "weekly_scoped", "scope": None}) == "7-day (model)"
    assert limit_label({"kind": "weekly_scoped", "scope": {"model": {}}}) == "7-day (model)"


def test_unknown_kinds_are_skipped():
    assert limit_label({"kind": "monthly_something"}) is None
    assert limit_label({}) is None


def test_any_model_name_still_reaches_the_gate():
    """The bug an exact-match label table had: a window nobody hardcoded silently vanishes.

    Dropping a window is not a cosmetic loss -- if the dropped one is the binding one, the
    gate stops seeing the limit that is actually about to bite.
    """
    for name in ["Fable", "Opus 5", "Some Model Nobody Has Shipped Yet"]:
        label = limit_label({"kind": "weekly_scoped",
                             "scope": {"model": {"display_name": name}}})
        assert window_duration(label) == 7 * 24 * 3600


# --- windows ---

def test_limits_are_preferred_over_the_legacy_pair():
    windows = claude_windows(PAYLOAD, NOW)
    assert [w["label"] for w in windows] == ["5-hour", "7-day", "7-day (Fable)"]
    assert [w["utilization_pct"] for w in windows] == [68, 7, 40]


def test_a_limit_with_no_reset_is_dropped():
    payload = {"limits": [
        {"kind": "session", "percent": 5, "resets_at": "2026-08-03T22:59:59+00:00"},
        {"kind": "weekly_scoped", "percent": 0, "resets_at": None,
         "scope": {"model": {"display_name": "Fable"}}},
    ]}
    assert [w["label"] for w in claude_windows(payload, NOW)] == ["5-hour"]


def test_the_legacy_pair_is_the_fallback():
    """An empty or all-unusable limits[] must not read as "no quota to speak of"."""
    payload = {k: v for k, v in PAYLOAD.items() if k != "limits"}
    windows = claude_windows(payload, NOW)
    assert [w["label"] for w in windows] == ["5-hour", "7-day"]
    assert [w["utilization_pct"] for w in windows] == [68.0, 7.0]
    assert claude_windows({**payload, "limits": []}, NOW) == windows


def test_a_legacy_window_with_no_reset_gets_a_full_window_from_now():
    """As the Go client assumes. Guessing the end beats dropping a window we can see."""
    windows = claude_windows({"five_hour": {"utilization": 50.0}}, NOW)
    parsed = parse_windows(usage_envelope({"five_hour": {"utilization": 50.0}}, NOW))
    assert windows[0]["label"] == "5-hour"
    assert parsed[0]["resets_at"] == pytest.approx(NOW + 5 * 3600, abs=1)


# --- the envelope the rest of the pacer already reads ---

def test_the_envelope_parses_as_the_kandev_payload_did():
    windows = parse_windows(usage_envelope(PAYLOAD, NOW, plan="max"))
    assert [w["label"] for w in windows] == ["5-hour", "7-day", "7-day (Fable)"]
    assert [w["consumed_frac"] for w in windows] == [0.68, 0.07, 0.40]
    assert [w["duration_s"] for w in windows] == [18000, 604800, 604800]


def test_a_foreign_provider_setting_still_reads_nothing(monkeypatch):
    """PACER_PROVIDER's fail-closed guard survives the source moving: we report anthropic,
    so a PACER_PROVIDER naming anyone else finds no windows and the gate goes blind."""
    monkeypatch.setattr(settings, "provider", "openai")
    assert parse_windows(usage_envelope(PAYLOAD, NOW)) == []


# --- the credential, read and never written ---

def _creds(tmp_path, expires_in_h=5.0, **overrides):
    path = tmp_path / ".credentials.json"
    oauth = {"accessToken": "tok", "refreshToken": "refresh",
             "expiresAt": int((NOW + expires_in_h * 3600) * 1000),
             "subscriptionType": "max", "scopes": ["user:inference"]}
    path.write_text(json.dumps({"claudeAiOauth": {**oauth, **overrides}}))
    return path


def test_a_live_token_is_used_as_is(tmp_path):
    assert access_token(read_credentials(_creds(tmp_path)), NOW) == "tok"


def test_an_expired_token_says_what_to_do_about_it(tmp_path):
    with pytest.raises(QuotaUnavailable) as caught:
        access_token(read_credentials(_creds(tmp_path, expires_in_h=-1)), NOW)
    assert "re-auth" in str(caught.value) and "/login" in str(caught.value)


def test_a_token_about_to_expire_counts_as_expired(tmp_path):
    """A fetch started inside the skew would be racing the expiry."""
    with pytest.raises(QuotaUnavailable):
        access_token(read_credentials(_creds(tmp_path, expires_in_h=0.001)), NOW)


def test_a_missing_or_tokenless_credential_names_the_file(tmp_path):
    with pytest.raises(QuotaUnavailable, match="log in with Claude Code"):
        read_credentials(tmp_path / "nothing.json")
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    with pytest.raises(QuotaUnavailable, match="no subscription token"):
        read_credentials(empty)


def test_reading_the_quota_never_touches_the_credentials_file(tmp_path, monkeypatch):
    """The constraint the port exists under: Claude Code owns this file, we only read it.

    Refreshing here would rotate the stored refresh token behind Claude Code's back, and
    the loser of that race holds a token the issuer may already have retired -- breaking
    the user's login to fix a pacing outage.
    """
    monkeypatch.setattr(provider, "_get", lambda token: PAYLOAD)
    path = _creds(tmp_path, expires_in_h=5)
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    provider.fetch(NOW, path=path)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, stamp)

    # And then the case that would tempt a refresh: expired, with a refresh token right
    # there in the file. It has to come back as an error, not as a rewritten credential.
    _creds(tmp_path, expires_in_h=-1)
    expired, expired_stamp = path.read_bytes(), path.stat().st_mtime_ns
    with pytest.raises(QuotaUnavailable):
        provider.fetch(NOW, path=path)

    assert (path.read_bytes(), path.stat().st_mtime_ns) == (expired, expired_stamp)
    assert b"refresh" in before      # the token a refresher would have spent was available


# --- the cache in front of the endpoint ---

def _counting(monkeypatch, answer=None):
    calls = []

    def fetch(now, path=None):
        calls.append(now)
        if isinstance(answer, Exception):
            raise answer
        return answer or {"agents": [], "n": len(calls)}
    monkeypatch.setattr(provider, "fetch", fetch)
    return calls


def test_steady_state_calls_the_provider_once_per_five_minutes(monkeypatch):
    """MIN_INTERVAL is 5s, so an uncached port would poll 60x harder than kandev did."""
    calls = _counting(monkeypatch)
    for offset in range(0, 900, 5):          # 15 minutes of the tightest tick there is
        provider.usage(now=NOW + offset)
    assert calls == [NOW, NOW + 300, NOW + 600]


def test_the_fresh_bypass_costs_one_extra_call(monkeypatch):
    """What a converging pause spends: the cached value until the estimate is due, then one."""
    calls = _counting(monkeypatch)
    provider.usage(now=NOW)
    for offset in range(5, 120, 5):
        provider.usage(now=NOW + offset)     # still cached: no estimate has come due
    assert len(calls) == 1
    provider.usage(fresh=True, now=NOW + 120)
    assert len(calls) == 2


def test_a_fresh_read_still_takes_a_very_recent_one_from_cache(monkeypatch):
    """`fresh` narrows the staleness bound to 15s; it is not an unconditional refetch."""
    calls = _counting(monkeypatch)
    provider.usage(now=NOW)
    provider.usage(fresh=True, now=NOW + 10)
    assert len(calls) == 1


def test_a_failure_is_cached_briefly(monkeypatch):
    """A provider that is down must not be retried once per tick."""
    calls = _counting(monkeypatch, answer=QuotaUnavailable("down"))
    for offset in range(0, 30, 5):
        with pytest.raises(QuotaUnavailable):
            provider.usage(now=NOW + offset)
    assert calls == [NOW, NOW + 15]


def test_a_failure_does_not_evict_a_still_valid_reading(monkeypatch):
    """A routine tick keeps being served the good value; only the fresh caller sees the error.

    Otherwise one blip at the wrong moment turns a working gate blind for five minutes.
    """
    good = {"agents": [{"usage": {"provider": "anthropic", "windows": []}}]}
    _counting(monkeypatch, answer=good)
    provider.usage(now=NOW)

    _counting(monkeypatch, answer=QuotaUnavailable("down"))
    with pytest.raises(QuotaUnavailable):
        provider.usage(fresh=True, now=NOW + 60)      # past 15s: this caller refetches
    assert provider.usage(now=NOW + 61) == good       # inside 5 min: still served


def test_the_default_credentials_path_is_the_one_claude_code_writes():
    assert settings.credentials_path.endswith("/.claude/.credentials.json")
    assert provider.USAGE_URL.startswith("https://api.anthropic.com/")


def test_pebble_snapshot_maps_both_services_and_scoped_windows(tmp_path, monkeypatch):
    path = tmp_path / "latest.json"
    path.write_text(json.dumps({
        "collected_at": "2025-07-15T00:00:00Z",
        "services": {
            "claude": {"windows": {"5h": {"duration_seconds": 18000,
                "resets_at": "2025-07-15T05:00:00Z", "used_fraction": 0.11},
                "7d (Fable)": {"duration_seconds": 604800,
                "resets_at": "2025-07-22T00:00:00Z", "used_fraction": 0.7}}},
            "codex": {"windows": {"7d": {"duration_seconds": 604800,
                "resets_at": "2025-07-22T00:00:00Z", "used_fraction": 0.2}}},
        },
    }))
    monkeypatch.setattr(settings, "pebble_max_age", 900000000)
    result = provider.pebble_fetch(NOW, path)
    assert [a["usage"]["provider"] for a in result["agents"]] == ["anthropic", "openai"]
    assert [w["label"] for w in result["agents"][0]["usage"]["windows"]] == [
        "5-hour", "7-day (Fable)"
    ]
    assert result["agents"][0]["usage"]["windows"][0]["utilization_pct"] == 11
    assert [w["label"] for w in parse_windows(result)] == ["5-hour", "7-day (Fable)"]


def test_pebble_snapshot_failures_name_the_path(tmp_path, monkeypatch):
    path = tmp_path / "missing.json"
    monkeypatch.setattr(settings, "pebble_max_age", 900)
    with pytest.raises(QuotaUnavailable, match=str(path)):
        provider.pebble_fetch(NOW, path)


@pytest.mark.parametrize("contents", ["{}", "not json"])
def test_invalid_or_stale_pebble_snapshot_fails_closed(tmp_path, monkeypatch, contents):
    path = tmp_path / "latest.json"
    path.write_text(contents)
    with pytest.raises(QuotaUnavailable, match=str(path)):
        provider.pebble_fetch(NOW, path)

    path.write_text(json.dumps({"collected_at": "2025-07-15T00:00:00Z", "services": {}}))
    monkeypatch.setattr(settings, "pebble_max_age", 1)
    with pytest.raises(QuotaUnavailable, match="stale"):
        provider.pebble_fetch(NOW, path)


def test_direct_quota_source_remains_the_default(monkeypatch):
    monkeypatch.delenv("PACER_QUOTA_SOURCE", raising=False)
    assert Settings().quota_source == "direct"
    monkeypatch.setattr(settings, "quota_source", "direct")
    monkeypatch.setattr(provider, "usage", lambda **kwargs: {"agents": ["direct"]})
    assert provider._read_source("anthropic", False, NOW) == {"agents": ["direct"]}


def test_pebble_quota_source_is_selected(monkeypatch):
    monkeypatch.setattr(provider, "direct_read", lambda *a: {"agents": []})
    monkeypatch.setenv("PACER_QUOTA_SOURCE", "pebble")
    monkeypatch.setattr(settings, "quota_source", Settings().quota_source)
    envelope = {"agents": [{"usage": {"provider": "anthropic"}}]}
    monkeypatch.setattr(provider, "pebble_fetch", lambda now: envelope)
    assert provider._read_source("anthropic", False, NOW) == envelope


def test_pebble_source_filters_the_requested_provider(monkeypatch):
    monkeypatch.setattr(provider, "direct_read", lambda *a: {"agents": []})
    monkeypatch.setattr(settings, "quota_source", "pebble")
    monkeypatch.setattr(provider, "pebble_fetch", lambda now: {"agents": [
        {"usage": {"provider": "anthropic"}}, {"usage": {"provider": "openai"}}
    ]})
    agents = provider._read_source("openai", False, NOW)["agents"]
    assert [a["usage"]["provider"] for a in agents] == ["openai"]


def test_the_module_reads_the_credential_fresh_every_fetch(tmp_path, monkeypatch):
    """No token is held across ticks, so a rotation by Claude Code is picked up at once."""
    path = _creds(tmp_path)
    seen = []
    monkeypatch.setattr(provider, "_get", lambda token: seen.append(token) or PAYLOAD)
    provider.fetch(NOW, path=path)
    _creds(tmp_path, accessToken="rotated")
    provider.fetch(NOW, path=path)
    assert seen == ["tok", "rotated"]
