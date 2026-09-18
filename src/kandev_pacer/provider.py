"""Subscription quota, read from Anthropic directly rather than through kandev.

kandev used to answer `GET /api/v1/agents/usage`. v0.83.0 removed it (upstream a7005e5d2,
reverting #1697) and moved account-wide usage out of core into a plugin, so from 0.83.0 on
the endpoint 404s and every tick died before a verdict was ever formed. The machinery it
was built on is still in kandev's tree; what was withdrawn was our access to it. So this
module is a port of `internal/agent/usage/client_claude.go` plus its cache, and the pacer
now depends on kandev for the *board* only.

Two deliberate departures from the Go original:

*Read-only.* The Go client refreshes an expired token and writes the new one back over
`~/.claude/.credentials.json`. That was safe for kandev as the file's sole owner. It is not
safe here: Claude Code refreshes the same file, and two independent refreshers where each
refresh rotates the stored refresh token is a lost update -- the second writer clobbers the
first, and whoever loses may be holding a token the issuer has already retired. Breaking
the user's login is strictly worse than the outage this module fixes. So an expired token
fails closed with something actionable to do about it, and nothing here ever opens the file
for writing. Failing closed costs nothing real: the turns the pacer releases are run by
kandev as Claude Code sessions against this same credential, so a token too dead to read
the quota with is too dead to spend it with either. In practice the window never opens --
Claude Code refreshes hours ahead of the ~8h expiry, and a tick picks the new token up
immediately, because it re-reads the file every time it fetches.

*Cached.* `poll_interval` halves the estimated wait as a pause converges, down to a 5s
floor, so an uncached port would hit an undocumented endpoint every 5 seconds at exactly
the busiest moment. The Go service never fetched more than once per 5 minutes; that TTL is
carried here, along with the short negative cache, and the `fresh` bypass the deleted
host.go used -- which the loop spends once per pause, when its own estimate says the gate
is due to reopen.
"""

import base64
import json
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import WINDOW_DURATIONS, settings

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
# The name this module reports usage under, and so the value PACER_PROVIDER must match.
# Anything else leaves `parse_windows` with nothing and the gate blind -- which is the
# existing fail-closed behaviour for a provider the payload does not carry.
PROVIDER = "anthropic"

# The second source: Codex, read from ChatGPT directly. Reported as "openai" -- the provider
# a codex-acp agent's subscription burn shows up under (see config.AGENT_PROVIDERS) -- so a
# board pacing it sets PACER_PROVIDERS=anthropic,openai.
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
CODEX_PROVIDER = "openai"
PROMPT_CACHE_TTLS = {PROVIDER: 3600.0, CODEX_PROVIDER: 300.0}
# The endpoint is Cloudflare-fronted and refuses a bare Python-urllib User-Agent -- and a
# browser one -- with 403; it answers only to the Codex CLI's own agent string. So the read
# has to present as codex_cli_rs. The version is cosmetic to the check today (any codex_cli_rs
# version answers), but is kept current in case that tightens -- the live contract test is
# where a future requirement would surface. Verified 2026-08-08: no UA and Mozilla/5.0 both
# 403, `codex_cli_rs/0.147.0` gets 200.
CODEX_USER_AGENT = "codex_cli_rs/0.147.0"

# The Go service's cacheTTL and failureCacheTTL, and host.go's freshMaxAge.
CACHE_TTL = 300.0
FAILURE_TTL = 15.0
FRESH_MAX_AGE = 15.0
# Treat a token expiring within a minute as already expired, as the Go client does: a
# fetch started now would be racing the expiry.
EXPIRY_SKEW = 60.0

TIMEOUT = 15.0


def prompt_cache_ttl(provider):
    return PROMPT_CACHE_TTLS.get(provider)


class QuotaUnavailable(RuntimeError):
    """The quota cannot be read, and the tick must withhold rather than guess.

    Carries the whole operator-facing sentence, because every cause has a different thing
    to do about it -- re-authenticate, check the network, look at the credentials file --
    and "quota unavailable" on its own tells nobody which.
    """


# --- pure: the payload, and what kandev called its parts ---


