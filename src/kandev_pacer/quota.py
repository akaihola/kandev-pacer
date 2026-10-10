"""Quota math, usage parsing, and structured observation records."""

from dataclasses import dataclass
from datetime import datetime

from .config import MAX_INTERVAL, MIN_INTERVAL, settings, window_duration


def elapsed_frac(now, resets_at, duration_s):
    """How far through its window we are, 0..1."""
    fraction = 1 - (resets_at - now) / duration_s
    return min(max(fraction, 0.0), 1.0)


def headroom(consumed_frac, elapsed_frac_, alpha=1.0):
    """How far ahead of the burn line this window is. Negative means overspending."""
    return alpha * elapsed_frac_ - consumed_frac


def worst_headroom(windows, now, alpha=1.0):
    """The tightest window's headroom -- the one that decides the gate."""
    return min(
        (
            headroom(
                window["consumed_frac"],
                elapsed_frac(now, window["resets_at"], window["duration_s"]),
                alpha,
            )
            for window in windows
        ),
        default=1.0,
    )


def scope_names(windows):
    """The model scopes among these windows, e.g. `("Fable",)`; empty when none is scoped."""
    return tuple(sorted({window["scope"] for window in windows if window.get("scope")}))


def class_windows(windows, scopes):
    """The window subset one scope class gates on.

    Every shared window -- each model spends those -- plus the scoped windows whose model
    the class can run. A scoped window whose model name the payload leaves out reads as
    shared, so every class gates on it: over-strict, which is the safe direction.
    """
    return [
        window
        for window in windows
        if not window.get("scope") or window["scope"] in scopes
    ]


def window_resume_at(window, now, alpha=1.0):
    """When this window's headroom reaches 0, assuming nothing more is consumed.

    While withheld, `c` is frozen and `e` keeps rising, so the release is the moment
    `alpha*e == c` -- i.e. `e == c/alpha`, which inverts to a wall-clock instant through
    `e = 1 - (resets_at - t)/d`. A window needing `e > 1` cannot heal before it rolls, so
    its answer is the roll itself, where `c` drops to 0 and `h` with it.
    """
    needed_e = window["consumed_frac"] / alpha
    duration, resets_at = window["duration_s"], window["resets_at"]
    if needed_e > 1:
        return resets_at
    return max(now, resets_at - duration * (1 - needed_e))


def resume_at(windows, now, alpha=1.0):
    """When the *gate* reopens: the last window to heal is the one holding it shut."""
    return max((window_resume_at(w, now, alpha) for w in windows), default=None)


def format_wait(seconds):
    """`4h 07m 12s` -- hours dropped when zero, so short waits stay readable."""
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours}h {minutes:02d}m {seconds:02d}s" if hours else f"{minutes}m {seconds:02d}s"


def resume_note(eta, now):
    """The clause a withholding tick ends on, or nothing when there is no estimate."""
    if eta is None:
        return ""
    return (
        f", resumes in ~{format_wait(eta - now)} "
        f"(~{datetime.fromtimestamp(eta):%H:%M})"
    )


def poll_interval(verdict, now, config):
    """How long to sleep before the next tick.

    Negative headroom is the only thing worth converging on, and there the sleep is half
    the estimated wait, clipped to `MIN_INTERVAL`..`MAX_INTERVAL`. Halving converges from
    above: a two-hour wait costs the ceiling's worth of polling, every tick from there
    halves what is left, and the gate is met at most `MIN_INTERVAL` late.

    That band is fixed rather than bounded by `interval`, because the two answer different
    questions. `interval` is how eagerly to look for work that could start *now*, and is
    worth setting to seconds; how often to re-check a wait whose end is already computed
    is not, and polling a two-hour pause at that rate is pure waste. So a low
    PACER_INTERVAL buys a responsive gate without buying a busy one, and the wait paces
    itself either way -- slower than the configured tick, or faster than it.

    At `h >= 0` the configured interval always stands: nothing to climb back to, and it is
    the pacing rate rather than a poll ceiling. That covers an open gate, a blind one, and
    the rare ceiling that binds while `h` is already back above the line.
    """
    if verdict.headroom >= 0 or verdict.resumes_at is None:
        return config.interval
    return min(max(0.5 * (verdict.resumes_at - now), MIN_INTERVAL), MAX_INTERVAL)


@dataclass(frozen=True)
class Verdict:
    """What the quota says about launching right now.

    `gate` is the reason, not just a yes/no, because the log and the operator both
    need to tell "we are ahead of the curve" apart from "we cannot see the quota at
    all" -- the second is a misconfiguration that looks identical if you only record
    that nothing launched.
    """

    gate: str        # open | paused | ceiling | blind
    headroom: float
    message: str     # the line to print; empty when the gate is open
    # When this gate is expected to reopen, or None when there is nothing to wait for:
    # an open gate, or a blind one, whose reopening no arithmetic can predict. The
    # instant rather than the ready-made clause, because the loop paces itself by it as
    # well as printing it -- `resume_note` renders it, and the caller tacks that on,
    # since only the caller knows what else belongs on the line first.
    resumes_at: float | None = None

    @property
    def is_open(self):
        return self.gate == "open"


