# Can the pacer be a Kandev plugin?

Assessed 2026-07-26 against the Kandev plugin documentation and SDK source
(`docs/public/plugins.md`, `plugins-authoring.md`, `plugins-manifest.md`,
`docs/specs/plugins/spec.md`, `docs/decisions/0043-plugin-host-data-api.md`,
`apps/backend/pkg/pluginsdk/data_types.go`).

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

1. **No launch primitive.** `session.launch` — the whole point of the pacer — has
   no Host API equivalent. `capabilities.api_write` is accepted by the manifest
   parser but enforces nothing; the deferred write RPCs named in the spec and
   ADR 0043 are `CreateTask`, `UpdateTask`, and `CreateComment`. Starting a
   session is not on that list.
2. **No usage data.** The `api_read` vocabulary is `tasks`, `sessions`,
   `messages`, `workspaces`, `workflows`, `agent_profiles`, `repositories`.
   Nothing exposes quota. `GET /api/v1/agents/usage` has no Host counterpart.
3. **`Task` carries no `workflow_step_id`** (`data_types.go:92`). Which step a
   task is parked on is the pacer's central fact, and it is invisible through the
   Host API.
4. **`WorkflowStep` is `{ID, WorkflowID, Name, Position, StageType}`**
   (`data_types.go:367`) — no `events`, no `on_turn_complete`, no
   `is_start_step`. The work-step classifier deliberately reads
   `on_turn_complete` rather than trusting `stage_type`; on Host data only the
   read-only `stage_type` hint remains, which the README already rejects as
   corroborating evidence rather than a source of truth.

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
| Usage windows | `GET /api/v1/agents/usage` |
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
