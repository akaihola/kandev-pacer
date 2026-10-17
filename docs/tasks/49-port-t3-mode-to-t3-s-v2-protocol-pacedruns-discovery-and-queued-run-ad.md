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
