"""Configuration: the knobs read from the environment, and the fixed kandev vocabulary.

Two different things live here, deliberately separated. `Settings` is what an operator
sets per run. The constants below it are what kandev calls things -- not knobs, and
changing one is a code change, not a configuration change.
"""

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

# What PACER_APIKEY_POLICY may be. Defined here so the field validator can see it before
# `settings = Settings()` runs at import.
APIKEY_POLICIES = {"withhold", "cap"}


class Settings(BaseSettings):
    """Every environment variable the pacer reads, validated once at startup.

    A bad value fails here with the variable's name in the message, rather than as a
    traceback out of whichever line first used it -- an unattended loop that runs all
    week should not start at all on a typo'd number.
    """

    url: str = Field("http://127.0.0.1:38429", alias="KANDEV_URL")
    # Empty means "derive it from url"; see the ws_url property.
    ws_url_override: str = Field("", alias="KANDEV_WS_URL")
    # The single subscription to gate on. PACER_PROVIDERS supersedes it for more than one;
    # see the `providers` property. Kept as its own field so a lone PACER_PROVIDER, and every
    # single-provider run, reproduces exactly what the pacer did before two were possible.
    provider: str = Field("anthropic", alias="PACER_PROVIDER")
    # Comma- or space-separated list of subscriptions to pace, in priority order. Empty means
    # "just PACER_PROVIDER". Naming two here is what turns on per-provider gating.
    providers_raw: str = Field("", alias="PACER_PROVIDERS")
    # Operator override of which subscription an agent's burn shows up under, for when kandev's
    # billing_type is wrong for pacing. Format: `agent=provider` pairs, comma- or space-
    # separated, e.g. `hermes-acp=openai`. Consulted *before* billing_type, so it can pace an
    # agent kandev reports as api_key but which actually spends a subscription -- Hermes runs
    # gpt-5.6-sol on the Codex OAuth subscription (shared with ~/.codex/auth.json), which kandev
    # nonetheless labels api_key. Without this the pacer would withhold it, or under `cap`
    # launch it ungated -- and it burns the very Codex window the openai gate is pacing.
    agent_providers_raw: str = Field("", alias="PACER_AGENT_PROVIDERS")
    # What to do with a task whose agent is billed per token (billing_type=api_key): it has no
    # subscription window to pace against, so it is launched gated on the concurrency cap
    # alone (`cap`) -- the pacer never withholds work it does not pace. `withhold` never
    # launches it (fail-closed, so an upgrade does not quietly start spending metered money);
    # opt into that when metered spend must stay off unless asked for.
    apikey_policy: str = Field("cap", alias="PACER_APIKEY_POLICY")

    # How many turns may be in flight at once, per provider. `concurrency_total` caps the sum
    # across providers -- the machine running the worktrees is the one genuinely shared thing.
    concurrency: int = Field(1, alias="PACER_CONCURRENCY", ge=1)
    # None means no ceiling on the sum; each provider is limited only by its own cap.
    concurrency_total: int | None = Field(None, alias="PACER_CONCURRENCY_TOTAL", ge=1)
    # Target burn line: alpha < 1 aims below steady, finishing each window with quota spare.
    alpha: float = Field(1.0, alias="PACER_ALPHA", gt=0)
    # Hard ceiling on consumed fraction, whatever the burn line says. 1.0 disables it.
    backstop: float = Field(0.97, alias="PACER_BACKSTOP", gt=0, le=1.0)
    # Seconds between ticks while headroom is at or above the burn line. Below it the tick
    # paces itself on the wait instead, in MIN_INTERVAL..MAX_INTERVAL -- so this can be set
    # low for a responsive gate without polling a two-hour pause at that rate. See
    # quota.poll_interval.
    interval: int = Field(120, alias="PACER_INTERVAL", gt=0)

    # kandev answers session.launch only once the session it started is done needing it --
    # observed 17s to 15 minutes after the send. A *refusal* comes back in milliseconds. So a
    # launch waits only long enough to catch a refusal, then treats the request as in flight
    # and lets the board confirm it. Waiting for the real answer freezes the loop for minutes
    # with nothing printed, which is what made a restarted pacer look hung.
    refusal_window: float = Field(10.0, alias="PACER_REFUSAL_WINDOW", gt=0)
    # How long kandev may take to make an accepted launch visible on the board. A reservation
    # has to outlive that or the pacer launches the same task twice.
    launch_latency: float = Field(90.0, alias="PACER_LAUNCH_LATENCY", ge=0)

    # Unset means "three ticks at the configured interval"; see the fail_backoff property.
    # 0 is a real value -- retry with no backoff at all.
    fail_backoff_override: int | None = Field(None, alias="PACER_FAIL_BACKOFF", ge=0)
    # Consecutive failures before the card is flagged BLOCKED. 0 keeps the pacer read-only.
    block_after: int = Field(2, alias="PACER_BLOCK_AFTER", ge=0)
    # Consecutive launches whose turn left the worktree untouched before the pacer gives
    # up on the card and flags it BLOCKED. This is the only stop on a task that is simply
    # *finished*: nothing on the board distinguishes done from mid-flight, so without it a
    # completed task is relaunched every tick until its context window fills. 0 disables
    # the check and restores that behaviour. See progress.py for what "untouched" reads.
    stall_after: int = Field(5, alias="PACER_STALL_AFTER", ge=0)

    # Where Claude Code keeps the subscription token the quota is read with. Read, never
    # written -- see provider.py on why the pacer refuses to refresh it.
    credentials_path: str = Field(
        str(Path.home() / ".claude" / ".credentials.json"), alias="PACER_CREDENTIALS"
    )
    # Where Codex CLI keeps the ChatGPT token the second quota is read with. Same rule: read,
    # never written -- Codex CLI owns and refreshes it, and racing that refresh is the same
    # lost update the Claude credential refuses to risk.
    codex_credentials_path: str = Field(
        str(Path.home() / ".codex" / "auth.json"), alias="PACER_CODEX_CREDENTIALS"
    )

    # Empty disables logging.
    log_path: str = Field(
        str(Path.home() / ".local" / "state" / "kandev-pacer" / "pacer.jsonl"),
        alias="PACER_LOG",
    )

    # The kandev automation that refills the board from each repository's TASKS.md has a
    # webhook trigger and no schedule; the pacer calls it when a provider is about to run
    # dry. Empty URL turns the feature off. The secret goes in the X-Webhook-Secret
    # header and never into a log line or the JSONL.
    heartbeat_webhook_url: str = Field("", alias="PACER_HEARTBEAT_WEBHOOK_URL")
    heartbeat_webhook_secret: str = Field("", alias="PACER_HEARTBEAT_WEBHOOK_SECRET")
    # Call when a provider's gate is open and its launchable count is below this.
    heartbeat_min_launchable: int = Field(2, alias="PACER_HEARTBEAT_MIN_LAUNCHABLE", ge=0)
    # Seconds between attempts, waived once the pacer has launched a task since the last one.
    heartbeat_min_interval: int = Field(1800, alias="PACER_HEARTBEAT_MIN_INTERVAL", ge=0)
    fallback_after: float = Field(0, alias="PACER_FALLBACK_AFTER", ge=0)
    fallback_profiles_raw: str = Field("", alias="PACER_FALLBACK_PROFILES")

    # --- values derived from the ones above ---
    #
    # Properties, not fields a validator fills in: a derived value that is settable is
    # indistinguishable from a configured one, and its declared type has to admit the
    # unset state forever after. These are always what they say they are.

    @property
    def ws_url(self):
        """The /ws endpoint. Same host as the API unless KANDEV_WS_URL says otherwise."""
        if self.ws_url_override:
            return self.ws_url_override
        return self.url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"

    @property
    def providers(self):
        """The subscriptions to pace, in priority order.

        PACER_PROVIDERS when set, otherwise the single PACER_PROVIDER -- so nothing configured
        the old way changes meaning, and a run naming exactly one provider takes the pre-plural
        path in full (see runtime.py: no agents lookup, no attribution, one tick record).
        """
        names = [name.strip() for name in self.providers_raw.replace(",", " ").split()]
        # dict.fromkeys dedupes while keeping the priority order the operator wrote.
        return list(dict.fromkeys(names)) if names else [self.provider]

    @property
    def agent_provider_overrides(self):
        """`{agent name: provider}` from PACER_AGENT_PROVIDERS -- see the field's comment."""
        overrides = {}
        for pair in self.agent_providers_raw.replace(",", " ").split():
            agent, _, provider = pair.partition("=")
            if agent.strip() and provider.strip():
                overrides[agent.strip()] = provider.strip()
        return overrides

    @property
    def fallback_profiles(self):
        """`{source profile name: fallback profile name}` mappings."""
        return {
            match.group(1).strip(): match.group(2).strip()
            for match in re.finditer(
                r"(.+?)\s*=\s*(.+?)(?=\s+\S+\s*=|$)",
                self.fallback_profiles_raw,
            )
            if match.group(1).strip() and match.group(2).strip()
        }

    @property
    def fail_backoff(self):
        """Seconds to hold a refused task before retrying, before any doubling."""
        if self.fail_backoff_override is None:
            return 3 * self.interval
        return self.fail_backoff_override

    @property
    def booking_ttl(self):
        """How long a booking is honoured before the task counts as launchable again.

        Three ticks at the configured interval, plus however long kandev may take to make
        an accepted launch visible -- a reservation that expires before the board catches
        up is how the same task gets launched twice.

        What does the protecting is the `launch_latency` term, not the tick count: ticks
        are only evenly spaced while `h >= 0`, and a withheld one can be as far apart as
        MAX_INTERVAL whatever `interval` says. Since nothing launches while withheld, a
        reservation outliving the latency is enough however the ticks fall.
        """
        return 3 * self.interval + self.launch_latency

    @field_validator("url", mode="after")
    @classmethod
    def _no_trailing_slash(cls, value):
        # Every caller appends a path that already starts with "/".
        return value.rstrip("/")

    @field_validator("apikey_policy", mode="after")
    @classmethod
    def _known_apikey_policy(cls, value):
        if value not in APIKEY_POLICIES:
            raise ValueError(
                f"PACER_APIKEY_POLICY must be one of {sorted(APIKEY_POLICIES)}, not {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _heartbeat_needs_a_secret(self):
        # A URL without a secret would be refused with 401 every half hour, forever.
        if self.heartbeat_webhook_url and not self.heartbeat_webhook_secret:
            raise ValueError(
                "PACER_HEARTBEAT_WEBHOOK_URL is set but PACER_HEARTBEAT_WEBHOOK_SECRET "
                "is empty: the webhook needs both"
            )
        return self


CONFIG_FIELDS = {
    field.alias: name for name, field in Settings.model_fields.items()
}
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "kandev-pacer" / "config.toml"


def load_settings(config_path=None, overrides: dict[str, Any] | None = None):
    """Load TOML, environment and command-line values in increasing precedence."""
    values = {}
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if path.is_file():
        with path.open("rb") as file:
            values.update({
                Settings.model_fields[name].alias: value
                for name, value in tomllib.load(file).get("kandev-pacer", {}).items()
                if name in Settings.model_fields
            })
    values.update({key: value for key, value in os.environ.items() if key in CONFIG_FIELDS})
    values.update(overrides or {})
    return Settings(**values)  # ty: ignore[invalid-argument-type]


settings = load_settings()
"""Read once, at import, so a bad value stops the pacer before it launches anything.

Modules read attributes off this object rather than importing values, so a test can
point one knob somewhere else without re-importing half the package.
"""


# --- kandev's vocabulary: names and shapes the server defines, not knobs ---

# The payload gives resets_at, not duration, so each window's length is hardcoded.
WINDOW_DURATIONS = {
    "5-hour": 5 * 3600,
    "7-day": 7 * 24 * 3600,
}


def window_duration(label):
    """How long the window behind this label runs, or None if the label is not one of ours.

    Matched on the family the label starts with, not the whole string. A per-model weekly
    limit is labelled `7-day (<whatever the provider calls that model>)`, so an exact-match
    table can only ever list the models someone thought of -- and a label the table misses
    is not reported as odd, it is dropped, which quietly removes a window from the gate.
    Dropping the *binding* window is the direction that under-withholds.
    """
    return next(
        (
            duration
            for prefix, duration in WINDOW_DURATIONS.items()
            if label and label.startswith(prefix)
        ),
        None,
    )


# Which usage-envelope provider an agent's subscription burn shows up under. kandev names the
# *agent* (claude-acp, codex-acp) while the usage payload names the *provider* (anthropic,
# openai), so the two are bridged here, matched on a substring of the agent name. Deliberately
# by agent, not by model: hermes-acp runs an OpenAI model on an API key, and reading `openai`
# out of its model string would gate it on the ChatGPT quota it never touches -- billing_type
# catches that first, but the mapping must not invite it. An agent listed nowhere here is
# unresolvable, and its tasks are withheld rather than paced against a guess.
AGENT_PROVIDERS = {"claude": "anthropic", "codex": "openai"}

# Returned by the task-provider resolver for a profile billed per token: there is no
# subscription window behind it, so PACER_APIKEY_POLICY -- not a gate -- decides its fate.
API_KEY = "__api_key__"


def agent_provider(agent):
    """The usage provider an agent's subscription spends against, or None if unrecognised."""
    name = (agent or "").lower()
    return next(
        (provider for key, provider in AGENT_PROVIDERS.items() if key in name), None
    )


# A step carrying one of these on on_turn_complete routes the task away when a turn ends,
# which is what makes it safe to pace: a task still sitting there has an unfinished turn.
TRANSITION_KINDS = {"move_to_next", "move_to_previous", "move_to_step"}
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}

