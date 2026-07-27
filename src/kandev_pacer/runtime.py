"""The pacing loop and the command-line interface.

One tick runs in three steps, kept apart on purpose:

1. **gather** -- `Pacer.read()` makes every kandev call and returns a `Snapshot`.
2. **decide** -- `quota.quota_verdict()` and `workflow.select_action()` are pure
   functions over that snapshot. Everything worth arguing about is testable with
   plain dictionaries and no fake server.
3. **act** -- `Pacer.attempt()` makes the one call that spends a turn.
"""

import argparse
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from . import client
from .config import (
    BLOCKED_STATE,
    DRAINABLE_STATES,
    MAX_ATTEMPTS,
    RUNNING_STATES,
    settings,
)
from .quota import parse_windows, quota_verdict, tick_record, window_records
from .workflow import (
    ActionKey,
    Board,
    action_key,
    backed_off,
    fail_backoff,
    is_launchable,
    launch_target,
    lint_workflow,
    newest_session,
    ordered_tasks,
    pick_intent,
    prune_booked,
    prune_failures,
    queue_step_id,
    select_action,
    uses_manual_order,
)

log = logging.getLogger("kandev-pacer")


@dataclass(frozen=True)
class Snapshot:
    """Everything one tick read from kandev, before any decision was made."""

    now: float
    usage: dict
    windows: list
    tasks: list
    sessions: dict       # task id -> its sessions
    running: int         # turns in flight right now


def label(task):
    return task.get("title") or task["id"]


