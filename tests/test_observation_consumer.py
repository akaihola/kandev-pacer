"""Consumer decisions against real temporary Git vaults and a fake Kandev API."""

import json
import subprocess
import urllib.error
from email.message import Message
from typing import Any

import pytest
from conftest import CLEAN

from kandev_pacer import observation_consumer as consumer
from kandev_pacer.selection import OBSERVATION_CONSUMER_TITLE


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def record(
    vault, name="a", title="Project work", at="2026-09-20T12:00:00Z", legacy=False
):
    path = vault / consumer.OBSERVATIONS / "2026-09" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"task": name, "title": title, "archived_at": at}
    if legacy:
        path.write_text("---\n" + json.dumps(data) + "\n---\n# Evidence\n")
    else:
        path.write_text("# Evidence\n")
        path.with_suffix(".json").write_text(json.dumps(data))
    return "2026-09/" + path.name


@pytest.fixture
def review(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    git(vault, "init", "-q")
    git(vault, "config", "user.email", "test@example.invalid")
    git(vault, "config", "user.name", "Test")
    git(vault, "commit", "--allow-empty", "-qm", "Initial")
    prompt = tmp_path / "consumer.md"
    prompt.write_text("Read evidence. Write unreviewed proposals.\n")
    state = tmp_path / "state"
    api: dict[str, Any] = {"tasks": [], "total": 0, "lookup": None, "updated": []}

    def get(path):
        if "/by-external-id?" in path:
            if api["lookup"] is None:
                cause = urllib.error.HTTPError(path, 404, "missing", Message(), None)
                raise consumer.client.ReadFailed("missing") from cause
            return api["lookup"]
        if "/tasks?page=" in path:
            return {"tasks": api["tasks"], "total": api["total"]}
        if path == "/api/v1/repositories/repo":
            return {"local_path": str(tmp_path / "prg/kandev-config")}
        if path == "/api/v1/tasks/consumer":
            return api["lookup"]
        if path == "/api/v1/workflows/wf/workflow/steps":
            return {"steps": CLEAN}
        raise AssertionError(path)

    def update(action, body):
        api["updated"].append((action, body))
        api["lookup"]["priority"] = body["priority"]

    monkeypatch.setattr(consumer.client, "get", get)
    monkeypatch.setattr(consumer.client, "ws_request", update)

    def prepare():
        return consumer.prepare(
            vault, state, "ws", "wf", "agent", "executor", prompt, tmp_path
        )

    return vault, state, api, prepare


def created(api, **overrides):
    api["lookup"] = {
        "id": "consumer",
        "title": OBSERVATION_CONSUMER_TITLE,
        "workspace_id": "ws",
        "workflow_id": "wf",
        "state": "TODO",
        "priority": "medium",
        "repositories": [{"repository_id": "repo"}],
        "creation_complete": True,
        **overrides,
    }
    return api["lookup"]


def test_empty_and_self_only_do_not_create(review):
    vault, state, _, prepare = review
    assert prepare()["status"] == "empty"
    record(vault, title=OBSERVATION_CONSUMER_TITLE)
    assert prepare() == {"status": "empty", "skipped_self": 1, "errors": []}
    assert not (state / "pending.json").exists()


def test_mixed_formats_batch_cap_and_bad_sidecar(review):
    vault, _, _, prepare = review
    for n in range(23):
        record(vault, str(n), at=f"2026-09-{n + 1:02}T12:00:00Z", legacy=n % 2 == 0)
    bad = vault / consumer.OBSERVATIONS / record(vault, "broken", legacy=True)
    bad.with_suffix(".json").write_text("{bad")
    result = prepare()
    assert len(result["errors"]) == 1
    batch = json.loads(result["arguments"]["prompt"].split("## Frozen batch\n\n")[1])
    assert [r["task"] for r in batch["records"]] == [str(n) for n in range(20)]


def test_frozen_create_arguments_survive_arrivals(review):
    vault, _, _, prepare = review
    record(vault)
    args = prepare()["arguments"]
    record(vault, "older", at="2020-01-01T00:00:00Z")
    assert prepare()["arguments"] == args
    assert args["local_path"].endswith("/prg/kandev-config")
    assert args["workflow_id"] == "wf"
    assert args["workflow_step_id"] == "bk"
    assert args["start_agent"] is False
    assert args["prompt"].startswith("Read evidence.")
    assert args["external_id"].startswith("observation-consumer:")


@pytest.mark.parametrize("state", ["TODO", "BLOCKED", "REVIEW", "WAITING_FOR_INPUT"])
def test_open_consumer_in_another_workflow_suppresses_create(review, state):
    vault, _, api, prepare = review
    record(vault)
    api.update(
        tasks=[
            {
                "id": "old",
                "title": OBSERVATION_CONSUMER_TITLE,
                "workflow_id": "other",
                "state": state,
            }
        ],
        total=1,
    )
    assert prepare() == {"status": "open", "task_id": "old"}


def test_incomplete_listing_fails_closed(review):
    vault, _, api, prepare = review
    record(vault)
    api["total"] = 1
    with pytest.raises(ValueError, match="incomplete"):
        prepare()


def test_uncertain_creation_reconciles_and_sets_low_priority(review):
    vault, _, api, prepare = review
    record(vault)
    prepare()
    created(api, creation_complete=False)
    assert prepare()["status"] == "unsettled"
    assert api["updated"] == []
    api["lookup"]["creation_complete"] = True
    assert prepare()["status"] == "open"
    assert api["updated"] == [("task.update", {"id": "consumer", "priority": "low"})]
    assert prepare()["status"] == "open"
    assert len(api["updated"]) == 1


def test_priority_timeout_retries_same_task(review, monkeypatch):
    vault, _, api, prepare = review
    record(vault)
    args = prepare()["arguments"]
    created(api)
    original = consumer.client.ws_request
    monkeypatch.setattr(
        consumer.client,
        "ws_request",
        lambda *a: (_ for _ in ()).throw(RuntimeError("timeout")),
    )
    with pytest.raises(RuntimeError, match="timeout"):
        prepare()
    monkeypatch.setattr(consumer.client, "ws_request", original)
    assert prepare() == {"status": "open", "task_id": "consumer"}
    assert api["lookup"]["priority"] == "low"
    assert args["external_id"]


def test_wrong_binding_and_closed_failed_batch_are_not_recreated(review):
    vault, _, api, prepare = review
    record(vault)
    prepare()
    created(api, repositories=[])
    with pytest.raises(ValueError, match="repository binding"):
        prepare()
    created(api, state="COMPLETED")
    assert prepare()["status"] == "blocked"
    assert prepare()["status"] == "blocked"


def test_acknowledge_partial_no_finding_self_skip_and_next_batch(review):
    vault, state, api, prepare = review
    a, b = record(vault), record(vault, "b")
    record(vault, "self", title=OBSERVATION_CONSUMER_TITLE)
    args = prepare()["arguments"]
    created(api)
    consumer.acknowledge(vault, state, args["external_id"], [a])
    text, names = consumer.ledger(vault, state)
    assert names == {a, "2026-09/self.md"}
    assert "skipped: consumer's own run" in text
    assert b not in names
    assert consumer.acknowledge(vault, state, args["external_id"], [a])["records"] == 0
    consumer.acknowledge(vault, state, args["external_id"], [b])
    api["lookup"]["state"] = "COMPLETED"
    assert prepare()["status"] == "empty"
    record(vault, "late", at="2020-01-01T00:00:00Z")
    next_args = prepare()["arguments"]
    assert next_args["external_id"] != args["external_id"]


def test_failed_ledger_commit_recovers_without_staging_other_files(review, monkeypatch):
    vault, state, _, prepare = review
    name = record(vault)
    args = prepare()["arguments"]
    (vault / "unrelated").write_text("keep staged")
    git(vault, "add", "unrelated")
    original = consumer.commit_records
    monkeypatch.setattr(
        consumer,
        "commit_records",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("commit failed")),
    )
    with pytest.raises(RuntimeError, match="commit failed"):
        consumer.acknowledge(vault, state, args["external_id"], [name])
    monkeypatch.setattr(consumer, "commit_records", original)
    assert prepare()["status"] == "empty"
    assert name in consumer.ledger(vault, state)[1]
    assert git(vault, "diff", "--cached", "--name-only") == "unrelated"
    assert git(vault, "show", "--format=", "--name-only", "HEAD") == str(
        consumer.OBSERVATIONS / "consumed.md"
    )


