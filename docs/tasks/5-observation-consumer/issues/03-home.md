# Where does the consumer live?

Type: grilling
Status: resolved
Blocked by: 02, 06

## Question

Which of these hosts the consumer: the Tyko vault (a `HEARTBEAT.md` item with an Opus
sub-agent, or a LEARN-style capture), this repo (a `kandev-learn` subcommand or a
script on the same timer), or kandev (an automation or a scheduled Paced exploration
task)? Consider where the records are readable, where the pacer config is available,
and who is allowed to spend model calls.

## Answer

Decided by Antti, 2026-09-13.

A Kandev task, collected by the Heartbeat automation that `kandev-pacer` fires when a
provider runs low on work, and placed as the lowest-priority task on the board. It
therefore runs only when all other work is done and some subscription quota is left.
The Heartbeat skips creating it when every observation has already been learned
from. The Tyko vault is not the home; the records stay there as the input.
