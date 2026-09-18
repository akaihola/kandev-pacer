# How does the consumer know which records are already learned from?

Type: grilling
Status: resolved
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

## Answer

Decided by Antti, 2026-09-18: option one, a list file.

The consumer keeps a plain list of the records it has already read. Nothing is ever
written into a record, because records are immutable.

- **Where.** `~/my-knowledge/pages/PKB/Kandev Session Observations/consumed.md`, a
  sibling of the `YYYY-MM/` record folders. Chosen over this repository because the
  consumer runs in a per-task worktree that may belong to any repository, while the
  vault is one fixed path both the Heartbeat and the consumer can reach, already
  holds the records, and already gets local commits from the collector. The digest
  globs `*/*.md`, so a file at the folder root is never mistaken for a record.
  Verified 2026-09-18 in `learn.py`.
- **What is in it.** One line per record: the record's filename plus, for a record
  that was skipped, why. Nothing else. A human can read it and a script can compare
  it against the folder listing.
- **Who writes it.** The consumer, as the last step of a run, with a local commit to
  the vault. Never pushed, following the vault's own rule.
- **How the Heartbeat uses it.** List the record folders, subtract the lines in the
  list file, and create the consumer task only when something is left over. That is
  one directory listing and one file read, cheap enough for every Heartbeat run.
- **Counted as read even with no proposal.** A record that produced nothing still
  goes on the list, or the consumer would re-read it forever.
- **The consumer's own records.** A record of a consumer task goes on the list
  without being read, marked as skipped. That is what stops the consumer feeding on
  its own runs.
- **Out-of-order records.** A re-archived task produces a record with an older
  timestamp. The list handles this by construction, because it compares filenames
  rather than a high-water mark. This is why the list beat the single-timestamp
  option.

Open for the planning task, not a decision: whether the consumer reads every
leftover record in one run or caps the batch. There are 127 records as of
2026-09-18, and the first run would face all of them.
