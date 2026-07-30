"""The pacing loop and the command-line interface.

One tick runs in three steps, kept apart on purpose:

1. **gather** -- `Pacer.read()` makes the kandev calls and returns a `Snapshot`.
2. **decide** -- `quota.quota_verdict()` and `selection.select_action()` are pure
   functions over that snapshot. Everything worth arguing about is testable with
   plain dictionaries and no fake server.
3. **act** -- `Pacer.attempt()` re-checks the task, then `Pacer.drain()` or
   `Pacer.launch()` makes the single call that spends a turn.
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
from .selection import (
    ActionKey,
    Booking,
    action_key,
    backed_off,
    fail_backoff,
    is_launchable,
    launch_target,
    ordered_tasks,
    pick_intent,
    prune_booked,
    prune_failures,
    select_action,
    task_agent_profile,
    uses_manual_order,
)
from .workflow import Board, lint_workflow, queue_step_id

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
    sidebar: dict | None  # the user's sidebar settings, or None if not consulted


def label(task):
    return task.get("title") or task["id"]


def queued_counts(task_sessions):
    """Pending prompt counts, for every idle session that could hold one.

    Every idle session, not just each task's newest: kandev queues a prompt on whichever
    session the user had open, which is regularly the primary one created with the task.
    Asking only the newest reported an empty queue for a task that had a prompt waiting,
    so the drain never fired and the card sat parked indefinitely.

    A session we cannot ask about is left out, which reads downstream as "empty" -- a
    queue-status hiccup must degrade to launch-only pacing, never stop the tick.
    """
    counts = {}
    for sessions in task_sessions.values():
        for session in sessions:
            if session.get("state") not in DRAINABLE_STATES:
                continue
            try:
                counts[session["id"]] = client.ws_queue_count(session["id"])
            except Exception as error:
                log.warning(f"queue status failed for session {session['id']} ({error}) "
                            "-> treating as empty")
    return counts


@dataclass(frozen=True)
class Outcome:
    """What one attempt did, for the caller to log and record. No I/O of its own.

    An attempt returns None instead when kandev refused it: a refusal spends no
    quota, so there is nothing to report and the tick owes the board another
    candidate.
    """

    action: str            # the tick record's action field: launched/drained/skipped/empty
    message: str           # what to tell the operator, minus the shared headroom prefix
    launched: dict | None = None   # the launch detail, when a turn was actually spent


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
        """Everything a tick needs before it can decide anything.

        One exception, deliberate: the per-session queue counts are fetched later, in
        tick(), because they cost a WebSocket round trip per idle session and are only
        needed once the gate is open and there is room to launch.
        """
        usage = client.get("/api/v1/agents/usage")
        now = time.time()
        tasks = client.paced_tasks(self.board.scope)
        sessions = {task["id"]: client.sessions_of(task["id"]) for task in tasks}
        running = sum(
            1
            for task in tasks
            for session in sessions[task["id"]]
            if session.get("state") in RUNNING_STATES
        )
        # Read once per tick, not once per attempt: the drag order cannot change
        # mid-tick, and MAX_ATTEMPTS candidates would otherwise mean three fetches.
        sidebar = client.user_settings()
        return Snapshot(now, usage, parse_windows(usage), tasks, sessions, running, sidebar)

    # --- 2. decide (the pure parts live in quota.py and workflow.py) ---

    def skip_keys(self, now):
        """Actions this tick must leave alone: already paid for, or backing off."""
        return set(self.booked) | backed_off(self.failed, now)

    def next_action(self, snapshot, queued):
        """The one Candidate to spend this turn on, or None."""
        ordered = ordered_tasks(snapshot.tasks, snapshot.sidebar, self.board.step_ranks)
        return select_action(
            ordered, snapshot.sessions, queued, self.board, self.skip_keys(snapshot.now)
        )

    # --- 3. act ---

    def tick(self):
        """Run one tick. Returns whether the gate was open, for the caller to report."""
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
                task, snapshot.sessions[task["id"]], self.board, self.skip_keys(snapshot.now)
            )
        )

        verdict = quota_verdict(
            snapshot.usage, snapshot.windows, snapshot.now, self.config
        )

        def report(gate, outcome, extra="", warn=False):
            """The one way a tick ends: say what happened, record it, hand back the gate.

            An open gate's lines all share the headroom opening, so it is written here
            once; a closed gate's message already reads as a whole sentence.
            """
            line = f"{outcome.message}{extra}"
            if gate == "open":
                line = f"headroom={verdict.headroom:+.3f} open, {line}"
            (log.warning if warn else log.info)(line)
            self.log_event(tick_record(
                snapshot.now, gate, outcome.action, records, snapshot.running, launchable,
                self.config.concurrency, len(self.booked), outcome.launched,
            ))
            return gate == "open"

        if not verdict.is_open:
            running = f" ({snapshot.running} running)" if verdict.gate == "paused" else ""
            return report(verdict.gate, Outcome("withheld", verdict.message),
                          f"{running}{verdict.resume}")

        cap = self.config.concurrency
        if snapshot.running >= cap:
            return report("open", Outcome("full", f"full ({snapshot.running}/{cap})"))

        queued = queued_counts(snapshot.sessions)
        for _ in range(MAX_ATTEMPTS):
            candidate = self.next_action(snapshot, queued)
            if candidate is None:
                held = f", {len(self.failed)} held after a failed launch" if self.failed else ""
                return report("open", Outcome("nothing", "nothing to launch"),
                              f" ({snapshot.running}/{cap} running{held})")
            # A drain books against the session that holds the queue, not the task's
            # newest -- a booking naming a session the board never lists is pruned next
            # tick, and the same prompt gets drained twice.
            key = action_key(
                candidate.task,
                snapshot.sessions.get(candidate.task["id"], []),
                candidate.session,
            )
            outcome = self.attempt(candidate, key, snapshot)
            if outcome is None:      # refused, no quota spent -- try the next candidate
                continue
            # The occupancy count only means anything when a turn was actually spent.
            spent = f" ({snapshot.running + 1}/{cap})" if outcome.launched else ""
            return report("open", outcome, spent)
        return report(
            "open",
            Outcome("exhausted",
                    f"{MAX_ATTEMPTS} candidates rejected this tick -> waiting for the next one"),
            warn=True,
        )

    def attempt(self, candidate, key, snapshot):
        """Try one action. Returns the Outcome for the caller to report, or None.

        None means kandev refused: the refusal spent no quota, so the tick still owes
        the board a turn and should move on to the next candidate.
        """
        # Re-read: selection ran against a snapshot, and a turn is expensive enough to
        # be worth confirming the task is still where we think it is.
        task = candidate.task
        fresh = client.task_of(task["id"])
        if fresh is None or fresh.get("workflow_step_id", "") != task.get("workflow_step_id", ""):
            return Outcome("skipped", f"{label(task)!r} moved since selection -> skip")
        sessions = client.sessions_of(task["id"])
        if candidate.kind == "drain":
            return self.drain(fresh, key, sessions, snapshot, candidate.session)
        return self.launch(fresh, key, sessions, snapshot)

    def drain(self, task, key, sessions, snapshot, drain_id):
        """Release the user prompt parked on `drain_id`, the session holding the queue.

        Re-read from the fresh session list rather than trusting the snapshot: the state
        may have moved on since selection chose it.
        """
        holder = next((s for s in sessions if s["id"] == drain_id), None)
        if holder is None or holder.get("state") not in DRAINABLE_STATES:
            return Outcome("skipped", f"{label(task)!r} no longer idle -> skip drain")
        try:
            drained = bool((client.ws_drain(holder["id"]) or {}).get("drained"))
        except client.RequestOutcomeUnknown as error:
            self.note_uncertain(key, task, f"drain: {error}")
            return Outcome("uncertain", f"{label(task)!r} drain unconfirmed", launched={})
        except Exception as error:
            self.note_failure(key, task, f"drain rejected: {error}", snapshot.now)
            return None
        if not drained:
            # Emptied between the count and the take: kandev spent no turn, so nor did we.
            return Outcome("empty", f"queue for {label(task)!r} already empty -> no turn spent")

        step = task.get("workflow_step_id", "")
        self.book(task["id"], step, holder["id"], snapshot.now)
        return Outcome(
            "drained",
            f"drained queued prompt for {label(task)!r} at {self.board.name_of(step)!r}",
            {"task": task["id"], "session": holder["id"], "step": step},
        )

    def launch(self, task, key, sessions, snapshot):
        """Start a turn: in place at a work step, or admitting the task from the queue."""
        workflow = self.board.name_of(task.get("workflow_id"))
        target = launch_target(task, self.board)
        if target is None:
            self.note_failure(
                key, task, f"no admission target in workflow {workflow!r}", snapshot.now
            )
            return None
        intent, session_id = pick_intent(sessions)
        # Only intent:start needs the profile, and only because kandev's start path will
        # not resolve one itself. intent:workflow_step is how a profile-switch step spawns
        # its own session, so it stays profile-free -- kandev owns routing, the pacer owns
        # timing.
        profile = task_agent_profile(task) if intent == "start" else None
        try:
            client.ws_launch(task["id"], intent, session_id, target, agent_profile_id=profile)
        except client.RequestOutcomeUnknown as error:
            self.note_uncertain(
                ActionKey(task["id"], task.get("workflow_step_id", ""), session_id),
                task, str(error),
            )
            return Outcome("uncertain", f"{label(task)!r} launch unconfirmed", launched={})
        except Exception as error:
            self.note_failure(key, task, str(error), snapshot.now)
            return None

        step = task.get("workflow_step_id", "")
        self.book(task["id"], step, session_id, snapshot.now)
        return Outcome(
            "launched",
            f"launched {label(task)!r} intent={intent} workflow={workflow!r} "
            f"target={self.board.name_of(target)!r}",
            {"task": task["id"], "intent": intent, "session": session_id, "target": target},
        )

    def note_uncertain(self, key, task, reason):
        """Reserve the slot when kandev may have acted but its answer never arrived.

        Routine, not alarming: kandev answers an accepted launch long after the pacer
        stops listening, so this is what a normal launch looks like from here. Only the
        board can say whether it took, which is exactly what the reservation waits for.
        Deliberately not a failure -- a failure frees the slot for another candidate, and
        that is how the same turn gets started twice.
        """
        now = time.time()
        self.booked[key] = Booking(now, uncertain=True)
        for stale in [k for k in self.failed if k.task == key.task]:
            del self.failed[stale]
        log.info(f"{label(task)!r} is in kandev's hands, unconfirmed ({reason}) -- holding "
                 f"its slot for {self.config.booking_ttl:g}s until the board shows it")
        self.log_event({
            "t": round(now, 3), "kind": "uncertain", "task": task["id"], "reason": reason,
        })

    def book(self, task_id, step, session_id, now):
        """Record a spent turn, and forget this task's failures -- something worked."""
        self.booked[ActionKey(task_id, step, session_id)] = Booking(now)
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
            client.ws_set_state(task["id"], BLOCKED_STATE)
        except Exception as error:
            log.warning(f"could not flag {label(task)!r} {BLOCKED_STATE} ({error})")
            return False
        log.warning(f"flagged {label(task)!r} {BLOCKED_STATE} on the board: {reason}")
        return True

    def run(self):
        """Tick forever. A tick that raises skips its turn rather than ending the run."""
        while True:
            try:
                self.tick()
            except Exception as error:
                log.warning(f"tick error: {error} -> pausing this tick")
                self.log_event({
                    "t": round(time.time(), 3), "kind": "error",
                    "where": "tick", "msg": str(error),
                })
            time.sleep(self.config.interval)


