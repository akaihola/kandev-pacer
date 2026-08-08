"""kandev's half of the contract, checked against a running kandev.

Every other test in this suite runs against conftest's fake board, so the whole suite
stays green when kandev renames or deletes the thing being faked. That is not
hypothetical: 0.82.0 -> 0.84.1 removed `GET /api/v1/agents/usage`, every tick died on a
404, and every test in this suite went on passing. This module is the one place the promises are
checked against the real server, so an upgrade breaks a test instead of the loop.

Opt-in, because it needs a server and the default suite must stay hermetic. Run it after
every kandev upgrade:

    PACER_LIVE_KANDEV=1 uv run pytest tests/test_contract_live.py -v

It asserts *shape* -- which endpoints answer, in what envelope, carrying which keys --
and never the contents of anyone's board, so it passes against any instance with at
least one workflow. Rows that need a task or a session skip themselves on an empty one.

Nothing here starts a turn or spends quota. The REST calls are reads; the provider call
is the same read the gate already makes every five minutes; and of the four WebSocket
actions the pacer sends, only the two that cannot act are sent -- `message.queue.get`,
which is a read, and `task.state` with a payload kandev rejects before touching the
task. `session.launch` and `message.queue.drain` are deliberately never sent, which
leaves their names the one part of the contract this module cannot check.

Keys come in two tiers, because Go's `omitempty` decides which is which. A field
declared without it is in every response, so its absence is a contract break and is
asserted per item. A field declared with it vanishes when unset -- `prompt` on a step
with no prompt -- so it can only be checked where it appears, and a rename of one shows
up as "present nowhere", which an instance that simply never sets it looks identical to.
`agent_profile_id` is already in that state at 0.84.1: no step of any workflow carries
one, so `workflow.step_lint`'s per-step-agent branch is unverifiable from outside.
"""

import json
import os
import time
import urllib.error
from pathlib import Path

import pytest

from kandev_pacer import client, provider
from kandev_pacer.config import (
    IDLE_STATES,
    RUNNING_STATES,
    TRANSITION_KINDS,
    WINDOW_DURATIONS,
    agent_provider,
    settings,
)
from kandev_pacer.quota import parse_windows

pytestmark = pytest.mark.skipif(
    not os.environ.get("PACER_LIVE_KANDEV"),
    reason="live kandev contract test; set PACER_LIVE_KANDEV=1 to run it",
)

# Every session state config.py names, which is what the pacer's gates are written in.
KNOWN_SESSION_STATES = RUNNING_STATES | IDLE_STATES
# The rest of kandev's TaskSessionState vocabulary (pkg/api/v1/task.go). The pacer does
# not act on these, but a live session sitting in one is not a contract break -- so they
# are listed here rather than left to fail the vocabulary check.
TERMINAL_SESSION_STATES = {"COMPLETED", "FAILED", "CANCELLED"}

# The kinds of event a step can carry that the pacer looks for by name.
KNOWN_EVENT_KINDS = {*TRANSITION_KINDS, "auto_start_agent"}

UNKNOWN_ACTION = "UNKNOWN_ACTION"


def require(payload, keys, where):
    """Assert `payload` still carries `keys`, naming the endpoint when it does not.

    The point of the wording: a break reads as "kandev changed this endpoint", not as a
    KeyError three frames inside a helper that happened to be the first to look.
    """
    missing = sorted(key for key in keys if key not in payload)
    assert not missing, (
        f"{where}: kandev no longer returns {missing} "
        f"(got {sorted(payload)}) -- the pacer reads {missing[0]!r} from it"
    )


# --- the live instance, fetched once ---


@pytest.fixture(scope="module")
def live():
    """The kandev under test, or a skip explaining that there is none.

    Unreachable is not a contract break, and must not read as one: an enabled run on a
    machine with no server should say so rather than report kandev as changed.
    """
    try:
        client.get("/api/v1/workflows")
    except client.ReadFailed as error:
        # A server that answers and a server that is not there fail the same call, and
        # must not read the same way. An HTTP status means kandev is up and the endpoint
        # every scan starts from is gone -- the 0.84.1 break exactly. Anything else never
        # got that far, which is not kandev changing anything.
        if isinstance(error.__cause__, urllib.error.HTTPError):
            pytest.fail(f"{error} -- kandev is up, but the endpoint every scan starts "
                        "from is gone")
        pytest.skip(f"no kandev at {settings.url} ({error}) -- start one, or unset "
                    "PACER_LIVE_KANDEV")
    return settings.url


