---
depends-on: [50]
---

# Port T3 mode to T3's V2 protocol: `pacedRuns` discovery and `queued-run.admit`

Step 1 of the pacing-hook sequence (steward decisions D27–D30 in
`/home/agent/my-knowledge/agents/steward/decisions.md`). Upgrading gogo's T3 past
0.0.45 breaks T3 mode until this lands; the T3 deployment waits for it.

## Background

The T3 side lives on branch `t3code/implement-pacing-hook` in
`/home/agent/repos/ai/t3code` (local only): docs commit `c45bcb2e63`, code commit
`ea944594ce`, on upstream `5cbe82a870` (`v0.0.46-nightly.20261011.2980-21`). Its
`docs/pacing-hook/README.md` ("Open items", "Checked end to end") records an
end-to-end check from 2026-10-15 against a dev server, with a stand-in pacer
speaking the wire protocol. A prompt ending in `...` or `…` is paced (D28).
This repository's contract is `docs/t3-pacing-hook.md`.

## Required changes (all observed on the wire, 2026-10-15)

1. **Transport.** `T3Client.call()` in `src/kandev_pacer/t3_client.py` builds
   `/ws?wsTicket=…`. V2 needs `/ws?orchestrationProtocol=2&wsTicket=…`; without it T3
   answers HTTP 426 `orchestration_protocol_incompatible`. The auth flow is unchanged
   (bearer token, `POST /api/auth/websocket-ticket`, `wsTicket`); a session with
   `orchestration:read` and `orchestration:operate` suffices. Update the module
   docstring ("T3 0.0.44").
2. **Activity.** `activity()` and the tick filter in `t3.py` read V1 fields
   (`session`, `latestTurn`, `hasPendingApprovals`, `backgroundLiveness`). V2 uses
   `status`, `activeRunId`, `activityRunStatus`, `pendingRuntimeRequest`,
   `pendingBackgroundTasks` and top-level `lastError`; archived threads move to a
   separate `archivedThreads` array. A parked run is not activity: its thread stays
   `idle` (never ran on V2) or `completed`, with `activeRunId: null`. Do not count it
   against `concurrency`.
3. **Launch.** `prepare()` sends `thread.turn.start` with `bootstrap` and
   `requireWorktree`. V2 uses `orchestration.launchThread` with
   `workspaceStrategy {type: "worktree", baseRef, branch, startFromOrigin}` and
   `initialMessage`; it returns `{threadId, projection, resumed}` rather than
   `{sequence}`, which affects `launch()`. Follow-ups use `message.dispatch`. How a
   failed bootstrap behaves on V2 is unverified. The pacer's own prompts must not end
   in `...` or `…`, or they park.
4. **Discovery.** Read the first `snapshot` chunk of `orchestration.subscribeShell {}`.
   `pacedRuns` present (even `[]`) means a patched server; absent means admit nothing
   and log once. Detect by shape, not version (main still reports 0.0.45). Items are
   `{threadId, runId, modelSelection: {instanceId, model, options?}, requestedAt}`;
   `options` can carry e.g. `effort` and `fastMode`, which the doc's example omits.
   Skip threads with `lastErrorClass: "usage_limit"`. Add each listed run's provider
   to the reset-redemption "work is waiting" set. Log and skip unmappable providers
   without withholding the rest of the tick.
5. **Admit.** `orchestration.dispatchCommand {type: "queued-run.admit",
   commandId: "pace-admit:<runId>:<attempt>", threadId, runId}`. Persist the
   `commandId` before sending. Oldest first, at most one per tick, before pending
   launches.
6. **Replies.** Success `{sequence}` with the run already `running` (the doc says
   "usually `starting`"). A resend of the same `commandId` returns the same
   `sequence` and starts nothing; on timeout, resend the same id. `busy`, `not_next`,
   `usage_limited`: retry later with a new id. `not_waiting`, `inactive`: drop the
   run. A resend of a refused id gets "…was previously rejected…" without the reason:
   mint a new id.

