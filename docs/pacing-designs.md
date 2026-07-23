# Pacing design options

The pacer prevents an AI subscription from spending its quota too quickly. It
does not stop a running agent. It decides whether the next **turn** (one prompt)
may start.

Kandev moves a task through workflow **steps**, such as Plan, Implement, and
Review. Entering a step can start an agent automatically. When a turn finishes,
the current step's rules choose the next step. Those rules may branch or loop.

The difficult part is pausing between turns without losing the workflow's choice
of next step.

## Quick comparison

| Design | Extra steps | Kandev change | Branches and loops | Verdict |
|---|---:|---|---|---|
| 1. Single Kanban gate | 1 | No | No | Good for a flat queue |
| 2. Next-step marker | 1 | Usually | Yes | Correct but duplicates state |
| 3. Infer from history | 1 | No | No | Only safe for linear flows |
| 4. Waiting/running pairs | One per stage | No | Yes | Reliable but verbose |
| 5. Start in place | 0 | Maybe | Yes | Clean model; needs a start call |
| 6. Existing launch surface | 0 | No | Usually | Primitive confirmed; not universal |
| 7. Shared launchpad | 1 | No | Yes in theory | Racy and loses step settings |
| 8. Shadow workflow | 0 in the main flow | No | Yes | Duplicates the whole workflow |
| 9. Completion-signal gate | 0–1 | No | Yes | The agent may release itself |
| 10. Agent-specific hook | 0 | No | Yes | Not portable across agents |
| 11. Model API proxy | 0 | No | Yes | Gates API calls, not turns |
| 12. ACP turnstile | 0 | No | Yes | Best general solution |

## 1. Single Kanban gate

New tasks wait in an inert **Backlog** step. When quota allows, the pacer moves
one task to an auto-starting **In Progress** step.

- **Good:** Very small and uses only Kandev's move API.
- **Bad:** Only the first boundary is paced. Later workflow steps can start
  freely, so it does not work for a real multi-step pipeline.

## 2. Explicit next-step marker

When a task is put on hold, store its intended destination in a task field or
tag. The pacer reads that value and moves the task there.

- **Good:** The destination is exact, including branches and loops.
- **Bad:** Something must write and maintain the marker. It copies a routing
  decision the workflow has already made.

## 3. Infer the destination from history

The pacer reads the task's step history and computes what should come next.

- **Good:** Uses data Kandev already stores.
- **Bad:** History says where the task was, not which branch was selected.
  Multiple sessions, pacer-created moves, and read timing make it more fragile.
  It is suitable only for simple linear workflows.

## 4. Per-stage waiting and running steps

Each logical stage becomes two steps, for example `Implement waiting` and
`Implement running`. Workflow rules route into a waiting step. The pacer moves
the task to its matching running step.

- **Good:** The task's position records the selected branch. It needs no marker
  or guesswork and uses only the move API.
- **Bad:** It nearly doubles the workflow and makes every transition harder to
  configure.

## 5. Universal gate: start in place

Disable auto-start on all work steps. Kandev still routes a completed task to
the correct next step, where it waits. The pacer starts the next turn without
moving the task.

- **Good:** Kandev owns routing and the pacer owns timing. Workflow complexity
  no longer matters.
- **Bad:** The pacer needs a reliable "start this task's current step" operation.
  A generic REST endpoint was not found; using Kandev's WebSocket protocol adds
  session-state and reconnect complexity.

## 6. Use an existing session-launch surface

Kandev has internal, MCP, WebSocket, and quick-chat paths that can launch
sessions. One of them might provide the start operation needed by Design 5.

- **Good:** Could implement start-in-place without changing Kandev.
- **Bad:** Starting an additional or quick-chat session is not necessarily the
  same as continuing the task's primary session. It must be tested to confirm
  that the step prompt, agent profile, and `on_turn_complete` routing all work.

### Probe result — `spawn_session` and quick-chat ruled out (verified empirically)

This probe tested two of the surfaces — `spawn_session` and REST quick-chat. It
did **not** test the `workflow_step` launch intent, which the re-assessment below
covers and which changes the overall verdict.

I settled this with a live experiment against a running Kandev, then deleted
everything. I created a throwaway workflow with two identical steps —
`GateManual` (auto-start **off**) and `GateAuto` (auto-start **on**) — each
carrying the same marker prompt (`[GATE PHASE MARKER] Reply with GATE-PROMPT-SEEN`)
and each routing `on_turn_complete → Sink`. A parked task on `GateManual` was
released with `spawn_session`; a control task auto-started on `GateAuto`.

**What works — routing is session-agnostic.** The `spawn_session` turn's
completion fired the step's `on_turn_complete`: the task moved `GateManual → Sink`
on its own. So the *start* mechanism and the *routing* mechanism are decoupled —
you do not have to start the turn the "official" way for Kandev to route it.

