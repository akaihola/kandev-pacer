"""Environment-backed pacer configuration."""

import os
from pathlib import Path

URL = os.environ.get("KANDEV_URL", "http://127.0.0.1:38429").rstrip("/")
PACED_WORKFLOWS = [
    value.strip()
    for value in (os.environ.get("PACED_WORKFLOW_ID") or "").split(",")
    if value.strip()
]
PROVIDER = os.environ.get("PACER_PROVIDER", "anthropic")
CONCURRENCY = int(os.environ.get("PACER_CONCURRENCY", "1"))
ALPHA = float(os.environ.get("PACER_ALPHA", "1.0"))
RESUME_MARGIN = float(os.environ.get("PACER_RESUME_MARGIN", "0.0"))
BACKSTOP = float(os.environ.get("PACER_BACKSTOP", "0.97"))
INTERVAL = int(os.environ.get("PACER_INTERVAL", "120"))
WS_REQUEST_TIMEOUT = float(os.environ.get("PACER_WS_TIMEOUT", "30"))
# kandev answers `session.launch` only once the session it started is done needing it -- the
# pacer's own log has answers arriving 17s to 15 minutes after the send. A *refusal* comes back
# in milliseconds. So a launch waits only long enough to catch a refusal and then treats the
# request as in flight, leaving the board to confirm it. Waiting for the answer instead freezes
# the loop for minutes with nothing printed, which is what made a restarted pacer look hung.
REFUSAL_WINDOW = float(os.environ.get("PACER_REFUSAL_WINDOW", "10"))
ORDER = (os.environ.get("PACER_ORDER") or "auto").lower()
# How long kandev may take to make an accepted launch visible on the board. A reservation has to
# outlive that -- three board re-checks past it -- or the pacer launches the same task twice.
LAUNCH_LATENCY = float(os.environ.get("PACER_LAUNCH_LATENCY", "90"))
BOOKING_TTL = 3 * INTERVAL + LAUNCH_LATENCY
FAIL_BACKOFF = int(os.environ.get("PACER_FAIL_BACKOFF", str(3 * INTERVAL)))
FAIL_BACKOFF_MAX_MULT = 8
BLOCK_AFTER = int(os.environ.get("PACER_BLOCK_AFTER", "2"))
BLOCKED_STATE = "BLOCKED"
MAX_ATTEMPTS = 3
ADMISSION_OVERRIDE = os.environ.get("PACER_ADMISSION_STEP_ID") or None
LOG_PATH = os.environ.get(
    "PACER_LOG",
    str(Path.home() / ".local" / "state" / "kandev-pacer" / "pacer.jsonl"),
)
WS_URL = os.environ.get("KANDEV_WS_URL") or (
    URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
)

WINDOW_DURATIONS = {
    "5-hour": 5 * 3600,
    "7-day": 7 * 24 * 3600,
    "7-day (Fable)": 7 * 24 * 3600,
}
TRANSITION_KINDS = {"move_to_next", "move_to_previous", "move_to_step"}
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
RUNNING_STATES = {"STARTING", "RUNNING"}
READY_STATE = "WAITING_FOR_INPUT"
CREATED_STATE = "CREATED"
IDLE_STATES = {CREATED_STATE, READY_STATE}
DRAINABLE_STATES = {READY_STATE}