## Docs and tests

Update `docs/t3-pacing-hook.md` (protocol parameter, `options`, `running`),
`docs/t3-pacing.md`, `tests/test_t3.py` and `tests/test_t3_client.py`.

## Acceptance

- Unit tests cover the V2 transport, activity, launch, discovery, admit and every
  reply class above.
- An end-to-end run against a patched T3 dev server, following the README's "Checked
  end to end" recipe (pruned copy of the V1 database, scratch project, cheapest
  model): the pacer launches an issue on V2 and admits a paced prompt exactly once.
  The stand-in pacer and pruning SQL used on 2026-10-15 were in
  `/tmp/claude-1002/pacing-e2e/` (`pacer.py`, `prune-v1.sql`) and may be gone.
- Live gogo T3 (0.0.45, V1) is not touched; deployment is a separate coordination
  item in the vault tracker.

Out of scope: the `t3-start-thread` skill, which also speaks V1 (separate item).

## Implementation and validation, 2026-10-17

Implemented in `05986f7`, merged into local main. V2 transport, activity, worktree
launches, archived-thread observation, paced-run discovery and durable admission
retries are covered. Saved unresolved V1 commands are held for inspection.

Validation used the canonical checkout's Python environment with `PYTHONPATH=src`
to load this worktree's code:

- Full suite: 904 passed, 20 skipped. Skips are opt-in live tests.
- `darker --check`, `graylint` with Ruff and ty, and `git diff --check` passed.
- The workflow tests also use V2 launch envelopes for TASKS.md, scratch and backlog
  sources. Every admission reply class has offline coverage.

The end-to-end check used the patched T3 source at `ea944594ce` in
`/home/agent/.t3/worktrees/t3code/t3code-9f66b19e`. A read-only backup of the earlier
V1 seed database was copied and pruned with the recovered `prune-v1.sql`, leaving
12 stopped threads. The server imported that copy into V2. A separate server bound
only `127.0.0.1:18749`, with private state and a new scratch Git repository under
`/home/agent/tmp/pacer-v2-49`. Its bearer session had only `orchestration:read` and
`orchestration:operate`. No browser interaction was needed for this wire check.

The real `T3Pacer.tick()` scheduled a scratch issue and launched Haiku 4.5 through
`orchestration.launchThread`. The worker wrote `launch-ok.txt` in the isolated
worktree and completed. A `message.dispatch` follow-up ending in `…` parked and
appeared in `pacedRuns`. The pacer admitted it, deliberately discarded the
successful reply, then restarted and resent the saved command ID. Both replies
had sequence `166626`. The follow-up completed with exactly one provider attempt.

Thread: `3d37c8c8-51eb-4441-9739-12e7d8dbc8ca`. Paced run:
`run:thread:3d37c8c8-51eb-4441-9739-12e7d8dbc8ca:ordinal:2`.
Read-only database checks found exactly two completed runs and two completed
provider attempts on that thread, one for the initial launch and one for the
paced follow-up. The isolated server was stopped afterwards.

The end-to-end harness used a deterministic open quota verdict and empty quota
readings. It exercised the real transport, scheduler, persistence, restart and
provider turns without redeeming resets or depending on live allowance timing.
Offline tests cover closed quota gates, provider and total capacity, quota read
errors, disabled providers, API-key policy, and reset waiting.

Local evidence is under `/home/agent/tmp/pacer-v2-49`: `e2e.py`, `admit_e2e.py`,
`admit.json`, `result.json`, `database-evidence.json` and `pytest-final.log`.
The token file and server state are private test artifacts, not repository files.

Live gogo T3 0.0.45 and its running pacer were not restarted or changed. Deployment
remains a separate coordination item. V2 failed-workspace cleanup remains
unverified, as in the original request. Completion awaits user acceptance.
