# Pacing design options

The pacer prevents an AI subscription from spending its quota too quickly. It
does not stop a running agent. It decides whether the next **turn** (one prompt)
may start.

Kandev moves a task through workflow **steps**, such as Plan, Implement, and
Review. Entering a step can start an agent automatically. When a turn finishes,
the current step's rules choose the next step. Those rules may branch or loop.

The difficult part is pausing between turns without losing the workflow's choice
of next step.

Unless marked otherwise, the claims below are verified against the Kandev
source (`~/repos/ai/kandev`, 2026-07-24).

## Quick comparison

| Design | Extra steps | Kandev change | Branches and loops | Verdict |
|---|---:|---|---|---|
| 1. Single Kanban gate | 1 | No | No | Good for a flat queue |
| 2. Next-step marker | 1 | Usually | Yes | Correct but duplicates state |
| 3. Infer from history | 1 | No | No | History is not even stored |
| 4. Waiting/running pairs | One per stage | No | Yes | Reliable but verbose |
| 5. Start in place | 0 | No | Yes | Sound model; realized by design 13 |
| 6. Existing launch surface | 0 | No | Usually | Confirmed primitive behind 13 |
| 7. Shared launchpad | 1 | No | Yes in theory | Racy and loses step settings |
| 8. Shadow workflow | 0 in the main flow | No | Yes | Duplicates the whole workflow |
| 9. Completion-signal gate | 0–1 | No | Yes | The agent releases itself |
| 10. Agent-specific hook | 0 | No | Yes | Not portable across agents |
| 11. Model API proxy | 0 | No | Yes | Gates API calls, not turns |
| 12. ACP turnstile | 0 | No | Yes | Strong, but misses passthrough |
| 13. Hold in place, launch on release | 0 | No | Yes | Best fit for the criteria |

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

- **Good:** Would need no marker and no extra configuration.
- **Bad:** History says where the task was, not which branch was selected.
  Worse, Kandev does not actually store it: the step-history writer
  (`CreateStepTransition`) has no caller in the current source, so the audit
  table stays empty and history would have to be reconstructed from live
  events. Multiple sessions, pacer-created moves, and read timing make that
  reconstruction fragile. Unsafe even for simple linear workflows.

## 4. Per-stage waiting and running steps

Each logical stage becomes two steps, for example `Implement waiting` and
`Implement running`. Workflow rules route into a waiting step. The pacer moves
the task to its matching running step.

- **Good:** The task's position records the selected branch. It needs no marker
  or guesswork and uses only the move API. Confirmed in source: a pacer-driven
  move into an auto-start step reliably launches the agent, with or without an
  existing session, and has no start race because the pacer initiates the
  move. This is the only fully race-free external gate.
- **Bad:** It nearly doubles the workflow and makes every transition harder to
  configure.

## 5. Start in place

Disable auto-start on all work steps. Kandev still routes a completed task to
the correct next step, where it waits. The pacer starts the next turn without
moving the task.

- **Good:** Kandev owns routing and the pacer owns timing. Ordinary branches
  and loops need no special handling.
- **Bad:** The required "start this task's current step" operation exists only
  on Kandev's WebSocket surface (Design 6's `session.launch`). The REST API
  has no turn-start endpoint at all — confirmed against the full route
  inventory — so the pacer takes on WebSocket session-state and reconnect
  complexity. Kandev can also auto-launch a step after an agent-profile
  change, bypassing this gate.

Design 13 realizes this model with Design 6's primitive.

## 6. Use an existing session-launch surface

Kandev has several ways to launch a session. The important one is the public
WebSocket action `session.launch` with `intent: workflow_step`.

### Probe result

The live probe and source trace found three different behaviors:

- `spawn_session` sends the caller's text, adds peer-agent framing, and
  immediately creates an additional parallel session on the task. It is the
  wrong operation.
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
  Kandev even when `auto_start_agent` is absent. That bypasses the pacer. The
  bypass needs a non-empty step prompt; a profile-switch step without a prompt
  only creates the switched session and waits.
- `workflow_step_id` is not checked against the task's current step, and the
  call unconditionally moves the task there (clearing any review status on the
  way). Passing the current step id, read immediately before the call, makes
  the move a no-op and shrinks the race to a narrow window.
- A session awaiting review approval (`ReviewStatus = pending`) rejects
  `intent: workflow_step`; the other intents carry no such check. A step
  behind an approval gate cannot be released by the pacer until the reviewer
  acts.
- Because a profile-switch step creates a new session on entry, the pacer must
  resolve the task's newest live session id at release time, never from cache.
- The pacer still needs WebSocket reconnect, response matching, and duplicate
  start protection. There is no REST alternative: the route inventory has no
  turn-start endpoint.

**Revised verdict.** The launch primitive is confirmed and requires no Kandev
change. The source trace reaches the public WebSocket handler, and the same
prompt path serves ACP profiles and CLI-passthrough profiles alike (Kandev's
passthrough test routes the prompt to PTY stdin), so the primitive works for
any built-in agent profile in either mode. Stock Kandev has no Hermes Agent
adapter — the agent catalog is compiled into the binary — so Hermes support is
out of scope until an adapter exists.

