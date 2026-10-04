---
depends_on: []
---

# Pace Hai TASKS.md tasks in T3 Code

## Intended result

Add a T3 Code execution mode to kandev-pacer. Use the TASKS.md system in `~/prg/hai`
as the task source, through its `hai_tasks` Python module. Hai's CLI provides manual
inspection and troubleshooting. Reuse the existing quota gates and concurrency limits.
Keep the existing Kandev mode available.

The user confirmed that `hai_tasks` is ready for kandev-pacer to use. The earlier
dependency on Hai task [16]'s Python API is satisfied. Build against this module;
do not add another Markdown parser or queue format to kandev-pacer.

## Responsibilities

- Hai owns task parsing, backlog order, dependencies, descriptions, and tracker updates.
  Select eligible work using its native sections and dependency rules. Do not launch
  unverified proposals or completed work.
- Pacer decides when quota and concurrency permit a launch. Attribute each launch to
  the subscription used by its configured T3 model and provider.
- T3 Code runs the admitted task in a thread and isolated Git worktree. Use the
  `t3-start-thread` skill in `~/prg/skills-akaihola/` as the reference for authentication,
  thread creation, settings inheritance, and recovery after interrupted requests.

## Lifecycle and recovery

Follow the repository's TASKS.md scheduling, implementation, validation, and completion
rules through Hai's API, including its conflict checks. Build the task prompt from the
issue summary, linked description, repository instructions, and permitted scope.
Configure the parent thread explicitly when inheriting model and runtime settings.

Persist the canonical tracker path and stable issue identity together with the T3
thread ID, command ID, and launch record before submitting a request. Reconcile that
record with T3 after a restart or lost reply instead of creating a duplicate thread.
This is execution bookkeeping; TASKS.md remains the task queue. Never persist tokens.

Monitor starting and running turns for concurrency and report errors, pending
approvals, and pending user input. An idle thread must not automatically relaunch.
A completed turn alone must not mark the issue complete. Completion follows the
tracker's validation and merge rules, and only explicit user acceptance checks `[x]`.

## Verification

- Exercise backlog order, unresolved dependencies, excluded sections, and Hai revision
  conflicts with fixtures, including multiple trackers with overlapping issue numbers.
- Verify quota and concurrency prevent launches, with unknown provider or thread state
  withholding work until it can be reconciled.
- Simulate a lost launch reply and pacer restart; verify the same task creates one
  thread and retains its worktree and inherited settings.
- Verify waiting for input and turn completion cannot cause automatic relaunch or user
  acceptance, and that the existing Kandev mode still works.

## Source reference checked on 2026-10-04

Gogo runs T3 Code `0.0.44` at `http://100.81.121.7:3773`. The matching source is checked
out at `~/repos/ai/t3code-v0.0.44`. Its contracts support `thread.turn.start`, worktree
bootstrap with `requireWorktree`, turn/session state, `hasPendingApprovals`, and
`hasPendingUserInput`. Recheck the installed version before implementation and make
the endpoint configurable.

## Implementation plan, 2026-10-04

- Add opt-in `PACER_MODE=t3` with a JSON list of tracker/parent-thread pairs,
  a configurable endpoint, base branch, and private state directory. Keep Kandev
  as the default. Install Hai as an optional local Python dependency.
- Read issues and linked descriptions with `hai_tasks.TasksStore`. Select scheduled
  issues before eligible ordered backlog issues, retaining order within each section.
  Resolve dependency references within each tracker, requiring completed dependencies.
- Reuse provider quota reads, scoped model gates, and per-provider/total caps.
  Count starting and running T3 turns across the server. Unknown state or attribution
  blocks admission. Inherit settings only from the explicitly configured parent.
- Persist canonical tracker identity, issue identity, revision, prompt, and exact T3
  command atomically before scheduling or dispatch. Lock the state directory against
  concurrent pacers. Schedule through Hai with revision checks and commit only the
  tracker on the configured base branch before T3 prepares its worktree.
- Reconcile saved commands before selecting new work. Reuse command/thread IDs after
  lost replies. Retain records for idle, failed, deleted, and completed threads so none
  relaunch automatically. Let the worker follow tracker rules for implementation,
  validation, merge, and completion; never infer completion from turn state.
- Add offline tests using real Hai fixture trackers and a fake T3 server, covering
  dependencies, overlapping numbers, conflicts, gates, state, and interrupted launch.
  Run the full Python suite and repository lint/type checks.

## Implementation evidence, 2026-10-04

Implemented opt-in T3 mode, Hai scheduling, subscription gates, private launch
records, restart reconciliation, thread-state reporting, and isolated worktree
bootstrap with explicit parent settings. Setup and recovery instructions are in
[docs/t3-pacing.md](../t3-pacing.md).

Validation: 641 tests passed, 20 live-contract tests skipped. The 31 new tests cover
Hai selection and conflicts, overlapping tracker identities, gates and capacity,
waiting/finished turns, lost replies and restart, private records, process locking,
authentication renewal, and WebSocket framing. `darker --check`, `graylint`, whole-tree
`ty check src tests`, and `git diff --check` passed.

The local T3 CLI reports 0.0.44. Gogo was reachable over HTTP, but SSH authentication
was refused, so its installed version was not independently reconfirmed. No live
backlog work was launched. Activation requires tracker paths, explicit parent thread
IDs, and endpoint authentication. Kandev remains the default.
