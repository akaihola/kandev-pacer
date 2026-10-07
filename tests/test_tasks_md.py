"""Tracker mutations preserve other issues and refuse stale/dirty checkouts."""

import pytest
from test_t3 import tracker as make_tracker

from kandev_pacer import tasks_md

pytest.importorskip("hai_tasks")


def test_schedule_commit_and_revision_checks(tmp_path):
    path = make_tracker(tmp_path / "repo")
    revision, before = tasks_md.tracker(path)
    result = tasks_md.move(path, "1", revision, "Scheduled", "main")
    _, after = tasks_md.tracker(path)
    assert result["commit"]
    assert next(item for item in after if item["key"] == "1")["section"] == "Scheduled"
    assert {item["id"]: item["bullet"] for item in before if item["key"] != "1"} == {
        item["id"]: item["bullet"] for item in after if item["key"] != "1"
    }
    with pytest.raises(ValueError, match="Tracker changed"):
        tasks_md.move(path, "2", revision, "Scheduled", "main")
    (path.parent / "unrelated").write_text("dirty")
    with pytest.raises(ValueError, match="clean checkout"):
        tasks_md.move(path, "2", result["revision"], "Scheduled", "main")


def test_numbered_duplicates_invalid_and_slug_mentions_are_not_keys(tmp_path):
    path = make_tracker(
        tmp_path / "repo", backlog="- [*] Task [4] tried to condense provider models\n"
    )
    _, items = tasks_md.tracker(path)
    assert items[1]["key"] == "task-4-tried-to-condense-provider"
    path.write_text(
        path.read_text().replace("## Scheduled", "## Scheduled\n\n- [4] Duplicate")
    )
    with pytest.raises(Exception, match="Duplicate task reference"):
        tasks_md.tracker(path)


def test_legacy_empty_proposals_section_read_without_write(tmp_path):
    path = make_tracker(tmp_path / "repo", backlog="- [1] First\n")
    text = path.read_text().replace("## Unverified proposals\n\n- [4] Proposal\n\n", "")
    path.write_text(text)
    _, items = tasks_md.tracker(path)
    assert items[0]["key"] == "1" and path.read_text() == text


def test_issue_file_adapters_and_claimed_dependency(tmp_path):
    (tmp_path / "README.md").write_text("documentation")
    (tmp_path / "wish.md").write_text("Add desktop app\n")
    md = tasks_md.issue_files(tmp_path, ".md")
    assert len(md) == 1 and md[0]["key"] == "wish" and tasks_md.eligible(md[0], md)
    (tmp_path / "001-first.rst").write_text(
        "First\n=====\n:Status: Fixed\n:Depends on: (none)\n"
    )
    path = tmp_path / "002-second.rst"
    path.write_text(
        "Second\n======\n:Status: Accepted\n:Depends on: 001\n:Claimed: task-123\n"
    )
    rst = tasks_md.issue_files(tmp_path, ".rst")
    assert not tasks_md.eligible(rst[1], rst)
    path.write_text(path.read_text().replace(":Claimed: task-123\n", ""))
    rst = tasks_md.issue_files(tmp_path, ".rst")
    assert tasks_md.eligible(rst[1], rst)
    path.write_text(
        path.read_text().replace(":Status: Accepted", ":Status: Unexpected")
    )
    with pytest.raises(ValueError, match="unknown :Status:"):
        tasks_md.issue_files(tmp_path, ".rst")


def test_six_word_slug_removes_punctuation():
    assert (
        tasks_md.slug("Don't re-use `foo_bar` [4] now please later")
        == "dont-reuse-foobar-4-now-please"
    )
