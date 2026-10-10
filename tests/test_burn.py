"""The burn-time meter, fed recorded tick shapes, with no network and no real home."""

import json

import pytest

from kandev_pacer.burn import (
    MAX_GAP,
    MIN_SPAN,
    TRAILING,
    BurnMeter,
    burn_note,
)
from kandev_pacer.quota import tick_record

WEEK = 7 * 86400
HOUR = 3600
T0 = 1_791_400_000.0


def weekly(c, scope=None):
    """The internal window shape the runtime hands the meter."""
    return {"label": "7-day", "scope": scope, "consumed_frac": c,
            "resets_at": T0 + WEEK, "duration_s": WEEK}


def five_hour(c):
    return {"label": "5-hour", "scope": None, "consumed_frac": c,
            "resets_at": T0 + 5 * HOUR, "duration_s": 5 * HOUR}


def feed(meter, provider, readings, step=30.0):
    """Readings every `step` seconds, as a running pacer would make them."""
    changed = []
    for index, c in enumerate(readings):
        now = T0 + index * step
        if meter.observe(provider, [weekly(c), five_hour(0.1)], now):
            changed.append((now, meter.estimate(provider, now)))
    return changed


def ramp(start, end, seconds, step=30.0):
    """Whole-percent readings rising evenly, like the provider reports them."""
    count = int(seconds / step)
    return [round(start + (end - start) * i / count, 2) for i in range(count + 1)]


def test_no_history_means_no_estimate():
    meter = BurnMeter(None)
    assert meter.estimate("openai", T0) is None
    assert meter.observe("openai", [], T0) is False
    assert meter.estimate("openai", T0) is None
    assert burn_note(None) == ""


def test_codex_weekly_rise_over_five_hours_sets_the_burn_time():
    # 2026-10-04: the Codex weekly bar went 7% -> 70% in five hours after a reset.
    meter = BurnMeter(None)
    changed = feed(meter, "openai", ramp(0.07, 0.70, 5 * HOUR))
    estimate = meter.estimate("openai", T0 + 5 * HOUR)
    assert estimate["rise"] == pytest.approx(0.63, abs=0.01)
    assert estimate["span"] == 5 * HOUR
    assert estimate["s"] == pytest.approx(5 * HOUR / 0.63, rel=0.02)
    assert estimate["from"] == T0 and estimate["to"] == T0 + 5 * HOUR
    assert changed and changed[0][0] >= T0 + MIN_SPAN
    assert burn_note(estimate).startswith("~7h ")
    assert "rose 63% in 5h 00m 00s" in burn_note(estimate)


def test_claude_slow_rise_gives_a_long_burn_time_and_short_rises_do_not_count():
    # 2026-09-20: the Claude weekly bar rose 10% in about five hours while paced.
    meter = BurnMeter(None)
    feed(meter, "anthropic", ramp(0.39, 0.49, 5 * HOUR + 15 * 60))
    estimate = meter.estimate("anthropic", T0 + 6 * HOUR)
    assert 50 * HOUR <= estimate["s"] <= 55 * HOUR
    # A 4% move in five hours is rounding, not evidence.
    quiet = BurnMeter(None)
    feed(quiet, "anthropic", ramp(0.10, 0.14, 6 * HOUR))
    assert quiet.estimate("anthropic", T0 + 6 * HOUR) is None


def test_faulty_full_readings_and_rollovers_do_not_count_as_rises():
    # 2026-09-28: a credential expiry read the weekly bar as 100% for a day, then it
    # dropped back. Neither the jump up nor the drop is agents working.
    meter = BurnMeter(None)
    readings = [0.02] * 200 + [1.0] * 1200 + [0.02] * 1200
    feed(meter, "anthropic", readings)
    assert meter.estimate("anthropic", T0 + len(readings) * 30) is None
    # A genuine rollover (a drop to zero) also starts a fresh run.
    meter = BurnMeter(None)
    readings = ramp(0.60, 0.70, 3 * HOUR) + [0.0] + ramp(0.0, 0.08, 3 * HOUR)
    feed(meter, "openai", readings)
    assert meter.estimate("openai", T0 + len(readings) * 30) is None


def test_a_gap_in_readings_breaks_the_run():
    meter = BurnMeter(None)
    meter.observe("openai", [weekly(0.10)], T0)
    meter.observe("openai", [weekly(0.60)], T0 + MAX_GAP + 1)
    meter.observe("openai", [weekly(0.60)], T0 + MAX_GAP + 1 + MIN_SPAN)
    assert meter.estimate("openai", T0 + 2 * MAX_GAP + MIN_SPAN) is None


def test_scoped_windows_are_ignored_and_providers_are_separate():
    meter = BurnMeter(None)
    for index, c in enumerate(ramp(0.0, 0.5, 5 * HOUR)):
        now = T0 + index * 30
        meter.observe("anthropic", [weekly(c * 2, scope="Fable"), weekly(0.1)], now)
        meter.observe("openai", [weekly(c)], now)
    assert meter.estimate("anthropic", T0 + 5 * HOUR) is None
    assert meter.estimate("openai", T0 + 5 * HOUR)["rise"] == pytest.approx(0.5)


def test_the_fastest_rise_wins_and_ages_out():
    meter = BurnMeter(None)
    feed(meter, "openai", ramp(0.0, 0.3, 5 * HOUR))          # 30% in 5h
    later = T0 + 2 * 86400
    for index, c in enumerate(ramp(0.3, 0.4, 5 * HOUR)):     # 10% in 5h, slower
        meter.observe("openai", [weekly(c)], later + index * 30)
    assert meter.estimate("openai", later + 5 * HOUR)["rise"] == pytest.approx(0.3)
    # Four weeks after the fast rise ended, the slower one is all that is left.
    aged = later + 5 * HOUR + TRAILING - 1
    assert meter.estimate("openai", aged)["rise"] == pytest.approx(0.1)
    assert meter.estimate("openai", aged + 2) is None


def test_estimate_survives_a_restart(tmp_path):
    path = tmp_path / "state" / "burn.json"
    meter = BurnMeter(path)
    feed(meter, "openai", ramp(0.07, 0.70, 5 * HOUR))
    before = meter.estimate("openai", T0 + 5 * HOUR)
    assert path.is_file()
    restarted = BurnMeter(path)
    assert restarted.estimate("openai", T0 + 5 * HOUR) == before
    # The run continues after the restart too, rather than starting over: a faster
    # half hour pairs with the readings saved before it.
    restarted.observe("openai", [weekly(0.75)], T0 + 5 * HOUR + 15 * 60)
    restarted.observe("openai", [weekly(0.80)], T0 + 5 * HOUR + 30 * 60)
    after = restarted.estimate("openai", T0 + 6 * HOUR)
    assert after["s"] < before["s"] and after["to"] == T0 + 5 * HOUR + 30 * 60


def test_unreadable_state_is_ignored_and_rewritten(tmp_path, caplog):
    path = tmp_path / "burn.json"
    path.write_text("{not json")
    meter = BurnMeter(path)
    assert meter.state == {}
    assert "ignoring unreadable" in caplog.text
    meter.observe("openai", [weekly(0.1)], T0)
    assert json.loads(path.read_text())["openai"]["last"] == [T0, 0.1]


def test_tick_record_carries_the_estimate():
    record = tick_record(T0, "open", "nothing", [], 0, 0, 1, 0, burn={"s": 26091})
    assert record["burn"] == {"s": 26091}
    assert tick_record(T0, "open", "nothing", [], 0, 0, 1, 0)["burn"] is None