@pytest.fixture(scope="module")
def entries(live):
    """Every workflow with its steps -- the same call the startup scan makes."""
    found = client.workflow_entries()
    if not found:
        pytest.skip("instance has no workflows; nothing to check the board shape against")
    return found


@pytest.fixture(scope="module")
def steps(entries):
    return [step for _, workflow_steps in entries for step in workflow_steps]


@pytest.fixture(scope="module")
def tasks(entries):
    found = [task for workflow, _ in entries for task in client.tasks_of(workflow["id"])]
    if not found:
        pytest.skip("instance has no tasks; task and session shapes need at least one")
    return found


@pytest.fixture(scope="module")
def sessions(tasks):
    for task in tasks:
        found = client.sessions_of(task["id"])
        if found:
            return found
    pytest.skip("no task on this instance has a session yet")


# --- REST: the seven paths client.py reads ---


def test_workflows_envelope(live):
    response = client.get("/api/v1/workflows")
    require(response, ["workflows"], "GET /api/v1/workflows")
    assert isinstance(response["workflows"], list)
    for workflow in response["workflows"]:
        require(workflow, ["id", "name"], "GET /api/v1/workflows [workflow]")


def test_workflow_steps_envelope(entries):
    for workflow, workflow_steps in entries:
        where = f"GET /api/v1/workflows/{{id}}/workflow/steps ({workflow['name']!r})"
        assert isinstance(workflow_steps, list), f"{where}: no 'steps' list in the envelope"
        for step in workflow_steps:
            require(step, ["id", "workflow_id", "name", "position", "is_start_step",
                           "auto_advance_requires_signal"], f"{where} [step]")


def test_step_events_vocabulary(steps):
    """The event names pacing is decided by, wherever a step carries them.

    `events` and `stage_type` are omitempty, so a step without them is normal; what must
    not change is what the ones that are there are called. `on_turn_complete` carrying a
    transition is the whole reason a step is safe to pace, and `is_work_step` reads both
    the slot name and the action's `type`.
    """
    kinds = {
        action.get("type")
        for step in steps
        for slot in ("on_turn_complete", "on_enter", "on_turn_start")
        for action in (step.get("events") or {}).get(slot) or []
    }
    assert all(kinds), "step events: an action arrived without a 'type'"
    for step in steps:
        stage = step.get("stage_type")
        assert stage is None or isinstance(stage, str), (
            f"step {step['name']!r}: stage_type is {type(stage).__name__}, not a string"
        )
    if kinds and not kinds & KNOWN_EVENT_KINDS:
        pytest.fail(
            "GET .../workflow/steps [events]: no step action is one of "
            f"{sorted(KNOWN_EVENT_KINDS)} -- kandev renamed the event kinds (saw "
            f"{sorted(kinds)})"
        )


def test_workflow_tasks_envelope(entries):
    for workflow, _ in entries:
        where = f"GET /api/v1/workflows/{{id}}/tasks ({workflow['name']!r})"
        response = client.get(f"/api/v1/workflows/{workflow['id']}/tasks") or {}
        require(response, ["tasks"], where)
        for task in response["tasks"]:
            require(task, ["id", "workflow_id", "workflow_step_id", "position",
                           "priority", "created_at", "wip_admitted"], f"{where} [task]")
            assert isinstance(task["wip_admitted"], bool), (
                f"{where}: wip_admitted is no longer a bool -- selection.is_wip_admitted "
                "reads it as one"
            )


def test_task_by_id(tasks):
    """One task by id, in either shape `client.task_of` accepts.

    Which of the two kandev answers with is not the contract -- the client takes both on
    purpose. That one of them arrives is.
    """
    task_id = tasks[0]["id"]
    found = client.task_of(task_id)
    assert found is not None, (
        "GET /api/v1/tasks/{id}: no task came back for a task the board just listed "
        "-- neither the bare object nor a {'task': ...} envelope"
    )
    require(found, ["id", "workflow_step_id"], "GET /api/v1/tasks/{id}")
    assert found["id"] == task_id


def test_task_metadata_carries_the_agent_profile(tasks):
    """`metadata.agent_profile_id`, which a launch of a CREATED session has to pass on.

    omitempty on both levels, so its absence everywhere is only suspicious, not proof --
    but a rename shows up here as "no task on the board has one", which is worth seeing.
    """
    profiles = [profile for task in tasks
                if (profile := (task.get("metadata") or {}).get("agent_profile_id"))]
    if not profiles:
        pytest.skip("no task on this instance carries metadata.agent_profile_id")
    assert all(isinstance(profile, str) for profile in profiles), (
        "GET /api/v1/workflows/{id}/tasks: metadata.agent_profile_id is no longer a "
        f"string ({profiles}) -- ws_launch passes it straight through"
    )


