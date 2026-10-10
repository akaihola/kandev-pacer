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