class Pacer:
    """The loop's mutable state: what it has spent a turn on, and what it has given up on.

    Both are keyed by (task id, step id, session id), so a booking or a failure stops
    applying the moment the task moves, gets a new session, or is re-queued -- which
    is the whole point of the key being a triple and not just the task id.
    """

    def __init__(self, board, config=settings):
        self.board = board
        self.config = config
        self.booked = {}     # key -> when the turn was spent
        self.failed = {}     # key -> {at, attempts, flagged}
        self._log_failed = False

    # --- the event log ---

    def log_event(self, record):
        """Append a JSONL record without letting log failure stop the pacing loop."""
        if not self.config.log_path:
            return
        try:
            path = Path(self.config.log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as file:
                file.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError as error:
            if not self._log_failed:
                log.warning(f"log: {error} (continuing unlogged)")
                self._log_failed = True

    # --- 1. gather ---

    def read(self):
        """Every kandev read one tick makes. The only place in a tick that fetches."""
        usage = client.request("GET", "/api/v1/agents/usage")
        now = time.time()
        tasks = client.paced_tasks(self.board.scope)
        sessions = {task["id"]: client.sessions_of(task["id"]) for task in tasks}
        running = sum(
            1
            for task in tasks
            for session in sessions[task["id"]]
            if session.get("state") in RUNNING_STATES
        )
        return Snapshot(now, usage, parse_windows(usage), tasks, sessions, running)

    # --- 2. decide (the pure parts live in quota.py and workflow.py) ---

    def skip_keys(self, now):
        """Actions this tick must leave alone: already paid for, or backing off."""
        return set(self.booked) | backed_off(self.failed, now)

    def next_action(self, snapshot, queued):
        """The (kind, task) to spend this turn on, or (None, None)."""
        ordered = ordered_tasks(
            snapshot.tasks,
            client.user_settings(self.config.order),
            self.config.order,
            self.board.step_ranks,
        )
        return select_action(
            ordered,
            snapshot.sessions,
            queued,
            self.board.work_steps,
            self.board.queue_steps,
            self.skip_keys(snapshot.now),
        )

    # --- 3. act ---

    def tick(self, was_open):
        """Run one tick. Returns whether the gate is open, which the next tick needs."""
        snapshot = self.read()
        self.booked = prune_booked(
            self.booked, snapshot.tasks, snapshot.sessions,
            snapshot.now, self.config.booking_ttl,
        )
        self.failed = prune_failures(self.failed, snapshot.tasks, snapshot.sessions)
        records = window_records(snapshot.windows, snapshot.now, self.config.alpha)
        launchable = sum(
            1
            for task in snapshot.tasks
            if is_launchable(
                task, snapshot.sessions[task["id"]],
                self.board.work_steps, self.board.queue_steps,
                self.skip_keys(snapshot.now),
            )
        )

        def emit(gate, action, launched=None):
            self.log_event(tick_record(
                snapshot.now, gate, action, records, snapshot.running, launchable,
                self.config.concurrency, len(self.booked), launched,
            ))

        verdict = quota_verdict(
            snapshot.usage, snapshot.windows, snapshot.now, was_open, self.config
        )
        if not verdict.is_open:
            suffix = f" ({snapshot.running} running)" if verdict.gate == "paused" else ""
            log.info(verdict.message + suffix)
            emit(verdict.gate, "withheld")
            return verdict.is_open

        headroom = verdict.headroom
        cap = self.config.concurrency
        if snapshot.running >= cap:
            log.info(f"headroom={headroom:+.3f} open, full ({snapshot.running}/{cap})")
            emit("open", "full")
            return True

        queued = client.queued_counts(snapshot.sessions)
        for _ in range(MAX_ATTEMPTS):
            kind, task = self.next_action(snapshot, queued)
            if kind is None:
                held = f", {len(self.failed)} held after a failed launch" if self.failed else ""
                log.info(f"headroom={headroom:+.3f} open, nothing to launch "
                         f"({snapshot.running}/{cap} running{held})")
                emit("open", "nothing")
                return True
            key = action_key(task, snapshot.sessions.get(task["id"], []))
            if self.attempt(kind, task, key, headroom, snapshot.running, snapshot.now, emit):
                return True
        log.warning(f"headroom={headroom:+.3f} open, {MAX_ATTEMPTS} candidates rejected "
                    "this tick -> waiting for the next one")
        emit("open", "exhausted")
        return True

    def attempt(self, kind, task, key, headroom, running, now, emit):
        """Try one action; False means the rejection spent no quota, so try another task."""
        # Re-read: selection ran against a snapshot, and a turn is expensive enough to
        # be worth confirming the task is still where we think it is.
        fresh = next(
            (candidate for candidate in client.paced_tasks(self.board.scope)
             if candidate["id"] == task["id"]),
            None,
        )
        if fresh is None or fresh.get("workflow_step_id", "") != task.get("workflow_step_id", ""):
            log.info(f"headroom={headroom:+.3f} open, {label(task)!r} moved since selection "
                     "-> skip")
            emit("open", "skipped")
            return True
        step = fresh.get("workflow_step_id", "")
        sessions = client.sessions_of(task["id"])
        cap = self.config.concurrency

        if kind == "drain":
            newest = newest_session(sessions)
            if newest is None or newest.get("state") not in DRAINABLE_STATES:
                log.info(f"headroom={headroom:+.3f} open, {label(fresh)!r} no longer idle "
                         "-> skip drain")
                emit("open", "skipped")
                return True
            try:
                drained = bool((client.ws_drain(newest["id"]) or {}).get("drained"))
            except Exception as error:
                self.note_failure(key, fresh, f"drain rejected: {error}", now)
                return False
            if not drained:
                log.info(f"headroom={headroom:+.3f} open, queue for {label(fresh)!r} already "
                         "empty -> no turn spent")
                emit("open", "empty")
                return True
            self.book(task["id"], step, newest["id"], now)
            log.info(f"headroom={headroom:+.3f} open -> drained queued prompt for "
                     f"{label(fresh)!r} at {self.board.name_of(step)!r} ({running + 1}/{cap})")
            emit("open", "drained",
                 {"task": task["id"], "session": newest["id"], "step": step})
            return True

        target = launch_target(fresh, self.board.work_steps, self.board.admission)
        if target is None:
            self.note_failure(key, fresh, "no admission target in workflow "
                              f"{self.board.name_of(fresh.get('workflow_id'))!r}", now)
            return False
        intent, session_id = pick_intent(sessions)
        try:
            client.ws_launch(task["id"], intent, session_id, target)
        except Exception as error:
            self.note_failure(key, fresh, str(error), now)
            return False
        self.book(task["id"], step, session_id, now)
        log.info(f"headroom={headroom:+.3f} open -> launched {label(fresh)!r} intent={intent} "
                 f"workflow={self.board.name_of(fresh.get('workflow_id'))!r} "
                 f"target={self.board.name_of(target)!r} ({running + 1}/{cap})")
        emit("open", "launched",
             {"task": task["id"], "intent": intent, "session": session_id, "target": target})
        return True

    def book(self, task_id, step, session_id, now):
        """Record a spent turn, and forget this task's failures -- something worked."""
        self.booked[ActionKey(task_id, step, session_id)] = now
        for key in [key for key in self.failed if key.task == task_id]:
            del self.failed[key]

    def note_failure(self, key, task, reason, now):
        previous = self.failed.get(key) or {}
        attempts = previous.get("attempts", 0) + 1
        self.failed[key] = {
            "at": now,
            "attempts": attempts,
            "flagged": previous.get("flagged", False),
        }
        log.warning(f"kandev refused to start {label(task)!r} ({reason}) -- failure "
                    f"{attempts}, holding it for {fail_backoff(attempts)}s")
        self.log_event({
            "t": round(now, 3), "kind": "failure", "task": task["id"],
            "reason": reason, "attempts": attempts,
        })
        block_after = self.config.block_after
        if block_after and attempts >= block_after and not self.failed[key]["flagged"]:
            self.failed[key]["flagged"] = self.flag_blocked(task, reason)

    def flag_blocked(self, task, reason):
        """Put a task the pacer has given up on where a person will see it: the card."""
        try:
            client.ws_set_state(task["id"], BLOCKED_STATE, "pace-block")
        except Exception as error:
            log.warning(f"could not flag {label(task)!r} {BLOCKED_STATE} ({error})")
            return False
        log.warning(f"flagged {label(task)!r} {BLOCKED_STATE} on the board: {reason}")
        return True

    def run(self):
        """Tick forever. A tick that raises pauses the gate rather than ending the run."""
        was_open = True
        while True:
            try:
                was_open = self.tick(was_open)
            except Exception as error:
                log.warning(f"tick error: {error} -> pausing this tick")
                self.log_event({
                    "t": round(time.time(), 3), "kind": "error",
                    "where": "tick", "msg": str(error),
                })
                was_open = False
            time.sleep(self.config.interval)


# --- startup reporting ---


def dump_order(pacer):
    """Print the queue launches will work down, without spending a turn to find out."""
    board = pacer.board
    sidebar = client.user_settings(settings.order)
    manual = uses_manual_order(sidebar, settings.order)
    placed = ((sidebar or {}).get("sidebar_task_prefs") or {}).get("ordered_task_ids") or []
    description = (
        f"manual sidebar order over board pull order ({len(placed)} task(s) placed by hand)"
        if manual
        else "board pull order (sidebar order not consulted)"
    )
    print(f"order={settings.order} -> {description}")
    print(f"scope: {[board.name_of(workflow) for workflow in board.scope]}")
    tasks = client.paced_tasks(board.scope)
    for index, task in enumerate(
        ordered_tasks(tasks, sidebar, settings.order, board.step_ranks), start=1
    ):
        print(f"{index:3}. {label(task)}")
    if not board.skipped:
        return

    in_scope = {task["id"] for task in tasks}
    elsewhere = {
        task["id"]: (task, workflow)
        for workflow, _ in board.skipped
        for task in client.tasks_of(workflow["id"])
    }
    print(f"\nnot paced: {len(board.skipped)} workflow(s) out of scope")
    for workflow, reason in board.skipped:
        print(f"  - {workflow.get('name') or workflow['id']}: {reason}")
    # A task dragged to the top of the sidebar but sitting in an unpaced workflow will
    # never launch, and the sidebar gives no hint of that. Say so by name.
    for task_id in placed:
        if task_id in in_scope or task_id not in elsewhere:
            continue
        task, workflow = elsewhere[task_id]
        print(f"  ! placed by hand but not paced: {label(task)!r} "
              f"(in {workflow.get('name') or workflow['id']!r})")


def report_startup(board):
    """What is being paced, what is not, and anything suspicious about either."""
    for workflow_id in board.scope:
        steps = board.steps[workflow_id]
        for warning in lint_workflow(steps):
            log.warning(f"lint: [{board.name_of(workflow_id)}] {warning}")
    log.info(f"pacing {settings.url} every {settings.interval}s (alpha={settings.alpha}, "
             f"concurrency={settings.concurrency}, provider={settings.provider}, "
             f"order={settings.order})\n  log={settings.log_path or '(disabled)'}")
    for workflow_id in board.scope:
        steps = board.steps[workflow_id]
        works = sorted(board.name_of(step["id"]) for step in steps
                       if step["id"] in board.work_steps)
        log.info(f"  {board.name_of(workflow_id)!r}: work_steps={works} "
                 f"queue={board.name_of(queue_step_id(steps))!r} "
                 f"admission={board.name_of(board.admission.get(workflow_id))!r}")
    for workflow, reason in board.skipped:
        log.info(f"  not paced: {workflow.get('name') or workflow['id']!r} -- {reason}")


def main():
    parser = argparse.ArgumentParser(
        prog="kandev-pacer",
        description="Pace kandev task launches against the subscription quota. "
                    "Configured entirely through the environment; see the README.",
    )
    parser.add_argument(
        "--dump-order",
        action="store_true",
        help="print the queue launches will work down, then exit (spends no quota)",
    )
    args = parser.parse_args()
    # No timestamps: the pacing lines are read live, and the JSONL log is what carries
    # time. `journalctl` stamps them anyway when this runs as a unit.
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        entries = client.workflow_entries()
    except OSError as error:
        log.error(f"cannot reach kandev at {settings.url} ({error}). "
                  "Is it running? Set KANDEV_URL if it listens elsewhere.")
        return 1
    board = Board.scan(entries, settings.paced_workflows)
    pacer = Pacer(board)
    if args.dump_order:
        dump_order(pacer)
        return None
    if not board.scope:
        log.warning("no workflow is enrolled for pacing (no step tagged stage_type=work "
                    "that routes on turn complete). Run: make-paced '<workflow>' --apply")
        return 1

    report_startup(board)
    pacer.log_event({
        "t": round(time.time(), 3), "kind": "start", "url": settings.url,
        "scope": list(board.scope), "interval": settings.interval,
        "alpha": settings.alpha, "cap": settings.concurrency, "order": settings.order,
        "backstop": settings.backstop, "resume_margin": settings.resume_margin,
        "provider": settings.provider,
    })
    pacer.run()
