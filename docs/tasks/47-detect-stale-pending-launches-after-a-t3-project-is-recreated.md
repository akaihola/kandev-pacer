---
depends_on: []
---

# Detect stale pending launches after a T3 project is recreated

## Decision to make

Should the pacer detect a saved launch targeting a deleted T3 project before it
tries to dispatch, and explain how to recover without launching the issue twice?
This is an optional follow-up from the independent review of [44]. The reviewer
found no defects within [44]'s requested scope. This limitation existed before
that change and does not block acceptance of [44].

## What breaks, with an example

A repository's directory and its T3 project ID are different things. Deleting a
project and adding the same directory again can create a new project ID even
though the directory has not moved.

1. The pacer prepares a launch for project A and saves its exact command in a
   launch record. The saved `bootstrap.createThread.projectId` is A.
2. Before the launch finishes, someone deletes A and adds project B using the same
   `workspaceRoot`. They also fix the configured parent thread to belong to B,
   if the tracker uses a parent.
3. The current tracker checks pass: exactly one project uses the right directory,
   and the parent belongs to it. However, the pending record still targets A.
4. Dispatch resends the saved command rather than preparing one for B. In the
   review probe, a server double rejects it with `Project does not exist`.
5. The existing rejection handling tries three times, with cooldowns, then holds
   the entire tracker permanently. The hold gives generic worktree-setup guidance
   instead of identifying the stale project ID. Other trackers keep launching.

Fixing the workspace-root or parent configuration is therefore insufficient in
this particular recovery flow. New work from the affected repository eventually
stays held until a person inspects and resolves its saved launch.

## Evidence and its limits

Independent review on 2026-10-11 reproduced this with both `prepared` and
`submitted` records on both the reviewed [44] implementation and its pre-task
baseline. All four probe cases confirmed three rejected attempts followed by a
tracker hold with no automatic retry. The healthy tracker continued launching.
The reviewed range was `4f3c859..9d327c05bfcde70419e85ff561f9a9d33d539391`.

The probe used temporary trackers and a server double that rejects absent project
IDs. It did not recreate a project on a live T3 server. The four passing tests
verify the observed failure, not successful recovery. The reviewer independently
ran the existing T3 suite with 127 passing tests; its project-recovery cases
restore the original project ID and do not cover recreation with a different ID.

Local reproduction and output:

- `/home/agent/.local/state/t3-review-batch/20261011/ea1682dc-probes/test_recovery.py`
- `/home/agent/.local/state/t3-review-batch/20261011/ea1682dc-probes/results.txt`

Review thread: `b6c1a25e-a036-41a8-9338-267e76522e06`.
Original implementation thread: `ea1682dc-b6e4-4a5f-9c31-2dc058bfaafd`.

## Reasons to act or defer

Act if deleting/recreating T3 projects, migrating project registrations, or
recovering interrupted pending launches is an expected maintenance operation.
Early detection would avoid futile retries and tell an operator which saved
record needs attention. A diagnostic and documentation change could be useful
without introducing automatic command migration.

Defer if projects normally retain their IDs and recreation is rare enough for
manual recovery. No live occurrence was established by this review, and the
failure is isolated to one tracker. There is no evidence here that all projects
stop, that quota is spent on successful agent turns, or that work was lost.

## Proposed scope and recovery choices

Compare each pending command's saved project ID with the uniquely matched current
project before scheduling or dispatch. If they differ, skip or hold only that
tracker and report the tracker, issue, record, saved/current project IDs, and
specific inspection steps. Avoid consuming the ordinary rejection budget merely
to discover that the saved target no longer exists. Keep the saved record so
restart still prevents duplicate launches.

Prefer explicit human recovery for the first implementation. Explain how to
inspect the original thread and issue, determine whether the launch happened,
and intentionally retire or replace the record only when safe. Removing a record
for queued work can cause another launch; generic advice to delete it is not
sufficient.

Automatic migration of a definitely unsubmitted `prepared` record is a separate
design option. Evaluate it only after verifying what each phase guarantees,
including crashes between saving a phase and making the RPC. Do not silently
rewrite an ambiguously submitted command: a lost reply can mean T3 accepted the
original request, and sending a different command could create duplicate work.
Preserve original command and thread IDs unless an explicit, justified recovery
operation replaces them.

Qualify the recovery claim in [44]'s implementation notes: restoring the original
project or parent allows retry, but recreating the project with a new ID may
require pending-record recovery. Update `docs/t3-pacing.md` with the chosen
operator procedure. Leave [44]'s historical validation evidence intact.

## Done when, if approved

- Tests recreate a project with a new ID at the same workspace root for prepared
  and submitted records, including a parent moved to the new project.
- A stale saved project ID produces a specific diagnostic before a doomed
  dispatch and does not block a healthy tracker in the same tick.
- Repeated ticks and restart retain duplicate prevention. A submitted record is
  not silently retargeted or replaced with a new command/thread ID.
- Recovery with the original project ID still works. Tests distinguish this from
  recovery through a new ID and verify the chosen manual or automatic procedure.
- Server-URL mismatches still withhold all launches. Existing dirty/off-branch
  skips, capacity accounting and missing-thread holds remain covered.
- Operator docs explain the failure, the evidence needed to recover, and the
  duplicate-launch risk. The T3 tests and required diff-scoped checks pass.

No hard dependency on [44] acceptance; its implementation is already merged.

[44]: 44-a-tracker-dropped-from-the-config-or-t3-stops-pacing-for-every-project.md
