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
| 5. Start in place | 0 | Maybe | Usually | Clean model; profile changes can bypass it |
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

## 5. Start in place

Disable auto-start on all work steps. Kandev still routes a completed task to
the correct next step, where it waits. The pacer starts the next turn without
moving the task.

- **Good:** Kandev owns routing and the pacer owns timing. Ordinary branches
  and loops need no special handling.
- **Bad:** The pacer needs a reliable "start this task's current step" operation.
  A generic REST endpoint was not found; using Kandev's WebSocket protocol adds
  session-state and reconnect complexity. Kandev can also auto-launch a step
  after an agent-profile change, bypassing this gate.

## 6. Use an existing session-launch surface

Kandev has several ways to launch a session. The important one is the public
WebSocket action `session.launch` with `intent: workflow_step`.

### Probe result

The live probe and source trace found three different behaviors:

- `spawn_session` sends the caller's text, adds peer-agent framing, and creates
  another session on later turns. It is the wrong operation.
- Quick chat creates a separate temporary task. It is also the wrong operation.
- `workflow_step` is the operation used by Kandev's own auto-start callback. It
  continues the supplied session, builds the configured step prompt, applies
  plan mode, and leaves `on_turn_complete` routing unchanged.

The useful request is:

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
- A session awaiting review approval (`ReviewStatus = pending`) rejects the call
  outright — a step with an approval gate cannot be released by the pacer until
  the reviewer acts.
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
- Use **Design 6** only for controlled workflows with one stable agent profile
  and no competing task moves. Its WebSocket launch primitive is real, but
  Kandev can bypass it after an agent-profile change.
- Prefer **Design 12, the ACP turnstile**, when the pacer must stay agnostic to
  Kandev's protocol and work identically across Claude Code, Codex, and Hermes
  Agent — it gates at the agent boundary rather than the Kandev boundary.
- Do not use history inference for branching workflows.
