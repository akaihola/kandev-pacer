# Where does the consumer live?

Type: grilling
Status: open
Blocked by: 02, 06

## Question

Which of these hosts the consumer: the Tyko vault (a `HEARTBEAT.md` item with an Opus
sub-agent, or a LEARN-style capture), this repo (a `kandev-learn` subcommand or a
script on the same timer), or kandev (an automation or a scheduled Paced exploration
task)? Consider where the records are readable, where the pacer config is available,
and who is allowed to spend model calls.

## Proposed answer (needs Antti, waits on tickets two and four)

The Tyko vault, as a `HEARTBEAT.md` item that spawns an Opus sub-agent when new
records exist since the last seen `archived_at` in `memory/heartbeat-state.json`.
Ticket six shows this needs no new machinery, the records are already there, and the
vault already planned a session-review successor. This repo keeps `digest` as the
deterministic table the sub-agent may quote, but the sub-agent reads record files
directly (ticket one's environment finding). A kandev automation is the fallback if
Antti wants the review to be a visible task on the board.