def test_reject_unknown_ack_and_manual_ledger_edits(review):
    vault, state, _, prepare = review
    record(vault)
    args = prepare()["arguments"]
    with pytest.raises(ValueError, match="outside the batch"):
        consumer.acknowledge(vault, state, args["external_id"], ["../elsewhere"])
    with pytest.raises(ValueError, match="does not match"):
        consumer.acknowledge(vault, state, "wrong", [])
    (vault / consumer.OBSERVATIONS / "consumed.md").write_text("manual\n")
    with pytest.raises(ValueError, match="uncommitted"):
        prepare()


def test_legacy_ledger_filename(review):
    vault, _, _, prepare = review
    record(vault)
    path = vault / consumer.OBSERVATIONS / "consumed.md"
    path.write_text("a.md\tskipped: old entry\n")
    git(vault, "add", str(path))
    git(vault, "commit", "-qm", "Ledger")
    assert prepare()["status"] == "empty"


def test_repository_routing_uses_archived_task_binding(tmp_path, monkeypatch):
    responses = {
        "/api/v1/tasks/original": {
            "archived_at": "2026-09-20",
            "repositories": [{"repository_id": "p"}],
        },
        "/api/v1/repositories/p": {"local_path": str(tmp_path / "project")},
    }
    monkeypatch.setattr(consumer.client, "get", responses.__getitem__)
    assert consumer.target_repository("project", "original", tmp_path) == str(
        tmp_path / "project"
    )
    assert consumer.target_repository("kandev", "missing", tmp_path) == str(
        tmp_path / "prg/kandev-config"
    )
    assert consumer.target_repository("global", "missing", tmp_path) == str(
        tmp_path / "repos/nixos-config"
    )
    for bindings in ([], [{"repository_id": "p"}] * 2):
        responses["/api/v1/tasks/original"]["repositories"] = bindings
        with pytest.raises(ValueError, match="exactly one"):
            consumer.target_repository("project", "original", tmp_path)


