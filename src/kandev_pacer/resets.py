"""Account-wide reset redemption shared by the Kandev and T3 loops."""

import fcntl
import hashlib
import json
import logging
import os
import tempfile
import urllib.request
import uuid
from pathlib import Path

from . import provider
from .quota import reset_plan, windows_by_provider

log = logging.getLogger("kandev-pacer")


def _post(url, token, body, account_id=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": provider.CODEX_USER_AGENT
        if account_id is not None
        else provider.CLAUDE_USER_AGENT,
    }
    if account_id is not None:
        headers["chatgpt-account-id"] = account_id
    else:
        headers["anthropic-beta"] = provider.BETA_HEADER
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=provider.TIMEOUT) as response:
            result = json.loads(response.read())
            if not isinstance(result, dict):
                raise ValueError("reset response must be an object")
            return result
    except Exception as error:
        raise provider.QuotaUnavailable(
            f"reset request unconfirmed ({type(error).__name__}); retrying the same request"
        ) from error


def _identity(name, config, now):
    if name == "openai":
        tokens = provider.read_codex_credentials(config.codex_credentials_path)
        token = provider.codex_access_token(tokens, now)
        account = tokens.get("account_id")
        if not account:
            raise provider.QuotaUnavailable(
                "Codex reset needs a subscription account ID"
            )
        return account, token
    oauth = provider.read_credentials(config.credentials_path)
    token = provider.access_token(oauth, now)
    try:
        account = json.loads(Path(config.claude_account_path).expanduser().read_text())[
            "oauthAccount"
        ]["organizationUuid"]
        uuid.UUID(account)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise provider.QuotaUnavailable(
            "Claude reset needs organizationUuid in claude_account_path"
        ) from error
    return account, token


def _save(path, record):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _fresh(name, now):
    cache = provider._codex_cache if name == "openai" else provider._cache
    cache.usage = None
    return provider.direct_read(name, True, now)


def _credits(usage, name) -> list[dict] | None:
    return next(
        (
            a["usage"].get("reset_credits")
            for a in usage.get("agents", [])
            if (a.get("usage") or {}).get("provider") == name
        ),
        None,
    )


def _confirmed(record, usage, name):
    credits = _credits(usage, name)
    if credits is None:
        return False
    remaining = sum(c["count"] for c in credits if c["id"] == record["credit"]["id"])
    weekly = [
        w
        for w in windows_by_provider(usage).get(name, [])
        if w["duration_s"] == 7 * 86400 and not w.get("scope")
    ]
    return (
        remaining < record["credit"]["count"]
        and bool(weekly)
        and all(w["consumed_frac"] < 1 for w in weekly)
    )


def redeem(name, config, now, burn=None):
    """Lock, re-read, and keep a retry key until both credit and quota confirm.

    `burn` is the provider's measured burn time in seconds, or None; see `reset_plan`.
    """
    account, token = _identity(name, config, now)
    directory = Path(config.reset_state_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(f"{name}:{account}".encode()).hexdigest()
    path = directory / f"{key}.json"
    with (directory / f"{key}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        usage = _fresh(name, now)
        record: dict | None = json.loads(path.read_text()) if path.exists() else None
        if record and _confirmed(record, usage, name):
            path.unlink()
            return usage
        if record is None:
            windows, count = reset_plan(
                usage, windows_by_provider(usage).get(name, []), now, name, burn
            )
            exhausted = any(
                w["consumed_frac"] >= 1
                and w.get("pace_resets_at") is not None
                and not w.get("scope")
                and w["resets_at"] > now
                for w in windows
            )
            if not count or not exhausted:
                return usage
            credits = _credits(usage, name) or []
            if not credits:
                return usage
            credit = credits[0]
            record = {
                "provider": name,
                "credit": credit,
                "request_id": str(uuid.uuid4()),
            }
            _save(path, record)
        credit = record["credit"]
        if name == "openai":
            response = _post(
                provider.CODEX_CREDITS_URL + "/consume",
                token,
                {"credit_id": credit["id"], "redeem_request_id": record["request_id"]},
                account,
            )
            outcome = response.get("code")
        else:
            response = _post(
                f"https://api.anthropic.com/api/organizations/{account}/reset_rate_limits",
                token,
                {
                    "program": "cedar_ember",
                    "grant_id": credit["id"],
                    "request_id": record["request_id"],
                },
            )
            outcome = response.get("result")
        if outcome in {
            "nothing_to_reset",
            "no_credit",
            "not_limited",
            "ineligible",
            "cooldown",
        }:
            path.unlink()
            raise provider.QuotaUnavailable(
                f"reset not applied ({outcome}); withholding until next quota read"
            )
        if outcome not in {"reset", "already_redeemed", "already_used"}:
            raise provider.QuotaUnavailable(
                "reset response unconfirmed; retrying the same request"
            )
        fresh = _fresh(name, now)
        if not _confirmed(record, fresh, name):
            raise provider.QuotaUnavailable(
                "reset awaiting fresh quota confirmation; withholding"
            )
        path.unlink()
        log.info("[%s] subscription reset applied and quota confirmed", name)
        return fresh


def pending_providers(config):
    return {
        json.loads(path.read_text()).get("provider")
        for path in Path(config.reset_state_dir).expanduser().glob("*.json")
    }


def apply(usage, errors, names, config, now, burn=None):
    """Redeem only for providers with waiting work, leaving bypassed providers alone.

    `burn` maps a provider to its measured burn time in seconds; see `reset_plan`.
    """
    burn = burn or {}
    pending = pending_providers(config)
    for name in (pending & set(config.providers)) - names:
        errors.setdefault(
            name,
            provider.QuotaUnavailable("pending reset awaits confirmation; withholding"),
        )
    for name in sorted(names - errors.keys()):
        windows, count = reset_plan(
            usage, windows_by_provider(usage).get(name, []), now, name, burn.get(name)
        )
        if name not in pending and (
            not count
            or not any(
                w["consumed_frac"] >= 1 and "pace_resets_at" in w and not w.get("scope")
                for w in windows
            )
        ):
            continue
        try:
            fresh = redeem(name, config, now, burn.get(name))
        except (provider.QuotaUnavailable, OSError, ValueError) as error:
            errors[name] = provider.QuotaUnavailable(str(error))
            continue
        usage = {
            "agents": [
                a
                for a in usage.get("agents", [])
                if (a.get("usage") or {}).get("provider") != name
            ]
            + fresh["agents"]
        }
    return usage, errors