def test_task_sessions_envelope(tasks):
    where = "GET /api/v1/tasks/{id}/sessions"
    for task in tasks:
        response = client.get(f"/api/v1/tasks/{task['id']}/sessions") or {}
        require(response, ["sessions"], where)
        for session in response["sessions"]:
            require(session, ["id", "state", "started_at"], f"{where} [session]")


def test_session_states_are_the_vocabulary_the_pacer_gates_on(sessions):
    """Every live session's state is a name config.py knows.

    A renamed state is the quietest possible break: `RUNNING_STATES` stops matching,
    every session reads as idle, and the pacer launches on top of running turns.
    """
    known = KNOWN_SESSION_STATES | TERMINAL_SESSION_STATES
    for session in sessions:
        assert session["state"] in known, (
            f"GET /api/v1/tasks/{{id}}/sessions: state {session['state']!r} is not in "
            f"kandev's vocabulary as config.py records it ({sorted(known)})"
        )


def test_agents_envelope(live):
    """`GET /api/v1/agents`: the profile -> agent -> billing map two-subscription pacing reads.

    New in the two-subscriptions change, and undocumented like the rest -- so it earns a
    contract row. `billing_type` decides whether a task is paced at all, and the agent name is
    what config.agent_provider maps to a usage provider, so a rename of either goes blind the
    same quiet way a renamed session state does.
    """
    where = "GET /api/v1/agents"
    response = client.get("/api/v1/agents") or {}
    require(response, ["agents"], where)
    named = set()
    for agent in response["agents"]:
        require(agent, ["name", "profiles"], f"{where} [agent]")
        named.add(agent["name"])
        for profile in agent.get("profiles") or []:
            require(profile, ["id", "billing_type"], f"{where} [profile]")
    subscription_agents = {
        agent["name"]
        for agent in response["agents"]
        for profile in agent.get("profiles") or []
        if profile.get("billing_type") == "subscription"
    }
    unmapped = {name for name in subscription_agents if agent_provider(name) is None}
    assert not unmapped, (
        f"{where}: subscription agents {sorted(unmapped)} map to no provider "
        "(config.AGENT_PROVIDERS) -- their tasks would be withheld as unresolvable"
    )


def test_agents_maps_a_profile_to_its_billing(live):
    """client.agents() reads the endpoint into the shape the resolver consumes."""
    mapped = client.agents()
    if not mapped:
        pytest.skip("instance defines no agent profiles")
    for info in mapped.values():
        assert set(info) >= {"agent", "billing_type"}, (
            f"client.agents: entry {info} is missing the keys task_provider reads"
        )


def test_user_settings_envelope(live):
    """The sidebar keys selection.py orders the board by.

    Ordering falls back to FIFO when these are unreadable, so a break here is silent by
    design -- the pacer keeps running and just stops honouring the sidebar.
    """
    where = "GET /api/v1/user/settings"
    response = client.get("/api/v1/user/settings") or {}
    require(response, ["settings"], where)
    require(response["settings"],
            ["sidebar_views", "sidebar_active_view_id", "sidebar_draft",
             "sidebar_task_prefs"],
            f"{where} [settings]")


# --- the quota, which no longer comes from kandev at all ---


def test_provider_usage_envelope():
    """The Anthropic usage read that replaced `GET /api/v1/agents/usage`.

    Read-only and quota-free: it reports the windows, it does not consume them. A
    missing credential is a skip -- the machine is not logged in, which is not a contract
    break -- but anything else `provider.fetch` raises is one, and says what to do.
    """
    if not Path(settings.credentials_path).exists():
        pytest.skip(f"no Claude credentials at {settings.credentials_path}")
    usage = provider.fetch(time.time())
    agents = usage.get("agents") or []
    assert agents, "provider usage: envelope carried no agents"
    reported = (agents[0].get("usage") or {}).get("provider")
    assert reported == settings.provider, (
        f"provider usage: reported provider {reported!r} is not PACER_PROVIDER "
        f"({settings.provider!r}) -- the gate would go blind"
    )
    windows = parse_windows(usage)
    if not windows:
        pytest.skip("account reports no usage windows yet (a genuinely fresh one has none)")
    for window in windows:
        assert window["label"] in WINDOW_DURATIONS or window["label"].startswith("7-day"), (
            f"provider usage: window label {window['label']!r} has no known duration"
        )
        assert 0.0 <= window["consumed_frac"] <= 1.5, (
            f"provider usage: {window['label']} utilization is {window['consumed_frac']} "
            "-- no longer a percentage?"
        )