RUNNING_STATES = {"STARTING", "RUNNING"}
READY_STATE = "WAITING_FOR_INPUT"
CREATED_STATE = "CREATED"          # session prepared when the task was made, never started
IDLE_STATES = {CREATED_STATE, READY_STATE}
DRAINABLE_STATES = {READY_STATE}   # a CREATED session cannot take a queued prompt yet
BLOCKED_STATE = "BLOCKED"

# The band the tick paces itself in while headroom is negative -- fixed, and deliberately
# independent of PACER_INTERVAL, which governs only the at-or-above-the-line rate. The
# floor is how late a reopening may be noticed and how hard kandev is asked at the busiest
# moment; the ceiling keeps a long wait cheap even when the configured interval is seconds.
MIN_INTERVAL = 5
MAX_INTERVAL = 120
# Candidates one tick will try before giving up, so a board of broken cards can't turn
# one tick into a walk of the whole backlog.
MAX_ATTEMPTS = 3
# Deadline for one WebSocket request, connect included. The socket's own timeout is not
# enough on its own: every unrelated pushed event makes recv() return and starts a fresh
# socket timeout, so a busy event stream could keep one request alive forever.
WS_TIMEOUT = 30.0
# Ceiling on the failure-backoff doubling: a hopeless task still gets an occasional
# "has the user fixed it yet?" retry.
FAIL_BACKOFF_MAX_MULT = 8
