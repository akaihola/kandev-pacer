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
from dataclasses import dataclass, field, replace
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import NamedTuple

from . import client, heartbeat, progress, provider, resets
from .burn import BurnMeter, burn_note
from .config import (
    API_KEY,
    BLOCKED_STATE,
    CONFIG_FIELDS,
    DRAINABLE_STATES,
    MAX_ATTEMPTS,
    MAX_INTERVAL,
    MIN_INTERVAL,
    RUNNING_STATES,
    load_settings,
    settings,
)
from .quota import (
    Verdict,
    class_windows,
    poll_interval,
    quota_verdict,
    reset_plan,
    resume_note,
    scope_names,
    tick_record,
    window_records,
    windows_by_provider,
)
from .selection import (
    ActionKey,
    Booking,
    action_key,
    backed_off,
    fail_backoff,
    is_capacity_deferred,
    is_launchable,
    is_observation_consumer,
    is_wip_admitted,
    launch_profile,
    launch_session,
    launch_target,
    matched_scopes,
    note_stall,
    ordered_tasks,
    pick_intent,
    profile_provider,
    prune_booked,
    prune_failures,
    prune_stalls,
    select_action,
    stalled_keys,
    stalled_out,
    task_agent_profile,
    task_models,
    task_provider,
    uses_manual_order,
)
from .workflow import Board, lint_workflow, queue_step_id

log = logging.getLogger("kandev-pacer")


class PacerLogHandler(TimedRotatingFileHandler):
    max_bytes = 50 * 1024 * 1024

    def shouldRollover(self, record):
        if self.stream is None:
            self.stream = self._open()
        if self.stream.tell() + len(self.format(record).encode()) + 1 >= self.max_bytes:
            return 1
        return super().shouldRollover(record)


@dataclass(frozen=True)
class Snapshot:
    """Everything one tick read from kandev, before any decision was made."""

    now: float
    usage: dict
    windows: dict  # provider -> [window, ...]; windows_by_provider(usage)
    tasks: list
    sessions: dict  # task id -> its sessions
    running: (
        int  # every turn in flight right now, for the shared PACER_CONCURRENCY_TOTAL
    )
    sidebar: dict | None  # the user's sidebar settings, or None if not consulted
    # profile id -> {agent, billing_type, model}; empty on a single-provider run with no
    # profile-naming task and no scoped window, which needs neither attribution nor models.
    agents: dict
    # task id -> the provider a launch of it burns, or None to withhold it (unresolvable, an
    # unconfigured provider, or an api-key task under the withhold policy). On a
    # single-provider run every subscription task maps to that one provider; only api-key
    # tasks divert to the cap-gated pseudo-provider.
    task_providers: dict
    # provider -> the QuotaUnavailable of a source that could not be read this tick. A provider
    # in here has a shut gate; the others are unaffected. Empty when every source answered.
    quota_errors: dict
    # task id -> the scope class the task's launch gates in: the scoped window names its
    # models can spend (("Fable",)), or every scope of its provider when the model is
    # unknown. Empty tuples throughout when no payload carries a scoped window.
    task_scopes: dict = field(default_factory=dict)


class GateGroup(NamedTuple):
    """One gate this tick decides over: a provider, and the model scopes it gates on.

    A provider whose payload carries a model-scoped window (the Fable weekly limit)
    splits into scope classes: `scopes` holds every scoped name for the all-windows
    class -- the only class when no model information says otherwise -- and is empty
    for the class of the shared windows alone. With no scoped window in the payload
    there is one group per provider with empty scopes: the pre-scope pacer, unchanged.
    """

    provider: str
    scopes: tuple = ()


def label(task):
    return task.get("title") or task["id"]


def _provider_label(provider_name):
    """A gate's name for the log and the paused line -- the provider, or "api-key"."""
    return "api-key" if provider_name == API_KEY else provider_name


def _group_label(group):
    """Name the provider and any model scopes, even when there is only one class."""
    name = _provider_label(group.provider)
    if group.scopes:
        return f"{name} ({'+'.join(group.scopes)})"
    return name


def queued_counts(task_sessions):
    """Pending prompt counts, for every idle session that could hold one.

    Every idle session, not just each task's newest: kandev queues a prompt on whichever
    session the user had open, which is regularly the primary one created with the task.
    Asking only the newest reported an empty queue for a task that had a prompt waiting,
    so the drain never fired and the card sat parked indefinitely.

    A failed read is unknown (None), so that task cannot launch a workflow prompt
    ahead of a possibly queued user prompt. Other tasks can still proceed.
    """
    counts = {}
    for task_id, sessions in task_sessions.items():
        for session in sessions:
            if session.get("state") not in DRAINABLE_STATES:
                continue
            try:
                counts[session["id"]] = client.ws_queue_count(
                    task_id, session["id"], session.get("queue_incarnation_id")
                )
            except Exception as error:
                counts[session["id"]] = None
                log.warning(
                    f"queue status failed for session {session['id']} ({error}) "
                    "-> withholding automatic launches for this task"
                )
    return counts


class QuotaUnreadable(RuntimeError):
    """The usage endpoint could not be read, so this tick has no quota to reason from.

    Every other read failure costs the tick some information. This one costs it the
    verdict itself: no windows means no headroom, and the gate stays shut for as long
    as it lasts. Named so a long outage says that, rather than reading as one more
    anonymous tick error.
    """


# How long the loop stays quiet about an unchanged failure before restating it. Short
# enough that a live tail still shows a standing outage, long enough that a tick every
# few seconds does not write a line per tick.
ERROR_RESTATE = 600


@dataclass
class TickError:
    """The failure the loop is currently repeating, and how much of it went unsaid."""

    message: str
    since: float      # when this failure was first seen
    told: float       # when it was last reported
    count: int = 1    # ticks that failed this way, reported or not


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


@dataclass
class DeferredLaunch:
    """A backend-owned launch, or its bounded wait for visible session state."""

    key: ActionKey
    sessions: frozenset[str]
    already_running: frozenset[str]
    group: GateGroup
    # None while persisted; a deadline once the marker disappears before replay.
    deadline: float | None = None

    def running(self, sessions):
        selected = launch_session(sessions)
        return bool(
            selected
            and selected.get("state") in RUNNING_STATES
            and selected["id"] not in self.already_running
            and (
                selected["id"] == self.key.session
                or selected["id"] not in self.sessions
            )
        )


