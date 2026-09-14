# Where is "Unverified proposals" in each target repo?

Type: grilling
Status: resolved
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

## Answer

Decided by Antti, 2026-09-14.

The consumer follows each target repository's `AGENTS.md` to find where proposals
go, so that a proposal is never a candidate for implementation before Antti has
reviewed it. No shared convention is imposed on project repos.

Done as part of this decision (Antti asked for it):
- `~/prg/kandev-config` got a `TASKS.md` (commit `637720b`) with the rules copied
  from the newest iteration among our repositories (`~/prg/dallape/TASKS.md`,
  2026-09-14), minus its reference to a `tools/check_tasks_md.py` that exists in
  no repository, plus a rule defining `## Unverified proposals`. An `AGENTS.md`
  points the Heartbeat at it.
- This repo's `TASKS.md` got the `## Unverified proposals` section and the same
  rule.
- `~/repos/nixos-config` keeps its own convention: one file per idea under
  `docs/ideas/`, as its `docs/README.md` says. The consumer follows that.

The rule, in both files: proposals under that heading are never scheduled from
there; Antti moves them into the backlog or deletes them.
