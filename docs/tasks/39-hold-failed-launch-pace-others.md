One stuck launch stops pacing for every project. Put the failing task or project on
hold, tell the user on the terminal, and keep pacing the other eligible tasks.

## What happened, 2026-10-07 to 2026-10-10

The T3 pacer printed this line on every tick for about three days and launched
nothing in any of its 17 trackers:

    T3 withheld: T3 rejected launching simple-ca7153b94516ed5f from
    /home/agent/prg/filemill/TASKS.md 3 times; inspect T3 worktree setup for that
    project, then delete this pending launch record to retry

- Between 17:35 and 17:37 UTC on 2026-10-07, T3 rejected the filemill launch three
  times. Each time the worktree checkout finished (297/297 files), then
  `git config branch.pacer-<thread>.gh-merge-base main` failed in
  `/home/agent/prg/filemill`, recorded only as `GitVcsDriver.createWorktree.configureBaseRef
  ... exited with a non-zero status`. T3 did not keep Git's error output.
- The same `git config` write succeeded on 2026-10-10, so the failure was temporary. The
  most likely cause is a `.git/config.lock` that existed for those two minutes. The
  filemill agent thread `4f310518` was running in that repository at the time.
- The pending launch record
  `~/.local/state/kandev-pacer/t3/aa394114….json` reached `rejections: 3`. From then
  on `dispatch()` raised `Withheld` on every tick. Because "an unresolved launch blocks
  new admissions" (see [31]), no other tracker got a turn, and capacity sat at
  `0/1 running, 0/2 total` for both providers.
- Each rejection left an orphan worktree and branch behind in filemill:
  `pacer-b1c943b4…`, `pacer-2de97ce1…`, `pacer-2a028785…`.

## What to change

- When a pending launch hits `REJECTION_LIMIT`, or fails in any other way that needs a
  human, mark that issue as held, instead of keeping it as the one pending launch that
  everything else waits behind. Then admit the next eligible issue in the same tick.
- Decide whether the hold covers only the issue or its whole tracker. Worktree setup
  failures belong to the project, so another issue from the same tracker would probably
  fail the same way. Tracker-level holds look like the safer default; issue-level holds
  fit failures tied to one issue, such as a malformed prompt.
- Show held items on every status tick in a short form that says what is held, why,
  since when, and the exact command or file deletion that releases it. One line per
  hold, not the current full error repeated on every tick (compare [38]).
- Keep T3's error text in the held record so the cause is still there after the
  console scrollback is gone. This time the only copy of the rejection message was in
  tmux scrollback, which had already rotated out.
- Consider an automatic retry after a cooldown (for example one hour) for failures that
  look temporary, such as a Git lock. Holds that a person has to clear should still
  stay held until they act.
- Clean up, or at least list, the orphan `pacer-<thread>` worktrees and branches that
  rejected launches leave behind.

## Keep

- At most one launch is in flight at a time per admission decision, so a slow T3 accept
  still can't cause a double launch.
- [31]'s rule that a dirty or off-branch checkout skips only its own tracker.

[31]: 31-dirty-repo-blocks-all-trackers.md
[38]: 38-heavy-console-output.md

## Implementation, 2026-10-10

A pending launch that fails in a way covered below is now held. The pacer writes a
`hold` into its launch record, logs one `T3 holding ...` warning with the full reason,
and admits the next eligible issue in the same tick. Each later tick logs one
`T3 HELD ...` line per hold with the scope, tracker, start time, short reason and the
`rm` command for its record. Deleting the record is the release for every hold. The
record path was already the documented fix for the rejection case, and a new CLI
command would only have wrapped `rm`.

Scope decisions:

- T3 rejections hold the whole tracker, as proposed. So do TASKS.md changes between
  saving the record and the scheduling commit, which used to withhold everything.
- An issue that leaves the queue before T3 accepts it holds only that issue. The
  tracker is fine, so its next issue launches.
- A pending record whose checkout left the base branch skips its tracker for that
  tick, like [31]'s check on new launches. It used to block every tracker.

Rejections now wait an hour between attempts instead of retrying on the next tick.
The first and second rejection hold the tracker with an automatic retry. The third
holds it until a person deletes the record. With this, the filemill lock on
2026-10-07 would most likely have cost one attempt instead of all three. I kept the
limit at three, so the number of leftover worktrees per tracker stays the same.

The record keeps a `rejected` list with each rejected branch, the time and T3's error
text (first 4000 characters). The `T3 holding` warning lists the leftover branches.
The pacer does not delete leftover worktrees. Doing it from the pacer would mean
writing to the same `.git` that just failed, and the dotfiles T3 copies into each
worktree make a plain `git worktree remove` refuse anyway. docs/t3-pacing.md gives
the commands to find and remove them.

The live record `aa394114….json` has `rejections: 3` and no hold. After a restart, the
new code holds filemill's tracker on its first tick and paces the other trackers.

### Trade-off

Before, three rejections anywhere stopped all launches, so a broken T3 left at most
three leftover worktrees in total. Now each tracker gets its own three attempts. If T3
itself breaks and rejects every launch, each project can collect up to three. I left
out a server-wide limit because it would bring back the blocking this task removes.

### Still blocking every tracker

These failures outside pending launches still withhold the whole tick. They are
candidates for follow-up tasks:

- A record whose accepted or observed thread is missing from T3. Deleting an old
  pacer thread in T3 would trigger this.
- A tracker that matches zero or several T3 projects, and a parent thread from
  another project.
- Git or Hai errors during the scheduling commit, such as an `index.lock`, and lost
  dispatch replies. These retry on the next tick, so they block only while they last.

### Validation

`HOME=$TMPDIR/home XDG_CONFIG_HOME=$TMPDIR/home/.config uv run --extra t3 --frozen
pytest -q`: 802 passed, 20 skipped. `darker --check`, `graylint` and `ty check` on the
changed files pass. New tests in tests/test_t3.py replay the stuck filemill record,
check that a rejected tracker lets another tracker launch in the same tick, and cover
the hourly retry, the hold after three rejections and release by `rm`, the issue-level
hold, and the off-branch pending record.