def limit_label(limit):
    """kandev's window label for one `limits[]` entry, or None for a kind we don't know.

    `claudeLimitLabel`, verbatim. An unknown kind is skipped rather than shown under a
    cryptic label -- and skipping is safe only because `parse_windows` matches labels on
    their family now, so a scoped weekly window under a model name nobody hardcoded still
    reaches the gate.
    """
    kind = limit.get("kind")
    if kind == "session":
        return "5-hour"
    if kind == "weekly_all":
        return "7-day"
    if kind == "weekly_scoped":
        model = (limit.get("scope") or {}).get("model") or {}
        return f"7-day ({model.get('display_name') or 'model'})"
    return None


def claude_windows(payload, now):
    """The utilization windows in the payload, in the shape kandev's API published.

    `limits[]` is preferred when it carries anything usable, exactly as `claudeWindows`
    does: it is the same numbers plus the per-model weekly breakdown. The legacy
    `five_hour`/`seven_day` pair is the fallback for a response that predates it.
    """
    windows = [
        {
            "label": limit_label(limit),
            # The scope's model name ("Fable"), kept so the gate can class tasks by it;
            # None on the shared windows every model spends.
            "scope": ((limit.get("scope") or {}).get("model") or {}).get("display_name"),
            "utilization_pct": limit.get("percent"),
            "reset_at": limit.get("resets_at"),
        }
        for limit in payload.get("limits") or []
    ]
    windows = [
        window for window in windows
        if window["label"] and window["reset_at"] and window["utilization_pct"] is not None
    ]
    if windows:
        return windows
    legacy = [("five_hour", "5-hour"), ("seven_day", "7-day")]
    return [
        {
            "label": label,
            "utilization_pct": (payload[key] or {}).get("utilization"),
            # No resets_at means the window's end is unstated, not that it has none. The
            # Go client assumes a full window from now, and so do we -- the alternative is
            # dropping a window that may be the binding one.
            "reset_at": (payload[key] or {}).get("resets_at")
            or _iso(now + WINDOW_DURATIONS[label]),
        }
        for key, label in legacy
        if payload.get(key) and (payload[key] or {}).get("utilization") is not None
    ]


def usage_envelope(payload, now, plan=""):
    """The provider payload, wrapped as the kandev endpoint used to answer.

    Keeping the old shape is what leaves `parse_windows`, the provider guard, and every
    test written against a real recorded response untouched by the source moving.
    """
    return {
        "agents": [
            {
                "agent_id": "claude-acp",
                "usage": {
                    "provider": PROVIDER,
                    "plan": plan,
                    "windows": claude_windows(payload, now),
                },
            }
        ]
    }