def test_lookup_failure_never_becomes_absence(review, monkeypatch):
    vault, state, _, prepare = review
    record(vault)
    prepare()
    original = (state / "pending.json").read_bytes()

    def unavailable(*args):
        raise consumer.client.ReadFailed("server unavailable")

    monkeypatch.setattr(consumer, "lookup", unavailable)
    with pytest.raises(consumer.client.ReadFailed):
        prepare()
    assert (state / "pending.json").read_bytes() == original


def test_listing_paginates_and_rejects_repeated_pages(monkeypatch):
    first = [{"id": str(n)} for n in range(100)]
    last = [{"id": "consumer", "title": OBSERVATION_CONSUMER_TITLE, "state": "BLOCKED"}]
    monkeypatch.setattr(
        consumer.client,
        "get",
        lambda path: {"tasks": first if "page=1&" in path else last, "total": 101},
    )
    assert consumer.workspace_tasks("ws")[-1] == last[0]
    monkeypatch.setattr(
        consumer.client, "get", lambda path: {"tasks": first, "total": 101}
    )
    with pytest.raises(ValueError, match="changed while paging"):
        consumer.workspace_tasks("ws")


def test_commit_published_before_failure_is_recovered(review, monkeypatch):
    vault, state, _, prepare = review
    name = record(vault)
    args = prepare()["arguments"]
    original = consumer.commit_records

    def committed_then_failed(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("process stopped after commit")

    monkeypatch.setattr(consumer, "commit_records", committed_then_failed)
    with pytest.raises(RuntimeError, match="stopped"):
        consumer.acknowledge(vault, state, args["external_id"], [name])
    head = git(vault, "rev-parse", "HEAD")
    monkeypatch.setattr(consumer, "commit_records", original)
    assert (
        consumer.acknowledge(vault, state, args["external_id"], [name])["records"] == 0
    )
    assert git(vault, "rev-parse", "HEAD") == head
    assert not (state / "acknowledgement.json").exists()


def test_reject_state_reuse_for_another_vault(review, tmp_path):
    vault, state, _, prepare = review
    name = record(vault)
    args = prepare()["arguments"]
    pending_path = state / "pending.json"
    pending = json.loads(pending_path.read_text())
    pending["vault"] = str(tmp_path / "different")
    pending_path.write_text(json.dumps(pending))
    with pytest.raises(ValueError, match="another server, workspace or vault"):
        prepare()
    with pytest.raises(ValueError, match="another vault"):
        consumer.acknowledge(vault, state, args["external_id"], [name])


def test_task_listing_shrinks_between_pages(review, monkeypatch):
    vault, state, _, prepare = review
    record(vault)
    first = [{"id": str(n), "title": "ordinary", "state": "TODO"} for n in range(100)]
    # A removal moves the last task onto the first page, which we already read.
    monkeypatch.setattr(
        consumer.client,
        "get",
        lambda path: {
            "tasks": first if "page=1&" in path else [],
            "total": 101 if "page=1&" in path else 100,
        },
    )
    with pytest.raises(ValueError, match="changed while paging"):
        prepare()
    assert not (state / "pending.json").exists()


def test_consumed_batch_is_not_recreated_after_task_deletion(review):
    vault, state, api, prepare = review
    name = record(vault)
    args = prepare()["arguments"]
    created(api)
    consumer.acknowledge(vault, state, args["external_id"], [name])
    api["lookup"] = None
    assert prepare()["status"] == "empty"
    assert prepare()["status"] == "empty"
    assert not (state / "pending.json").exists()
