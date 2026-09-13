# Where is "Unverified proposals" in each target repo?

Type: grilling
Status: open
Blocked by:

## Question

Each target repository needs one place the consumer appends to. Found 2026-09-13:
`~/prg/kandev-config` has no task registry at all (README, `export.py`, `apply.py`,
`workspaces/`, `docs/decisions/`); `~/repos/nixos-config` tracks ideas as one file
per idea under `docs/ideas/` with an `AGENDA.md`; this repo's `TASKS.md` has no
"Unverified proposals" section; other project repos vary and their `AGENTS.md`
is what the Heartbeat reads. Decide: one shared convention (a section name in
`TASKS.md` and a fallback file when a repo has none), or per-repo instructions the
consumer reads from each `AGENTS.md`. Also decide who adds the section to
`kandev-config` and this repo.
