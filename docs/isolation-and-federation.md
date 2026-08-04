# Kandev Pacer: Isolation, Federation, and Multi-Instance Coordination

## Status

Produced in a ChatGPT Live session, 2026-07-27, reconciled the same day against this repo and
the kandev source facts recorded in it, and re-checked on 2026-08-04 against the code as it
stands.

This document summarizes an architectural discussion about extending **kandev-pacer** to
coordinate work across multiple kandev environments while keeping sensitive projects isolated.
Nothing here is implemented; the pacer today drives exactly one kandev instance (`KANDEV_URL`,
`src/kandev_pacer/config.py`).

The architecture is a proposal. The claims about kandev and about the pacer have been corrected
to match what is already established elsewhere in the repo, and the sections that other
documents settle now say so and link to them:

- the pacing rule, the launch primitive, and the confirmed kandev API surface — [`design.md`]
- what a kandev plugin can and cannot do — [`kandev-plugin-feasibility.md`]
- why global concurrency is a weaker lever than global pacing — [`concurrency-sizing.md`]
- the enforcement design this sits on top of — [`pacing-designs.md`] §13
- the upstream conversation — [kdlbs/kandev#1669]

Statements that remain unverified are marked as such.

One thing changed under this document after it was written, and it changed the design rather
than the wording. The pacer no longer reads the quota through kandev: kandev v0.83.0 removed
`GET /api/v1/agents/usage`, so `src/kandev_pacer/provider.py` now reads
`https://api.anthropic.com/api/oauth/usage` directly with the local Claude Code credential, and
kandev is depended on for the *board* only. Wherever this document said the coordinator has to
be handed usage numbers by some domain, it no longer does — see § *Pacer placement*.

## Context

Kandev Pacer currently controls the rate at which new Kandev work is started. Its purpose is to make efficient use of a shared Claude subscription without exhausting the available usage too early in the subscription window.

The basic pacing model is:

- When usage is below the desired consumption curve, Pacer schedules more work.
- When usage is ahead of the desired curve, Pacer pauses.
- Interactive use in Claude.ai or local Claude Code consumes from the same subscription allowance.
- Pacer adapts automatically because scheduled work resumes only when actual consumption falls back below the target curve.
- Tasks are ordered by priority so that the most important available work is started first.

Concretely ([`design.md`] § *The pacing rule*, § *Which task goes next*): the gate compares consumed
fraction `c` against elapsed fraction `e` inside each quota window and opens while headroom
`h = α·e − c` is positive, taking the worst window; `PACER_ALPHA` (α, default 1) tightens the
target line to leave quota unspent at the reset. "Priority" is not a number the pacer owns — it is the user's Kandev
sidebar drag order (stored per *user*, not per view or per board) layered over each board's
kanban-pull order. Kandev's own `priority` field is a four-value enum
(`critical|high|medium|low`), not a rank.

This works naturally when there is one Kandev instance and one task queue.

## The new problem

The desired setup spans several computers and several confidentiality domains:

- A low-trust or experimental computer in a closet runs services that may be exposed to friends or used for miscellaneous experiments.
- A main laptop contains customer projects, private projects, financial information, health-related material, and other sensitive data.
- Some projects should remain remotely accessible from a phone or another laptop.
- The integrated Kandev experience is valuable: task workflow, agent reports, follow-up prompts, status transitions, and project details are visible in one interface.
- The same Claude subscription may be used by work running in several environments.

Running a second independent Kandev and a second independent Pacer would create two separate priority queues. Both would schedule work without knowing which task is globally most important.

More importantly, placing unrelated sensitive projects in one Kandev instance may collapse them into one trust domain. If the Kandev process, its agents, plugins, database, logs, or workspace permissions can access all projects, information could leak between projects even when the current task concerns only one of them.

## Threat model

The main concern is not only remote shell access. Sensitive information can also leave a machine through:

- prompts and task descriptions;
- agent transcripts;
- command output and logs;
- generated patches and diffs;
- repository paths and filenames;
- environment variables and credentials;
- Kandev databases;
- plugin APIs;
- shared caches or temporary directories;
- error reports and telemetry;
- links to locally served applications;
- task summaries sent to a central coordinator.

A remote runner with restricted SSH access can still leak data if it reports detailed logs or task content to a less trusted machine.

The closet computer should therefore be treated as a permanently low-trust environment rather than as a machine that can be made safe merely by adding a lightweight sandbox.

## Key architectural principle

> One scheduler may coordinate many workers, but one Kandev security domain should not automatically contain every project.

The preferred conceptual model is:

**one global ordering mechanism, multiple isolated execution and user-interface domains.**

The central component should know only enough to decide which domain may start the next unit of work. It should not need access to source code, prompts, transcripts, diffs, logs, or other sensitive project content.

## Trust domains

Each Kandev instance should be considered a trust boundary.

Projects may share an instance only when it is acceptable for the Kandev process and every sufficiently privileged component inside that instance to access all of them.

Possible domains include:

- public and open-source work;
- experiments and low-sensitivity personal projects;
- customer work;
- finance;
- health-related work;
- other private personal projects.

The appropriate level of isolation depends on the consequences of leakage. Options, from weaker to stronger, include:

1. Separate project directories inside one Kandev instance.
2. Separate OS users.
3. Separate containers with carefully restricted mounts and credentials.
4. Separate virtual machines.
5. Separate physical machines.

Separate directories or Git worktrees are useful for concurrency and repository hygiene, but they should not be assumed to provide confidentiality if the controlling process can read all of them.

For strongly separated domains, use separate Kandev instances under separate OS identities, containers, VMs, or machines.

## Why two independent Pacers are insufficient

Two Pacer instances using the same Claude subscription both read the same account-level number
— `https://api.anthropic.com/api/oauth/usage`, the provider's own windows — and every source of
burn on that account (Routines, UI-created tasks, interactive Claude Code, and Claude.ai to the
extent the plan shares one window) is already inside it ([`concurrency-sizing.md`] conclusion 7).
So the pacing *invariant* is global for free. Two independent Pacers would keep the aggregate
roughly on the steady-burn curve, each pausing when the shared number gets ahead of the line.

That the number comes from the provider rather than from kandev makes this stronger than it
first was: the shared counter is not a per-instance aggregation that two domains might
interpret differently, it is one endpoint neither domain owns.

What independent Pacers cannot provide is a global **order**, and they are measurably blind to
each other in the short run. Usage feedback is quantized to whole percent and arrives in lumps:
over a 5.6-minute probe the 5-hour figure moved exactly once, by 4 points, 204 s after the work
that caused it ([`concurrency-sizing.md`] § *Measured*). Two Pacers therefore cannot see each
other's launches for at least a full tick, which is the same dead-time exposure a single Pacer
has against concurrency — `N × burn × dead_time` — with no cap to bound `N`.

The ordering failure is the more fundamental one. For example:

- Queue A contains a low-priority task.
- Queue B contains a critical task.
- Both Pacers see available capacity.
- Both may start work at nearly the same time.
- The low-priority task consumes capacity that should have gone first to the critical task.

Other potential problems include:

- simultaneous starts after a pause — both gates reopen on the same observed number, and each
  now times its own reopening from that number, so two pacers watching one counter converge on
  the same instant rather than drifting apart;
- oscillation around pacing thresholds — the single-instance gate has no hysteresis knob to
  lean on (the margin knob it once had was removed as unearned), so this is bounded only by the
  tick rate, per gate, against a line neither of them owns;
- starvation or unfairness;
- difficulty reserving capacity for interactive use (`PACER_ALPHA` and `PACER_BACKSTOP` are
  per-process knobs; two processes each reserving 20 % do not reserve 20 % together).

Two things on the original list turned out not to be problems. There is no **duplicate
accounting** — neither Pacer accounts for anything; both read the provider's own counter, and a
turn is counted once no matter who started it. And there is no **inconsistent interpretation of
subscription state** as long as both run the same gate code against the same endpoint; the
inconsistency is temporal (one tick of blindness), not semantic.

The system therefore needs either one authoritative scheduler or a distributed coordination
protocol with equivalent behavior. Note how little the coordinator needs to be told about usage:
**nothing.** The number is account-level and read straight from the provider with a subscription
credential, so a coordinator holding that credential reads it itself, with no kandev instance in
the path and nothing to aggregate. The original sketch had one domain reporting window
percentages outward; that plumbing is unnecessary. What the coordinator still needs from the
domains is the queue — rank ordinals and readiness — and that is the whole of the trust
question.

## Proposed architecture

### 1. Global Pacer and redacted queue

A central Pacer maintains the global ordering and pacing state.

It stores only non-sensitive scheduling metadata, such as:

- opaque task identifier;
- domain or worker identifier;
- a rank ordinal exported by the domain (see below);
- readiness state;
- creation time;
- optional deadline;
- minimal safe status;
- a capability label describing where the task may run.

It should not store:

- source code;
- repository contents;
- full prompts;
- agent transcripts;
- command output;
- diffs;
- secrets;
- customer names, unless explicitly considered safe;
- detailed failure messages.

Example central record:

```json
{
  "task_id": "customer-a:7f23c8",
  "domain": "customer-a",
  "rank": 3,
  "state": "ready",
  "executor_class": "customer-a-local",
  "safe_summary": "Repository maintenance task"
}
```

The opaque identifier maps to a local task inside the isolated Kandev instance.

Two corrections to that record, relative to the original sketch.

**Rank, not priority.** The coordinator must not invent priorities. Each domain already has an
order — sidebar drag order over kanban-pull order — and only the domain can compute it, since it
depends on task positions, sessions and the user's own sidebar settings. The domain therefore
exports its ordinal ("this is my 3rd-most-important ready task") and the coordinator merges the
per-domain lists by policy (weights, aging, reserved lanes). This also answers "how are
priorities edited when the details stay local": in the local Kandev UI, exactly as today.

**No cost estimate.** The original sketch carried an `estimated_units` field. There is nothing to
put in it. Turn cost is not knowable ahead of time and is wildly skewed — median 11 s, mean
124 s, p90 316 s, max 75 min over 349 real turns — and usage feedback is quantized to whole
percent ([`concurrency-sizing.md`] § *Measured*). Scheduling must stay feedback-driven: admit one
turn, watch the shared counter, decide again. An estimate field would invite the coordinator to
pre-allocate quota it cannot measure.

### 2. Domain-local Kandev instances

Each confidentiality domain runs its own Kandev instance and keeps all detailed information locally:

- task prompt;
- repository path;
- agent conversation;
- logs;
- patches;
- workflow state;
- follow-up questions;
- credentials;
- detailed error information.

The central Pacer grants permission to start work but does not retrieve those details.

### 3. Pull-based coordination

A domain-local component polls or claims work from the coordinator rather than accepting arbitrary inbound commands.

This is **mandatory, not merely preferable**, and Kandev's API surface is why. `/api/v1` is
unauthenticated, and `/ws` only checks the `Origin` header — which any non-browser client sets to
whatever it likes (the pacer already sets `Origin = KANDEV_URL`). Anything that can reach a
Kandev instance's port can therefore read every task, prompt and session in that instance and
launch turns in it. There is no "restricted remote-control" grant to hand the coordinator: a
route in is full access. The only structure that preserves the trust boundary is the one where
the sensitive side initiates every connection.

The same fact makes a second rule non-negotiable: **never expose a Kandev port on the overlay
network directly.** An authenticating reverse proxy in front of each instance is the minimum,
because the application behind it has no authentication of its own.

Readiness is computable locally without any new Kandev feature — it is `is_launchable`
(`src/kandev_pacer/selection.py`): an idle or absent session on a pace-eligible step, minus the
cards kandev is holding behind a step's WIP limit, plus tasks with a drainable queued prompt at
any step. A domain adapter is therefore roughly the pacer split at the gate: candidate
enumeration, launch and drain stay local; only the open/closed decision moves to the
coordinator. The split is already most of the way there in the code — `selection.py` and
`quota.py` are pure and import nothing but `config`, so "which task is next" and "may we spend a
turn" are separable without untangling anything.

A typical flow:

1. The local instance reports that an opaque task is ready.
2. The central Pacer includes it in the global priority queue.
3. When pacing permits, the Pacer issues a short-lived lease for that task.
4. The local instance verifies that the lease matches its domain and task.
5. The local instance starts the detailed Kandev task.
6. It periodically reports only safe state, such as `running`.
7. On completion it reports `completed`, `failed`, or `needs_review`.
8. Detailed output remains available only in the local Kandev interface.

Step 5 is "spend one turn", not necessarily "launch": for a task with a queued user prompt the
local action is `message.queue.drain`, and drain must be tried before launch, or the launch
spends the turn on the step prompt and lets the queued prompt out un-paced afterwards
([`queueing-user-prompts.md`]). The coordinator does not need to know
which of the two happened — both spend exactly one turn, which is the only unit it schedules in.

One local rule the coordinator must not override: an accepted launch that kandev has not yet
made visible still holds its slot. The pacer books the slot at request time and lets the booking
expire only if the launch never spends a turn, precisely because kandev answers a launch minutes
after accepting it. A coordinator that recounted running sessions itself would see a free slot
in that gap and hand out a second lease.

This removes the need for inbound SSH or a general remote shell.

### 4. Leases instead of permanent assignment

A lease avoids tasks becoming permanently stuck if a worker disappears.

A lease should include:

- task identifier;
- permitted domain;
- issue time;
- expiry time;
- unique lease token;
- optional concurrency slot;
- coordinator signature or authenticated channel.

The worker renews the lease while running. If it stops renewing, the coordinator eventually marks the task as uncertain or available for recovery.

**A lease authorizes a start, not a run.** Preemption is out of scope for the pacer by design —
Design 13 gates what may *begin* and never kills a running turn — so lease expiry cannot stop
anything. Turns run up to 75 minutes in the observed history, far longer than a sane lease TTL,
which answers "what happens when a lease expires during a long operation": nothing, at the
worker. Expiry is only the coordinator's licence to stop *waiting* for a status report. If it
also released the slot for reuse it would double-spend quota against a turn that is still
burning, so expiry should reduce the assumed free capacity, not restore it.

Care is required to avoid starting the same non-idempotent task twice after a network partition.
The authoritative anti-double-start record is local, not central: the pacer's `booked` ledger
records that a turn was spent on a task, a separate `failed` map records refusals with backoff
([`design.md`] § *A launch kandev refuses is one task's problem*), and a third, `stalled`, gives
up on a card whose turns have stopped changing anything. A lost lease reply must resolve to "the
worker already booked it" rather than to a second launch, so the central lease is advisory over a
local ledger that wins every disagreement.

Those three maps are also the reason a lease cannot carry the whole decision. Each is keyed by
`(task, step, session)` and each encodes a local judgement — spent, refused, futile — that the
coordinator has no way to form, since forming it means reading sessions and worktrees inside the
domain.

### 5. Minimal status federation

The central interface may display only coarse states:

- queued;
- pacing wait;
- leased;
- running;
- completed;
- failed;
- needs local review;
- given up on — the pacer's own `BLOCKED` flag after `PACER_BLOCK_AFTER` refused launches, or a
  card whose turns stopped changing the worktree;
- unavailable.

A task may include a link to the domain-local Kandev UI. The browser follows that link directly over a private network such as Tailscale.

The central server does not proxy or store the sensitive page.

The worker has to *derive* those states rather than read them: kandev records nothing when a turn
ends, and the launch call returns before the session even reaches `STARTING`. "Running" is a
session state the worker polls; "finished" has no event at all, which is why the pacer keeps its
own JSONL history and why nothing kandev serves distinguishes a completed task from one
mid-flight — both sit on their work step with an idle session. The single-instance answer is to
watch the *worktree* instead: work moves `HEAD` or dirties the tree, and a card that has stopped
doing either is stalled. That answer does not federate. It needs filesystem access inside the
domain, which is exactly what the coordinator must not have — so "done" stays a local judgement
the worker reports, never one the coordinator infers.

`failed` is the state that needs the most care: a launch kandev refuses spends no quota and must
be reported as *not started* rather than as a failed run, or the coordinator will believe capacity
was consumed that never was.

## Preserving the integrated Kandev experience

A major usability requirement is to avoid replacing Kandev with a crude local runner and a separate log viewer.

Several approaches are possible.

### Option A: Multiple full Kandev interfaces plus a central queue

The central Pacer provides only global prioritization and a dashboard. Selecting a task opens the correct Kandev instance.

Advantages:

- simplest security model;
- preserves the complete Kandev UI inside each domain;
- little custom UI work;
- no need to transfer sensitive details.

Disadvantages:

- navigation crosses between several Kandev URLs;
- no single page contains every detailed task view.

This is likely the most practical first implementation.

### Option B: Federated shell with direct browser panels

A central dashboard embeds or links to a detail panel served directly by the domain-local Kandev instance.

The browser communicates with both systems:

- scheduling metadata comes from the central Pacer;
- sensitive detail comes directly from the local domain.

This can feel like one interface without sending sensitive content through the coordinator.

Security considerations include:

- browser same-origin restrictions;
- authentication in each domain;
- iframe policies;
- cross-site request forgery;
- accidental copying of content into central analytics or error logs;
- protection against a compromised low-trust central UI reading embedded content.

Because a malicious parent page may interact with embedded content in unexpected ways, strong origin isolation and careful browser security are essential.

### Option C: Kandev plugin for federated navigation

A plugin can add:

- central queue status;
- links to tasks in other instances;
- domain indicators;
- pacing state;
- safe notifications.

It cannot implement the coordination mechanism. This is settled, not open:
[`kandev-plugin-feasibility.md`] assessed the plugin contract
against the SDK source and found no session-launch RPC (the deferred write RPCs named in the spec
are `CreateTask`, `UpdateTask`, `CreateComment`), no usage or quota reader, no `workflow_step_id`
on the Host `Task`, and no step routing on the Host `WorkflowStep`. A plugin therefore cannot
start a turn, cannot see the quota it would be pacing against, and cannot tell which step a task
is parked on — the three facts the worker side of a lease needs.

*Re-verified against kandev v0.84.1 (2026-08-04):* the conclusion holds, one of its premises
does not. The third write RPC is `SendMessage`, not `CreateComment`, and it does start or
resume a session — so "cannot start a turn" is too strong. What it cannot do is start *this*
turn: it delivers the caller's own text and carries no `workflow_step_id`, so a plugin still
cannot launch a work step's configured prompt at a chosen step. The other two gaps are
unchanged, and the worker side of a lease still needs an external adapter.

The quota gap has since been closed *outside* the plugin contract rather than inside it: the
pacer reads Anthropic directly, from a credential file on the host. A plugin is a JavaScript
bundle plus a gRPC connection, with no route to that file, so the Host API's lack of a usage
reader still stands as a reason the plugin cannot be the thing that decides.

What it *is* good for here is the operator surface, and one non-obvious piece of the mechanism:
plugin **Host state** is a per-plugin key/value store in Kandev's own database, scoped by
`(scope, scopeID, key)` — a natural home for lease tokens and the local booking ledger, surviving
restarts. The `app-status-bar-left` slot can show global pacing state and the domain the instance
belongs to on every page.

So the division is: the plugin is a UI and lifecycle layer inside each domain; an external
adapter process alongside it does the claiming, launching and draining over `/api/v1` and `/ws`;
the coordinator stays outside every domain. The upstream asks that would collapse those two local
components into one are listed in the feasibility document's recommendation.

## Pacer placement

The Pacer may run:

- on a dedicated small trusted machine;
- in one selected Kandev domain;
- as a minimal central service on a private network;
- locally on the main laptop, if remote availability is not required.

The coordinator becomes sensitive even when it stores only redacted metadata, because priorities and domain names may themselves reveal information.

It should therefore have:

- no repository mounts;
- no general shell endpoint;
- minimal dependencies;
- authenticated clients;
- encrypted transport;
- an append-only audit trail for scheduling decisions;
- strict log redaction;
- backups that contain no project content;
- a clear list of allowed domains and workers;
- no inbound route into any domain — workers connect out (§3). There is no exception to make for
  usage: the coordinator reads the provider endpoint itself and needs nothing from any kandev
  instance to form the gate decision. Its only inputs from the domains are the ones the workers
  push: rank ordinals, readiness, coarse status.

That last point moves the credential rather than removing it. The coordinator now holds a
subscription token that can spend quota, which makes it worth compromising for something other
than metadata. It is a read-only use of that token, and the same token already sits on every
machine running Claude Code, so this is not a new secret in the deployment — but it is a new
place to keep one, and the coordinator was otherwise the component with nothing worth stealing.

## Subscription-aware coordination

The shared pacing algorithm should be authoritative across all domains.

It may consider:

- current observed Claude subscription usage;
- target consumption curve;
- reserved headroom for interactive Claude.ai use;
- reserved headroom for local Claude Code sessions;
- maximum concurrent tasks;
- domain-specific concurrency limits;
- cooldown periods after bursts;
- manual pause;
- quiet hours or device availability.

Two of these are less important than they look. **Reserved headroom for interactive use already
exists** as `PACER_ALPHA` (tighten the target line) and `PACER_BACKSTOP` (hard ceiling); it needs
centralizing, not inventing. And **global concurrency is second-order**: [`concurrency-sizing.md`]
found the binding constraint to be queue depth rather than the cap, and that a shorter tick buys
the same throughput as a second slot without multiplying dead-time overshoot. (Its cache-locality
argument against breadth-first has since weakened — Claude Code requests the 1-hour prompt-cache
TTL on a subscription, so a pause under an hour costs nothing; see
[`claude-code-hook-feasibility.md`]. The reason it still lands on a cap of 2 is a different one
entirely: at cap 1 a human working in a paced workflow fills the only slot and the backlog gets
nothing.) Federation makes the cap slightly more relevant — `N` domains each capped at 1 is a
global cap of `N` — which argues for the coordinator owning a global cap as a *ceiling*, not as
something to raise. Kandev also has per-workflow WIP limits of its own, which bound parallelism
inside an instance independently of any of this, and which the pacer already defers to: a card
kandev is holding behind a full step is not launchable, so a coordinator's lease for it would be
refused locally rather than obeyed.

A simple scheduling decision could be:

```text
eligible tasks
  = ready tasks
  ∩ tasks allowed on an online worker
  ∩ tasks whose domain has capacity
  ∩ tasks whose required capability is available

next task
  = highest-ranked eligible task under the merge policy

start only if
  worst-window headroom h = α·e − c is positive
  and global_concurrency < limit
```

The gate condition is the existing single-instance one, unchanged and evaluated once globally: it
is a comparison against elapsed *window fraction*, not a running total the coordinator maintains.
The merge policy should be deterministic enough that all participants can understand why a task
did or did not start.

## Possible queue models

### Strict global priority

Always start the highest-priority eligible task.

This is easy to reason about but can starve lower-priority domains.

### Weighted domains

Assign each domain a weight, then choose high-priority tasks while preserving a long-term share.

Useful when customer work must dominate but personal maintenance should still occasionally progress.

### Priority with aging

Increase effective priority while a task waits.

This prevents indefinite starvation.

### Reserved lanes

Reserve some capacity for specific domains or interactive use.

For example:

- one global automated slot;
- one customer-only slot;
- 20% usage headroom for manual work.

Pacer can combine these methods.

## Information-flow rules

Recommended hard rules:

1. The coordinator never requests a task prompt.
2. Workers never upload full logs to the coordinator.
3. Safe summaries are written explicitly, not generated by copying arbitrary agent output.
4. Domain-local URLs use opaque identifiers.
5. Secrets are provisioned independently in each domain.
6. A low-trust domain cannot enumerate other domains' task details.
7. A plugin running in one domain is not automatically trusted by another.
8. Central crash reports and analytics contain identifiers only after redaction.
9. Cross-domain task dependencies expose only completion state and explicitly approved artifacts.
10. Moving a task to another domain is an explicit export operation, not an automatic reschedule.

## Cross-domain artifacts

Some workflows may legitimately need to pass an artifact between domains.

That should be modeled as an explicit, reviewable transfer:

1. The source domain creates an export package.
2. A human or policy checks its contents.
3. The package is encrypted for the destination.
4. The transfer is recorded.
5. The destination imports it into a controlled location.
6. The original task does not grant general access to the source repository.

Examples of safer transferable artifacts:

- a deliberately redacted bug description;
- a generated binary;
- a public patch;
- a test result with approved fields;
- a sanitized interface specification.

## Device strategy

The experimental closet laptop should remain the low-trust environment for:

- public repositories;
- disposable experiments;
- demos;
- chats shared with friends;
- Hermes experiments;
- services where leakage is acceptable.

Sensitive domains should run elsewhere.

A small dedicated machine can be useful, but one machine does not automatically create isolation between finance, health, customer, and private work. Multiple VMs, containers with strong boundaries, or separate OS users may still be needed.

A practical setup could be:

```text
Global Pacer
└── redacted scheduling database

Low-trust machine
└── Kandev: public and experimental projects

Trusted machine or VM host
├── Kandev: customer A
├── Kandev: finance
├── Kandev: health
└── Kandev: private projects
```

Each trusted Kandev instance has its own credentials, storage, network policy, and URL.

## Remote access

For access from a phone or another laptop:

- expose each Kandev instance only through a private overlay network, and only behind an
  authenticating reverse proxy — `/api/v1` has no authentication at all and `/ws` checks only an
  `Origin` header that a non-browser client sets freely, so reaching the port *is* the privilege;
- use separate hostnames per domain;
- require strong authentication;
- avoid publishing the instances directly to the internet;
- do not reuse session cookies across domains;
- consider device approval and short session lifetimes for highly sensitive domains;
- ensure backups and log collectors follow the same isolation rules.

Example hostnames:

```text
pacer.internal
public-kandev.internal
customer-a-kandev.internal
finance-kandev.internal
health-kandev.internal
```

## Kandev feature questions

### Already answered

- **Is there an API suitable for claiming tasks with leases?** No. There is no scheduling or
  claim API of any kind. The surfaces the pacer uses are `/api/v1` reads, `session.launch`,
  `message.queue.*` and `task.state` over `/ws` ([`design.md`] § *Confirmed against kandev source*).
- **Can plugins read all projects and tasks?** Yes — `api_read` covers `tasks`, `sessions`,
  `messages`, `workspaces`, `workflows`, `agent_profiles`, `repositories`, gated only by a
  capability the plugin declares about itself. A plugin is therefore inside the instance's trust
  domain, not a lesser one.
- **Can plugins create, reorder, start, pause, or transition tasks?** ~~No. `api_write` parses but
  enforces nothing, and the deferred write RPCs are `CreateTask`, `UpdateTask`, `CreateComment` —
  no session launch, no step transition.~~ **Answer moved at v0.84.1:** partly yes. The writes
  landed and are gated — undeclared `api_write:<resource>` is `PermissionDenied` — and they are
  `CreateTask`, `UpdateTask`, `SendMessage`. So create yes; transition yes, via `UpdateTask`'s
  `workflow_step_id`; start a turn yes, but only by sending the plugin's own text, not a work
  step's prompt. Reorder and pause remain no.
- **Can plugins add full UI pages or only panels?** Slots, e.g. `app-status-bar-left` and
  `plugin-settings`, from a JavaScript bundle Kandev loads into its own web app.
- **Are there webhooks or event subscriptions?** Yes, inbound to plugins: Kandev pushes bus
  events (`turn.completed`, `turn.started`, `task_session.state_changed`, `agent.completed`,
  `task.moved`) and relayed webhooks over the plugin's gRPC connection.
- **Can external services insert safe metadata into the UI?** Only through a plugin's UI bundle;
  there is no data-injection surface for a non-plugin service.
- **What data is stored in the Kandev database?** Everything sensitive: task descriptions —
  which *are* the agent prompt — sessions, messages, queued messages, workflows and steps, plus
  the workspaces and worktrees on disk beside it (`~/.kandev/data/kandev.db`).

### Still to verify

- Can one Kandev instance be restricted to a fixed set of repositories?
- What can agents access outside the active repository?
- Are task workspaces isolated by process, container, worktree, or permissions? (Worktrees are
  used for concurrency; whether they are a permission boundary is unverified — assume not.)
- Can different projects use separate credentials?
- Can executors run remotely without giving the Kandev server general SSH access?
- Are there per-project executor policies?
- Do logs contain prompts, source excerpts, environment variables, or command output?
- Are multiple Kandev instances expected to share a database?
- Is federation, multi-tenancy, or project-level access control planned?

## Potential upstream feature request

A Kandev federation feature could support:

- independent Kandev instances;
- one redacted global queue;
- instance and domain identities;
- task leases;
- global pacing hooks;
- plugin-provided central navigation;
- safe status federation;
- per-instance capabilities;
- no requirement to replicate prompts, logs, or repositories;
- explicit artifact export between domains.

A concise upstream framing might be:

> Support coordination of multiple independently secured Kandev instances through a redacted global scheduling API. Each instance retains its own tasks, prompts, logs, repositories, and credentials. A coordinator sees opaque task metadata, grants short-lived execution leases, and receives coarse status only.

### Where that conversation already stands

[kdlbs/kandev#1669](https://github.com/kdlbs/kandev/issues/1669) — *"Pull-based flow for
perishable agent capacity"* — is the open upstream issue for the single-instance half of this,
and it is the right thread to extend rather than opening a second one. As of 2026-07-27 it is
open, with a maintainer reply (2026-07-24) describing what exists and what is planned:

- **Today:** per-workflow WIP limits in Kanban workflows, to bound parallelism. No quota-aware
  pacing.
- **Planned, in "office mode":** model tiers (Frontier / Balanced / Economy) assigning agents to
  tasks by tier, and provider routing (Codex → Claude Code → OpenCode) as a fallback when limits
  are reached.

Provider routing matters to this design: when running out of quota can be answered by *switching
provider* rather than by *waiting*, the scheduler's decision space grows a dimension, and a
federated coordinator would need per-provider windows rather than one shared curve. The four
plugin-API asks in [`kandev-plugin-feasibility.md`] are the smaller,
concrete prerequisites; federation is the larger ask and should not be raised before them.

## Minimal viable implementation for Kandev Pacer

A staged approach avoids building a complete federation platform immediately.

### Phase 1: External registry

Add a small registry to Pacer:

```text
domain_id
instance_url
worker_status
opaque_task_id
rank
ready
safe_status
```

Tasks are entered manually or through a small local adapter.

Pacer chooses the next task and displays a link to the correct Kandev instance.

### Phase 2: Local adapters

Run an adapter beside each Kandev instance.

The adapter:

- reads local ready tasks;
- publishes redacted metadata;
- receives leases;
- starts the selected local task;
- reports coarse status.

No task details leave the domain.

### Phase 3: Kandev plugin

Use a plugin to:

- show Pacer state inside Kandev (`app-status-bar-left`);
- display the task's global rank and its domain;
- store lease tokens and the booking ledger in Host state;
- navigate between instances;
- supervise the adapter's lifecycle and render its settings form.

Not "claim leases" or "register tasks by writing to Kandev" — the Host API cannot launch a
session or read quota, so the adapter of Phase 2 stays the thing that acts. The plugin is the
operator experience wrapped around it, which is the same verdict the single-instance packaging
question reached.

### Phase 4: Unified browser experience

Add a central dashboard with safe metadata and direct links or carefully isolated detail panels.

Do not centralize sensitive task content merely to improve visual integration.

## Open questions

Answered above, kept here as an index:

- **How does Pacer measure Claude subscription usage?** `GET https://api.anthropic.com/api/oauth/usage`,
  authenticated with `~/.claude/.credentials.json` → per-window `utilization_pct` and `resets_at`,
  account-level, all burn included. It used to come from kandev's `/api/v1/agents/usage`; kandev
  v0.83.0 removed that, so no kandev instance is in the path at all.
- **Can several clients query that state reliably without races?** Reads are fine; the race is
  that the number lags a launch by ~a tick, so concurrent deciders overshoot.
- **Is global concurrency needed, or only global pacing?** Pacing. Concurrency as a ceiling only.
- **Should interactive Claude use have reserved capacity?** It already does — `PACER_ALPHA`,
  `PACER_BACKSTOP` — and centralizing the gate is what makes one reservation mean one reservation.
- **How should task cost be estimated?** It should not be; feedback, not estimates.
- **How are task priorities edited when details stay local?** In the local Kandev UI; domains
  export rank ordinals.
- **What happens when a lease expires during a long operation?** Nothing at the worker — there is
  no preemption. Expiry must not free the slot.
- **Which parts can be implemented through Kandev plugins today?** UI, settings, lifecycle,
  durable state. Not launching, not quota, not step routing.

Genuinely open:

- Which domains require separate physical machines rather than VMs?
- Is the central rank list itself sensitive? (Domain names and relative urgency leak something
  even with opaque ids.)
- Can a task depend on another domain without revealing why?
- What is the recovery behavior after a coordinator or worker crash — in particular, how does a
  restarted coordinator learn which turns are still burning quota, given that Kandev records
  nothing when a turn *ends*, and the local worktree signal that answers this inside one domain
  cannot cross a trust boundary (§5)?
- Would upstream Kandev accept a generic federation API?
- Does any of this pay for itself before the queue is ever deep? [`concurrency-sizing.md`] found 0
  launchable tasks on a real board; a coordinator that arbitrates between domains that are each
  idle 90 % of the time is solving a contention problem that has not happened yet.

## Recommended direction

The most balanced design is:

1. Keep full Kandev interfaces inside each trust domain.
2. Use one Pacer as the authoritative subscription-aware scheduler.
3. Store only redacted task metadata in the central queue.
4. Coordinate through pull-based adapters and short-lived leases.
5. Keep prompts, logs, agent reports, repositories, workflow details, and credentials local.
6. Open the relevant local Kandev UI when detailed review or interaction is required.
7. Treat a unified federated UI as a later enhancement, not as a reason to weaken isolation.
8. Use Kandev plugins as the operator surface only; scheduling stays in an external adapter until
   a *step-addressed* session launch and a quota reader exist in the Host API. (v0.84.1 has a
   launch primitive, `SendMessage`, but it cannot name a workflow step — see
   [`kandev-plugin-feasibility.md`].)

And one sequencing point the original discussion did not make: **the isolation half is worth
doing on its own, before any coordinator exists.** Splitting sensitive projects into separate
Kandev instances needs no new software — it is deployment work — and it is what actually
addresses the threat model. The coordinator only buys global ordering, whose value scales with
how often two domains want to start a turn at the same moment. Run the domains split, with two
independent pacers and a shared account (which, per § *Why two independent Pacers are
insufficient*, already holds the burn rate roughly right), log both, and let the observed
contention decide whether Phase 1 is worth building.

This preserves the main value of kandev-pacer—global priority and efficient subscription use—without turning every sensitive project into part of one shared security domain.

[`design.md`]: design.md
[`concurrency-sizing.md`]: concurrency-sizing.md
[`queueing-user-prompts.md`]: queueing-user-prompts.md
[`kandev-plugin-feasibility.md`]: history/kandev-plugin-feasibility.md
[`pacing-designs.md`]: history/pacing-designs.md
[kdlbs/kandev#1669]: https://github.com/kdlbs/kandev/issues/1669
[`claude-code-hook-feasibility.md`]: history/claude-code-hook-feasibility.md
