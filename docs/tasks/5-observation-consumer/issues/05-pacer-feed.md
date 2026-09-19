# Should cost and cache-gap facts feed pacer scheduling?

Type: research
Status: resolved
Blocked by: 01

## Question

Can the archived records improve pacer scheduling decisions beyond what the pacer
already reads live (`cache_waste` readers, `pacer.jsonl`, kandev usage events)? If
not, rule this candidate out of scope.

## Answer

Resolved 2026-09-13. Recommendation: rule this candidate out of scope.

- The pacer already reads the live sources the records are built from:
  `kandev_pacer.cache_waste` reads transcripts and usage events, `pacer.jsonl` holds
  its own waits, and kandev's `task_usage_events` is read every tick.
- A record appears only after a task is archived, often a day or more after the
  sessions ran. Scheduling needs the state of open sessions now.
- Cache-gap rows are elapsed gaps, not misses (ticket one). The pacer's cache-deadline
  rule is tracked as [task 9](../../9-prompt-cache-deadline-order.md) and works
  from live turn times.
- Cost is missing for Codex, so cost-based ordering would be provider-blind.

What the records can do for the pacer is indirect: a human review can point at a
pacing knob to change (ticket four). Direct machine feeding is closed here. Antti can
reopen it by redrawing the destination.
