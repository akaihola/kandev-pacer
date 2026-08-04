# Can the pacer be a Kandev plugin?

Assessed 2026-07-26 against the Kandev plugin documentation and SDK source
(`docs/public/plugins.md`, `plugins-authoring.md`, `plugins-manifest.md`,
`docs/specs/plugins/spec.md`, `docs/decisions/0043-plugin-host-data-api.md`,
`apps/backend/pkg/pluginsdk/data_types.go`).

**Re-verified 2026-08-04 against kandev v0.84.1.** Two of the four "what is
missing" findings have moved; two stand. The original text is kept and the
moved claims are struck through in place — what a conclusion *was* is part of
why the later ones were reached. See "What v0.84.1 changed" at the end for the
two additions that have no 2026-07-26 counterpart.

A Kandev **plugin** is a native executable that Kandev spawns and supervises as a
subprocess. It talks to Kandev over a private gRPC connection: Kandev pushes bus
events and relayed webhooks in, the plugin calls a fixed set of **Host API**
methods back out. Each Host method is gated by a capability the plugin declares
in its `manifest.yaml`. A plugin may also ship a JavaScript UI bundle that Kandev
loads into its own web app. Go is the only SDK Kandev ships.

## Verdict

**Yes as a packaging and lifecycle wrapper; no as a Host-API-native plugin.**

The plugin contract gives the pacer a supervised home, an operator settings form,
event triggers, and UI surfaces. It gives the pacer none of the four things the
pacing loop actually needs. Those would still be done exactly as `pacer.py` does
them today — unauthenticated `/api/v1` REST plus the `/ws` WebSocket against
localhost.

*Amended at v0.84.1:* the verdict holds, the count does not. Of the four, one is
partly answered (a write RPC can start a turn, but not a *step's* turn) and one
has become moot in kandev's direction and worse in ours (quota left kandev
entirely). Read "none of the four" as "not enough of the four", and the fallback
line as REST and WebSocket for the board only — quota is read from the provider.

## What the contract does give us

- **Supervised process.** Kandev spawns the binary, health-checks it every 30 s,
  and restarts it on crash with backoff. Replaces whatever unit or cron entry
  runs `pacer.py`.
- **Operator config.** `config_schema` in the manifest renders a settings form at
  *Settings > Plugins > kandev-pacer*, so the `PACER_*` environment block becomes
  a UI. Saving restarts the plugin, so it re-reads cleanly. Fields marked secret
  go to Kandev's vault.
- **Event triggers instead of polling.** `capabilities.events` can subscribe to
  `turn.completed`, `turn.started`, `task_session.state_changed`,
  `agent.completed`, and `task.moved`. A turn finishing is precisely the moment
  the gate should re-evaluate, so this is strictly better than `PACER_INTERVAL`
  — though a timer is still needed, because the gate also reopens on the passage
  of time alone (headroom `h = e − c` climbs while paused, with no event to
  announce it).
- **Durable bookkeeping.** Host state is a per-plugin key/value store in Kandev's
  own database, addressed by `(scope, scopeID, key)`. The `_BOOKED` duplicate-start
  ledger fits it directly, scoped `task`, and survives restarts instead of being
  rebuilt from scratch.
- **UI surfaces.** The `app-status-bar-left` slot is a compact, always-visible
  strip on every page — the natural place for live headroom and gate state. The
  `plugin-settings` slot could show the workflow lint warnings the pacer
  currently prints once at startup.

## What is missing

1. ~~**No launch primitive.** `session.launch` — the whole point of the pacer — has
   no Host API equivalent. `capabilities.api_write` is accepted by the manifest
   parser but enforces nothing; the deferred write RPCs named in the spec and
   ADR 0043 are `CreateTask`, `UpdateTask`, and `CreateComment`.~~ Starting a
   session is not on that list.

   **Superseded on both counts (v0.84.1).** The writes landed in 279e15b31
   (#1970, 2026-07-27), and they are gated: `internal/plugins/host_write.go:4`
   — "Writes are gated on `api_write:<resource>` — undeclared → gRPC
   `PermissionDenied`". The third RPC is not `CreateComment` but **`SendMessage`**
   (`docs/plans/plugins/HOST-DATA-API.proto:47`, message at `:217`), gated
   `api_write:messages`, which "resolves session_id (empty → the task's primary
   session), queueing when the session is running and **resuming/starting it
   otherwise**", answering `"queued" | "sent" | "started"`.

   So a plugin *can* start a turn. It still cannot start **this** turn: the RPC
   delivers the caller's own `text` rather than the work step's configured
   prompt, and carries no `workflow_step_id`, so it cannot express
   `intent: workflow_step` at a chosen step. Both halves matter — the flat "no
   launch primitive" verdict is wrong, and the primitive that now exists is the
   wrong shape for how the pacer launches. The ask below narrows accordingly
   rather than disappearing.
