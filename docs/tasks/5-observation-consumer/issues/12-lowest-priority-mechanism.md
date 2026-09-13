# How is "lowest priority" expressed to the pacer?

Type: research
Status: resolved
Blocked by:

## Question

What in `kandev-pacer` makes one task launch last, and can the Heartbeat set it when
creating the consumer task?

## Answer

Resolved 2026-09-13 from `src/kandev_pacer/selection.py`.

The pull order key is column first (rightmost column wins, so anything in flight
goes before a fresh task), then the drag position inside the column, then the
Kandev `priority` field, then creation time. A sidebar drag order, when one exists,
overrides the board order. So:

- Column already does most of the work: a newly created consumer task sits in the
  first column behind every task that has started.
- Among not-yet-started tasks, the drag position dominates and `priority: low` is
  only a tie-break. The Heartbeat cannot set a drag position through
  `create_task_kandev`, and all Kandev tasks currently carry `priority: medium`.
- Recommendation for the plan: the Heartbeat creates the task with the lowest
  Kandev priority, and the pacer additionally recognises the consumer task by its
  title template and sorts it after every other launchable task of the same
  provider. That keeps the rule visible in `--dump-order` and independent of drag
  state. This is an implementation choice for the plan handoff, not a new decision.
