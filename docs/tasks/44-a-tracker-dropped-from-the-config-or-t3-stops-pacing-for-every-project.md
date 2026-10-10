Taking one project out of the pacer's configuration, or out of T3, stops every
tracker. Skip only the affected tracker, with one status line per tick, the way [31]
skips a dirty checkout.

## What happens now

Two checks in `T3Pacer.tick()` raise `Withheld` for the whole tick:

- **Record for an unconfigured tracker.** Every launch record must name a tracker
  that is still in `t3_trackers`. [33] made the message clear but kept the stop. The
  pacer never deletes records (69 on 2026-10-10), so every tracker that has launched
  work has some. Removing any of those trackers from the configuration, for example
  to retire a project, stops all pacing until a person moves its records out of the
  state directory.
- **Tracker without exactly one T3 project.** A tracker must match one T3 project by
  `workspaceRoot`. If its project is deleted or added twice in T3, or the repository
  moves, the tick stops at that tracker. Trackers after it in the configured order get
  no turn.

A configured `parent_thread` that is missing or belongs to another project stops the
tick the same way. No tracker in the current configuration sets `parent_thread`, so
this one matters less.

## What to change

- Ignore records whose tracker is no longer configured, and say so in one short line.
  These records can't launch anything. Running threads count against capacity
  through T3's snapshot whether or not a record exists. If the tracker comes back,
  its records still prevent duplicate launches.
- Skip a tracker with zero or several matching projects, or a bad parent thread. Log
  one line that names the fix, then go on to the next tracker.
- Keep stopping everything when a record's T3 server URL differs from `t3_url`. That
  means the pacer points at the wrong server, so every record is suspect.

## Keep

- [33]'s recovery steps in docs/t3-pacing.md, updated to match.
- [31]'s per-tracker skip for dirty or off-branch checkouts.

[31]: 31-dirty-repo-blocks-all-trackers.md
[33]: 33-unclear-withheld-message.md
