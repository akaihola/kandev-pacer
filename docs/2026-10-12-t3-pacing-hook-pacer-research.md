# Pacer-side findings for a T3 "paced prompt" hook

> Research from 2026-10-12, kept as written. It was done while the T3 marker was still `[pace]` at the start of a prompt. The marker is now a trailing `...` or `…`, and [t3-pacing-hook.md][hook] has the current contract. `T:` below means the t3code worktree at upstream `5cbe82a870`; the code is now on branch `t3code/implement-pacing-hook` in `/home/agent/repos/ai/t3code`.

This was a read-only investigation. I edited nothing and started no servers. I read `kandev-pacer` at `bdf72a7` and compared it with the T3 worktree at `5cbe82a870`.

**Path prefixes used below:**
- `P:` is [agent@gogo:~/prg/kandev-pacer/](http://gogo/prg/kandev-pacer/?f)
- `T:` is [agent@gogo:~/.t3/worktrees/t3code/t3code-9f66b19e/](http://gogo/.t3/worktrees/t3code/t3code-9f66b19e/?f), upstream main
- `V44:` is [agent@gogo:~/repos/ai/t3code-v0.0.44/](http://gogo/repos/ai/t3code-v0.0.44/?f)

---

## 1. How the pacer talks to T3 today

**How T3 mode starts.** `mode: Literal["kandev","t3"]` (`P:src/kandev_pacer/config.py:40`) makes `main()` hand off to `t3.run` (`P:src/kandev_pacer/runtime.py:1850-1852`). T3 mode is a polling loop that holds a process lock (`P:src/kandev_pacer/t3.py:882-895`).

**Transport.** It uses Effect-RPC JSON over WebSocket, opening a new connection for every call. Everything below is in `P:src/kandev_pacer/t3_client.py`:
- **Ticket:** `POST {url}/api/auth/websocket-ticket` with `Authorization: Bearer <token>`, reading `["ticket"]` (113-120).
- **Connect:** `ws(s)://…/ws?wsTicket=<ticket>` (154-155).
- **Request:** one frame, `{"_tag":"Request","id":<uuid4>,"tag":method,"payload":…,"headers":[]}` (156-169).
- **Replies:**
  - `Ping` is answered with `Pong` (173-175).
  - Frames with other `requestId`s are ignored (176-177).
  - For streams, it returns the first `Chunk` value with `kind=="snapshot"` (178-181).
  - An `Exit` with `Success` returns its value; any other `Exit` raises `T3Rejected` (182-189).
- **Limits:** a 45 s deadline (157, 190-191), the socket closes after each call (192-193), and there are no retries (106-107).
- It never holds a live subscription.

**Auth.**
- `T3_THREAD_AUTH_TOKEN` from the environment is used if set (65).
- Otherwise, for loopback URLs only, it runs `t3 auth session issue --ttl 5m --label "T3 thread handoff" --token-only [--base-dir]` (66-96).
- A non-loopback URL without a token is a configuration error (67-76).
- An auto-issued token is renewed after 240 s (108-112).
- Error text is scrubbed of secrets (121-153).
- Docs: `P:docs/t3-pacing.md:171-177`, `200-250`.

**RPC methods used.** There are only two:
1. `orchestration.subscribeShell` with payload `{}`, taking the first snapshot only (`P:src/kandev_pacer/t3_client.py:195-197`). It is called once per tick (`P:src/kandev_pacer/t3.py:544-545`) and once more after a rejected dispatch (`t3.py:828`).
2. `orchestration.dispatchCommand` (`t3.py:826`), with exactly one command type:
   - `thread.turn.start` with `commandId`, `threadId`, `modelSelection`, `runtimeMode`, `interactionMode`, `createdAt`, and `message{messageId, role:"user", text, attachments:[]}`.
   - `bootstrap.createThread{projectId, title, …, branch:null, worktreePath:null}`.
   - `bootstrap.prepareWorktree{projectCwd, baseBranch, branch:"pacer-<threadId>", startFromOrigin:false, requireWorktree:true}`.
   - Built at `t3.py:318-347`.

**Reading thread and run state.** It reads only the shell snapshot:
- `threads[]`: `id`, `modelSelection`, `session{status, activeTurnId, providerName, lastError}`, `latestTurn{state}`, `hasPendingApprovals`, `hasPendingUserInput`, `backgroundLiveness`, `projectId`, `branch`, `worktreePath` (`t3.py:139-192`, `587-607`, `670-671`).
- `projects[]`: `id`, `workspaceRoot` (`t3.py:652-654`).
- There is no concept of a run id or queued run anywhere.

**How it starts turns.** It only starts new threads for Hai `TASKS.md` issues.
- The prompt is built from the issue (`t3.py:295-317`).
- Model and modes come from the parent thread or from config (`t3.py:292-294`, `436-471`).
- It **never sends follow-ups**: "Idle, failed, waiting, and completed threads … never relaunch automatically. Continue those threads manually" (`P:docs/t3-pacing.md:323-324`; `P:docs/tasks/18-hai-tasks-t3-code.md:41-44`).

**Avoiding duplicate launches.**
- The record is saved with fsync before any external write (`t3.py:103-118`, `348-357`).
- Phases run prepared → scheduled → submitted → accepted → observed (`t3.py:360-404`, `859-861`, `586-605`).
- After a lost reply, it resends the same `commandId` (`P:docs/t3-pacing.md:319-322`).
- If T3 rejects the launch and the thread is absent, it rotates `threadId`, `commandId`, `messageId` and branch, then holds the tracker (`t3.py:827-858`).

**Identifying a thread's provider and model.**
- `selection_provider()` maps `modelSelection.instanceId`, falling back to `provider`, through `PROVIDERS={"codex":"openai","claudeAgent":"anthropic"}` (`t3.py:35`, `41-43`). Custom instance ids are not mapped (`P:docs/t3-pacing.md:252-258`).
- A non-empty `model` is required (`t3.py:408`).
- The model string selects model-scoped quota windows by case-insensitive substring match (`t3.py:423-434`; `P:src/kandev_pacer/selection.py:282-295`).
- `session.providerName` is only used in log text (`t3.py:149-153`).

## 2. The quota gate, tick cadence, and the running count

**Gate function.** `T3Pacer.admitted(selection, running, usage, errors, now)` (`t3.py:406-421`) checks, in order:
1. The provider is mapped and a model is present.
2. `running[name] < concurrency`.
3. The sum of running work across providers is below `concurrency_total`.
4. If the provider has a quota bypass, it returns `apikey_policy=="cap"`.
5. A quota read error means closed.
6. `selection_verdict(...).is_open` (`t3.py:423-434`), which calls `quota_verdict` (`P:src/kandev_pacer/quota.py:227-302`):
   - `blind` when the provider is missing from the usage data (fail closed, 236-244)
   - `ceiling` (266-290)
   - `paused` when the worst headroom is below 0 (300-301)
   - `open` otherwise (302)
   - Headroom is `α·e − c` (15-17).

**Usage data.**
- Read with `provider.read(config.providers, now=now)` (`t3.py:689`; `P:src/kandev_pacer/provider.py:616-635`).
- It is cached for up to 300 s; failures are cached for 15 s (`provider.py:67-69`).
- Reset redemption runs only for providers in `waiting`, which is built from pending launches and eligible tracker issues (`t3.py:690-716`; `P:src/kandev_pacer/resets.py:208-236`). **Paced prompts would need to add their provider to `waiting`.** Otherwise no reset is ever redeemed for them.

**Tick cadence.**
- A fixed `time.sleep(config.interval)` (`t3.py:895`), default 30 s (`config.py:120-124`).
- T3 mode does not use the adaptive `poll_interval` (`quota.py:92-113`). Only Kandev mode does (`runtime.py:806`).
- At most one launch per tick: `tick()` returns after an accepted launch (`t3.py:733-734`, `758-760`).
- A pending launch whose gate is closed returns early and blocks everything after it (`t3.py:720-732`). An admission branch placed after that loop would be starved, so it has to go before it.

**Running count.** It is server-wide and counts at most one per thread (`t3.py:546-565`).
- `activity(thread)` (`t3.py:159-192`) is true when any of these hold:
  - `session.status ∈ {starting, running}`
  - `session.activeTurnId`
  - `latestTurn.state == "running"`
  - `backgroundLiveness ∈ {working, monitoring}`
  - `hasPendingApprovals`
  - `hasPendingUserInput`
- An unknown session or turn enum value, or a missing `session` / `latestTurn` key, raises `Withheld` for the whole tick (`t3.py:161-179`).
- An active thread on an unmapped provider withholds the whole server (`t3.py:551-564`).
- There is **no booking or reservation** in T3 mode. Kandev mode has one (`runtime.py:1611-1613`; `P:docs/design.md:350-352`). T3 mode relies on one launch per tick and the next snapshot.

**What this means for waiting paced prompts:**
- **A waiting prompt must not light up any activity field.**
  - V1: the session stays idle and `latestTurn` is not `running`.
  - V2 (upstream main): the run's status must stay out of `{preparing, starting, running, waiting}`. Those feed `activityRunStatus` and `activeRunId` (`T:apps/server/src/orchestration-v2/ProjectionStore.ts:1494`, `1583-1595`).
  - `queued` is safe. `defer_start` puts a run in `preparing` (`T:apps/server/src/orchestration-v2/Orchestrator.ts:5386`), which would count as running.
- **No new enum values in fields the pacer validates.** The current pacer would withhold everything (`t3.py:161-177`).
- **Count admitted runs until they are observed.** After an admission the pacer should treat the run as running until the snapshot shows it, the same way Kandev's `book()` works.

## 3. Existing "human prompt" or queue concepts

- **Kandev only.**
  - The whole of `P:docs/queueing-user-prompts.md` is about Kandev's `message.queue.*` WebSocket API (73-99).
  - The userscript adds a Queue button for Kandev ports 38429 and 8449 (`P:userscript/kandev-queue-button.user.js:1-27`, `@include` at line 6).
  - The pacer drains queued prompts per tick (doc 187-217):
    - `queued_counts` (`runtime.py:155-180`)
    - `drain` (`runtime.py:1287-1330`)
    - `ws_drain` / `ws_queue_count` (`P:src/kandev_pacer/client.py:292-307`)
    - drainable only at `WAITING_FOR_INPUT` (`config.py:441-445`)
  - **Releases outrank launches** (doc 209, 216-217; `P:docs/design.md:336-340`).
  - `README.md:157-170` is also Kandev-only.
  - There are no T3 mentions in the queue doc or the userscript.
- **In T3 mode, human prompts are not paced at all.**
  - They start immediately and only count against capacity (`P:docs/t3-pacing.md:308-312`).
  - The pacer never continues threads (`t3-pacing.md:323-324`).
  - A separate Kandev server is not counted (`t3-pacing.md:310-312`).
- **Kandev lessons that apply directly to the T3 hook:**
  1. **Server-side auto-dispatch defeats pacing.** Examples are Auto-run (doc 9-11, 64-71) and drains at turn end or step entry (121-143). The hold must be enforced by the server.
  2. **A restart released parked prompts** (incident, doc 155-185). Holds must be durable and never auto-released.
  3. **A prompt queued behind a running turn is released un-paced when that turn ends** (doc 141-143). Paced runs must stay held when they reach the head of the queue.
  4. **Admission needs a precise identity check.** A drain re-checks idle state and queue incarnation (`runtime.py:1293-1308`; doc 344-356).
     - `drained:false` means no turn was spent and nothing is booked (doc 221-223; `runtime.py:1322-1324`).
     - A busy rejection is benign (doc 224-229).
     - So admission should name one specific run and report "not admitted" separately from errors.
  5. **Kandev has a manual escape** ("Send now", doc 113-119, 236-238). T3 needs an equivalent way out of the waiting state.

## 4. The smallest T3-side contract (pacer's view)

**What the pacer needs each tick:**
- **Discover** waiting runs: `threadId`, `runId`, the run's `modelSelection` (`instanceId` plus `model`), `requestedAt`, and the thread's idle state.
- **Admit** one run by `{commandId, threadId, runId}`.
- **Probe** whether the server supports this.

V2 runs already carry `id`, `modelSelection`, `requestedAt`, `queuePosition` and `queueHeld` (`T:packages/contracts/src/orchestrationV2.ts:543-559`).

**(a) The pacer pushes admission. Recommended.**
- **Discovery.** Add one optional field to `OrchestrationV2ThreadShell`, for example `pacedRuns?: [{runId, requestedAt, modelSelection}]`.
  - The shell already has many optional fields (`orchestrationV2.ts:1927-2032`), so official clients that don't know the field keep decoding it.
  - The pacer already reads that snapshot every tick (`t3.py:544`).
- **Why not per-thread projections.** Using `orchestration.getThreadProjection` (`orchestrationV2.ts:3246`) would mean N full projections per tick, each with messages and turn items (`orchestrationV2.ts:1880-1900`). The shell gives no hint which threads to fetch, because `status` skips held runs (`latestUnheldRun`, `ProjectionStore.ts:1411-1415`).
- **Admission.** Add one client command through the existing `orchestration.dispatchCommand` (`T:apps/server/src/ws.ts:1821-1866`), for example `{type:"paced-run.admit", commandId, threadId, runId}`.
  - The Orchestrator decides without I/O: reject unless the run is still waiting; start it if the thread is idle; otherwise give a benign rejection.
- **No double launches on retry.** Command receipts replay or reject a repeated `commandId` (`T:apps/server/src/orchestration-v2/Orchestrator.ts:10898-10930`). That matches the pacer's "persist the commandId before sending" pattern.
- **Cancellation** goes through the existing `queued-run.cancel` (`orchestrationV2.ts:3043`). A later admit then rejects.
- **Existing commands don't fit:**
  - `queue.resume` clears `queueHeld` on every queued run in the thread (`Orchestrator.ts:10812-10823`). That breaks per-turn admission.
  - `prepared-run.release` (`orchestrationV2.ts:2970`; `Orchestrator.ts:8074-8092`) is for workspace preparation and uses `preparing`, which counts as activity.
- **Fit with the pacer.**
  - It mirrors the Kandev drain path almost exactly.
  - It reuses `Server.call` and `admitted()` with the run's `modelSelection`.
  - It adds one branch in `tick()` before pending launches, oldest `requestedAt` first. Optionally it can order by cache deadline (`P:docs/tasks/9-prompt-cache-deadline-order.md`).
  - If the pacer is down, nobody admits, so it fails closed.
- **Scope guard.** The pacer's issued session has administrative scopes by default (`T:apps/server/src/cli/auth.ts:164-166`). Optionally, guard the new command with `AuthOrchestrationOperateScope`, as `stopWorkflow` is (`T:packages/contracts/src/clientRpcPermissions.ts:52`).

**(b) T3 calls out to the pacer over HTTP. Poorer fit.**
- **The pacer has no inbound server.** There is no HTTP server code in `src`; its only HTTP dependency is `websocket-client` (`pyproject.toml:13`). All its I/O is outbound: T3 calls, quota fetches, and the heartbeat POST (`README.md:172-190`).
- **This would add a long-lived server to the pacer, plus coordination with the tick.** Two concurrent callouts could each see the same free slot and over-admit.
- **T3 would need new machinery:**
  - A re-ask timer. Quota frees up as time passes (`quota.py:54-71`), not on an event, so fail-closed means polling from T3.
  - An outbound HTTP client with its own URL and secret settings.
  - Timeouts and I/O in the effect worker.
  - Re-checking holds on restart anyway.
- **It cuts across "remote ready".** T3 may sit behind a tunnel or on another host.
- **The decision is global anyway.** It needs server-wide running counts and ordering against Hai launches (`t3.py:720-760`), which the pacer computes per tick from its snapshot.

**Pacer-side follow-ups:**
- Paced runs on unmapped providers (Cursor, Grok, OpenCode, custom instances) can never be admitted. The pacer should log them without withholding the whole tick. Unlike `t3.py:551-564`, a waiting run is not active.
- T3 needs a user-visible way out for those runs, such as cancel or a manual admit.
- Add an optional capability flag, in the style of `ExecutionEnvironmentCapabilities` (`T:packages/contracts/src/environment.ts:98-130`), so the pacer can detect a patched server. The pacer reads no capabilities today.

## 5. Targeted T3 version, and what breaks on upstream main

**The pacer targets T3 0.0.44, the V1 orchestrator.**
- `t3_client.py:1` says "T3 0.0.44 RPC transport".
- Also stated in `P:docs/t3-pacing.md:174-177` and `P:docs/tasks/18-hai-tasks-t3-code.md:57-63`, `101-102`.
- The source skill is pinned to 0.0.44 as well ([agent@gogo:~/prg/skills-akaihola/t3-start-thread/SKILL.md](http://gogo/prg/skills-akaihola/t3-start-thread/SKILL.md?f) lines 82-86; `start_thread.py:235` uses `thread.turn.start`).

**Version numbers don't tell V1 and V2 apart.**
- The installed CLI is `agent@gogo:/nix/store/a3w9…-t3code-0.0.45`. Its server bundle `bin.mjs` contains `thread.turn.start` (24 matches), `requireWorktree` and `hasPendingUserInput`, and has no `orchestration.launchThread`. **So the installed build still speaks V1.**
- The V2 orchestrator landed in `de34391427` "feat(orchestrator): introduce new orchestrator (#2829)" on 2026-10-02.
  - It is not an ancestor of tag `v0.0.45`.
  - It first appears in `v0.0.46-nightly.20261003.2610`.
- Upstream main still reports `"version": "0.0.45"` (`T:apps/server/package.json:3`).
- So the pacer needs a capability or shape check rather than a version check.

| Pacer assumption | Pacer location | Upstream main | Effect |
|---|---|---|---|
| Effect-RPC JSON frames (Request/Chunk/Exit/Ping/Pong); string request id | `t3_client.py:156-189` | `RpcSerialization.layerJson` (`T:apps/server/src/ws.ts:3211`); envelope tags unchanged; string ids accepted (`T:.repos/effect-smol/packages/effect/src/rpc/RpcMessage.ts:47-56`, `71-81`, `258-262`) | Compatible |
| `/api/auth/websocket-ticket` → `ticket`; `?wsTicket=` | `t3_client.py:113-120`, `154` | `T:packages/contracts/src/environmentHttp.ts:479-483`; `T:packages/contracts/src/auth.ts:319-322`; `T:apps/server/src/auth/EnvironmentAuth.ts:598` | Compatible |
| `t3 auth session issue --ttl --label --token-only --base-dir` | `t3_client.py:77-96` | `T:apps/server/src/cli/auth.ts:54-55`, `164-195`; `T:apps/server/src/cli/config.ts:209-212` | Compatible |
| `orchestration.subscribeShell {}` → first `kind:"snapshot"` | `t3_client.py:178-181`, `195-197` | Same method name (`orchestrationV2.ts:3252`); optional inputs (`3324-3336`); snapshot is the first item (`ws.ts:1100-1110`) | Compatible at the transport level |
| `threads[].session`, `latestTurn`, `hasPendingApprovals`, `hasPendingUserInput`, `backgroundLiveness` | `t3.py:139-192`, `591-607` | Removed. V2 shell has `status`, `activeRunId`, `activityRunStatus`, `pendingRuntimeRequest`, `pendingBackgroundTasks`, top-level `lastError`, `providerInstanceId` (`orchestrationV2.ts:1927-2032`). V1 shape for comparison: `V44:packages/contracts/src/orchestration.ts:900-928` | **Breaks.** `activity()` raises "Incomplete T3 snapshot; withholding work" (`t3.py:178-179`) on every tick, so nothing ever launches (fails closed) |
| Archived threads disappear from `threads` | `t3.py:545`; `t3-pacing.md:414-415` | Moved to a separate `archivedThreads` array (`orchestrationV2.ts:2036-2041`) | Same observable behaviour |
| `projects[].id`, `workspaceRoot` | `t3.py:652-654` | `T:packages/contracts/src/orchestrationProject.ts:9-12` | Compatible |
| `modelSelection.instanceId`; options as an object | `t3.py:41-43`; `config.py:23-29` | `instanceId` kept (`T:packages/contracts/src/modelSelection.ts:16-20`); options are canonically an array, legacy object still decodes (`T:packages/contracts/src/model.ts:90-113`); driver kinds still `codex` / `claudeAgent` (`model.ts:146-147`) | Compatible |
| `dispatchCommand` with `thread.turn.start` + `bootstrap` + `requireWorktree` | `t3.py:318-347`, `826` | No `thread.*turn*` command in the V2 union (`orchestrationV2.ts:2703-3200`). Replacement is `orchestration.launchThread` with `workspaceStrategy{type:"worktree", baseRef, branch, startFromOrigin}` + `initialMessage` (`orchestrationV2.ts:520-536`, `3287-3313`), as translated by `T:packages/client-runtime/src/operations/commands.ts:646-690`. Follow-ups use `message.dispatch` with `dispatchMode` (`orchestrationV2.ts:2930-2966`). `requireWorktree` survives only as the `requiredWorktreeBootstrap` capability (`environment.ts:118-119`) | **Breaks.** Payload decode fails → `T3Rejected` → the rejection and hold path (`t3.py:827-858`); this is only reached if the tick isn't already withheld |
| A failed bootstrap deletes the thread but keeps its worktree | `t3.py:831-833`; `t3-pacing.md:417-424` | V2 has `prepared-run.fail` / `prepared-run.retry` (`orchestrationV2.ts:2990`, `2998`) | Probably different; not verified |

**Bottom line for Q5.** Pointing the pacer at upstream main breaks T3 mode before any pacing hook comes into play: it withholds every tick and never launches. Using a paced hook on main requires porting `t3.py` to V2:
- Compute activity from `activityRunStatus`, `pendingRuntimeRequest` and `pendingBackgroundTasks`.
- Launch with `orchestration.launchThread`, which returns `{threadId, projection, resumed}` instead of `{sequence}` (`orchestrationV2.ts:3309-3318`).
- Read top-level `status` and `lastError`.
- Then add the discovery field and admit command from section 4.

[hook]: t3-pacing-hook.md