# --- startup reporting ---


def dump_order(pacer):
    """Print the queue launches will work down, without spending a turn to find out."""
    board = pacer.board
    sidebar = client.user_settings()
    placed = ((sidebar or {}).get("sidebar_task_prefs") or {}).get("ordered_task_ids") or []
    if uses_manual_order(sidebar):
        description = (
            f"manual sidebar order over board pull order "
            f"({len(placed)} task(s) placed by hand)"
        )
    else:
        description = "board pull order (no sidebar view sorts by hand)"
    print(f"order: {description}")
    print(f"scope: {[board.name_of(workflow) for workflow in board.scope]}")

    tasks = client.paced_tasks(board.scope)
    ordered = ordered_tasks(tasks, sidebar, board.step_ranks)
    for index, task in enumerate(ordered, start=1):
        print(f"{index:3}. {label(task)}")
    if board.skipped:
        report_out_of_scope(board, {task["id"] for task in tasks}, placed)


def report_out_of_scope(board, in_scope, placed):
    """The workflows not being paced -- and any task you dragged that sits in one.

    Worth its own pass over the board because it costs a tasks call per unpaced
    workflow, and because a task dragged to the top of the sidebar that will never
    launch looks identical, in the sidebar, to one that will.
    """
    print(f"\nnot paced: {len(board.skipped)} workflow(s) out of scope")
    for workflow, reason in board.skipped:
        print(f"  - {board.label_of(workflow)}: {reason}")

    elsewhere = {
        task["id"]: (task, workflow)
        for workflow, _ in board.skipped
        for task in client.tasks_of(workflow["id"])
    }
    for task_id in placed:
        if task_id in in_scope or task_id not in elsewhere:
            continue
        task, workflow = elsewhere[task_id]
        print(f"  ! placed by hand but not paced: {label(task)!r} "
              f"(in {board.label_of(workflow)!r})")


def report_startup(board):
    """What is being paced, what is not, and anything suspicious about either."""
    for workflow_id in board.scope:
        steps = board.steps[workflow_id]
        for warning in lint_workflow(steps):
            log.warning(f"lint: [{board.name_of(workflow_id)}] {warning}")
    log.info(f"pacing {settings.url} every {settings.interval}s (alpha={settings.alpha}, "
             f"concurrency={settings.concurrency}, provider={settings.provider})"
             f"\n  log={settings.log_path or '(disabled)'}")
    for workflow_id in board.scope:
        steps = board.steps[workflow_id]
        works = sorted(board.name_of(step["id"]) for step in steps
                       if step["id"] in board.work_steps)
        log.info(f"  {board.name_of(workflow_id)!r}: work_steps={works} "
                 f"queue={board.name_of(queue_step_id(steps))!r} "
                 f"admission={board.name_of(board.admission.get(workflow_id))!r}")
    for workflow, reason in board.skipped:
        log.info(f"  not paced: {board.label_of(workflow)!r} -- {reason}")


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
    board = Board.scan(entries)
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
        "alpha": settings.alpha, "cap": settings.concurrency,
        "backstop": settings.backstop, "provider": settings.provider,
    })
    pacer.run()
