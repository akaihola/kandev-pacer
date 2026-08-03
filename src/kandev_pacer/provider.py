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

# The Go service's cacheTTL and failureCacheTTL, and host.go's freshMaxAge.
CACHE_TTL = 300.0
FAILURE_TTL = 15.0
FRESH_MAX_AGE = 15.0
# Treat a token expiring within a minute as already expired, as the Go client does: a
# fetch started now would be racing the expiry.
EXPIRY_SKEW = 60.0

TIMEOUT = 15.0


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


def reset_cache():
    global _cache
    _cache = _Cache()


def usage(fresh=False, now=None):
    """The current usage envelope, from cache unless it is older than the caller allows.

    `fresh=True` narrows the window a cached reading may have been taken in from five
    minutes to fifteen seconds. The loop spends that once per pause -- when the estimate
    it computed says the gate is due back -- so a converging pause costs one extra call
    in total rather than one per tick.
    """
    now = time.time() if now is None else now
    max_age = FRESH_MAX_AGE if fresh else CACHE_TTL
    if _cache.usage is not None and now - _cache.at < max_age:
        return _cache.usage
    if _cache.error is not None and now - _cache.error_at < min(FAILURE_TTL, max_age):
        raise _cache.error
    try:
        fetched = fetch(now)
    except QuotaUnavailable as error:
        _cache.error, _cache.error_at = error, now
        raise
    _cache.usage, _cache.at, _cache.error = fetched, now, None
    return fetched