2. ~~**No usage data.**~~ **Still true, and the fallback is gone.** The `api_read`
   vocabulary is `tasks`, `sessions`, `messages`, `workspaces`, `workflows`,
   `agent_profiles`, `repositories`; nothing in the proto exposes quota (the only
   `usage` hits are `acp_session_id`, an attribution join key for external tools).
   But the 2026-07-26 text treated the absent Host reader as harmless *because*
   `GET /api/v1/agents/usage` was there to fall back to, and that endpoint was
   removed in a7005e5d2, shipped in v0.83.0. There is now no Host counterpart
   **and** no REST original. Quota comes from the provider directly — see
   `src/kandev_pacer/provider.py` and `docs/design.md`.
3. **`Task` carries no `workflow_step_id`** (`data_types.go:92`). Which step a
   task is parked on is the pacer's central fact, and it is invisible through the
   Host API. *Re-verified at v0.84.1* — `data_types.go:92` and
   `HOST-DATA-API.proto:65` both still stop at `metadata`. Note the asymmetry
   that arrived with the writes: `UpdateTaskRequest` **does** carry
   `workflow_step_id` (`proto:214`), so a plugin may now *move* a task to a step
   it has no way to *read*.
4. **`WorkflowStep` is `{ID, WorkflowID, Name, Position, StageType}`**
   (`data_types.go:367`) — no `events`, no `on_turn_complete`, no
   `is_start_step`. The work-step classifier deliberately reads
   `on_turn_complete` rather than trusting `stage_type`; on Host data only the
   read-only `stage_type` hint remains, which the README already rejects as
   corroborating evidence rather than a source of truth. *Re-verified at
   v0.84.1* — `data_types.go:367` and `HOST-DATA-API.proto:129` are unchanged,
   five fields each.

## The shape that would work

Package the pacer as a plugin whose backend ignores the Host data API for the
pacing loop and calls Kandev's own REST and WebSocket surfaces on localhost.
There is precedent: `kandev-plugin-github` shells out to the `gh` command-line
tool from its handler. The subprocess inherits the backend's full environment
(`internal/plugins/runtime/manager.go:383`), so it can derive its own base URL,
and `/api/v1` is unauthenticated.

Division of labour:

| Need | Source |
|---|---|
| Re-evaluate the gate | bus events (`turn.completed`, …) plus an internal timer |
| Usage windows | ~~`GET /api/v1/agents/usage`~~ → `GET https://api.anthropic.com/api/oauth/usage`, read directly (`provider.py`); the kandev endpoint was removed in v0.83.0 |
| Tasks, steps, sessions | `/api/v1` REST (Host readers lack step id and step routing) |
| Launch a turn | `session.launch` over `/ws` |
| Booking ledger | Host state, scope `task` |
| Knobs | `config_schema` settings form |
| Headroom readout | `app-status-bar-left` UI bundle |

Two costs come with that shape. The capability manifest becomes a fiction: the
one genuinely privileged thing the pacer does is neither declared nor gated. And
it only works when the pacer runs on the same host as an unauthenticated
`/api/v1`, which a plugin's portable packaging otherwise implies it does not
require.

**At v0.84.1 the recommendation shifts.** The row that most justified "ignore the
Host API" was usage, and it is no longer a choice between two kandev surfaces:
quota comes from outside kandev either way. So the pacing loop's quota half stops
being an argument about plugin architecture at all, and what remains against the
Host API is narrower — step identity (3), step routing (4), and a launch that can
name a step (1). The capability-manifest-as-fiction cost also shrinks, because
`api_write:messages` is real and enforced: a plugin that launched via
`SendMessage` would be declaring and being gated on the privileged thing it does.
It just would not be launching the way the pacer launches.