def _iso(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat()


# --- reading the credential, never writing it ---


def read_credentials(path):
    """The `claudeAiOauth` entry, or a QuotaUnavailable saying which part is missing."""
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise QuotaUnavailable(
            f"no Claude credentials at {path} -- log in with Claude Code"
        ) from None
    except (OSError, ValueError) as error:
        raise QuotaUnavailable(f"cannot read {path}: {error}") from error
    oauth = raw.get("claudeAiOauth") or {}
    if not oauth.get("accessToken"):
        raise QuotaUnavailable(
            f"no subscription token in {path} -- an API-key login has no quota to pace"
        )
    return oauth


def access_token(oauth, now):
    """The access token, if it is still good. Never refreshes; see the module docstring."""
    expires_at = (oauth.get("expiresAt") or 0) / 1000.0
    if expires_at - now <= EXPIRY_SKEW:
        raise QuotaUnavailable(
            "quota credentials expired -- re-auth Claude Code (`claude` then `/login`); "
            "the pacer never refreshes the token itself, to avoid racing Claude Code "
            "over the same file"
        )
    return oauth["accessToken"]


def _get(token):
    """The one HTTP call, alone in a function so a test can stand in for it."""
    request = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": f"Bearer {token}", "anthropic-beta": BETA_HEADER},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except Exception as error:
        raise QuotaUnavailable(f"provider usage read failed: {error}") from error


def fetch(now, path=None):
    """One live read of the provider's usage endpoint, in kandev's envelope.

    The credential is re-read here rather than held: whatever Claude Code last wrote is
    what this fetch uses, which is why never refreshing it costs nothing in practice.
    """
    oauth = read_credentials(path or settings.credentials_path)
    payload = _get(access_token(oauth, now))
    return usage_envelope(payload, now, oauth.get("subscriptionType") or "")


# --- the second source: Codex, read from ChatGPT directly ---
#
# Same read-only rule as the Claude credential, for the same reason: Codex CLI owns and
# refreshes ~/.codex/auth.json, and racing that refresh is the lost update provider.py refuses.
# Three shape differences from Anthropic, all handled below: the duration arrives in the
# payload (so it maps onto the internal window better than Anthropic's does), expiry is a JWT
# claim rather than a field, and reset is an epoch rather than an ISO string.

_CODEX_LABELS = {5 * 3600: "5-hour", 24 * 3600: "1-day", 7 * 24 * 3600: "7-day"}


def _codex_label(duration_s):
    """A readable label for a Codex window of `duration_s`, for the log and the paused line.

    Only cosmetic here: the gate reads the duration the payload states, not this. So an
    unfamiliar length still reaches the gate under a `<n>s` label rather than being dropped --
    the opposite of Anthropic, where an unrecognised label *is* a dropped window.
    """
    return _CODEX_LABELS.get(duration_s) or (
        f"{duration_s}s" if duration_s else "codex"
    )


def _jwt_exp(token):
    """The `exp` claim (epoch seconds) of a JWT, decoded without verifying the signature.

    The Codex access token states its expiry as a JWT claim, not a field like the Claude
    credential's `expiresAt`. Verifying the signature is the issuer's job when the token is
    used; all the pacer needs is when to stop trusting a cached read, so it reads the claim
    only. An undecodable token yields None -- treated as "no stated expiry", i.e. used as-is,
    since a live request will reject it soon enough if it really is dead.
    """
    try:
        segment = token.split(".")[1]
        padded = segment + "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(padded)).get("exp")
    except Exception:
        return None


def read_codex_credentials(path):
    """The Codex OAuth tokens, or a QuotaUnavailable saying which part is missing."""
    try:
        raw = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise QuotaUnavailable(
            f"no Codex credentials at {path} -- log in with Codex CLI"
        ) from None
    except (OSError, ValueError) as error:
        raise QuotaUnavailable(f"cannot read {path}: {error}") from error
    if raw.get("auth_mode") == "apikey":
        raise QuotaUnavailable(
            f"Codex login in {path} is api-key (auth_mode=apikey) -- no subscription quota "
            "to pace, only metered spend"
        )
    tokens = raw.get("tokens") or {}
    if not tokens.get("access_token"):
        raise QuotaUnavailable(
            f"no subscription token in {path} -- an API-key Codex login has no quota to pace"
        )
    return tokens


def codex_access_token(tokens, now):
    """The Codex access token, if it is still good. Never refreshes; see the module docstring."""
    exp = _jwt_exp(tokens["access_token"])
    if exp is not None and exp - now <= EXPIRY_SKEW:
        raise QuotaUnavailable(
            "Codex credentials expired -- re-auth Codex CLI; the pacer never refreshes the "
            "token itself, to avoid racing Codex CLI over the same file"
        )
    return tokens["access_token"]


def _codex_get(token, account_id):
    """The one Codex HTTP call, alone in a function so a test can stand in for it."""
    request = urllib.request.Request(
        CODEX_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "chatgpt-account-id": account_id or "",
            # Without this the endpoint 403s (see CODEX_USER_AGENT).
            "User-Agent": CODEX_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except Exception as error:
        raise QuotaUnavailable(f"Codex usage read failed: {error}") from error


def codex_windows(payload, now):
    """The rate-limit windows in a Codex usage payload, in the internal envelope shape.

    Both `primary_window` and `secondary_window` are read: the names are slots, not durations,
    so which is the binding one is not fixed. `limit_window_seconds` carries the duration
    outright, so it rides along as `duration_s`; `parse_windows` prefers it over the label.
    """
    rate = payload.get("rate_limit") or {}
    windows = []
    for slot in ("primary_window", "secondary_window"):
        window = rate.get(slot) or {}
        if window.get("used_percent") is None:
            continue
        duration = window.get("limit_window_seconds")
        reset_at = window.get("reset_at")
        if reset_at is None and window.get("reset_after_seconds") is not None:
            reset_at = now + window["reset_after_seconds"]
        if reset_at is None:
            continue
        windows.append(
            {
                "label": _codex_label(duration),
                "utilization_pct": window["used_percent"],
                "reset_at": _iso(reset_at),
                "duration_s": duration,
            }
        )
    return windows


def codex_envelope(payload, now, plan=""):
    """The Codex payload wrapped in kandev's envelope, so quota.py reads it like any other."""
    return {
        "agents": [
            {
                "agent_id": "codex-acp",
                "usage": {
                    "provider": CODEX_PROVIDER,
                    "plan": plan,
                    "windows": codex_windows(payload, now),
                },
            }
        ]
    }


def codex_fetch(now, path=None):
    """One live read of the Codex usage endpoint, in kandev's envelope. Re-reads the token."""
    tokens = read_codex_credentials(path or settings.codex_credentials_path)
    payload = _codex_get(codex_access_token(tokens, now), tokens.get("account_id"))
    # plan_type rides in the response body, not the credential -- cosmetic, for the log line.
    return codex_envelope(payload, now, payload.get("plan_type") or "")


# --- the cache in front of it ---

@dataclass
class _Cache:
    """Last success and last failure, tracked apart as the Go cache tracks them.

    Apart, so a failed refresh does not evict a still-valid reading: a routine tick keeps
    being served the cached value while only the `fresh` caller sees the error.
    """

    usage: dict | None = None
    at: float = 0.0
    error: QuotaUnavailable | None = None
    error_at: float = 0.0


_cache = _Cache()
_codex_cache = _Cache()


def reset_cache():
    global _cache, _codex_cache
    _cache = _Cache()
    _codex_cache = _Cache()


def _cached_read(cache, fetch_fn, fresh, now):
    """Serve `cache` unless it is staler than the caller allows, else fetch and store.

    One reading and one failure tracked apart, per source: a failed refresh of one provider
    does not evict another's still-valid value, and does not evict its own -- a routine tick
    keeps being served the cached reading while only the `fresh` caller sees the error.
    """
    now = time.time() if now is None else now
    max_age = FRESH_MAX_AGE if fresh else CACHE_TTL
    if cache.usage is not None and now - cache.at < max_age:
        return cache.usage
    if cache.error is not None and now - cache.error_at < min(FAILURE_TTL, max_age):
        raise cache.error
    try:
        fetched = fetch_fn(now)
    except QuotaUnavailable as error:
        cache.error, cache.error_at = error, now
        raise
    cache.usage, cache.at, cache.error = fetched, now, None
    return fetched


def usage(fresh=False, now=None):
    """The Anthropic usage envelope, from cache unless it is older than the caller allows.

    `fresh=True` narrows the window a cached reading may have been taken in from five
    minutes to fifteen seconds. The loop spends that once per pause -- when the estimate
    it computed says the gate is due back -- so a converging pause costs one extra call
    in total rather than one per tick.
    """
    return _cached_read(_cache, fetch, fresh, now)


def codex_usage(fresh=False, now=None):
    """The Codex usage envelope, behind its own cache -- see `usage` for the caching rule."""
    return _cached_read(_codex_cache, codex_fetch, fresh, now)


# The provider names each source answers under, mapping a PACER_PROVIDERS entry to a reader.
# Looked up by `read`, but dispatched through the module globals `usage`/`codex_usage` so a
# test standing in for either is honoured.
SOURCE_PROVIDERS = {"anthropic", CODEX_PROVIDER, "codex"}


def _read_source(name, fresh, now):
    """One provider's usage envelope, from its source. Raises QuotaUnavailable like the sources."""
    if name == "anthropic":
        return usage(fresh=fresh, now=now)
    if name in (CODEX_PROVIDER, "codex"):
        return codex_usage(fresh=fresh, now=now)
    raise QuotaUnavailable(
        f"provider {name!r} has no usage source -- PACER_PROVIDERS names it but nothing reads it"
    )


def read(providers, fresh=False, now=None):
    """Merge the named providers' usage envelopes, and the errors of those that could not.

    Each source is read behind its own cache and fails on its own: a provider whose read fails
    is recorded in the returned `errors` map and left out of the envelope, so the others still
    form a verdict. The runtime decides what a missing provider means -- withhold its tasks --
    while another provider's gate stays as open as its own quota says. When *every* configured
    source fails the caller sees that (errors covers them all) and can fall back to the
    single-provider behaviour of shutting the whole gate.
    """
    now = time.time() if now is None else now
    agents, errors = [], {}
    for name in providers:
        try:
            envelope = _read_source(name, fresh, now)
        except QuotaUnavailable as error:
            errors[name] = error
            continue
        agents.extend(envelope.get("agents", []))
    return {"agents": agents}, errors