**What breaks it — the turn is the wrong turn.**

- **Wrong prompt.** The control agent received the **step's** prompt and replied
  `GATE-PROMPT-SEEN`. The spawned agent received only the **caller's** prompt and
  never saw the step prompt at all. To use `spawn_session` the pacer would have to
  fetch each step's template and interpolate `{{task_prompt}}` itself, duplicating
  Kandev's `buildWorkflowPrompt`. (`spawn_session` also wraps its prompt in a
  "you are an additional peer helper session" system note — the wrong framing for
  doing a step's work.)
- **Session fragmentation from turn 2.** On the parked task's **first** turn the
  spawned session became primary (fine). A **second** `spawn_session` on the same
  task created a *second, non-primary* session instead of continuing the first.
  Every multi-step or looping workflow — exactly Design 6's target — takes more
  than one turn per task, so the task's history fragments across sessions.

(This corrects an earlier source-only guess that `spawn_session` is *always* a
non-primary session: it is primary on turn 1, non-primary from turn 2.)

The REST quick-chat path is also unusable: it creates a **new ephemeral task**,
not a turn on an already-parked one.

**Verdict of this probe:** `spawn_session` and quick-chat do not provide a clean
"start this task's current step in place" call. `spawn_session` sends the wrong
prompt and fragments into a peer session; quick-chat spawns a new ephemeral task.
Both are ruled out. But the primitive that matches auto-start *does* exist and
*is* reachable — just over a surface this probe didn't try. See the
re-assessment below.

The useful takeaway stands: because routing is session-agnostic (confirmed live),
Kandev's existing `on_turn_complete` routing does the rest once the right turn is
started the right way.

### Re-assessment — the `workflow_step` launch intent (source-verified)

Kandev's `session.launch` action takes an `intent` field. One intent —
`workflow_step` — is *exactly* the auto-start primitive Design 5/6 needs, and it
is not the peer/quick-chat framing the probe rejected. Source
(`orchestrator/session_launch.go`, `task_operations.go`, `workflow_callbacks.go`):

- **It is literally what auto-start runs.** The `auto_start_agent` on-enter
  callback is `LaunchSession{Intent: workflow_step, SessionID, WorkflowStepID}`
  (`workflow_callbacks.go:181`). Design 5's whole premise is "do what auto-start
  does, but on the pacer's clock." This intent *is* that, verbatim.
- **Right prompt — no duplication.** `launchWorkflowStep → StartSessionForWorkflowStep`
  calls `buildWorkflowPrompt` internally (`task_operations.go:1281`), performing
  the `{{task_prompt}}` interpolation of the step template. This is the exact
  `buildWorkflowPrompt` the probe warned the pacer would have to re-implement —
  the pacer no longer has to. Step plan mode (`OnEnterEnablePlanMode`) is applied
  too.
