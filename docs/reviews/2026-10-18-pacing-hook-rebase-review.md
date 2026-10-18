# Pacing-hook rebase review, 2026-10-18

Issue [58]. This review reads the rebased hook commit `91cfe8521f`, which sits on
stable v0.1.0 (`81493efb71`). It compares that commit with the pre-rebase commit
`ea944594ce` on `5cbe82a870`, and checks the 281 upstream commits between those
bases. All of it was read with `git show`, `git diff`, `git log`, `git grep` and
`gh api`. Nothing in t3code was checked out, fetched, built or run. The test and
typecheck results cited below are [56]'s.

## Verdict

**Go.** Deploy `t3code-pacing-hook-v0.1.0.patch` on v0.1.0. Nothing I found
blocks deployment, and no finding needs a code fix after it. The rebase
changed one hunk of the hook beyond context. That hunk is the Stop conflict,
and [56] resolved it correctly. Upstream's new Stop step cancels buffered
provider wakes, and those are agent messages that the hook never paces. No
upstream commit touches the hook's gate in `startNextQueuedRun`, the queue
branch of `dispatchMessage`, `queued-run.edit`, promote-to-steer,
`prepared-run.release` or the post-commit queue drain. No new start path can
reach a paced run. The tables, columns and snapshot route behind `pacedRuns` are
unchanged. The checks that `queued-run.admit` copies from `startNextQueuedRun`
haven't drifted, so an admission still starts exactly one run. The patch file
is byte-identical to `git format-patch -1 91cfe8521f` and applies cleanly to
v0.1.0. One decision stays with Antti before cutover. After a restart, v0.1.0
resumes interrupted work without asking the pacer, and it does so in more cases
than [56]'s checklist says (finding 3).

**Newer tag: no.** Stay on v0.1.0. There is no stable release after it. Every
later tag is a nightly or a preview, and the previews are titled "maintainer test
build, do not install". I checked the 197 commits up to
`v0.1.1-nightly.20261017.3238` and the files the hook depends on in the two
newer remote nightlies. None of them fixes the queue, Stop, restart, settle or
snapshot code the hook relies on, and none fixes the V1 import. The patch also
no longer applies to 3238 without a rebase (finding 8). That would mean another
round of the memory-heavy validation on gogo for no gain.

## Findings

Severities are: blocks deployment, fix after deployment, or note. All eight
findings are notes. Findings 1, 2 and 3 need a word from the steward or Antti. The rest
are background for the next rebase or for operating the pacer.

An invariant here means a rule the hook promises always holds. Example: "Resume
never starts a paced run." Findings 1 and 2 are cases where the issue states
an invariant the hook was never designed to keep.

### 1. A restart leaves a parked run with the pacer, not the user

Severity: note. The issue's invariant is wrong here, and the code matches the
spec.

The issue says a server restart returns a parked run to user control. It
doesn't, and it didn't before the rebase either. Restart recovery holds every
queued run that isn't held yet and skips the ones already held. A parked run is
already held, so it keeps `pacing: "held"`, stays in `pacedRuns` and can still be
admitted. Only a paced run that was still `"waiting"` behind other work gets
held for the user. The spec chose this. It says "Restart recovery leaves
already-held runs alone, so parked runs stay admittable after a restart", and
it lists "Demoting parked runs in recovery" under left-out items. The test
"restart keeps a parked run admittable and holds a waiting one for the user"
asserts it, and it passed on the rebase.

Evidence:

- `ProviderRuntimeRecoveryService.ts:290` at `5cbe82a870`:
  `if (run.status !== "queued" || run.queueHeld === true) continue;`
