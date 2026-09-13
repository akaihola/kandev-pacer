# How does the consumer know which records are already learned from?

Type: grilling
Status: open
Blocked by:

## Question

Records are immutable, so "learned from" cannot be marked on the record. Options:
a ledger file in this repo or the vault listing consumed `archived_at` plus task
IDs; a high-water mark like the collector's own sweep state under
`~/.local/state/kandev-learn/`; or a Kandev-side marker on the consumer task. The
Heartbeat must read it cheaply to decide whether to create the task, and the
consumer must update it at the end of a run. Also decide what "learned from" means
when a record produced no proposal (proposed: still consumed) and how the consumer
task's own records are excluded (proposed: skip records whose title matches the
consumer's title template, and log them as consumed).
