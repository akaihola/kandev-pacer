# When does the consumer run?

Type: grilling
Status: resolved
Blocked by: 03, 04

## Question

Trigger and cadence: after every sweep that produced records, on a daily or weekly
timer, or on demand? Bound the model spend per run and per week.

## Answer

Decided by Antti, 2026-09-13.

Whenever the Heartbeat runs and there are observation records that have not been
learned from yet. No timer of its own. The pacer's launch order puts the task last,
so it spends only leftover quota. Which records count as "learned from" and where
that is remembered is the ledger ticket.