The profile-switch bypass and the review-approval gate mean Design 6 alone is
still not a complete pacer. Design 13 packages this primitive with the
conventions that close the closable gaps.

## 7. One shared launchpad step

Add one auto-starting **Launchpad** step. To release a task waiting at step S,
move it `S → Launchpad → S`. The turn starts at Launchpad, but finishes while
the task is back at S, so S performs the routing.

- **Good:** Uses one extra step and, in theory, preserves arbitrary routing.
- **Bad:** Kandev processes moves and auto-start asynchronously (confirmed:
  step-entry actions run in a goroutine detached from the move), so a fast turn
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

A step with `auto_advance_requires_signal` runs its completion route only after
an explicit step-complete signal. Without the signal, the turn ends with the
session waiting for input and the task stays on its step; a signal arriving
later still triggers the route. The signal surface is the WebSocket action
`mcp.step_complete`, which any gateway client with the task and session ids can
call — so a pacer-side release primitive does exist.

- **Good:** Routing remains entirely inside Kandev and no waiting pair is
  needed. The held state and the late-release path are both implemented.
- **Bad:** The signal belongs to the agent. The `step_complete_kandev` MCP tool
  is registered unconditionally for every task session, with no per-profile or
  per-step off switch, and on gated steps the system prompt explicitly
  instructs the agent to call it when its work is done. Agent self-release is
  therefore the designed behavior, not a corner case; preventing it needs a
  prompt convention the agent may ignore. The manual "mark complete" fallback
  described in Kandev's ADR is not implemented. An initial task still needs
  one inert queue step or a Design 6 start.

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
Kandev -> paced-acp -> agent ACP bridge (Claude Code, Codex, ...)
                    -> central quota scheduler