# The task the Heartbeat automation runs as. One with a turn in flight is already looking
# for work, so the webhook is not called again behind it.
HEARTBEAT_TITLE = "[Auto] Heartbeat"


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
        self.stalled = {}    # key -> {fingerprint, count, flagged}
        self.deferred: dict[str, DeferredLaunch] = {}
        self.pending_signals = {}  # key -> (deadline, untouched turns)
        # How fast the agents have been seen to empty a weekly allowance, per provider.
        self.burn = BurnMeter(config.burn_state_path)
        # Seconds run() waits after the current tick. Each tick sets it from what the
        # quota says is coming, so a long pause is not polled at a short gate's rate.
        self.interval = config.interval
        # The previous tick's estimate of when the gate reopens. Read only to decide
        # whether this tick's quota reading may come from cache; see usage().
        self.resumes_at: float | None = None
        self._log_failed = False
        self._log_handler = None
        self._log_handler_path = None
        self._tick_error = (
            None  # the failure being repeated, or None if the last tick worked
        )
        self._spent = 0  # turns spent in the current tick, for the shared total cap
        self._noted_withheld = (
            set()
        )  # tasks named as unattributable, so it is said once
        # provider -> the TickError for a source outage being repeated, so a standing outage
        # on one provider is said once and restated slowly, not printed every tick.
        self._provider_errors = {}
        # When the Heartbeat webhook was last *attempted*, so a refused call (409 while a
        # run is active) waits out heartbeat_min_interval like a successful one.
        self._last_heartbeat_at: float | None = None
        # Whether a task was launched since that attempt. The cooldown throttles attempts
        # that produced nothing; one whose task the pacer has since launched (and, as a
        # rule, finished) has earned the next call as soon as the board runs dry again.
        self._launched_since_heartbeat = False
        # A proactive refill may retry once if running work ends before cooldown.
        self._heartbeat_idle_retry = False
        self._gate_closed_at = {}

    # --- the event log ---

    def log_event(self, record):
        """Append a JSONL record without letting log failure stop the pacing loop."""
        if not self.config.log_path:
            return
        try:
            path = Path(self.config.log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if self._log_handler_path != path:
                if self._log_handler:
                    self._log_handler.close()
                self._log_handler = PacerLogHandler(
                    path, when="D", interval=7, backupCount=4, encoding="utf-8"
                )
                self._log_handler.setFormatter(logging.Formatter("%(message)s"))
                self._log_handler_path = path
            handler = self._log_handler
            assert handler is not None
            handler.emit(
                logging.makeLogRecord({"msg": json.dumps(record, separators=(",", ":"))})
            )
        except OSError as error:
            if not self._log_failed:
                log.warning(f"log: {error} (continuing unlogged)")
                self._log_failed = True

    # --- 1. gather ---

    def usage(self, now):
        """The quota reading this tick decides on.

        Cached for five minutes, with one exception: once the previous tick's own estimate
        says the gate is due back, this tick pays for a fresh read. That is what keeps a
        converging pause -- whose interval halves down to `MIN_INTERVAL` -- from turning
        into a five-second poll of the provider, while still noticing the reopening
        promptly. Cost is one extra call per pause rather than one per tick.

        A quota that cannot be read is `QuotaUnreadable`, same as when kandev answered for
        it: no windows means no verdict, so the tick stops and the gate stays shut. What
        changes is the message it carries -- the provider says which of an expired login, a
        missing credentials file or a failed request it was, so the headline about the gate
        arrives with something to do about it.
        """
        due = self.resumes_at is not None and now >= self.resumes_at
        envelope, errors = provider.read(self.config.providers, fresh=due, now=now)
        if len(errors) >= len(self.config.providers):
            # Every configured source failed: no quota at all, so the whole tick withholds --
            # the same position as before providers went plural, raising the same exception
            # with the same sentence for a lone provider.
            raise QuotaUnreadable("; ".join(str(error) for error in errors.values()))
        return envelope, errors

    def attribute(self, task, agents):
        """Which gate a launch of this task runs under: a configured provider, or None.

        `None` withholds the task -- it is unresolvable, resolves to a provider that is not
        configured, or is an api-key task under the `withhold` policy. `API_KEY` is returned
        only under the `cap` policy, where api-key tasks form a pseudo-provider gated on the
        concurrency cap alone. The pacer will not guess which quota a task it cannot attribute
        would spend.
        """
        provider_name = task_provider(task, self.board, agents)
        if provider_name == API_KEY:
            return API_KEY if self.config.apikey_policy == "cap" else None
        return provider_name if provider_name in self.config.providers else None

    def attribute_single(self, task, agents):
        """Which gate a launch of this task runs under on a single-subscription run.

        Everything burns the one provider -- except an api-key task, which has no
        subscription window and so joins the cap-gated pseudo-provider (or is withheld
        under the `withhold` policy). Pacing it against the subscription gate instead
        withholds work behind a window it never touches.
        """
        if task_provider(task, self.board, agents) == API_KEY:
            return API_KEY if self.config.apikey_policy == "cap" else None
        return self.config.providers[0]

    def read(self):
        """Everything a tick needs before it can decide anything.

        One exception, deliberate: the per-session queue counts are fetched later, in
        tick(), because they cost a WebSocket round trip per idle session and are only
        needed once the gate is open and there is room to launch.
        """
        now = time.time()
        usage, quota_errors = self.usage(now)
        windows = windows_by_provider(usage)
        tasks = client.paced_tasks(self.board.scope)
        sessions = {task["id"]: client.sessions_of(task["id"]) for task in tasks}
        running = sum(
            1
            for task in tasks
            for session in sessions[task["id"]]
            if session.get("state") in RUNNING_STATES
        )
        scoped = {name: scope_names(ws) for name, ws in windows.items()}
        # Attribution is only needed, and only paid for, when more than one subscription is
        # paced. With one, every subscription task burns it -- exactly what the pacer assumed
        # before. The one exception is an api-key task: it has no window, so it is
        # attributed (and the agents lookup paid for) whenever a task names a profile that
        # could resolve to one -- otherwise it would be paced against a gate it never spends.
        if len(self.config.providers) > 1:
            agents = client.agents()
            task_providers = {
                task["id"]: self.attribute(task, agents) for task in tasks
            }
        else:
            # A model-scoped window makes the profile's model matter even with one
            # subscription. agents() degrades to {} unreadable, which classes every
            # task on all windows -- stricter, never looser.
            names_profile = any(
                task_agent_profile(task)
                or self.board.step_profiles.get(task.get("workflow_step_id", ""))
                for task in tasks
            )
            agents = client.agents() if (names_profile or any(scoped.values())) else {}
            task_providers = {
                task["id"]: self.attribute_single(task, agents) for task in tasks
            }
        task_scopes = {
            task["id"]: matched_scopes(
                task_models(task, self.board, agents),
                scoped.get(task_providers[task["id"]]) or (),
            )
            for task in tasks
        }
        # Read once per tick, not once per attempt: the drag order cannot change
        # mid-tick, and MAX_ATTEMPTS candidates would otherwise mean three fetches.
        sidebar = client.user_settings()
        return Snapshot(
            now,
            usage,
            windows,
            tasks,
            sessions,
            running,
            sidebar,
            agents,
            task_providers,
            quota_errors,
            task_scopes,
        )

    # --- 2. decide (the pure parts live in quota.py and workflow.py) ---

    def skip_keys(self, now):
        """Actions this tick must leave alone: paid for, backing off, or given up on."""
        return (
            set(self.booked)
            | set(self.pending_signals)
            | backed_off(self.failed, now)
            | stalled_keys(self.stalled, self.config.stall_after)
        )

    def withheld_reason(self, task, agents):
        """Why a withheld task cannot be attributed to a gate -- the specific case, actionable.

        The three are genuinely different situations with different fixes, and lumping them as
        "unresolvable or api-key" left the operator unable to tell which: an api-key task is
        working as configured, an unmapped agent is a code gap, and an unconfigured provider is
        a PACER_PROVIDERS omission.
        """
        raw = task_provider(task, self.board, agents)
        if raw == API_KEY:
            return (
                "runs on an api-key profile (metered, no subscription window) -- set "
                "PACER_APIKEY_POLICY=cap to launch it on the concurrency cap alone"
            )
        if raw is None:
            profile = launch_profile(task, self.board)
            return (
                f"its agent profile ({profile or 'none set'}) is not in GET /api/v1/agents, "
                "or its agent maps to no provider (config.AGENT_PROVIDERS)"
            )
        return f"burns {raw!r}, which is not in PACER_PROVIDERS={self.config.providers}"

    def note_withheld(self, snapshot):
        """Say, once per task, that a task is withheld because it cannot be attributed.

        A task mapped to None launches under no gate -- unresolvable, an unconfigured provider,
        or an api-key task under the `withhold` policy. Silence would leave an operator asking
        why a card never runs, so it is named the first time it is seen and not again, and
        forgotten once it leaves the board so a real recurrence is named afresh."""
        withheld = {
            tid for tid, provider in snapshot.task_providers.items() if provider is None
        }
        by_id = {task["id"]: task for task in snapshot.tasks}
        for task_id in withheld - self._noted_withheld:
            task = by_id.get(task_id, {"id": task_id})
            log.warning(
                f"{label(task)!r} withheld: {self.withheld_reason(task, snapshot.agents)}"
            )
        self._noted_withheld = withheld

    def source_outage_quiet(self, provider_name, message, now):
        """Whether to *suppress* this provider's source-outage line this tick.

        First occurrence and every ERROR_RESTATE are said; the identical repeats between are
        counted silently, exactly as `note_tick_error` does for the whole-tick outage -- a
        standing Codex outage must not print a line every tick while Claude runs on. The tick
        record is still written each tick, so the log dataset stays complete."""
        repeat = self._provider_errors.get(provider_name)
        if repeat is None or repeat.message != message:
            self._provider_errors[provider_name] = TickError(
                message, since=now, told=now
            )
            return False
        repeat.count += 1
        if now - repeat.told >= ERROR_RESTATE:
            repeat.told = now
            return False
        return True

    def note_provider_recoveries(self, snapshot):
        """Close out any provider whose source failed and now reads again, once."""
        for provider_name in list(self._provider_errors):
            if provider_name not in snapshot.quota_errors:
                repeat = self._provider_errors.pop(provider_name)
                log.warning(
                    f"[{provider_name}] quota source recovered after {repeat.count} "
                    f"failed read(s): {repeat.message}"
                )

    def gate_groups(self, snapshot):
        """The gates this tick decides over.

        Each configured provider, split into its scope classes when its payload carries a
        model-scoped window: the all-windows class always exists (it is today's sole
        class, and where every unknown lands), and each distinct class its tasks resolve
        to joins it. Plus the api-key pseudo-provider when the `cap` policy has admitted
        any api-key task to it."""
        groups = []
        for provider_name in self.config.providers:
            all_scopes = scope_names(snapshot.windows.get(provider_name, []))
            classes = {all_scopes} | {
                scopes
                for task_id, scopes in snapshot.task_scopes.items()
                if snapshot.task_providers.get(task_id) == provider_name
            }
            groups += [GateGroup(provider_name, scopes) for scopes in sorted(classes)]
        if API_KEY in snapshot.task_providers.values():
            groups.append(GateGroup(API_KEY))
        return groups

    def verdict_for(self, group, snapshot):
        """One gate group's quota verdict, over exactly the windows its class can spend.

        The api-key pseudo-provider has no window, so its gate is always open and only the
        cap holds it. A provider whose source could not be read this tick is blind -- its
        tasks are withheld, and the other providers are untouched (that is the whole point of
        reading each source apart). The backstop applies per class for free: a breached
        Fable window is only in the Fable class's subset, so it stops only Fable tasks.
        """
        if group.provider == API_KEY:
            return Verdict("open", 1.0, "")
        if group.provider in snapshot.quota_errors:
            return Verdict(
                "blind",
                1.0,
                f"quota unreadable ({snapshot.quota_errors[group.provider]}) -> WITHHOLDING",
            )
        return quota_verdict(
            snapshot.usage,
            class_windows(snapshot.windows.get(group.provider, []), group.scopes),
            snapshot.now,
            self.config,
            provider=group.provider,
        )

    def hold_capacity(self, task, sessions, snapshot, group=None):
        """Remember ownership without recording a failed or completed turn."""
        task_id = task["id"]
        if task_id not in self.deferred:
            if group is None:
                attribute = (
                    self.attribute
                    if len(self.config.providers) > 1
                    else self.attribute_single
                )
                name = attribute(task, snapshot.agents)
                group = GateGroup(
                    name,
                    matched_scopes(
                        task_models(task, self.board, snapshot.agents),
                        scope_names(snapshot.windows.get(name, [])),
                    ),
                )
            self.deferred[task_id] = DeferredLaunch(
                action_key(task, sessions),
                frozenset(session["id"] for session in sessions),
                frozenset(
                    s["id"] for s in sessions if s.get("state") in RUNNING_STATES
                ),
                group,
                None
                if is_capacity_deferred(task)
                else snapshot.now + self.config.booking_ttl,
            )
        elif is_capacity_deferred(task):
            self.deferred[task_id].deadline = None

    def reconcile_capacity(self, snapshot):
        tasks = {task["id"]: task for task in snapshot.tasks}
        for task in snapshot.tasks:
            if is_capacity_deferred(task):
                self.hold_capacity(task, snapshot.sessions[task["id"]], snapshot)
        for task_id, held in list(self.deferred.items()):
            task = tasks.get(task_id)
            if task is not None and is_capacity_deferred(task):
                continue
            if task is None or held.running(snapshot.sessions[task_id]):
                del self.deferred[task_id]
            elif held.deadline is None:
                # Marker removal can precede session visibility. Start this wait once.
                held.deadline = snapshot.now + self.config.booking_ttl
            elif snapshot.now >= held.deadline:
                del self.deferred[task_id]

    def deferred_of(self, snapshot, provider_name=None, scopes=None):
        return sum(
            1
            for task_id, held in self.deferred.items()
            if (provider_name is None or held.group.provider == provider_name)
            and (scopes is None or held.group.scopes in scopes)
            and not held.running(snapshot.sessions.get(task_id, []))
        )

    def running_of(self, provider_name, snapshot):
        """Turns in flight for one provider, counted from the tasks attributed to it."""
        return sum(
            1
            for task_id, sessions in snapshot.sessions.items()
            if snapshot.task_providers.get(task_id) == provider_name
            for session in sessions
            if session.get("state") in RUNNING_STATES
        )

    def total_full(self, snapshot):
        """Whether the shared PACER_CONCURRENCY_TOTAL is reached, counting this tick's spends."""
        total = self.config.concurrency_total
        return (
            total is not None
            and snapshot.running + self._spent + self.deferred_of(snapshot) >= total
        )

    def next_action(self, snapshot, queued, allowed):
        """The one Candidate for a provider to spend a turn on, or None.

        `allowed` is the set of the provider's gate groups whose gate is open, so queue
        order picks among every task whose class may run; None is the single-subscription,
        single-class path with no filter, exactly as before providers went plural.
        """
        ordered = ordered_tasks(snapshot.tasks, snapshot.sidebar, self.board.step_ranks)
        ordered = [task for task in ordered if task["id"] not in self.deferred]
        cache_deadlines = self.cache_deadlines(snapshot)
        if allowed is None:
            return select_action(
                ordered,
                snapshot.sessions,
                queued,
                self.board,
                self.skip_keys(snapshot.now),
                cache_deadlines=cache_deadlines,
            )
        return select_action(
            ordered,
            snapshot.sessions,
            queued,
            self.board,
            self.skip_keys(snapshot.now),
            allowed=allowed,
            provider_of=lambda task, session: GateGroup(
                snapshot.task_providers.get(task["id"]),
                snapshot.task_scopes.get(task["id"], ()),
            ),
            cache_deadlines=cache_deadlines,
        )

    def cache_deadlines(self, snapshot):
        """Warm prompt-cache deadlines, keyed by task; cold tasks stay in queue order."""
        deadlines = {}
        for task in snapshot.tasks:
            provider_name = snapshot.task_providers.get(task["id"])
            ttl = provider.prompt_cache_ttl(provider_name)
            if ttl is None:
                continue
            sessions = snapshot.sessions.get(task["id"], [])
            session = max(sessions, key=lambda item: item.get("started_at", ""),
                          default=None)
            turns = [turn for turn in (session or {}).get(
                "turns", (session or {}).get("task_session_turns", [])
            ) if turn.get("completed_at")]
            if turns:
                ended = max(turn["completed_at"] for turn in turns)
                try:
                    deadlines[task["id"]] = datetime.fromisoformat(
                        ended.replace("Z", "+00:00")
                    ).timestamp() + ttl
                except (TypeError, ValueError):
                    continue
        return {task_id: deadline for task_id, deadline in deadlines.items()
                if deadline > snapshot.now}

    # --- 3. act ---

    def tick(self):
        """Run one tick. Returns whether any gate was open, for the caller."""
        snapshot = self.read()
        if self.config.heartbeat_webhook_url and self.config.heartbeat_archive:
            try:
                cleanup = heartbeat.archive_finished(
                    Path(self.config.heartbeat_db),
                    self.config.heartbeat_workspace,
                    apply=True,
                )
                if cleanup["task_ids"]:
                    log.info(
                        "heartbeat: archived finished runs %s", cleanup["task_ids"]
                    )
                    snapshot = self.read()
            except Exception as error:
                log.warning("heartbeat: cleanup failed: %s", error)
        self.reconcile_capacity(snapshot)
        self.check_signals(snapshot.now, snapshot)
        self.booked = prune_booked(
            self.booked, snapshot.tasks, snapshot.sessions,
            snapshot.now, self.config.booking_ttl,
        )
        self.failed = prune_failures(self.failed, snapshot.tasks, snapshot.sessions)
        self.stalled = prune_stalls(self.stalled, snapshot.tasks, snapshot.sessions)
        self._spent = 0  # turns spent this tick, for the shared total cap

        waiting = {
            snapshot.task_providers[task["id"]]
            for task in snapshot.tasks
            if is_launchable(
                task,
                snapshot.sessions[task["id"]],
                self.board,
                self.skip_keys(snapshot.now),
            )
        } - {None, API_KEY}
        # A queued prompt on a parked card also needs quota. Read queues only
        # when an exhausted reset-assisted budget could otherwise hide that work.
        exhausted = {
            name
            for name, windows in snapshot.windows.items()
            if any(
                w["consumed_frac"] >= 1 and "pace_resets_at" in w
                for w in reset_plan(snapshot.usage, windows, snapshot.now, name)[0]
            )
        }
        exhausted.update(resets.pending_providers(self.config))
        parked = {
            task["id"]: snapshot.sessions[task["id"]]
            for task in snapshot.tasks
            if snapshot.task_providers[task["id"]] in exhausted - waiting
            and not is_capacity_deferred(task)
        }
        queued_for_reset = queued_counts(parked)
        waiting.update(
            snapshot.task_providers[task_id]
            for task_id, sessions in parked.items()
            if any(
                (queued_for_reset.get(session["id"]) or 0) > 0 for session in sessions
            )
        )
        usage, errors = resets.apply(
            snapshot.usage, snapshot.quota_errors, waiting, self.config, snapshot.now
        )
        snapshot = replace(
            snapshot,
            usage=usage,
            quota_errors=errors,
            windows=windows_by_provider(usage),
        )
        for name, windows in snapshot.windows.items():
            if self.burn.observe(name, windows, snapshot.now):
                log.info(
                    "[%s] burn time %s",
                    _provider_label(name),
                    burn_note(self.burn.estimate(name, snapshot.now)),
                )
        self.note_withheld(snapshot)
        self.note_provider_recoveries(snapshot)
        groups = self.gate_groups(snapshot)
        verdicts = {group: self.verdict_for(group, snapshot) for group in groups}
        # The loop paces itself on the *earliest* reopening: a two-hour Claude pause must not
        # stop it noticing Codex work it could start now. Both are the min across gates.
        self.interval = min(
            (
                poll_interval(verdict, snapshot.now, self.config)
                for verdict in verdicts.values()
            ),
            default=self.config.interval,
        )
        resumes = [v.resumes_at for v in verdicts.values() if v.resumes_at is not None]
        self.resumes_at = min(resumes) if resumes else None

        # Queue counts cost a WebSocket round trip per idle session; fetch once, and only if
        # some gate is open with room to act -- so a shut or full board reads none.
        needs_queue = any(
            verdicts[group].is_open
            and self.running_of(group.provider, snapshot)
            + self.deferred_of(snapshot, group.provider)
            < self.config.concurrency
            and not self.total_full(snapshot)
            for group in groups
        )
        queued = queued_counts(snapshot.sessions) if needs_queue else {}

        any_open = False
        for provider_name in dict.fromkeys(group.provider for group in groups):
            class_verdicts = {
                group: verdicts[group]
                for group in groups
                if group.provider == provider_name
            }
            any_open = (
                self.run_provider(provider_name, class_verdicts, snapshot, queued)
                or any_open
            )
        self.maybe_heartbeat(verdicts, snapshot)
        return any_open

    def launchable_of(self, provider_name, snapshot, scopes=None):
        """Count eligible tasks, optionally restricted to the given scope classes."""
        skip = self.skip_keys(snapshot.now)
        return sum(
            1
            for task in snapshot.tasks
            if snapshot.task_providers.get(task["id"]) == provider_name
            and (scopes is None or snapshot.task_scopes.get(task["id"], ()) in scopes)
            and task["id"] not in self.deferred
            and is_launchable(task, snapshot.sessions[task["id"]], self.board, skip)
        )

    def maybe_heartbeat(self, verdicts, snapshot):
        """Ask the Heartbeat automation for work when a provider is about to run dry.

        Fires for the first provider whose gate is open and whose launchable count is
        under heartbeat_min_launchable: quota is available and the board is about to
        idle. A closed gate with no work needs no new work. Not more often than
        heartbeat_min_interval since the last attempt unless a task was launched in
        between. A successful proactive attempt while work was running may also retry
        once when an open provider becomes idle. Repeated idle attempts stay throttled.
        Skip while a `[Auto] Heartbeat` task has a turn running: it is already looking.
        A failed call is a warning, not a tick error: the board paces without it.
        """
        config = self.config
        open_providers = dict.fromkeys(
            group.provider for group, verdict in verdicts.items() if verdict.is_open
        )
        dry = next(
            (
                (name, launchable)
                for name in open_providers
                if (launchable := self.launchable_of(name, snapshot))
                + self.deferred_of(snapshot, name)
                < config.heartbeat_min_launchable
            ),
            None,
        )
        now = snapshot.now
        remaining = 0
        if self._last_heartbeat_at is not None and not self._launched_since_heartbeat:
            remaining = config.heartbeat_min_interval - (now - self._last_heartbeat_at)
        idle_retry = False
        if (
            remaining > 0
            and self._heartbeat_idle_retry
            and config.heartbeat_min_launchable > 0
            and not self._spent
            # Fallback and unconfirmed actions can book a turn without raising _spent.
            and not any(booking.at >= now for booking in self.booked.values())
            and not self.total_full(snapshot)
        ):
            for name in open_providers:
                scopes = {
                    group.scopes
                    for group, verdict in verdicts.items()
                    if group.provider == name and verdict.is_open
                }
                if (
                    not self.running_of(name, snapshot)
                    and not self.deferred_of(snapshot, name, scopes)
                    and not self.launchable_of(name, snapshot, scopes)
                    and not any(
                        snapshot.task_providers.get(key.task) == name
                        for key in self.booked
                    )
                ):
                    dry = (name, 0)
                    idle_retry = True
                    break
        if dry is None:
            return
        if not config.heartbeat_webhook_url:
            log.info(
                "heartbeat: disabled; set PACER_HEARTBEAT_WEBHOOK_URL and "
                "PACER_HEARTBEAT_WEBHOOK_SECRET to enable refilling"
            )
            return
        if remaining > 0 and not idle_retry:
            log.info("heartbeat: cooldown, retry in %.0fs", remaining)
            return
        if any(
            task.get("title") == HEARTBEAT_TITLE
            and any(
                session.get("state") in RUNNING_STATES
                for session in snapshot.sessions.get(task["id"], [])
            )
            for task in snapshot.tasks
        ):
            log.info("heartbeat: already running")
            return
        provider_name, launchable = dry
        if config.heartbeat_preflight:
            try:
                repositories = [
                    Path(path) for path in config.heartbeat_repositories
                ] or [Path.home() / path for path in heartbeat.REPOSITORIES]
                allowed, reason = heartbeat.gate(
                    Path(config.heartbeat_db),
                    repositories,
                    Path(config.pebble_snapshot),
                    config.heartbeat_workspace,
                    Path(config.heartbeat_vault),
                    now,
                )
                if not allowed:
                    log.info("heartbeat: skipped; %s", reason)
                    return
                if reason.startswith("preflight needs attention"):
                    log.warning("heartbeat: %s", reason)
            except Exception as error:
                # An unreadable source needs a report, not a silent empty-board decision.
                log.warning(
                    "heartbeat: preflight failed; running for diagnosis: %s", error
                )
        proactive = (
            not idle_retry
            and not self._launched_since_heartbeat
            and self.running_of(provider_name, snapshot) > 0
        )
        self._last_heartbeat_at = now
        self._launched_since_heartbeat = False
        self._heartbeat_idle_retry = False
        try:
            status = client.post_webhook(
                config.heartbeat_webhook_url,
                config.heartbeat_webhook_secret,
                {"source": "kandev-pacer", "provider": provider_name,
                 "launchable": launchable},
            )
        except Exception as error:
            status = str(error)
        line = f"heartbeat: {provider_name} has {launchable} launchable -> webhook {status}"
        accepted = isinstance(status, int) and 200 <= status < 300
        self._heartbeat_idle_retry = accepted and proactive
        (log.info if accepted else log.warning)(line)
        self.log_event({
            "t": round(now, 3), "kind": "heartbeat", "provider": provider_name,
            "launchable": launchable, "status": status,
        })

    def run_provider(self, provider_name, verdicts, snapshot, queued):
        """Decide and act for one provider. Writes one tick record per scope class,
        spends one turn at most; returns whether any class's gate was open.

        This is the pre-plural tick body scoped to a provider: its own running count and
        cap, and one launch at most. On a run without a model-scoped window it is called
        with one class holding every window, so it behaves exactly as before. With one,
        each class gets its own verdict and its own record, but the classes still share
        the provider's cap and its single turn per tick -- unlike two providers, they
        spend the same subscription and the same machine, so the concurrency-sizing
        argument for one launch per re-read still applies. Queue order picks among the
        tasks of every open class.
        """
        now = snapshot.now
        closed = not any(verdict.is_open for verdict in verdicts.values())
        if closed:
            self._gate_closed_at.setdefault(provider_name, now)
        else:
            self._gate_closed_at.pop(provider_name, None)
        # Keep the single-subscription, single-class selection path unchanged.
        split = len(verdicts) > 1
        plain = (
            len(self.config.providers) == 1
            and provider_name != API_KEY
            and not split
        )

        def tag_of(group):
            return "" if plain and not group.scopes else f"[{_group_label(group)}] "

        running = self.running_of(provider_name, snapshot)

        def launchable_of(group):
            return sum(
                1
                for task in snapshot.tasks
                if snapshot.task_providers.get(task["id"]) == provider_name
                and snapshot.task_scopes.get(task["id"], ()) == group.scopes
                and task["id"] not in self.deferred
                and is_launchable(
                    task, snapshot.sessions[task["id"]], self.board, self.skip_keys(now)
                )
            )

        def report(group, gate, outcome, extra="", warn=False, quiet=False):
            verdict = verdicts[group]
            deferred = self.deferred_of(snapshot, provider_name, {group.scopes})
            if deferred:
                extra += f", {deferred} capacity deferred"
            line = f"{tag_of(group)}{outcome.message}{extra}"
            if gate == "open":
                line = (
                    f"{tag_of(group)}headroom={verdict.headroom:+.3f} open, "
                    f"{outcome.message}{extra}"
                )
            if (
                not quiet
            ):  # a standing source outage is counted silently between restates
                (log.warning if warn else log.info)(line)
            record = tick_record(
                now,
                gate,
                outcome.action,
                window_records(
                    reset_plan(
                        snapshot.usage,
                        class_windows(
                            snapshot.windows.get(provider_name, []), group.scopes
                        ),
                        now,
                        provider_name,
                    )[0],
                    now,
                    self.config.alpha,
                ),
                running,
                launchable_of(group),
                self.config.concurrency,
                len(self.booked),
                outcome.launched,
                round(self.interval, 1),
                provider=_provider_label(provider_name),
                scope="+".join(group.scopes) or None,
                fallback=bool(outcome.launched and outcome.launched.get("fallback")),
                burn=self.burn.estimate(provider_name, now),
            )
            record["deferred"] = deferred
            self.log_event(record)
            return gate == "open"

        open_groups = []
        for group, verdict in verdicts.items():
            if verdict.is_open:
                open_groups.append(group)
                continue
            running_note = f" ({running} running)" if verdict.gate == "paused" else ""
            eta = resume_note(verdict.resumes_at, now)
            # Said alongside the estimate, never on its own: the number only means anything
            # next to the wait it was derived from.
            soon = f", next tick in {self.interval:.0f}s" if eta else ""
            # A source that could not be read is an outage, said once and restated slowly, not
            # a fresh reason to print every tick; a burn-line pause or a misconfig is not.
            quiet = provider_name in snapshot.quota_errors and self.source_outage_quiet(
                provider_name, verdict.message, now
            )
            report(
                group,
                verdict.gate,
                Outcome("withheld", verdict.message),
                f"{running_note}{eta}{soon}",
                quiet=quiet,
            )
        if not open_groups:
            if (
                closed
                and self.config.fallback_after
                and now - self._gate_closed_at[provider_name]
                >= self.config.fallback_after
            ):
                # Existing fallback behavior is unchanged without capacity holds.
                if self.deferred and self.total_full(snapshot):
                    return False
                ordered = ordered_tasks(
                    snapshot.tasks,
                    snapshot.sidebar,
                    self.board.step_ranks,
                )
                ordered = [task for task in ordered if task["id"] not in self.deferred]
                # Fallback cannot drain while the normal gate is closed. Skip
                # pending and unreadable queues so they do not starve other work.
                fallback_queues = queued_counts(snapshot.sessions)
                ordered = [
                    task
                    for task in ordered
                    if all(
                        fallback_queues.get(session["id"], 0) == 0
                        for session in snapshot.sessions.get(task["id"], [])
                    )
                ]
                candidate = select_action(
                    ordered,
                    snapshot.sessions,
                    {},
                    self.board,
                    self.skip_keys(snapshot.now),
                    allowed=set(verdicts),
                    provider_of=lambda task, session: GateGroup(
                        snapshot.task_providers.get(task["id"]),
                        snapshot.task_scopes.get(task["id"], ()),
                    ),
                )
                if (
                    candidate
                    and candidate.kind == "launch"
                ):
                    key = action_key(
                        candidate.task,
                        snapshot.sessions.get(candidate.task["id"], []),
                        None,
                    )
                    outcome = self.attempt(candidate, key, snapshot, fallback=True)
                    if outcome is not None:
                        group = next(iter(verdicts))
                        report(group, verdicts[group].gate, outcome)
                        return False
            return False

        cap = self.config.concurrency
        if running + self.deferred_of(snapshot, provider_name) >= cap:
            for group in open_groups:
                report(group, "open", Outcome("full", f"full ({running}/{cap})"))
            return True
        if self.total_full(snapshot):
            committed = snapshot.running + self._spent + self.deferred_of(snapshot)
            for group in open_groups:
                report(
                    group,
                    "open",
                    Outcome(
                        "full",
                        f"full (total {committed}/{self.config.concurrency_total})",
                    ),
                )
            return True

        allowed = None if plain else set(open_groups)
        for _ in range(MAX_ATTEMPTS):
            candidate = self.next_action(snapshot, queued, allowed)
            if candidate is None:
                held = (
                    f", {len(self.failed)} global retained failure records"
                    if self.failed
                    else ""
                )
                skip = self.skip_keys(now)
                for group in open_groups:
                    # select_action withholds a task whose queue read failed, so it would
                    # otherwise count as launchable with no reason on this line.
                    unread = sum(
                        1
                        for task in snapshot.tasks
                        if snapshot.task_providers.get(task["id"]) == provider_name
                        and snapshot.task_scopes.get(task["id"], ()) == group.scopes
                        and any(
                            session["id"] in queued and queued[session["id"]] is None
                            for session in snapshot.sessions[task["id"]]
                        )
                        and is_launchable(
                            task, snapshot.sessions[task["id"]], self.board, skip
                        )
                    )
                    unread_note = f", {unread} held: queue unreadable" if unread else ""
                    report(
                        group,
                        "open",
                        Outcome("nothing", "nothing to launch"),
                        f" ({running}/{cap} running{held}{unread_note})",
                    )
                return True
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
            # A launch, confirmed or not, is the refill the last heartbeat asked for. A
            # drain releases a prompt the user queued on an existing task: not new work.
            if outcome.launched is not None and candidate.kind == "launch":
                self._launched_since_heartbeat = True
            if outcome.launched:
                self._spent += (
                    1  # counts against the shared total cap for later providers
                )
            # The outcome belongs to the class of the task it acted on; the other open
            # classes had their shot at the shared turn and queue order gave it away, and
            # their records say so -- "yielded", so the gate=open, action=nothing,
            # launchable>0 filter stays free of false positives.
            acted = next(
                (
                    group
                    for group in open_groups
                    if group.scopes
                    == snapshot.task_scopes.get(candidate.task["id"], ())
                ),
                open_groups[0],
            )
            # The occupancy count only means anything when a turn was actually spent.
            spent = f" ({running + 1}/{cap})" if outcome.launched else ""
            report(acted, "open", outcome, spent)
            for group in open_groups:
                if group != acted:
                    report(
                        group,
                        "open",
                        Outcome(
                            "yielded",
                            f"this tick's turn went to {label(candidate.task)!r}",
                        ),
                    )
            return True
        for group in open_groups:
            report(
                group,
                "open",
                Outcome("exhausted",
                        f"{MAX_ATTEMPTS} candidates rejected this tick "
                        "-> waiting for the next one"),
                warn=True,
            )
        return True

    def attempt(self, candidate, key, snapshot, fallback=False):
        """Try one action. Returns the Outcome for the caller to report, or None.

        None means kandev refused: the refusal spent no quota, so the tick still owes
        the board a turn and should move on to the next candidate.
        """
        # Re-read: selection ran against a snapshot, and a turn is expensive enough to
        # be worth confirming the task is still where we think it is.
        task = candidate.task
        fresh = client.task_of(task["id"])
        if fresh is not None and is_capacity_deferred(fresh):
            sessions = client.sessions_of(task["id"])
            self.hold_capacity(fresh, sessions, snapshot)
            return Outcome("deferred", f"{label(task)!r} waiting for session capacity")
        if fresh is None or fresh.get("workflow_step_id", "") != task.get("workflow_step_id", ""):
            return Outcome("skipped", f"{label(task)!r} moved since selection -> skip")
        sessions = client.sessions_of(task["id"])
        if task["id"] in self.deferred:
            return Outcome("deferred", f"{label(task)!r} waiting for session capacity")
        if candidate.kind == "drain":
            return self.drain(fresh, key, sessions, snapshot, candidate.session)
        # Recheck before launch, including fallback paths that skipped queue reads.
        counts = queued_counts({task["id"]: sessions})
        if any(count != 0 for count in counts.values()):
            return Outcome(
                "skipped", f"{label(task)!r} queue pending or unknown -> skip launch"
            )
        return self.launch(fresh, key, sessions, snapshot, fallback=fallback)

    def drain(self, task, key, sessions, snapshot, drain_id):
        """Release the user prompt parked on `drain_id`, the session holding the queue.

        Re-read from the fresh session list rather than trusting the snapshot: the state
        may have moved on since selection chose it.
        """
        holder = next((s for s in sessions if s["id"] == drain_id), None)
        if holder is None or holder.get("state") not in DRAINABLE_STATES:
            return Outcome("skipped", f"{label(task)!r} no longer idle -> skip drain")
        previous = next(
            (s for s in snapshot.sessions.get(task["id"], []) if s["id"] == drain_id),
            None,
        )
        incarnation = holder.get("queue_incarnation_id")
        if (
            not incarnation
            or not previous
            or previous.get("queue_incarnation_id") != incarnation
        ):
            return Outcome(
                "skipped", f"{label(task)!r} changed queue identity -> skip drain"
            )
        try:
            drained = bool(
                (client.ws_drain(task["id"], holder["id"], incarnation) or {}).get(
                    "drained"
                )
            )
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

    def launch(self, task, key, sessions, snapshot, fallback=False):
        """Start a turn: in place at a work step, or admitting the task from the queue."""
        # Before anything is spent: has the last run of launches achieved anything? The
        # fingerprint read here is what the *previous* turn left behind, so comparing it
        # against the one taken at the previous launch is what says whether that turn did
        # any work. A finished task answers "no" every time, forever, and nothing on the
        # board would ever say so.
        if action_key(task, sessions) != key:
            self.stalled.pop(key, None)
            return Outcome("skipped", f"{label(task)!r} changed session -> skip")
        previous_stall = self.stalled.get(key)
        self.stalled[key] = note_stall(self.stalled, key, progress.fingerprint(sessions))
        if stalled_out(self.stalled, key, self.config.stall_after):
            return self.give_up(key, task, sessions)
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
        if fallback and intent == "start":
            source = snapshot.agents.get(profile)
            target_name = self.config.fallback_profiles.get((source or {}).get("name"))
            fallback_profile = next(
                (
                    pid for pid, info in snapshot.agents.items()
                    if info.get("name") == target_name
                ),
                None,
            )
            if fallback_profile:
                profile = fallback_profile
            else:
                return Outcome("skipped", f"fallback unavailable for {label(task)!r}")
        group = GateGroup(
            snapshot.task_providers.get(task["id"]),
            snapshot.task_scopes.get(task["id"], ()),
        )
        if profile and (fallback or len(self.config.providers) > 1):
            name = profile_provider(profile, snapshot.agents)
            models = task_models(
                {"metadata": {"agent_profile_id": profile}}, self.board, snapshot.agents
            )
            group = GateGroup(
                name,
                matched_scopes(models, scope_names(snapshot.windows.get(name, []))),
            )
        if (
            fallback
            and self.deferred
            and (
                self.running_of(group.provider, snapshot)
                + self.deferred_of(snapshot, group.provider)
                >= self.config.concurrency
                or self.total_full(snapshot)
            )
        ):
            if previous_stall is None:
                self.stalled.pop(key, None)
            else:
                self.stalled[key] = previous_stall
            return Outcome("full", "fallback waiting for committed capacity")
        confirmation_unknown = False
        try:
            response = client.ws_launch(
                task["id"], intent, session_id, target, agent_profile_id=profile
            )
        except client.RequestOutcomeUnknown as error:
            self.note_uncertain(
                ActionKey(task["id"], task.get("workflow_step_id", ""), session_id),
                task, str(error),
            )
            return Outcome("uncertain", f"{label(task)!r} launch unconfirmed", launched={})
        except Exception as error:
            if (
                str(error)
                != "session.launch failed: session ceiling deferred the workflow step launch"
            ):
                self.note_failure(key, task, str(error), snapshot.now)
                return None
            try:
                fresh = client.task_of(task["id"])
            except client.ReadFailed:
                confirmation_unknown = True
                fresh = None
                # The backend may own it; reconcile on the next successful read.
            else:
                if not fresh or not is_capacity_deferred(fresh):
                    self.note_failure(key, task, str(error), snapshot.now)
                    return None
            task = fresh or task
            response = {
                "activation_disposition": "queued",
                "activation_reason": "session_capacity",
            }

        if (
            isinstance(response, dict)
            and response.get("activation_disposition") == "queued"
            and response.get("activation_reason") == "session_capacity"
        ):
            if previous_stall is None:
                self.stalled.pop(key, None)
            else:
                self.stalled[key] = previous_stall
            self.hold_capacity(task, sessions, snapshot, group)
            action = "uncertain" if confirmation_unknown else "deferred"
            self.log_event(
                {
                    "t": round(snapshot.now, 3),
                    "kind": action,
                    "persisted": is_capacity_deferred(task),
                    "task": task["id"],
                    "reason": "session_capacity",
                }
            )
            message = (
                "capacity deferral unconfirmed"
                if confirmation_unknown
                else "waiting for session capacity"
            )
            return Outcome(action, f"{label(task)!r} {message}")

        step = task.get("workflow_step_id", "")
        self.book(task["id"], step, session_id, snapshot.now)
        return Outcome(
            "launched",
            f"launched {label(task)!r} intent={intent} workflow={workflow!r} "
            f"target={self.board.name_of(target)!r}",
            {
                "task": task["id"], "intent": intent, "session": session_id,
                "target": target, "fallback": fallback,
            },
        )

    def stall_event(self, key, turns, action, reason):
        log.warning(f"stalled task {key.task}: {action}: {reason}")
        self.log_event(
            {
                "t": round(time.time(), 3),
                "kind": "stalled",
                "task": key.task,
                "turns": turns,
                "step": key.step,
                "session": key.session,
                "action": action,
                "reason": reason,
            }
        )

    def give_up(self, key, task, sessions=(), reason=None):
        """Signal an eligible idle primary, or leave the stalled card for a person."""
        record = self.stalled[key]
        turns = record["count"]
        if reason is None and self.config.stall_action == "signal":
            session = next((s for s in sessions if s["id"] == key.session), {})
            if not session.get("is_primary"):
                reason = "stalled session is not primary"
            elif session.get("state") != "WAITING_FOR_INPUT":
                reason = "primary session is not waiting for input"
            else:
                try:
                    step = next(
                        (
                            s
                            for s in client.steps_of(task.get("workflow_id", ""))
                            if s["id"] == key.step
                        ),
                        {},
                    )
                except Exception as error:
                    reason = f"cannot read signal requirement: {error}"
                else:
                    if not step.get("auto_advance_requires_signal"):
                        reason = "step does not require a signal"
            if reason is None:
                summary = (
                    f"kandev-pacer: {turns} turns left the worktree untouched, "
                    "advancing on the agent's behalf"
                )
                action = "signalled"
                try:
                    client.ws_step_complete(key.task, key.session, summary)
                except client.RequestOutcomeUnknown as error:
                    action, summary = "signal_uncertain", str(error)
                except Exception as error:
                    reason = f"signal rejected: {error}"
                if reason is None:
                    self.pending_signals[key] = (
                        time.time() + self.config.launch_latency,
                        turns,
                    )
                    record["count"] = 0
                    self.stall_event(key, turns, action, summary)
                    return None
        reason = reason or f"{turns} consecutive turns left the workspace untouched"
        if not record["flagged"]:
            record["flagged"] = self.flag_blocked(task, reason)
        self.stall_event(
            key, turns, "blocked" if record["flagged"] else "block_failed", reason
        )
        return None

    def check_signals(self, now, snapshot=None):
        """Observe movement before retrying work, even when the quota gate is closed."""
        for key, (deadline, turns) in list(self.pending_signals.items()):
            try:
                task = client.task_of(key.task)
                sessions = client.sessions_of(key.task) if task else []
            except Exception as error:
                self.stall_event(key, turns, "signal_check_failed", str(error))
                continue
            if snapshot is not None and task is not None and is_capacity_deferred(task):
                self.hold_capacity(task, sessions, snapshot)
            if task is None or task.get("workflow_step_id", "") != key.step:
                self.stall_event(
                    key,
                    turns,
                    "advanced" if task else "signal_cancelled",
                    "task moved" if task else "task disappeared",
                )
            elif is_capacity_deferred(task) or key.task in self.deferred:
                self.stall_event(
                    key, turns, "signal_cancelled", "launch deferred for capacity"
                )
                del self.pending_signals[key]
                continue
            elif action_key(task, sessions) != key:
                self.stall_event(
                    key, turns, "signal_cancelled", "launch session changed"
                )
            elif any(
                s["id"] == key.session and s.get("state") in RUNNING_STATES
                for s in sessions
            ):
                self.stall_event(key, turns, "signal_cancelled", "primary resumed work")
            elif now < deadline:
                continue
            else:
                self.stalled[key] = {
                    "fingerprint": None,
                    "count": turns,
                    "flagged": False,
                }
                self.give_up(
                    key, task, reason="signal did not move the step before the deadline"
                )
                del self.pending_signals[key]
                continue
            del self.pending_signals[key]
            self.stalled.pop(key, None)

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

    def note_tick_error(self, error, now):
        """Report a failed tick, saying an unchanged failure once instead of every tick.

        A tick that keeps failing the same way -- a removed endpoint, a server that is
        down -- used to write one warning and one JSONL record per tick for as long as
        it lasted, which at a five-second gate is how the event log reached tens of
        megabytes. First occurrence in full, identical repeats counted silently and
        restated every ERROR_RESTATE seconds, so a standing outage stays visible in a
        live tail without either log growing at tick rate.
        """
        message = str(error)
        quota = isinstance(error, QuotaUnreadable)
        headline = (
            f"quota unreadable ({message}) -> the gate is shut: nothing will start "
            "until this read works"
            if quota
            else f"tick error: {message} -> pausing this tick"
        )
        repeat = self._tick_error
        if repeat is None or repeat.message != message:
            self._tick_error = TickError(message, since=now, told=now)
            log.warning(headline)
            self.log_event({
                "t": round(now, 3), "kind": "error",
                "where": "quota" if quota else "tick", "msg": message,
            })
            return
        repeat.count += 1
        if now - repeat.told < ERROR_RESTATE:
            return
        minutes = (now - repeat.since) / 60
        log.warning(f"{headline} (unchanged for {minutes:.0f}m, {repeat.count} ticks)")
        self.log_event({
            "t": round(now, 3), "kind": "error", "where": "quota" if quota else "tick",
            "msg": message, "ticks": repeat.count, "since": round(repeat.since, 3),
        })
        repeat.told = now

    def note_tick_recovered(self, now):
        """Close out a reported outage, so the quiet stretch has a visible end."""
        repeat, self._tick_error = self._tick_error, None
        if repeat is None:
            return
        log.warning(f"recovered after {repeat.count} failed tick(s): {repeat.message}")
        self.log_event({
            "t": round(now, 3), "kind": "recovered", "msg": repeat.message,
            "ticks": repeat.count, "since": round(repeat.since, 3),
        })

    def run(self):
        """Tick forever. A tick that raises skips its turn rather than ending the run."""
        while True:
            try:
                self.tick()
                self.note_tick_recovered(time.time())
            except Exception as error:
                self.note_tick_error(error, time.time())
                # A failed tick produced no verdict, so it has no estimate to pace by --
                # the same position as `h >= 0`, and it takes the same answer. Without
                # this the loop would keep whatever the last good tick chose, which near
                # a reopening is seconds and no longer means anything.
                self.interval = self.config.interval
            time.sleep(self.interval)


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
    ordered.sort(key=is_observation_consumer)
    if any(is_observation_consumer(task) for task in ordered):
        print(
            "static order: excludes live cache deadlines, provider gates and user-prompt drains"
        )
    for index, task in enumerate(ordered, start=1):
        # Same complaint as report_out_of_scope(): a card that will never launch looks
        # identical in the order to one that will. A WIP-queued card sits on its step,
        # in order, and is skipped every tick.
        queued = "" if is_wip_admitted(task) else "  (queued: step WIP limit full)"
        print(f"{index:3}. {label(task)}{queued}")
        if is_observation_consumer(task):
            print("     last among eligible launches of its provider")
        step = task.get("workflow_step_id", "")
        if step not in board.work_steps and step not in board.queue_steps:
            print(
                f"     step {board.name_of(step)!r} does not auto-launch, even when "
                "waiting for input; move to a paced work step or explicitly queue "
                "a prompt on an idle session"
            )
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
    providers = settings.providers
    # `provider=x` for one, so the single-subscription line is unchanged; the plural form,
    # with the shared cap, for more. The api-key policy is named in both: it decides what
    # happens to tasks no subscription window covers.
    if len(providers) == 1:
        gates = f"provider={providers[0]}, apikey_policy={settings.apikey_policy}"
    else:
        total = settings.concurrency_total
        gates = (
            f"providers={providers}, apikey_policy={settings.apikey_policy}"
            f"{f', total_cap={total}' if total is not None else ''}"
        )
    log.info(
        f"pacing {settings.url} every {settings.interval}s, "
        f"{MIN_INTERVAL}-{MAX_INTERVAL}s while withheld "
        f"(alpha={settings.alpha}, concurrency={settings.concurrency}, {gates})"
        f"\n  log={settings.log_path or '(disabled)'}"
    )
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
                    "Configuration: TOML file, environment variables, or command line.",
    )
    parser.add_argument(
        "--config", type=Path, help="TOML configuration file (default: %(default)s)",
        default=None,
    )
    for alias, name in CONFIG_FIELDS.items():
        parser.add_argument(
            f"--{name.replace('_', '-')}", dest=name, default=argparse.SUPPRESS,
            help=f"override {alias}",
        )
    parser.add_argument(
        "--dump-order",
        action="store_true",
        help="print the queue launches will work down, then exit (spends no quota)",
    )
    args = parser.parse_args()
    aliases = {name: alias for alias, name in CONFIG_FIELDS.items()}
    overrides = {
        aliases.get(name, name): value
        for name, value in vars(args).items()
    }
    config_path = overrides.pop("config")
    overrides.pop("dump_order", None)
    try:
        loaded = load_settings(config_path, overrides)
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))
    settings.__dict__.update(loaded.__dict__)
    # No timestamps: the pacing lines are read live, and the JSONL log is what carries
    # time. `journalctl` stamps them anyway when this runs as a unit.
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if settings.mode == "t3":
        from .t3 import run

        return run(settings, dump_order=args.dump_order)

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
    pacer.log_event(
        {
            "t": round(time.time(), 3),
            "kind": "start",
            "url": settings.url,
            "scope": list(board.scope),
            "interval": settings.interval,
            "alpha": settings.alpha,
            "cap": settings.concurrency,
            "total_cap": settings.concurrency_total,
            "backstop": settings.backstop,
            "providers": settings.providers,
        }
    )
    pacer.run()
