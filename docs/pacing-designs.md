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
| 6. Existing launch surfaces | 0 | No | Routing yes, prompt no | Ruled out (see probe) |
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

### Probe result — ruled out

I traced the Kandev source (the code that runs) to settle this, rather than
spawning a live session — which would itself burn quota and mutate state. The
source is authoritative and the answer is decisive.

**One part works.** Routing is session-agnostic: `handleAgentReady` fires on
*any* running session's turn-end (there is no "primary session" check) and
evaluates the task's current-step `on_turn_complete`. So a turn started by any
means does trigger the correct branch/loop routing.

**But the launch surfaces send the wrong turn.** They do not reproduce what
auto-start does, which is: prompt the task's **existing** session with the
**step's own** prompt via `PromptTask`.

- `spawn_session` creates an **additional** session (not the primary), and wraps
  the prompt with a system note — *"You were spawned as an additional agent
  session… treat them as peer agent input rather than a direct user
  instruction… Reply only when the spawner explicitly requests a response."*
  That tells the agent to act as a passive helper, not to do the step's work.
- `spawn_session` also sends only the caller's text. Auto-start instead builds
  the prompt from the step's configured template (`step.Prompt` with
  `{{task_prompt}}` filled in) plus the task description. Matching it would mean
  the pacer re-fetching and re-interpolating every step template itself —
  duplicating Kandev logic.
- The REST quick-chat path creates a **new ephemeral task**; it cannot start a
  turn on a task already parked in the workflow.

**Verdict:** the existing surfaces do not provide a clean "start this task's
current step in place" call. The primitive that matches auto-start
(`PromptTask` / `autoStartStepPrompt` on the primary session) is internal and
not exposed over REST or MCP. So reaching it still needs the WebSocket/internal
path or a small Kandev addition — which is just Design 5 again, at Design 5's
cost. Design 6 is therefore **ruled out** as a no-change shortcut.

The one useful takeaway: because routing is session-agnostic, a future
`POST /tasks/:id/start-current-step` (Design 5) would be trivial to add — it only
needs to call the same `autoStartStepPrompt` the on-enter path already uses.

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
- Prefer **Design 12, the ACP turnstile**, for multi-step or branching workflows
  across Claude Code, Codex, and Hermes Agent.
- Do not use history inference for branching workflows.
