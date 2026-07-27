"""Configuration: the knobs read from the environment, and the fixed kandev vocabulary.

Two different things live here, deliberately separated. `Settings` is what an operator
sets per run. The constants below it are what kandev calls things -- not knobs, and
changing one is a code change, not a configuration change.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every environment variable the pacer reads, validated once at startup.

    A bad value fails here with the variable's name in the message, rather than as a
    traceback out of whichever line first used it -- an unattended loop that runs all
    week should not start at all on a typo'd number.
    """

    # Field names work as keyword arguments too, so a test can write Settings(alpha=0.5)
    # instead of Settings(PACER_ALPHA=0.5).
    model_config = SettingsConfigDict(populate_by_name=True)

    url: str = Field("http://127.0.0.1:38429", alias="KANDEV_URL")
    ws_url: str = Field("", alias="KANDEV_WS_URL")
    provider: str = Field("anthropic", alias="PACER_PROVIDER")

    # How many turns may be in flight at once.
    concurrency: int = Field(1, alias="PACER_CONCURRENCY", ge=1)
    # Target burn line: alpha < 1 aims below steady, finishing each window with quota spare.
    alpha: float = Field(1.0, alias="PACER_ALPHA", gt=0)
    # Headroom the gate must regain before it reopens, to stop it flapping at h == 0.
    resume_margin: float = Field(0.0, alias="PACER_RESUME_MARGIN", ge=0)
    # Hard ceiling on consumed fraction, whatever the burn line says. 1.0 disables it.
    backstop: float = Field(0.97, alias="PACER_BACKSTOP", gt=0, le=1.0)
    interval: int = Field(120, alias="PACER_INTERVAL", gt=0)
    order: Literal["auto", "custom", "board"] = Field("auto", alias="PACER_ORDER")

    # Seconds before a refused launch is retried; doubles per consecutive failure.
    # Unset means three ticks, so it scales with the interval.
    fail_backoff: int | None = Field(None, alias="PACER_FAIL_BACKOFF", ge=0)
    # Consecutive failures before the card is flagged BLOCKED. 0 keeps the pacer read-only.
    block_after: int = Field(2, alias="PACER_BLOCK_AFTER", ge=0)

    # The database make-paced edits. kandev has no API for stage_type, so enrolling a
    # workflow is a direct write -- see make_paced.py.
    db_path: str = Field(
        str(Path.home() / ".kandev" / "data" / "kandev.db"), alias="KANDEV_DB"
    )

    # Empty disables logging.
    log_path: str = Field(
        str(Path.home() / ".local" / "state" / "kandev-pacer" / "pacer.jsonl"),
        alias="PACER_LOG",
    )

    @property
    def booking_ttl(self):
        """How long a booking is honoured before the task counts as launchable again."""
        return 3 * self.interval

    @field_validator("order", mode="before")
    @classmethod
    def _lowercase(cls, value):
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _derive(self):
        self.url = self.url.rstrip("/")
        if not self.ws_url:
            self.ws_url = (
                self.url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
            )
        if self.fail_backoff is None:
            self.fail_backoff = 3 * self.interval
        return self


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
    "7-day (Fable)": 7 * 24 * 3600,
}
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

# Candidates one tick will try before giving up, so a board of broken cards can't turn
# one tick into a walk of the whole backlog.
MAX_ATTEMPTS = 3
# Ceiling on the failure-backoff doubling: a hopeless task still gets an occasional
# "has the user fixed it yet?" retry.
FAIL_BACKOFF_MAX_MULT = 8