def test_codex_usage_envelope():
    """The Codex (openai) usage read, the second quota source.

    Read-only and quota-free, like the Anthropic one: it reports the windows, it does not
    consume them. Skipped unless the machine has Codex credentials, since a machine that never
    logged into Codex is not a contract break. The shape asserted is what `codex_windows`
    depends on -- a `rate_limit` object whose windows carry `used_percent` and a duration.
    """
    if not Path(settings.codex_credentials_path).exists():
        pytest.skip(f"no Codex credentials at {settings.codex_credentials_path}")
    tokens = provider.read_codex_credentials(settings.codex_credentials_path)
    payload = provider._codex_get(
        provider.codex_access_token(tokens, time.time()), tokens.get("account_id")
    )
    require(payload, ["rate_limit"], "GET codex/usage")
    rate = payload["rate_limit"]
    windows = [
        rate[slot] for slot in ("primary_window", "secondary_window") if rate.get(slot)
    ]
    if not windows:
        pytest.skip("Codex account reports no rate-limit windows yet")
    for window in windows:
        require(
            window, ["used_percent", "limit_window_seconds"], "codex/usage [window]"
        )
        assert 0 <= window["used_percent"] <= 150, (
            f"codex/usage: used_percent is {window['used_percent']} -- no longer a percentage?"
        )
    parsed = parse_windows(
        provider.codex_envelope(payload, time.time()), provider="openai"
    )
    assert parsed, "codex/usage: nothing parsed from a payload that carried a window"


# --- WebSocket: are the action names still recognized ---


def ws_probe(action, payload):
    """Send one frame and return kandev's answer, error frames included.

    `client.ws_request` raises on an error frame and drops the code with it, and the code
    is the whole point here: UNKNOWN_ACTION means kandev has never heard of the action,
    anything else means it has and merely refused this payload.
    """
    connection = client._ws_connect()
    try:
        connection.send(json.dumps({
            "id": "contract-probe", "type": "request",
            "action": action, "payload": payload,
        }))
        deadline = time.monotonic() + 10
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"{action}: no answer within 10s"
            connection.settimeout(remaining)
            raw = connection.recv()
            if not raw:
                continue
            message = json.loads(raw)
            # Unrelated pushed events share the socket; wait for our own id.
            if message.get("id") == "contract-probe":
                return message
    finally:
        connection.close()


def error_code(message):
    return ((message.get("payload") or {}).get("code")
            if message.get("type") == "error" else None)


def test_unknown_action_is_distinguishable(live):
    """The control the two probes below are read against.

    Without it, "not UNKNOWN_ACTION" proves nothing: a kandev that answered everything
    the same way would pass those and still have dropped the action.
    """
    message = ws_probe("pacer.contract.no.such.action", {})
    assert error_code(message) == UNKNOWN_ACTION, (
        "/ws: an invented action was not refused as UNKNOWN_ACTION "
        f"(got {message.get('type')} / {message.get('payload')}) -- this test can no "
        "longer tell a dropped action from a rejected one"
    )


@pytest.mark.parametrize(
    "action, payload",
    [
        # Both are refused on their payload before the handler touches anything:
        # queue.get wants a session_id, task.state wants an id and a state.
        ("message.queue.get", {}),
        ("task.state", {}),
    ],
)
def test_action_is_still_recognized(live, action, payload):
    message = ws_probe(action, payload)
    assert error_code(message) != UNKNOWN_ACTION, (
        f"/ws: kandev no longer has a handler for {action!r} -- the pacer's "
        "corresponding call is dead"
    )


def test_queue_get_answers_a_count(sessions):
    """The read the pacer makes to see whether a session has a prompt parked on it.

    A live read against an idle session, which is safe: it dispatches nothing. The count
    itself is board contents, so only its type is asserted.
    """
    idle = next((s for s in sessions if s["state"] in IDLE_STATES), None)
    if idle is None:
        pytest.skip("no idle session to read a queue from")
    assert isinstance(client.ws_queue_count(idle["id"]), int)