`make_paced.py` does not get cleaner either. `stage_type` still has no API, UI,
or import surface, so adapting a workflow still means writing to
`~/.kandev/data/kandev.db` directly — and a supervised plugin reaching behind the
server into its own database reads worse than a script doing the same thing,
even though it is the identical write-ahead-log write.

## Recommendation

Ship as a plugin only for the operator experience: config UI, status-bar
readout, supervised lifecycle. Accept that it is a wrapper around the same REST
and WebSocket calls we make today, and that the pacing logic gains nothing.

The honest upstream ask, if a native plugin is the goal, is four things:

- a session-launch write RPC gated by `api_write:sessions`,
- a usage or quota reader under `api_read`,
- `workflow_step_id` on the Host `Task` type,
- step routing (`on_turn_complete`) on the Host `WorkflowStep` type.

With those, the pacer becomes a genuinely native plugin and the direct SQLite
write in `make_paced.py` is the only remaining out-of-band access.

**The asks at v0.84.1.** Ask 1 is half-met and should now be phrased as an
amendment rather than a new RPC: `SendMessageRequest` needs an optional
`workflow_step_id`, so a plugin can ask for the step's own prompt at a named step
instead of pushing its own text at whichever session is primary. Ask 2 is
unchanged in substance but stronger in motivation — it used to mean "save us a
REST call", and now means "there is no kandev answer at all". Asks 3 and 4 stand
verbatim.

## Beyond one instance

The same four gaps decide the plugin's role in the multi-instance design sketched in
[`isolation-and-federation.md`]: a plugin there is a
status readout, a settings form, a lifecycle supervisor and a durable store for
lease tokens (Host state, which fits that as well as it fits the pacer's own booking ledger), while an
external adapter beside it does the claiming and launching over `/api/v1` and `/ws`.

One caveat from this document becomes a load-bearing constraint there. The pacer
only works where it can reach an unauthenticated `/api/v1` — noted above as a cost
to the plugin's portability. Across trust domains it reads the other way round:
because reaching the port *is* full access to the instance, the pacing decision can
never be made from outside the domain, and the sensitive side has to initiate every
connection.

[`isolation-and-federation.md`]: ../isolation-and-federation.md

At v0.84.1 that split survives ask 1 moving. `SendMessage` would let the plugin
half do the launching, but only by sending its own text at a task's primary
session — which is not a work-step launch, so the external adapter is still the
thing that launches. What the plugin gains is that its Host-state role is no
longer the *only* thing it can do with a declared capability.

## What v0.84.1 changed

**Plugins are no longer flagged.** 1a3c134f1 (#2086, 2026-07-31) shipped the
plugin system without a feature flag, so the packaging-wrapper verdict above is a
live option rather than a flagged experiment. `plugin-registry/plugins.yaml` now
lists three: `kandev-session-cost`, `kandev-provider-usage` and
`kandev-plugin-kandy`. The middle one is precedent, not just company —
provider-usage is doing exactly the job the pacer now does in `provider.py`,
reading quota from outside kandev, which is the shape we adopted independently.

**Upstream has quota-based deferral of its own.** `UsageService.IsPotentiallyRateLimited`
(`internal/agent/usage/service.go:59`) returns whether any window is at or above a
threshold (90% by default) together with the earliest reset among the windows that
breached it. Three things about it:

- It is **threshold-based, not burn-line-based.** It asks whether consumption is
  high; the pacer asks whether consumption is ahead of elapsed time, `h = e − c`.
  A 90% gate at hour 1 of a 5-hour window and at hour 4.9 read the same to it and
  opposite to us. It is not a replacement for the pacing rule.
- It is **not wired to anything** at v0.84.1. Grepping the tree finds the
  definition and four tests, no production caller. The `UsageService` around it
  *is* reachable, but only through Office mode's
  `GET /agents/:id/utilization` (`internal/office/agents/handler.go:44`), which
  calls plain `GetUsage` and never the predicate. Office is behind
  `features.office`, default false (`internal/runtimeflags/config.go:12`), and
  that route 404s on our instance (probed 2026-08-04).
- It nonetheless **strengthens the four asks rather than weakening them.** An
  unwired predicate with a considered doc comment is a statement that upstream
  regards quota gating as in scope. The asks are for the pieces a gate needs to
  act — a step-addressed launch, a quota reader, step identity, step routing —
  and none of them are made redundant by upstream owning the policy. If anything
  the ask gets easier to make: it is no longer asking kandev to care about
  something it has shown no interest in.
