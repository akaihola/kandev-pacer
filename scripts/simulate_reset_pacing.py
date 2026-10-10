"""Replay the reset planner over simulated weeks to compare redemption policies.

Drives the real `quota_verdict` and `reset_plan` against an idealized provider: one
weekly window, a backlog that never empties once work starts, and a fixed fastest time
to use one weekly allowance (`burn_days`, standing in for the five-hour limits). Nothing
here reads or redeems live quota. The output backs docs/reset-pacing-optimality.md.

    uv run python scripts/simulate_reset_pacing.py
"""

from datetime import UTC, datetime

from kandev_pacer.config import Settings
from kandev_pacer.quota import parse_windows, quota_verdict, reset_plan

HOUR = 3600
DAY = 24 * HOUR
WEEK = 7 * DAY
START = 1_799_712_000  # any week boundary works
STEP = 600


def envelope(name, consumed, roll, credits):
    window = {
        "label": "7-day",
        "utilization_pct": consumed * 100,
        "reset_at": datetime.fromtimestamp(roll, UTC).isoformat(),
    }
    usage = {"provider": name, "windows": [window], "reset_credits": credits}
    return {"agents": [{"usage": usage}]}


def simulate(name, expires_at, burn_days, weeks, policy="pacer", count=1, work_from=0):
    """Weekly allowances used and what happened to the resets.

    `pacer` is the current code: its gate, and redemption at weekly exhaustion, with
    `burn_days` passed in as the measured burn time. `asap` is the reference Codex rule
    since task 35: no weekly speed cap while a reset is unexpired, and redemption at
    100%. `hold` is the proposed Claude rule (task 36): skip redemption when less than
    one burn time is left and the reset outlives the next empty point.
    """
    config = Settings(PACER_PROVIDER=name)
    burn = burn_days * DAY
    credit = {"id": "reset", "count": count, "expires_at": expires_at}
    now, roll, consumed, used, redeemed = START, START + WEEK, 0.0, 0.0, []
    while now < START + weeks * WEEK:
        if now >= roll:
            consumed, roll = 0.0, roll + WEEK
        live = [dict(credit)] if credit["count"] and expires_at > now else []
        usage = envelope(name, consumed, roll, live)
        windows = parse_windows(usage, name)
        _, planned = reset_plan(usage, windows, now, name, burn)
        redeem = consumed >= 1 and bool(live if policy == "asap" else planned)
        if policy == "hold" and roll - now < burn and expires_at > roll + burn:
            redeem = False
        if redeem:
            credit["count"] -= 1
            redeemed.append(f"day {(now - START) / DAY:.2f}")
            consumed = 0.0
            if name == "openai":
                roll = now + WEEK
            continue
        if policy == "asap" and live:
            gate_open = consumed < 1
        else:
            gate_open = quota_verdict(usage, windows, now, config, burn=burn).is_open
        if gate_open and now >= START + work_from * DAY:
            step = min(STEP / burn, 1 - consumed)
            consumed += step
            used += step
        now += STEP
    left = credit["count"]
    fate = f"{left} unused, expired" if left and expires_at <= now else f"{left} held"
    return used, ", ".join(redeemed) or "never", fate if left else "all spent"


SCENARIOS = [
    (
        "A. Claude, 2 resets expire at the rollover, 1-day burn, 1 week",
        {"name": "anthropic", "expires_at": START + WEEK, "count": 2},
        {"burn_days": 1, "weeks": 1},
        ["pacer"],
    ),
    (
        "B. Claude, reset valid 4 weeks, 1-day burn, work starts day 5.9, 2 weeks",
        {"name": "anthropic", "expires_at": START + 4 * WEEK, "work_from": 5.9},
        {"burn_days": 1, "weeks": 2},
        ["pacer", "hold"],
    ),
    (
        "C. Codex, reset expires at the rollover, 1.5-day burn, 6 weeks",
        {"name": "openai", "expires_at": START + WEEK},
        {"burn_days": 1.5, "weeks": 6},
        ["pacer", "asap"],
    ),
    (
        "D. Codex, reset expires 6 hours into next week, 1.5-day burn, 6 weeks",
        {"name": "openai", "expires_at": START + WEEK + 6 * HOUR},
        {"burn_days": 1.5, "weeks": 6},
        ["pacer", "asap"],
    ),
    (
        "E. Codex, reset valid 4 weeks, 1-day burn, work starts day 5.9, 3 weeks",
        {"name": "openai", "expires_at": START + 4 * WEEK, "work_from": 5.9},
        {"burn_days": 1, "weeks": 3},
        ["asap", "pacer"],
    ),
]

if __name__ == "__main__":
    for title, setup, limits, policies in SCENARIOS:
        print(title)
        totals = []
        for policy in policies:
            used, redeemed, fate = simulate(policy=policy, **setup, **limits)
            totals.append(used)
            print(f"  {policy:5}  {used:6.3f} allowances, redeemed {redeemed}; {fate}")
        if len(totals) == 2:
            print(f"  gain   {totals[1] - totals[0]:+6.3f}")
