"""Tests for kandev_pacer/progress.py: reading whether a turn changed anything.

The path-gathering half is pure and tested with plain dictionaries. The git half is
tested against real repositories in tmp_path -- what is under test is precisely whether
the pacer reads git correctly, so faking git would test nothing.
"""
import subprocess

import pytest

from kandev_pacer.progress import fingerprint, worktree_paths, worktree_state


def git(path, *args):
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A real git repository with one commit, so HEAD resolves."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "pacer@example.invalid")
    git(tmp_path, "config", "user.name", "Pacer Test")
    (tmp_path / "file.txt").write_text("one\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "first")
    return tmp_path


# --- which paths get read ---

def test_both_worktree_shapes_are_read():
    """kandev carries a multi-repo list and a flat path; a session can have either."""
    assert worktree_paths([
        {"id": "a", "worktrees": [{"worktree_path": "/w/one"}, {"worktree_path": "/w/two"}]},
        {"id": "b", "worktree_path": "/w/three"},
    ]) == ["/w/one", "/w/three", "/w/two"]


def test_the_same_tree_named_twice_is_read_once():
    """Sibling sessions share a worktree; reading it twice would be pure waste."""
    assert worktree_paths([
        {"id": "a", "worktree_path": "/w/one", "worktrees": [{"worktree_path": "/w/one"}]},
        {"id": "b", "worktree_path": "/w/one"},
    ]) == ["/w/one"]


def test_empty_paths_are_not_paths():
    """kandev sends "" for a session with no worktree, not a missing key."""
    assert worktree_paths([{"id": "a", "worktree_path": "", "worktrees": []}]) == []


# --- what one tree reads as ---

def test_a_clean_tree_reads_as_its_head(repo):
    head, dirty = worktree_state(str(repo))
    assert len(head) == 40 and dirty is False


def test_an_edit_shows_up_without_a_commit(repo):
    """Work in progress is progress: a turn that edits has not stood still."""
    before = worktree_state(str(repo))
    (repo / "file.txt").write_text("two\n")
    assert worktree_state(str(repo)) != before


def test_a_new_untracked_file_shows_up(repo):
    """A turn whose whole output is a new file must not read as nothing."""
    before = worktree_state(str(repo))
    (repo / "new.txt").write_text("hello\n")
    assert worktree_state(str(repo)) != before


def test_committing_moves_head_back_to_clean_but_not_back_to_before(repo):
    """The two halves together: neither HEAD nor `dirty` alone would see this sequence."""
    before = worktree_state(str(repo))
    (repo / "file.txt").write_text("two\n")
    git(repo, "commit", "-qam", "second")
    after = worktree_state(str(repo))
    assert after[1] is False, "the tree is clean again"
    assert after != before, "but a commit happened, and that is the whole point"


def test_an_unreadable_tree_cannot_say(tmp_path):
    """Not a git repository, so there is no answer -- and None is not an answer of 'nothing'."""
    assert worktree_state(str(tmp_path / "nowhere")) is None


# --- what a task reads as ---

def test_a_task_with_no_worktree_is_a_real_answer():
    """`{}` not None: a task with no tree has nothing that *could* change, and the pacer
    is entitled to be sure of that. It is what lets a repo-less task stall out."""
    assert fingerprint([{"id": "s", "worktree_path": ""}]) == {}


def test_a_task_whose_trees_cannot_be_read_says_so(tmp_path):
    """Distinct from the above: there is a tree, git just could not answer about it."""
    assert fingerprint([{"id": "s", "worktree_path": str(tmp_path / "gone")}]) is None


def test_a_readable_tree_fingerprints_by_path(repo):
    assert fingerprint([{"id": "s", "worktree_path": str(repo)}]) == {
        str(repo): worktree_state(str(repo))
    }


def test_one_unreadable_tree_does_not_discard_the_readable_one(repo, tmp_path):
    """A partial read is still evidence: the tree that answered is kept."""
    print_ = fingerprint([
        {"id": "s", "worktree_path": str(repo)},
        {"id": "t", "worktree_path": str(tmp_path / "gone")},
    ])
    assert list(print_) == [str(repo)]