- **Same session — no fragmentation.** It runs `PromptTask` on the *passed-in*
  session (`task_operations.go:1288`), not a new peer session. It first
  `advanceTaskWorkflowStep` (sets the task's step) and `ensureSessionRunning`
  (resumes a stopped session, or starts the agent on a CREATED/prepared
  workspace). So turn 2, 3, … all continue the one primary session — the failure
  mode that killed `spawn_session` does not occur.
- **Routing still fires.** Nothing here bypasses `on_turn_complete`; the turn's
  completion routes the task as normal (already confirmed session-agnostic).

**Reachability — the one real cost.** This intent is exposed over the
**WebSocket** protocol: action `session.launch` (`pkg/websocket/actions.go`),
handled by `wsLaunchSession`, which forwards the full `LaunchSessionRequest`
(any intent) to `LaunchSession` (`orchestrator/handlers/handlers.go:79`). It is
*not* on a generic REST route, and MCP's `spawn_session` hardcodes `IntentStart`,
so neither the REST nor the MCP surface reaches it. The frontend drives it over
WebSocket routinely — it is a first-class external API, not an "internal" path.
The pacer today is REST-only (`pacer.py`), so adopting this means adding a small
WebSocket client that sends one message:

```json
{ "action": "session.launch",
  "payload": { "task_id": "…", "session_id": "…",
               "intent": "workflow_step", "workflow_step_id": "…" } }
```

**First-turn nuance.** `workflow_step` needs an existing `session_id`. For a
never-run task, use the same public action with `intent: start` and the current
`workflow_step_id`. With no prompt supplied, Kandev uses the task description,
applies the step template and plan mode, chooses the step's agent profile, and
creates the primary session. Later turns use `intent: workflow_step`.

**Important limits.**

- A step that changes the agent profile and has a prompt is auto-launched by
  Kandev even when `auto_start_agent` is absent. That bypasses the pacer.
- `workflow_step_id` is not checked against the task's current step. If the task
  moves after the pacer reads it, this call can move it back to a stale step.
- The pacer still needs WebSocket reconnect, response matching, and duplicate
  start protection.

**Revised verdict.** The launch primitive is confirmed and requires no Kandev
change. The source trace reaches the public WebSocket handler, and Kandev's
`TestResolveIntent` and `TestPromptTask_PassthroughRoutesToPTYStdin` tests pass.
It uses Kandev's common prompt path, so it is equally applicable to Claude Code,
Codex, and Hermes Agent profiles.

However, the profile-switch bypass and stale-step race mean Design 6 is safe
only for controlled workflows with one stable agent profile and no competing
task moves. It does not satisfy the requirement for every arbitrarily complex
workflow. Design 12 remains the general no-Kandev-change solution.

## 7. One shared launchpad step

Add one auto-starting **Launchpad** step. To release a task waiting at step S,
move it `S → Launchpad → S`. The turn starts at Launchpad, but finishes while
the task is back at S, so S performs the routing.

- **Good:** Uses one extra step and, in theory, preserves arbitrary routing.
- **Bad:** Kandev processes moves and auto-start asynchronously, so a fast turn
  can finish before the move back. The Launchpad may also use the wrong step
  prompt, agent profile, or mode. It is not robust enough.

## 8. Shadow workflow

Create an inert copy of the workflow. Held tasks use the copied steps, while
running tasks use matching auto-start steps in the active workflow.

- **Good:** Both copies can represent any branch or loop.
- **Bad:** It duplicates the complete workflow and requires every edit and step
  ID mapping to stay synchronized. This hides step duplication rather than
  removing it.

## 9. Gate the completion signal

Kandev can require an explicit step-complete signal before running a step's
completion route. The pacer could release that signal when quota allows, causing
the task to enter its next auto-starting step.

- **Good:** Routing remains entirely inside Kandev and no waiting pair is needed.
- **Bad:** This feature is designed for the agent to emit the signal itself.
  The agent can therefore bypass the pacer. Preventing or intercepting that call
  needs another convention or proxy. An initial task may still need one inert
  queue step.

## 10. Agent-specific prompt hook

An agent hook can block each submitted prompt until the pacer grants admission.
Claude Code's `UserPromptSubmit` hook is one example.

- **Good:** Zero workflow changes, and the hook runs at the correct turn boundary.
- **Bad:** Hook systems and behavior differ between Claude Code, Codex, and
  Hermes Agent. This does not meet the requirement to support all three equally.

## 11. Model API proxy

Point agents at a local HTTP proxy and delay requests to Anthropic, OpenAI, or
another model provider.

- **Good:** Independent of Kandev workflows.
- **Bad:** One agent turn may make many model requests, including tool-loop and
  subagent requests. Provider APIs and authentication also differ. The proxy
  cannot reliably identify the requested unit of work: one complete turn.

## 12. ACP turnstile

Place a small, external Agent Client Protocol (ACP) proxy between Kandev and the
real agent:

```text
Kandev -> paced-acp -> Claude Code, Codex, or Hermes Agent
                    -> central quota scheduler
```

The proxy passes normal ACP traffic through, but queues each `session/prompt`
request. When quota permits, the scheduler grants one request atomically and the
proxy forwards it unchanged. The matching response releases the concurrency
slot. Only then does Kandev run the step's normal completion route.

- **Good:** Adds no workflow steps and needs no Kandev source change. Kandev
  keeps all step prompts, profiles, branches, and loops. Claude Code, Codex, and
  Hermes Agent all support ACP, so the same proxy works for all three.
- **Bad:** Kandev may display a queued prompt as running, and each queued task
  may keep an agent process prepared. The proxy must correctly handle
  cancellation, crashes, fairness, and fail closed if its scheduler is down.

The wrapper can be installed through agent launch commands or selective `PATH`
shims. Low-priority profiles use the proxy; interactive or high-priority
profiles launch their agent directly.

## Recommendation

- Keep **Design 1** for a simple flat backlog.
- Use **Design 4** when only Kandev's move API may be used.
- **Design 6 via the `workflow_step` launch intent** now realizes Design 5
  (Kandev routes, pacer times, step-accurate, one primary session) with **no
  Kandev source change** — at the cost of the pacer speaking Kandev's WebSocket
  protocol. Prefer it over Design 4/5 when a WebSocket client is acceptable and
  the workflow branches or loops; validate the three live items above first.
- Prefer **Design 12, the ACP turnstile**, when the pacer must stay agnostic to
  Kandev's protocol and work identically across Claude Code, Codex, and Hermes
  Agent — it gates at the agent boundary rather than the Kandev boundary.
- Do not use history inference for branching workflows.
