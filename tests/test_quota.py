"""Tests for kandev_pacer/quota.py: pacing math, usage parsing, and tick records.

No network.
"""

from datetime import datetime

from kandev_pacer.config import Settings, settings
from kandev_pacer.quota import (
    binding_label,
    elapsed_frac,
    format_wait,
    headroom,
    parse_windows,
    provider_missing,
    quota_verdict,
    resume_at,
    tick_record,
    usage_providers,
    window_records,
    worst_headroom,
)

# --- pacing math ---

def test_elapsed():
    now = 1000.0
    assert elapsed_frac(now, resets_at=now + 3600, duration_s=3600) == 0.0   # just reset
    assert elapsed_frac(now, resets_at=now + 1800, duration_s=3600) == 0.5   # halfway
    assert elapsed_frac(now, resets_at=now, duration_s=3600) == 1.0          # at reset
    assert elapsed_frac(now, resets_at=now - 100, duration_s=3600) == 1.0    # past it, clamped

def test_steady_burn_sits_exactly_on_the_line():
    for e in (0.1, 0.5, 0.9):
        assert headroom(consumed_frac=e, elapsed_frac_=e) == 0.0

def test_overspend_and_underspend():
    assert headroom(0.8, 0.25) < 0      # 80% spent a quarter of the way in
    assert headroom(0.1, 0.5) > 0       # 10% spent halfway

def test_worst_window_wins():
    now = 0.0
    windows = [
        {"consumed_frac": 0.2, "resets_at": 1800, "duration_s": 3600},  # e=0.5, h=+0.3
        {"consumed_frac": 0.9, "resets_at": 1800, "duration_s": 3600},  # e=0.5, h=-0.4  <- worst
    ]
    assert worst_headroom(windows, now) == -0.4

def test_no_windows_open():
    assert worst_headroom([], now=0.0) == 1.0

def test_alpha_reserve():
    assert headroom(0.48, 0.5, alpha=0.9) < 0
    assert headroom(0.48, 0.5, alpha=1.0) > 0

# --- usage parsing (real payload shape) ---

USAGE = {"agents": [
    {"agent_id": "claude-acp", "usage": {"provider": "anthropic", "windows": [
        {"label": "5-hour", "utilization_pct": 9, "reset_at": "2026-07-23T19:39:59.5Z"},
        {"label": "7-day", "utilization_pct": 21, "reset_at": "2026-07-27T12:59:59.5Z"},
        {"label": "7-day (Fable)", "utilization_pct": 9, "reset_at": "2026-07-27T12:59:59.5Z"}]}},
    {"agent_id": "codex-acp", "usage": {"provider": "openai", "windows": [
        {"label": "7-day", "utilization_pct": 10, "reset_at": "2026-07-29T18:51:33+03:00"}]}}]}

def test_parse_windows_real_shape():
    ws = parse_windows(USAGE)
    assert len(ws) == 3                                   # all 3 anthropic windows, codex dropped
    assert [round(w["consumed_frac"], 2) for w in ws] == [0.09, 0.21, 0.09]
    assert [w["duration_s"] for w in ws] == [18000, 604800, 604800]

def test_parse_windows_provider_filter(monkeypatch):
    """One payload carries every agent's usage, so the provider filter is what picks ours."""
    monkeypatch.setattr(settings, "provider", "openai")
    assert len(parse_windows(USAGE)) == 1                 # only codex survives
    monkeypatch.setattr(settings, "provider", "nobody")
    assert parse_windows(USAGE) == []

def test_parse_windows_skips_unknown_label():
    u = {"agents": [{"usage": {"provider": "anthropic", "windows": [
        {"label": "1-minute", "utilization_pct": 5, "reset_at": "2026-07-23T19:39:59Z"}]}}]}
    assert parse_windows(u) == []

def test_provider_guard():
    """A PACER_PROVIDER that matches nothing must be caught, not silently fail open."""
    assert usage_providers(USAGE) == {"anthropic", "openai"}
    assert usage_providers({"agents": []}) == set()
    # typo -> present providers, ours absent -> guard fires
    assert provider_missing(USAGE, "claude")
    assert not provider_missing(USAGE, "anthropic")     # ours is present -> fine
    # empty payload -> can't tell -> don't withhold
    assert not provider_missing({"agents": []}, "anthropic")

# --- structured log records ---

