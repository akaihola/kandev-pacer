"""What a turn changed on disk, so a finished task is not relaunched forever.

The board cannot answer this. A task whose work is *done* sits on its work step in
exactly the shape a task mid-flight sits in -- same step, same state, an idle session
-- so `is_launchable` says yes to both, and the pacer relaunches the finished one every
tick. Nothing in kandev's task or session payloads separates the two: one observed task
took 509 turns over thirteen hours re-reading its own brief and answering "the task is
complete; stopping", until its context window filled.

The one thing that does differ is the worktree. Work moves `HEAD` or dirties the tree;
"stopping" moves neither. kandev already hands the pacer every session's
``worktree_path``, and the pacer runs on the same host, so the signal the API does not
carry is one ``git`` call away.

Read-only and best-effort throughout, and the two failure shapes are deliberately not
the same answer:

* **no worktree at all** -- a real, stable reading. A task with no tree has nothing that
  could change, so repeated turns on it accumulate towards a stall like any other.
* **a tree that cannot be read** -- ``None``, meaning *cannot say*. It never compares
  equal to anything, not even to another ``None``, so a git hiccup reads as progress.
  Stopping a live task because git was briefly unavailable is far worse than one extra
  launch.
"""

import subprocess

# Long enough for a cold cache on a large repository, short enough that a wedged git
# cannot hold the tick open: the pacer's whole loop is meant to be a few seconds.
GIT_TIMEOUT = 10


def _git(path, *args):
    """One read-only git command in `path`, or None if it could not be run."""
    try:
        done = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def worktree_state(path):
    """``(head, dirty)`` for one worktree, or None if git could not answer for it.

    Both halves matter. A turn that edits without committing moves only `dirty`, and a
    turn that commits moves only `head` once the tree is clean again -- taking either on
    its own would call one of those two kinds of real work "no change".
    """
    head = _git(path, "rev-parse", "HEAD")
    if head is None:
        return None
    status = _git(path, "status", "--porcelain")
    if status is None:
        return None
    return (head, bool(status))


def worktree_paths(sessions):
    """Every worktree path these sessions name, de-duplicated and in a stable order.

    Both shapes kandev uses are read: the ``worktrees`` list a multi-repo session
    carries, and the flat ``worktree_path`` that every session still has.
    """
    paths = set()
    for session in sessions:
        for worktree in session.get("worktrees") or []:
            if worktree.get("worktree_path"):
                paths.add(worktree["worktree_path"])
        if session.get("worktree_path"):
            paths.add(session["worktree_path"])
    return sorted(paths)


def fingerprint(sessions):
    """What this task's worktrees look like right now.

    ``{}`` when the task has no worktree -- a task with nothing to change is a thing the
    pacer can be sure about. ``None`` when it has worktrees but none of them could be
    read, which the comparison treats as "cannot say".
    """
    paths = worktree_paths(sessions)
    if not paths:
        return {}
    states = {}
    for path in paths:
        state = worktree_state(path)
        if state is not None:
            states[path] = state
    return states or None
