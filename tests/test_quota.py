"""Tests for kandev_pacer/quota.py: pacing math, usage parsing, tick/session records.

No network.
"""

from kandev_pacer.quota import (
    binding_label, ceiling_breached, clamp01, elapsed_frac,
    gate_open, headroom, parse_windows, provider_missing, tick_record,
    usage_providers, window_records, worst_headroom,
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

# --- structured log records ---

def test_parse_windows_carries_label():
    """The log names the binding window and matches windows across ticks -- both need the label."""
    assert [w["label"] for w in parse_windows(USAGE, provider="anthropic")] == [
        "5-hour", "7-day", "7-day (Fable)"]

def test_window_records_keep_duration():
    """h alone is not comparable across windows; the duration is what converts it to seconds."""
    now = 0.0
    ws = [{"label": "5-hour", "consumed_frac": 0.2, "resets_at": 9000, "duration_s": 18000},
          {"label": "7-day", "consumed_frac": 0.9, "resets_at": 302400, "duration_s": 604800}]
    recs = window_records(ws, now)
    assert [r["e"] for r in recs] == [0.5, 0.5]
    assert [r["h"] for r in recs] == [0.3, -0.4]
    assert [r["d"] for r in recs] == [18000, 604800]
    assert binding_label(recs) == "7-day"          # the window the gate is reacting to
    assert binding_label([]) is None

def test_tick_record_separates_gate_from_action():
    """gate=open + action=nothing + launchable>0 is the 'quota going unspent' signature."""
    recs = window_records([{"label": "5-hour", "consumed_frac": 0.1,
                            "resets_at": 9000, "duration_s": 18000}], now=0.0)
    r = tick_record(1234.5, "open", "nothing", recs, running=0, launchable=3,
                    cap=1, booked=0)
    assert (r["kind"], r["gate"], r["action"]) == ("tick", "open", "nothing")
    assert r["wh"] == 0.4 and r["bind"] == "5-hour"
    assert (r["running"], r["launchable"], r["cap"]) == (0, 3, 1)
    assert r["launched"] is None
    # No windows at all (blind tick): still a valid record, no crash on min() of empty.
    assert tick_record(1.0, "blind", "withheld", [], 0, 0, 1, 0)["wh"] == 1.0