def available_reset_credits(usage, now, provider):
    return [
        credit
        for agent in usage.get("agents", [])
        if (agent.get("usage") or {}).get("provider") == provider
        for credit in (agent.get("usage") or {}).get("reset_credits") or []
        if credit.get("expires_at") is None or credit["expires_at"] > now
    ]


def reset_plan(usage, windows, now, provider):
    """Divide a weekly allowance over usable resets without spending future quota."""
    credits = available_reset_credits(usage, now, provider)
    weekly = [w for w in windows if w["duration_s"] == 7 * 86400 and not w.get("scope")]
    if not credits or not weekly:
        return windows, 0
    roll = min(w["resets_at"] for w in weekly)
    # Codex replaces the natural rollover. Save longer-lived credits until the
    # last natural week before expiry; Claude can add budget to this week.
    usable = [
        credit
        for credit in credits
        if provider != "openai"
        or credit.get("expires_at") is None
        or credit["expires_at"] <= roll
    ]
    count = sum(credit["count"] for credit in usable)
    if not count:
        return windows, 0
    deadline = min([roll, *(c["expires_at"] for c in usable if c.get("expires_at"))])
    planned = []
    for window in windows:
        copy = dict(window)
        if window["duration_s"] == 7 * 86400:
            start = window["resets_at"] - window["duration_s"]
            end = min(window["resets_at"], deadline)
            duration = max(1.0, (end - start) / (count + 1))
            copy.update(pace_resets_at=start + duration, pace_duration_s=duration)
        planned.append(copy)
    return planned, count


def pacing_elapsed(window, now):
    return elapsed_frac(
        now,
        window.get("pace_resets_at", window["resets_at"]),
        window.get("pace_duration_s", window["duration_s"]),
    )


def quota_verdict(usage, windows, now, config, provider=None):
    """Read the usage payload against the burn line for one provider. Pure: no I/O, no state.

    Defaults to the configured single provider, so the pre-plural callers are unchanged; the
    runtime passes each provider explicitly. The fail-closed `blind` branch is per provider:
    a provider missing from the merged envelope withholds its own tasks and leaves the rest.
    """
    provider = config.provider if provider is None else provider
    if not windows and provider_missing(usage, provider):
        return Verdict(
            "blind",
            1.0,
            (
                f"provider {provider!r} not in usage "
                f"{sorted(usage_providers(usage))} -> WITHHOLDING (check PACER_PROVIDER)"
            ),
        )
    windows, resets = reset_plan(usage, windows, now, provider)
    active = [window for window in windows if _window_active(window, now, config)]
    worst = min(
        (
            headroom(
                window["consumed_frac"],
                pacing_elapsed(window, now),
                _window_alpha(window, config),
            )
            for window in active
        ),
        default=1.0,
    )
    # Withholding with no end in sight reads as an outage. It is not one: the release is
    # arithmetic. An estimate all the same -- a turn still running keeps spending `c`.
    healed = max(
        (_window_resume_at(window, now, config) for window in active),
        default=None,
    )
    # The hard ceiling first: it overrides the burn line, however much headroom says.
    over = [
        w
        for w in windows
        if (
            w["consumed_frac"] >= 1.0
            if "pace_resets_at" in w
            else config.backstop < 1.0 and w["consumed_frac"] > config.backstop
        )
    ]
    if over:
        # A ceiling lifts only when the breaching window rolls -- it reads `c`, which does
        # not fall before then, whatever `e` does. The burn line still has to clear
        # afterwards, so the release is the later of the two.
        rolls = max(w["resets_at"] for w in over)
        reason = (
            "reset budget exhausted"
            if any("pace_resets_at" in w for w in over)
            else f"consumed>{config.backstop:.2f}"
        )
        return Verdict(
            "ceiling",
            worst,
            (f"headroom={worst:+.3f} CEILING ({reason}) -> withholding"),
            max(rolls, healed or now),
        )
    available = sum(c["count"] for c in available_reset_credits(usage, now, provider))
    note = ""
    if resets:
        target = min(w["pace_resets_at"] for w in windows if "pace_resets_at" in w)
        note = (
            f"; {resets} resets planned, redeem at weekly exhaustion, "
            f"target ~{datetime.fromtimestamp(target):%Y-%m-%d %H:%M}"
        )
    elif available:
        note = f"; {available} resets reserved to preserve natural rollover"
    if worst < 0:
        return Verdict("paused", worst, f"headroom={worst:+.3f} PAUSED" + note, healed)
    return Verdict(
        "open", worst, f"headroom={worst:+.3f} open" + note if available else ""
    )


def _window_alpha(window, config):
    if "pace_resets_at" in window:
        return 1.0
    return config.alpha_5h if window["duration_s"] == 5 * 3600 else config.alpha