- Same rule at v0.1.0 `:317`, `if (run.queueHeld === true) continue;`, after
  `05e4f888a0` (#19099) split the condition.
- The hold payload `{ ...run, queueHeld: true }` keeps `pacing`: base `:298`,
  v0.1.0 `:325`.
- Test: `PacedQueue.test.ts:626` at `91cfe8521f`.

Smallest fix: none for deployment. Correct the invariant in the steward's notes.
If Antti wants a restart to hand parked runs back, the recovery loop would write
`Pacing.stopHold(run)` for queued runs with `pacing === "held"` instead of
skipping them. That would be a new hunk in a file the patch doesn't touch yet.

### 2. Settle refuses a thread with a parked run instead of freeing it

Severity: note. This is the same kind of invariant mismatch, unchanged since the
base.

Manual settle and inactivity auto-settle both go through one guard. It refuses
while any queued user message exists: "has a queued message. Send it or remove it
from the queue before settling." A parked run counts. A paced launch prompt that
is still preparing gets refused as a running run. So a settled thread can't
hold a parked run. That makes it harmless that `listPacedRuns` and
`admissionRefusal` don't filter settled threads.

One side effect is log noise. Auto-settle sees a parked thread as idle. Once a
run has been parked longer than `sidebarAutoSettleAfterDays`, every sweep tries
to settle it, gets refused and logs "automatic thread settlement skipped".
Upstream does the same for queues held by Stop.

Evidence:

- Settle guard: `Orchestrator.ts:2649` and `:2664` at `5cbe82a870`, `:2668` and
  `:2683` at v0.1.0, `:2678` and `:2693` at `91cfe8521f`. The block is
  byte-identical across the range.
- `isAutoSettlementCandidate`: v0.1.0 `ThreadSettlementService.ts:139`. The
  setting's default is at v0.1.0 `packages/contracts/src/settings.ts:1213`.
- Commits checked: `bf8ffe81a6` (#14642), `519d64f6ce` (#19355), `21bedfb175`
  (#15208).

Smallest fix: none.

### 3. Restart continuation starts unadmitted turns, now by default and in more cases

Severity: note, with a decision before cutover. It is a setting, not a hook
defect, and [56]'s checklist already raises it. The checklist's wording is
incomplete.

`b9ad3daf8d` (#18461) is in this range. It turned
`continueThreadsAfterServerUpdate` on by default and caps each thread at three
continuations in a row. The server dispatches a continuation as
`createdBy: "agent"` and `creationSource: "server"`, with
`restartContinuationOfRunId`. `isPacedDispatch` never paces it, by design.

`eeaac49a9d` (#19291) adds a second trigger. Take a run that had completed, or
was waiting, and whose background work the restart cut: a subagent, a monitor
or a background task. The server now wakes that run too. [56]'s checklist only
says "a thread cut off by a server restart continues by itself". [57]'s
scenario 6 showed that main counts a continuation on a launched thread as one
paced thread. It didn't test the new wake of completed runs.

Evidence:

- `packages/contracts/src/settings.ts:1110-1112` at `5cbe82a870`:
  `withDecodingDefault(Effect.succeed(false))`. At v0.1.0 `:1145-1147`:
  `Effect.succeed(true)`.
- At v0.1.0 `RestartContinuation.ts`: the cap at `:31`
  (`MAX_CONTINUATION_CHAIN = 3`), the wake of completed or waiting runs at
  `:150-183`, and the dispatch's provenance at `:298-310`.
- At `91cfe8521f`: `Pacing.ts:58-65`, where continuations are never paced.

Smallest fix: before cutover, add the wake of completed runs to the checklist
item. Antti then chooses as [57] framed it. He can keep the setting on and
accept unadmitted continuations, or switch it off and recover interrupted work
by hand.

### 4. Admitting just after a restart can cancel a pending continuation

Severity: note.

This is the spec's first edge case. An unpaced turn runs over a parked queue,
and a restart cuts it. Recovery leaves the parked run admittable and schedules a
continuation of the cut turn. If the pacer admits the parked run before that
continuation runs, `continueRestartedRun` sees a run that started after the cut
one and drops the continuation. Nothing starts twice. The cut turn just doesn't
resume by itself. This needs the setting from finding 3, which was off at the
base.

Evidence: v0.1.0 `RestartContinuation.ts:231-236`,
`runs.some((run) => run.id !== source.id && run.status !== "queued" && runRanAfter(run, source))`.

Smallest fix: none needed. If wanted, the pacer could skip admission for one
tick after it sees T3 restart.

### 5. Stop conflict: resolved correctly, one patch marker lost

Severity: note.

`05e4f888a0` (#19099) split Stop's check of queued runs. Stop now cancels a
queued run whose message is a buffered provider wake, and holds the rest. [56]
put the hook's condition after that cancel step, which is right. A buffered
wake needs `createdBy: "agent"`, `creationSource: "provider"` and a
notification. A paced prompt needs `createdBy: "user"` and no notification, so
no run can be both. Upstream's `ThreadStop.test.ts` passed on the rebase, 19
tests per [56].

The rebase also dropped the `Pacing patch:` prefix from this hunk's comment. It
is the only hunk that lost it: `Orchestrator.ts` had 9 markers before and has
8 after. At the next rebase, a search for that prefix will miss the Stop hunk.

Evidence:

- `Orchestrator.ts:8889-8890` at `ea944594ce`, and `:8971-8972` at
  `91cfe8521f`.
- Upstream's split at v0.1.0 `Orchestrator.ts:8895-8909`. Before it, the base
  had one combined check at `:8827`.
- At v0.1.0 `NotificationMailbox.ts:9-18`, and at `91cfe8521f`
  `Pacing.ts:49-56`.

Smallest fix: restore `// Pacing patch: Stop also takes a parked run back from
the pacer.` at the next rebase. It isn't worth a new patch now.

### 6. `pacedRuns` is read outside the snapshot's read worker

Severity: note. It was already true before the rebase.

`8659fcf145` (#19226) moved the shell snapshot's thread read to a read-only
worker connection, so large databases don't stall the server.
`Pacing.listPacedRuns(sql)` still runs on its own, on the main connection. So
each full snapshot runs one small extra query on the event loop, and `pacedRuns`
can be a moment newer than `threads`. Admission checks everything again, so a
stale list can only lead to a refusal.

Evidence: v0.1.0 `ShellStream.ts:59`, `.pipe(NodeSqliteClient.readOnly)`.
The hook's call is at `ws.ts:997` at `91cfe8521f` (`:986` at `ea944594ce`).

Smallest fix, if wanted: `Pacing.listPacedRuns(sql).pipe(NodeSqliteClient.readOnly)`.
In-memory tests can't exercise that worker, so it would need a file-backed test.

### 7. Opt-in storage cleanup can delete a long-parked thread's worktree

Severity: note. It is unchanged since the base and fails safe.

`storageCleanupThreadIdle` treats a parked thread as idle. So the opt-in rules
`worktreeAfterDays`, `worktreeOnMerge` and `worktreeUnchanged` can remove its
worktree. All three are off by default. An admission then reaches the
missing-worktree hold in `startNextQueuedRun`, and nothing starts. The new rule
for settled worktrees from `21bedfb175` (#15208) can't reach a parked run,
because settle is refused (finding 2).

Evidence: `apps/server/src/storageCleanup.ts:101` at `5cbe82a870`, and `:108`
at v0.1.0.

Smallest fix: none. Check that gogo's storage-cleanup rules are off, or accept
the hold.

### 8. The patch doesn't apply to 0.1.1 nightlies as is

Severity: note. It only matters when moving past v0.1.0.

`86534db4d3` (#19559) deleted the comment `/** Restart recovery holds the queue
until the user explicitly resumes it. */`. The patch's contracts hunk uses that
comment as context. On `v0.1.1-nightly.20261017.3238`, `git apply --check`
fails on `packages/contracts/src/orchestrationV2.ts` with "patch failed:
packages/contracts/src/orchestrationV2.ts:554". The other six files apply with
offsets of up to 168 lines. With `-C1` everything applies. Still, a deploy build
shouldn't rely on reduced context.

Evidence: the field is at `orchestrationV2.ts:554` at v0.1.0 and `:532` at
3238.

Smallest fix: rebase when moving to 0.1.1. Then rerun the focused tests and
`t3-lowmem tc --importers`.

## Range-diff, hunk by hunk

`git range-diff 5cbe82a870..ea944594ce 81493efb71..91cfe8521f`:

```text
1:  c45bcb2e63 = 1:  135be18f15 docs(pacing-hook): track spec and research for the pacing admission hook
2:  ea944594ce ! 2:  91cfe8521f feat(server): add pacing admission hook for prompts ending in "..."
```

The docs commit is unchanged. In the code commit, the commit message and the
three new files (`Pacing.ts`, `Pacing.test.ts`, `PacedQueue.test.ts`) are
byte-identical. Of the 25 hunks in existing files, 24 differ only in line
offsets. The table gives each hunk's start line in the patched file.

| # | File | `ea944594ce` | `91cfe8521f` | What the hunk does | Range-diff |
|---|---|---|---|---|---|
| 1 | `Orchestrator.ts` | 116 | 119 | Imports `Pacing` | same |
| 2 | `Orchestrator.ts` | 475 | 488 | `commandThreadId` routes `queued-run.admit` | same |
| 3 | `Orchestrator.ts` | 1365 | 1384 | `startNextQueuedRun` parks a paced head | same |
| 4 | `Orchestrator.ts` | 4712 | 4760 | `dispatchMessage` refuses paced fork or merge-back and forces the queue | same |
| 5 | `Orchestrator.ts` | 4947 | 4995 | `shouldQueue` also queues a paced prompt on an idle thread | same |
| 6 | `Orchestrator.ts` | 4962 | 5010 | Queue provider thread allowed to be missing | same |
| 7 | `Orchestrator.ts` | 4975 | 5023 | Target provider thread allowed to be missing | same |
| 8 | `Orchestrator.ts` | 5022 | 5070 | Checkpoint scope allowed to have no anchor run | same |
| 9 | `Orchestrator.ts` | 5048 | 5096 | `pacingHold` for an idle thread with an empty queue | same |
| 10 | `Orchestrator.ts` | 5064 | 5112 | New queued run gets `queueHeld` and `pacing` | same |
| 11 | `Orchestrator.ts` | 5418 | 5466 | A paced launch prompt's preparing run gets `pacing: "waiting"` | same |
| 12 | `Orchestrator.ts` | 5640 | 5688 | Refuses a paced fork, merge-back or provider-switch start | same |
| 13 | `Orchestrator.ts` | 7647 | 7699 | Promote-to-steer refuses a paced run | same |
| 14 | `Orchestrator.ts` | 8028 | 8080 | `queued-run.edit` re-derives pacing from the marker | same |
| 15 | `Orchestrator.ts` | 8194 | 8246 | `prepared-run.release` parks a paced launch prompt | same |
| 16 | `Orchestrator.ts` | 8886 | 8968 | Stop takes a parked run back | **changed**, see below |
| 17 | `Orchestrator.ts` | 10908 | 11103 | `dispatchOnce` handles `queued-run.admit` | same |
| 18 | `Orchestrator.ts` | 11023 | 11218 | Receipt replay drains the queue after admit | same |
| 19 | `Orchestrator.ts` | 11161 | 11356 | Commit drains the queue after admit | same |
| 20 | `testkit/OrchestratorScenario.ts` | 173 | 173 | Test harness routes `queued-run.admit` | same |
| 21 | `ws.ts` | 140 | 140 | Imports `Pacing` | same |
| 22 | `ws.ts` | 979 | 990 | Full shell snapshot adds `pacedRuns` | same |
| 23 | `contracts/orchestrationV2.ts` | 554 | 554 | `OrchestrationV2Run.pacing` | same |
| 24 | `contracts/orchestrationV2.ts` | 2029 | 2042 | `OrchestrationV2PacedRun` and `pacedRuns` | same |
| 25 | `contracts/orchestrationV2.ts` | 3069 | 3087 | `queued-run.admit` command | same |

Hunk 16 differs in three lines beyond context:

- The old patch replaced `if (run.status !== "queued" || run.queueHeld === true) continue;`.
  The new one replaces upstream's `if (run.queueHeld === true) continue;`,
  which `05e4f888a0` split off below its new buffered-wake cancel.
- The hook's condition went from
  `run.status !== "queued" || (run.queueHeld === true && run.pacing !== "held")`
  to `run.queueHeld === true && run.pacing !== "held"`. Upstream's
  `if (run.status !== "queued") continue;` above it now covers the status
  check, so the behaviour is the same.
- The comment lost its `Pacing patch:` prefix (finding 5).

The other changed lines in hunk 16 are context: `) {` and `markStoppedRun`
became `);` and `continue;`. Those come from upstream's new cancel block.

## Invariants after the rebase

| Invariant | Holds? | Why |
|---|---|---|
| A paced prompt (ending in `...` or `…`) parks until admitted | Yes | No upstream hunk lands in `startNextQueuedRun` (base `:1275-1910`) or in the queue branch of `dispatchMessage` (base `:4700-5440`). `dispatchPreparedRunRelease` and `dispatchPreparedRunRetry` are byte-identical across the range. `Orchestrator.ts` has the same start sites as at the base: four `status: "starting"` writes and four `provider-turn.start` effects. The only new `message.dispatch` sites are the delegated child, which has `senderThreadId`, and `DelegatedTaskQuestionReactor`, which is agent, server and notification. The hook never paces either. |
| `pacedRuns` lists every parked run | Yes | Runs are still written by one upsert. It stores the full payload, so `pacing` persists and admission's removal of it persists too. The SQL's columns are unchanged, and archive and delete still write `archived_at` and `deleted_at`. The pacer's `subscribeShell {}` still reaches `subscribeOrchestrationV2Shell`, and no new full-snapshot route was added. |
| `queued-run.admit` starts exactly one run | Yes | Admission loads every run of the thread, with no window. `canStartQueuedRun` is byte-identical. No new gate appeared in `startNextQueuedRun` that `admissionRefusal` would miss. Receipt replay and the post-commit drain are unchanged. |
| Stop returns a parked run to the user | Yes | Finding 5. |
| Settle returns a parked run to the user | No, settle is refused | Finding 2. This is unchanged since the base. |
| Delete returns a parked run to the user | Yes | Delete and archive exclude the run from `pacedRuns`, and admission refuses it as `inactive`. `ThreadDeletion.ts` has no commits in the range. Unarchiving makes the run admittable again, as at the base. |
| A server restart returns a parked run to the user | No, by design | Finding 1. A `"waiting"` paced run does go to the user. |
| Unpaced prompts are unaffected | Yes | Every hook edit on the dispatch path does nothing when `paced` is false, and upstream changed none of those lines. For unpaced runs, the Stop hunk reduces to upstream's code. |

## Upstream commits that overlap the hook

The tables cover every commit in `5cbe82a870..v0.1.0` that touches a file the
hook changes (13 commits), and every commit touching code the hook calls or
depends on (19 more). `OrchestratorScenario.ts`, `QueuedRunOrder.ts`,
`packages/shared/src/orchestrationV2ThreadError.ts`, `ProjectionRecovery.ts`,
`ThreadDeletion.ts`, `UsageLimitRecoveryWorker.ts`, `ThreadMessageIntake.ts` and
`ProviderContinuationService.ts` have no commits in the range.

### Commits that touch a file the hook changes

| Commit | PR | Files | Verdict |
|---|---|---|---|
| `265fb7000f` | #18778 | Orchestrator, contracts | Repairs Claude subagent outcomes in a branch that returns before any run handling. It adds a `subagent` target to an internal command. No overlap. |
| `05e4f888a0` | #19099 | Orchestrator, recovery | Snooze still refuses a thread with a queued user message, and a parked run is one. Stop: finding 5. Recovery cancels only buffered wakes: finding 1. Holds. |
| `4d73e1d5cd` | #19248 | Orchestrator, contracts, ProjectionStore | Adds a pull-request link event. No overlap. |
| `25629a76a0` | #19102 | Orchestrator, contracts, ThreadLaunchService | A delegated child can now prepare its own worktree (`defer_start`). It has `senderThreadId`, so it is never paced. The release path is unchanged. Holds. |
| `eeaac49a9d` | #19291 | Orchestrator, RestartContinuation, recovery, ProjectionStore | Its continuation-source check in `dispatchMessage` sits above the hook's paced check, and continuations are never paced. It adds the wake of completed runs (finding 3). Holds. |
| `22af947e0d` | #19631 | Orchestrator | Delegated-completion recovery now reads only runs. It starts nothing. No overlap. |
| `ab3a3cd162` | #19675 | Orchestrator, ProjectionStore | `readCommandProjection` skips finished leaf nodes, not runs. The gate and admission don't use nodes. Holds. |
| `c814e51af2` | #18459 | Orchestrator | Dismisses a cancelled task's child questions. No overlap. |
| `0b68a4a9c3` | #19210 | Orchestrator | A model change no longer restarts a steered turn. Paced prompts never steer. Holds. |
| `cb65643b07` | #19213 | ws.ts, deps | Effect 4.0.4. The dropped Effect patch only added an RPC ping hook. Decoding still strips unknown keys. Holds. |
| `5a65169bfc` | #15937 | ws.ts, contracts | Resubscribe replay skips unknown event types. The full snapshot is unchanged, and the hook adds no event types, so rolling back to an unpatched build is safe. Holds. |
| `1af138668b` | #18824 | ws.ts | Changes the attachment ID key. No overlap. |
| `55dfc5fc4d` | #15210 | contracts, http.ts, ThreadManagementService | Adds a read-only transcript endpoint. "Dropped" here means dragged into the composer, not a thread state. No overlap. |

### Commits that touch code the hook calls or depends on

| Commit | PR | Area | Verdict |
|---|---|---|---|
| `7ac952c630` | #19418 | ProjectionStore | Adds a read of worktree paths. No overlap. |
| `d0a73b56c2` | #19423 | ProjectionStore | Windowed history reads, which the Orchestrator doesn't use. No overlap. |
| `737192594f` | #19429 | ProjectionStore | Skips writes of unchanged nodes, not runs. No overlap. |
| `519d64f6ce` | #19355 | ProjectionStore, EventSink | Provider-thread writes only. No overlap. |
| `3b2ebcee2f` | #18890 | ProjectionStore | Patches the thread row's `updated_at` in place. Archive and delete still write their columns. Holds. |
| `84cb13412e` | #18628 | ProjectionStore, migration 062 | Changes an index and drops a hint. No overlap. |
| `8659fcf145` | #19226 | ShellStream | Finding 6. |
| `2d14185f32` | #19316 | WireProjection | File-change text only. No overlap. |
| `b9ad3daf8d` | #18461 | settings, RestartContinuation, recovery | Finding 3. |
| `e77a48e81a` | #19131 | RunExecutionService | A stream that ends without a terminal event now fails the run. The failure hold keeps `pacing`. Holds. |
| `bf8ffe81a6` | #14642 | ThreadSettlementService, settings | Settles less often. Finding 2. |
| `21bedfb175` | #15208 | storage cleanup, settings, ProviderTurnStartService | Settled-worktree cleanup is opt-in (finding 7). The turn start takes a workspace lease, and its start check is unchanged. Holds. |
| `fdacb3a380` | #19521 | DelegatedTaskQuestionReactor | A new wake (agent, server, notification), never paced. Like other wakes on an idle thread, it starts ahead of a parked queue. So the pacer may get `busy` for a moment. Holds. |
| `924fb0c4d4` | #19538 | MCP | Message provenance and modes unchanged. No overlap. |
| `8b460a3c85` | #14416 | MCP | Message provenance and modes unchanged. No overlap. |
| `ee2763e9b2` | #17823 | MCP | Message provenance and modes unchanged. No overlap. |
| `570542662a` | #16102 | server ownership | Startup still runs recovery before accepting commands. No overlap. |
| `94ca023bf4` | #19298 | server ownership | Startup still runs recovery before accepting commands. No overlap. |
| `48595c787c` | #18509 | auth | Restricts cookie credentials by origin. The pacer's bearer-to-WebSocket-ticket flow is unaffected. |

## Newer tags

- Locally, the latest tag is `v0.1.1-nightly.20261017.3238` (`70d60e03a7`),
  197 commits past v0.1.0. On GitHub there are two newer nightlies:
  `v0.1.1-nightly.20261017.3272` (`13aa882c22`) and
  `v0.1.1-nightly.20261018.3285` (`16918b57d6`). Previews go up to
  `v0.1.1-preview.20261017.3254`. There is no stable tag after v0.1.0.
- Local commits after v0.1.0 that touch the hook's files:
  - `a54a44c669` (#20184) queues a steer aimed at a compacting run. It only
    changes unpaced prompts, because paced ones are already queued.
  - `7111501be3` (#19750) makes restart recovery skip runs that never
    started. Queued runs were already skipped.
  - `86534db4d3` (#19559) removes comments (finding 8).
  - `4e85a93b20` (#20079) and `11ce406d2c` (#19751) are unrelated to the hook.
  - `a5d97921f6` (#15094) and `65d390fc52` (#19437) add other WebSocket
    methods.
  - `5752253818` (#19435) is a general compaction fix.
- On GitHub after 3238, two commits touch the hook's files. `8e0d5e5e4d`
  (#20449) is a folder-resolution refactor that doesn't reach the hook's hunks.
  `7e0418a641` (#20947) is migration 063, which drops six dead V1 projection
  tables to reclaim space. Neither fixes the V1 import.
- `git apply --check`: clean on v0.1.0. On 3238 it fails in the contracts file
  (finding 8). 3272 and 3285 weren't checked, because that would need a fetch.
  Both contain `86534db4d3`, so expect the same failure.

## Patch file

`/home/agent/.local/state/t3-upgrade-prep/2026-10-18/t3code-pacing-hook-v0.1.0.patch`:

- 63,105 bytes. SHA-256
  `4379ccc25dcd6ae9afeb67118867bef033dbad438d7ff9a5a468bac39dc621a9`, which
  matches [56]'s record.
- `cmp` against a fresh `git format-patch -1 91cfe8521f --stdout` reports no
  difference, and `git patch-id --stable` gives the same id,
  `ffc8916e0636cc0ca1abea7c53094ac534811927`, for both.
- It changes exactly the seven code and test files listed above, and no
  documentation.
- `git apply --cached --check --whitespace=error` against a temporary index
  loaded from v0.1.0 passes for all seven files.
- The commit's parent is the docs commit `135be18f15`. Its only change from
  v0.1.0 is 15 new files under `docs/pacing-hook/`. So the code patch applies
  to v0.1.0 itself.

## Method and limits

- I read only. I ran no tests, typecheck or dev server. The hook's test results
  (40 + 18 + 19 tests, plus the scoped typecheck) are [56]'s.
- The review traced each upstream change to the hook's functions and rules,
  and spot-checked the decisive lines (the settle guard, the restart setting's
  default, the restart and Stop hold loops, the slim run schema in
  `ProjectionStore.ts`). The slim schema predates the range and only decodes.
- I didn't review the kandev-pacer side of the contract here. [57] covered it
  end to end on a dev server.
- Line numbers refer to the revision named next to them. "Base" means
  `5cbe82a870`.

[56]: ../tasks/56-prepare-the-t3-upgrade-with-the-pacing-hook-patch-for-deployment-from-.md
[57]: ../tasks/57-rehearse-the-t3-cutover-run-main-against-a-dev-t3-server-built-from-56.md
[58]: ../tasks/58-review-the-pacing-hook-rebase-onto-t3-v0-1-0-before-antti-deploys-it.md
