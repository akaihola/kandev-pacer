---
depends_on: []
---

# Advance a finished task instead of flagging it BLOCKED

Land this after the "launch only the primary session" item. Both conditions below rest on
knowing which session is the primary one, and while the pacer still launches `survey-*`
children, a signal sent for one of those would advance a step the primary session has not
finished.

## Problem

`launch` in `runtime.py` fingerprints the task's worktrees, `note_stall` counts the
launches that left the fingerprint alone, and after `PACER_STALL_AFTER` of them `give_up`
sets the card BLOCKED. That is the right answer for a card that cannot progress, and the
wrong one for the commonest case observed: a primary session that **did** the work, wrote
its summary, and never called `step_complete_kandev`. The step requires the signal to
advance, so the task sits on a work step looking exactly like a wedged one. In the 48 h to
2026-09-06 four cards ended that way, three of them after their primary session had
finished a vault cleanup round.

Flagging is cheap to undo but costs a person's attention, and the information needed to
decide is already on the board.

## Change

Add `PACER_STALL_ACTION`, values `block` (default, today's behaviour) and `signal`.

In `give_up`, when the action is `signal`, require all three:

1. The stalled session is the task's **primary** session (`is_primary`).
2. Its state is `WAITING_FOR_INPUT`.
3. The task's current step has `auto_advance_requires_signal` true. A step that does not
   opt in ignores the signal, so sending one there is noise.

Then send the websocket action `mcp.step_complete` — the same action the agent's own
`step_complete_kandev` tool uses — through the existing `ws_request` in `client.py`:

```json
{"task_id": "...", "session_id": "<primary session>",
 "summary": "kandev-pacer: N turns left the worktree untouched, advancing on the agent's behalf"}
```

`task_id`, `session_id` and a non-empty `summary` are all required by kandev's handler
(`internal/mcp/handlers/handlers.go`). The handler re-publishes the bus event when the
session is `WAITING_FOR_INPUT`, which is exactly this case, so an idle session is the
intended target rather than an edge case.

Reset the stall record and log `{"kind": "stalled", "action": "signalled"}`. On a later
tick, if the task's `workflow_step_id` has not moved within `PACER_LAUNCH_LATENCY`, fall
back to `block`: a signal kandev ignored must still reach a person.

## Where it goes

- `config.py`: `stall_action`, validated against `{"block", "signal"}`.
- `client.py`: `ws_step_complete(task_id, session_id, summary)`.
- `runtime.py`: the branch in `give_up`, and the re-check that falls back to `block`.
- `tests/test_runtime.py`: signals when all three conditions hold; blocks when the session
  is not primary, is not waiting for input, or the step does not require a signal; falls
  back to `block` when the step does not move; `block` is unchanged by default.
- `tests/test_contract_live.py`: add `mcp.step_complete` to the action names kandev must
  still recognise. The live test cannot send it — sending it moves a card.
- `README.md`: the configuration table, and a paragraph in the troubleshooting section on
  what a `BLOCKED` card means once this exists.

## Risk to test first

A primary session that stopped because it hit a real blocker is advanced too. The Review
step's prompt already asks for the blocker to be recorded as evidence, so the next step
sees that evidence rather than nothing. Check this against a real blocked card before
setting `signal` on `agent@gogo`.

## Done when

`uv run pytest` passes, and on `agent@gogo` with `PACER_STALL_ACTION=signal` a week
produces `stalled` records with `"action": "signalled"` and no card flagged BLOCKED whose
primary session had already written a completion summary.