```

The proxy passes normal ACP traffic through, but queues each `session/prompt`
request. When quota permits, the scheduler grants one request atomically and the
proxy forwards it unchanged. The matching response releases the concurrency
slot. Only then does Kandev run the step's normal completion route.

- **Good:** Adds no workflow steps and needs no Kandev source change. Kandev
  keeps all step prompts, profiles, branches, and loops. Confirmed in source:
  Kandev puts no timeout on the prompt round trip (it deliberately detaches
  the caller's deadline), so a queued prompt can wait indefinitely without
  failing anything.
- **Bad:**
  - Kandev shows a queued prompt as a running session, and every queued task
    keeps its long-lived agent process alive (confirmed: one process per
    session, reused across turns).
  - CLI-passthrough profiles bypass ACP entirely — Kandev drives them through
    a PTY with raw keystrokes — so the proxy cannot pace them. Paced profiles
    must run in ACP mode.
  - Profiles cannot override the launch command; they can only append CLI
    flags. The shim must shadow `npx` — or the agent's native binary name such
    as `claude`, which Kandev prefers when found on `PATH` — for the whole
    backend, not per profile. Per-profile selection has to move into the
    proxy, for example a marker flag added via the profile's extra CLI flags
    that the shim strips before exec.
  - The proxy must queue at `session/prompt` only. Kandev enforces startup and
    prompt-readiness timeouts (about a minute) before dispatch, so delaying
    process start or `initialize` breaks the session.
  - Cancellation is two distinct paths: interrupting a turn sends ACP
    `session/cancel` and keeps the process, while stopping the task kills the
    agent process under the proxy. Both must be handled, plus crashes,
    fairness, and failing closed if the scheduler is down.
  - Hermes Agent has no adapter in stock Kandev (the catalog is compiled in).
    Hermes ships its own ACP adapter, so a forked or future integration would
    flow through the same proxy, but today the design covers only built-in
    agents.

## 13. Hold in place, launch on release

Combine Designs 5 and 6: hold every task at its routed step, then start it in
place with the confirmed launch primitive.

Configuration, in workflow settings only: remove `auto_start_agent` from every
paced step, and give no paced step both an agent-profile override and a step
prompt (that combination auto-launches and would bypass the gate).

Kandev still runs each step's completion route when a turn ends, so the task
lands on the correct next step — through any branch, loop, or agent-driven
`move_task` hand-off — and waits there with its session marked
waiting-for-input. When quota allows, the pacer starts the turn in place:

- Later turns: `session.launch` with `intent: workflow_step`, the task's
  current step id read immediately before the call (making the unvalidated
  move a no-op), and the task's newest live session id.
- First turn of a task: the same action with `intent: start`.

The pacer follows progress through the public WebSocket events
`session.state_changed`, `task.updated` (which carries the new step id), and
`session.turn_completed`, or by polling the task list.

- **Good:** Zero extra steps, no Kandev modification, and Kandev keeps all
  routing, prompts, templates, plan mode, and profile selection, so arbitrary
  branching works. The launch primitive uses Kandev's common prompt path, so
  it paces ACP and CLI-passthrough profiles alike — the gap Design 12 cannot
  cover. Between turns the session honestly reads waiting-for-input instead of
  appearing to run.
- **Bad:** It inherits Design 6's residual limits. The profile-switch-plus-
  prompt combination must be kept out of paced workflows by convention — a
  configuration lint, not an enforced rule. A review-gated step stays closed
  until the human approves, and the turn that approval triggers is not paced.
  The pacer needs WebSocket plumbing: reconnect, response matching, and
  duplicate-start protection. And it is coupled to Kandev's internal WebSocket
  actions, which are less stable than ACP.

### What the profile-switch convention costs

An agent profile bundles which agent runs a step and with which model. A step
prompt is the step's own instruction template. Kandev implements multi-agent
pipelines by combining the two on one step: entering the step switches to the
step's profile and launches the new agent with the step's instructions.

Design 13 bans only that combination — but the combination is where most of
the multi-agent value lives:

- **Cross-agent hand-offs with instructions.** "Implement under Claude Code,
  then review under Codex with the prompt 'adversarially review the changes'"
  is exactly a profile-switch step with a prompt.
- **Per-step model tiering.** "Plan on a strong model, implement on a default
  one, clean up on a cheap one" is done by switching profiles; any of those
  steps that also carries its own template hits the ban.

The two halves remain available, with limits:

- **Same profile, any prompt:** fully paced, nothing lost. This is the common
  single-agent case.
- **Profile switch, empty prompt:** Kandev still creates the switched session
  on step entry, and the pacer can launch it — but with no step template the
  launch prompt falls back to the plain task description, so the new agent is
  never told what its step is for.

Three softening notes:

- Keeping a banned step is a leak, not a breakage. Routing still works; the
  step's turn simply launches immediately, outside the quota gate and the
  pacer's concurrency cap. One unpaced turn per task through a rare review
  step may be an acceptable trade.
- Mitigations exist, each with a cost. The previous step's agent can leave a
  hand-off message, which Kandev merges into the next launch prompt — but
  that depends on agent discipline. Or the pacer can keep the step's template
  in its own configuration and supply it at release time — but that
  duplicates workflow state outside Kandev (Design 2's objection) and assumes
  `session.launch` honors a caller-supplied prompt on the `workflow_step`
  intent, which is unverified.
- The bypass fires only for ACP-mode sessions. CLI-passthrough sessions do
  not auto-launch on a profile switch, so for passthrough-only workflows the
  convention is stricter than technically necessary. Keeping it anyway keeps
  the rule simple.

A tempting variant — leave auto-start on, watch for launches, and immediately
`stop_task` anything over quota, relaunching it later — was rejected: the
launch runs in a detached goroutine after the step change, so the stop is a
race; each false start burns quota; and a stop parks the task in review state
with a cancelled session.

## Recommendation

- Keep **Design 1** for a simple flat backlog.
- Use **Design 4** when only Kandev's REST move API may be used; it is the one
  fully race-free gate.
- Prefer **Design 13** for the stated criteria — no Kandev modification,
  complicated and branching workflows, zero added steps. Its price is two
  workflow conventions (no auto-start, no profile-switch step with a prompt)
  and WebSocket plumbing.
- Use **Design 12, the ACP turnstile**, when the pacer must stay decoupled
  from Kandev's own protocol; it needs a backend-wide `PATH` shim and paces
  only ACP-mode profiles.
- Do not use history inference: Kandev does not store step history.

## Side note: pausing fights the prompt cache

To act on later, once a prototype works. This applies to every design above,
because they all insert waiting time between turns.

Anthropic caches the unchanged beginning of a conversation (the "prefix") for
a few minutes — 5 by default. A request that reuses a cached prefix pays about
0.1× the normal token price for it; a request whose cache has expired pays
about 1.25× to re-read and re-cache the whole prefix. That is roughly a 12.5×
price difference on the conversation history, and a long-running agent session
carries 50k–150k tokens of history.

During a turn the agent calls the model every few seconds, so the cache stays
warm on its own. The pacer only adds delay between turns — exactly where the
cache clock runs. Every pause longer than the TTL means the next turn re-reads
the whole history at full price. Because the pacer's gate measures the same
usage number that cache misses inflate, this cannot make it overspend; it just
means fewer useful turns per quota window.

Practical consequences, in order of importance:

- Quota pauses are unavoidable misses: they last far longer than the TTL, and
  keeping the cache warm would cost about as much as the miss. Accept them.
- Scheduling misses are avoidable: round-robining several tasks makes every
  task's between-turn gap longer than the TTL. Prefer depth-first scheduling —
  while the gate is open, run one task's steps back-to-back, then the next.
  Tasks do not compete for cache space; only elapsed time expires a prefix.
- React to turn-completed events instead of a slow polling tick, so an open
  gate releases the next turn within seconds, not minutes.
- A 1-hour cache TTL exists (2× write price instead of 1.25×) and would
  survive most quota pauses — but the TTL is chosen by the agent harness, not
  by Kandev or the pacer. Verify it is configurable before counting on it.
- To measure the real cost, read `cache_read_input_tokens` and
  `cache_creation_input_tokens` from the agent's session logs for a paced
  task.
