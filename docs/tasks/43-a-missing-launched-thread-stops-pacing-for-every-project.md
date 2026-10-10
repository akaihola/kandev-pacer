A launch record whose thread is no longer in T3 stops every tracker. The pacer should
hold that record and keep pacing the other trackers, the way [39] does for failed
launches.

## What happens now

On each tick, `T3Pacer.tick()` looks up the thread of every launch record in
`~/.local/state/kandev-pacer/t3/` in T3's snapshot. If a record in phase `accepted` or
`observed` has no thread there, the pacer raises
`Withheld("Previously accepted thread is missing; inspect T3")`. That ends the tick
before any tracker gets a turn. It repeats on every tick until a person moves the
record out of the state directory, and the message doesn't say which record or thread
is missing.

## How it can happen

- Someone deletes a finished pacer thread in T3. On 2026-10-10 the state directory
  held 69 records, and all 69 threads were still in T3. T3's `projection_threads` table
  had 22 deleted and 9 archived threads, none of them a pacer thread yet. Deleting any
  of the 69 would stop all pacing. Nobody has checked yet whether archived threads
  stay in the snapshot.
- T3 accepts a launch, then deletes the thread when worktree setup fails later. This
  hasn't been seen. [39] only covers T3 rejecting the RPC itself.

## What to change

- Hold the record instead of withholding the tick. Use [39]'s `hold` field and
  `T3 HELD` line, and name the record, issue and thread in it.
- Decide the scope. A thread the pacer saw (`observed`) that later vanished was most
  likely deleted by a person, so an issue hold fits. An `accepted` record whose thread
  never appeared points at T3 failing the setup, which [39] treats as a tracker
  problem.
- Archive a finished pacer thread in T3 and check whether it leaves the snapshot. If
  it does, handle archived threads the same way.

## Keep

- A missing thread never makes the pacer launch its issue again by itself. The record
  is what prevents a duplicate launch.
- Capacity still comes from the threads in T3's snapshot, not from records.

[39]: 39-hold-failed-launch-pace-others.md
