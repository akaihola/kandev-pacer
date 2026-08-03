"""Configuration: the knobs read from the environment, and the fixed kandev vocabulary.

Two different things live here, deliberately separated. `Settings` is what an operator
sets per run. The constants below it are what kandev calls things -- not knobs, and
changing one is a code change, not a configuration change.
"""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Every environment variable the pacer reads, validated once at startup.

    A bad value fails here with the variable's name in the message, rather than as a
    traceback out of whichever line first used it -- an unattended loop that runs all
    week should not start at all on a typo'd number.
    """

    url: str = Field("http://127.0.0.1:38429", alias="KANDEV_URL")
    # Empty means "derive it from url"; see the ws_url property.
    ws_url_override: str = Field("", alias="KANDEV_WS_URL")
    provider: str = Field("anthropic", alias="PACER_PROVIDER")

    # How many turns may be in flight at once.
    concurrency: int = Field(1, alias="PACER_CONCURRENCY", ge=1)
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

    # The database make-paced edits. kandev has no API for stage_type, so enrolling a
    # workflow is a direct write -- see make_paced.py.
    db_path: str = Field(
        str(Path.home() / ".kandev" / "data" / "kandev.db"), alias="KANDEV_DB"
    )

    # Where Claude Code keeps the subscription token the quota is read with. Read, never
    # written -- see provider.py on why the pacer refuses to refresh it.
    credentials_path: str = Field(
        str(Path.home() / ".claude" / ".credentials.json"), alias="PACER_CREDENTIALS"
    )

    # Empty disables logging.
    log_path: str = Field(
        str(Path.home() / ".local" / "state" / "kandev-pacer" / "pacer.jsonl"),
        alias="PACER_LOG",
    )

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


settings = Settings()
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