def test_parse_windows_carries_label():
    """The log names the binding window and matches windows across ticks -- both need the label."""
    assert [w["label"] for w in parse_windows(USAGE)] == [
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


# --- the whole quota decision, as one pure call ---

def _windows(consumed, resets_in=9000, label="5-hour"):
    return [{"label": label, "consumed_frac": consumed,
             "resets_at": 1000.0 + resets_in, "duration_s": 18000}]


def _usage(provider="anthropic"):
    return {"agents": [{"usage": {"provider": provider, "windows": []}}]}


def test_quota_verdict_open_when_under_the_burn_line():
    # Half elapsed, 10% consumed -> well ahead of steady.
    verdict = quota_verdict(_usage(), _windows(0.10), 1000.0, Settings())
    assert verdict.is_open and verdict.gate == "open"
    assert round(verdict.headroom, 3) == 0.4
    assert verdict.message == ""


def test_quota_verdict_pauses_when_over_it():
    verdict = quota_verdict(_usage(), _windows(0.80), 1000.0, Settings())
    assert not verdict.is_open and verdict.gate == "paused"
    assert "PAUSED" in verdict.message


def test_quota_verdict_ceiling_outranks_a_healthy_burn_line():
    """Consumption past the backstop withholds even when headroom is positive."""
    verdict = quota_verdict(_usage(), _windows(0.99, resets_in=1), 1000.0, Settings())
    assert verdict.gate == "ceiling" and not verdict.is_open


def test_quota_verdict_is_blind_when_the_provider_is_missing():
    """A PACER_PROVIDER typo must read as 'cannot see quota', not as 'plenty of room'."""
    verdict = quota_verdict(_usage("openai"), [], 1000.0, Settings())
    assert verdict.gate == "blind" and not verdict.is_open
    assert "PACER_PROVIDER" in verdict.message


def test_quota_verdict_says_when_a_paused_gate_reopens():
    """PAUSED with no end in sight reads as an outage; the wait and the clock time say it is not."""
    # 80% consumed at e=0.5 -> h=-0.30, healing when e reaches 0.80: 3600s before the
    # reset, which is 9000s out, so 5400s from now.
    verdict = quota_verdict(_usage(), _windows(0.80), 1000.0, Settings())
    assert verdict.resume == (
        f", resumes in ~1h 30m 00s (~{datetime.fromtimestamp(6400.0):%H:%M})"
    )
    assert quota_verdict(_usage(), _windows(0.10), 1000.0, Settings()).resume == ""


def test_quota_verdict_says_when_a_ceiling_lifts():
    """A ceiling waits for the roll, not for `e`: `c` stays put until the window resets."""
    # 99% consumed with the reset 1800s out. The burn line alone would clear 180s earlier,
    # when e reaches 0.99 -- but the backstop reads `c`, which only falls at the roll.
    verdict = quota_verdict(_usage(), _windows(0.99, resets_in=1800), 1000.0, Settings())
    assert verdict.gate == "ceiling"
    assert verdict.resume == f", resumes in ~30m 00s (~{datetime.fromtimestamp(2800.0):%H:%M})"


def test_resume_at_is_when_e_catches_up_with_c():
    now = 0.0
    w = {"consumed_frac": 0.8, "resets_at": 1800, "duration_s": 3600}  # e=0.5, needs e=0.8
    assert resume_at([w], now) == 1080.0                    # 1800 - 3600*0.2
    assert headroom(0.8, elapsed_frac(1080.0, 1800, 3600)) == 0.0   # h is exactly 0 there
    assert abs(resume_at([w], now, alpha=0.9) - 1400.0) < 1e-6      # needs e=0.8/0.9
    # A window too far behind to heal before it rolls resumes at the roll, not never.
    assert resume_at([{"consumed_frac": 1.0, "resets_at": 1800, "duration_s": 3600}], now) == 1800.0


def test_resume_at_takes_the_slowest_window_and_never_looks_backwards():
    now = 0.0
    fast = {"consumed_frac": 0.8, "resets_at": 1800, "duration_s": 3600}       # 1080s out
    slow = {"consumed_frac": 0.9, "resets_at": 302400, "duration_s": 604800}   # 241920s out
    assert resume_at([fast, slow], now) == 241920.0   # the 7-day is what holds the gate shut
    assert resume_at([{**fast, "consumed_frac": 0.1}], now) == now  # already open -> no wait
    assert resume_at([], now) is None                              # nothing to wait for


def test_format_wait():
    assert format_wait(4 * 3600 + 7 * 60 + 12) == "4h 07m 12s"
    assert format_wait(72) == "1m 12s"
    assert format_wait(-5) == "0m 00s"


def test_quota_verdict_reads_only_the_numbers_it_is_given():
    """The gate has no memory: the same windows give the same verdict every tick."""
    barely = _windows(0.4999)                       # h ~ +0.0001, just above the line
    assert quota_verdict(_usage(), barely, 1000.0, Settings()).is_open
    assert quota_verdict(_usage(), barely, 1000.0, Settings()).is_open