def _window_active(window, now, config):
    if window["duration_s"] != 5 * 3600 or not config.interactive_hours:
        return True
    start, end = (int(part) for part in config.interactive_hours.split("-"))
    return start <= datetime.fromtimestamp(now).hour < end


def _window_resume_at(window, now, config):
    paced = {
        **window,
        "resets_at": window.get("pace_resets_at", window["resets_at"]),
        "duration_s": window.get("pace_duration_s", window["duration_s"]),
    }
    return window_resume_at(paced, now, _window_alpha(window, config))


def window_records(windows, now, alpha=1.0):
    records = []
    for window in windows:
        elapsed = pacing_elapsed(window, now)
        records.append(
            {
                "label": window.get("label"),
                "c": round(window["consumed_frac"], 4),
                "e": round(elapsed, 4),
                "h": round(
                    headroom(
                        window["consumed_frac"],
                        elapsed,
                        1.0 if "pace_resets_at" in window else alpha,
                    ),
                    4,
                ),
                "d": window["duration_s"],
                "resets_at": window["resets_at"],
            }
        )
    return records


def binding_label(window_recs):
    return min(window_recs, key=lambda window: window["h"])["label"] if window_recs else None


def tick_record(
    now,
    gate,
    action,
    window_recs,
    running,
    launchable,
    cap,
    booked,
    launched=None,
    interval=None,
    provider=None,
    scope=None,
    fallback=False,
    burn=None,
):
    return {
        "t": round(now, 3),
        "kind": "tick",
        # Which subscription this record is about. None on a single-provider run, where the
        # question the record answers -- gate=open, action=nothing, launchable>0 -- has only
        # one subject; named once two are paced, so the same one-line filter still answers it.
        "provider": provider,
        # Which of the provider's scope classes this record is about: the scoped window
        # names the class gates on ("Fable"), or None for the class of every shared window
        # -- which is the whole provider whenever the payload has no scoped window.
        "scope": scope,
        "gate": gate,
        "action": action,
        "wh": min((window["h"] for window in window_recs), default=1.0),
        "bind": binding_label(window_recs),
        "windows": window_recs,
        "running": running,
        "launchable": launchable,
        "cap": cap,
        "booked": booked,
        "launched": launched,
        # How long until the next tick. Without it the gaps between records read as
        # stalls, now that the interval moves with the estimated wait.
        "interval": interval,
        "fallback": fallback,
        # The measured burn time for this provider -- `{s, rise, span, from, to}` from
        # burn.py -- or None until enough history exists. Logged on every tick so an
        # operator can check it against the provider's usage page.
        "burn": burn,
    }


def _epoch(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


def _internal_window(window):
    """One envelope window in the pacer's internal shape, or None if it cannot be placed.

    Duration comes from the payload when it carries one -- Codex states `duration_s`
    (`limit_window_seconds`) outright -- and falls back to the label lookup for Anthropic,
    which states only a reset. Preferring the stated duration is what lets a Codex window of
    any length reach the gate, rather than being dropped because no label prefix matched it.
    A window with neither a known duration nor a reset cannot be put on a burn line, so it is
    dropped -- dropping the binding one is the direction that under-withholds.
    """
    duration = window.get("duration_s") or window_duration(window.get("label"))
    reset_at = window.get("reset_at")
    if duration is None or not reset_at:
        return None
    return {
        "label": window.get("label"),
        "scope": window.get("scope"),
        "consumed_frac": window["utilization_pct"] / 100.0,
        "resets_at": _epoch(reset_at),
        "duration_s": duration,
    }


def parse_windows(usage, provider=None):
    """The quota windows for one provider, as fractions and epoch seconds.

    Defaults to the configured single provider, so a caller passing nothing gets exactly what
    it always got; the runtime passes each configured provider in turn.
    """
    provider = settings.provider if provider is None else provider
    return [
        internal
        for agent in usage.get("agents", [])
        if (agent.get("usage") or {}).get("provider") == provider
        for window in (agent.get("usage") or {}).get("windows", [])
        if (internal := _internal_window(window)) is not None
    ]


def windows_by_provider(usage):
    """Every provider's windows, grouped as `{provider: [window, ...]}`.

    A provider with no usable window is absent from the map rather than present empty, so the
    runtime's blind check -- configured but not in the payload -- still reads off
    `usage_providers`, which counts a provider that answered at all.
    """
    grouped = {}
    for agent in usage.get("agents", []):
        agent_usage = agent.get("usage") or {}
        provider = agent_usage.get("provider")
        if not provider:
            continue
        for window in agent_usage.get("windows", []):
            internal = _internal_window(window)
            if internal is not None:
                grouped.setdefault(provider, []).append(internal)
    return grouped


def usage_providers(usage):
    return {
        (agent.get("usage") or {}).get("provider")
        for agent in usage.get("agents", [])
    } - {None}


def provider_missing(usage, provider):
    providers = usage_providers(usage)
    return bool(providers) and provider not in providers
