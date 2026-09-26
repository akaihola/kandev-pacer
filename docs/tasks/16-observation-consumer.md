---
depends_on: [5]
---

# Learn from session observation records

Build the consumer decided in the [observation consumer map][map]. Read that map's
Decisions so far and Handoff first; every ticket linked there is resolved, and this
task does not reopen them.

## What it does

A Kandev task, created by the Heartbeat automation and launched last, reads the
observation records it has not read yet, finds problems worth Antti's attention, and
writes each one as observations plus a proposed solution prompt into the task registry
of the repository the fix belongs to. Antti then reviews the proposals together with an
agent. Nothing is scheduled or applied without that review.

## Three parts

1. **The consumer task's prompt.** Most of the work. It:
   - reads unread records from
     `~/my-knowledge/pages/PKB/Kandev Session Observations/YYYY-MM/`;
   - picks the target repository: `~/prg/kandev-config` for Kandev configuration such
     as workflow step prompts, `~/repos/nixos-config` for global configuration such as
     globally installed skills, otherwise the original task's project repository;
   - follows the target's `AGENTS.md` to find where unreviewed proposals go
     (`## Unverified proposals` in `TASKS.md` for this repo and kandev-config,
     `docs/ideas/` for nixos-config), and commits there on the default branch;
   - searches existing proposals first: drops an exact duplicate, and merges a similar
     one into a single better revision that keeps the ideas of both;
   - never reads records of its own past runs, and never creates tasks or backlog items;
   - appends every record it handled, read or skipped, to the list file
     `~/my-knowledge/pages/PKB/Kandev Session Observations/consumed.md`, with a local
     vault commit and no push.
2. **The Heartbeat change** in `~/prg/kandev-config`: create the consumer task at the
   lowest Kandev priority only when the record folders hold a record the list file does
   not name, and never while a consumer task is open.
3. **The pacer change** in this repository: recognise the consumer task and order it
   after every other launchable task of the same provider, so it spends only leftover
   quota. `--dump-order` must show why it is last.

## Things to decide while planning

- **Batch size.** 333 records are waiting as of 2026-09-26, so the first run cannot read
  them all. Pick a cap per run, and decide whether the backlog is read oldest first or
  whether old records are listed as skipped in one go.
- **How to recognise the consumer task**, by the Heartbeat, the pacer and the consumer
  itself. The map proposes the task title template.
- **Two record shapes.** 112 records have a `.json` sidecar with the full values; the
  older 221 carry the same data as JSON frontmatter. Read the sidecar when it exists,
  otherwise the frontmatter, as `docs/session-observations.md` describes. Never parse
  the filename: its format has changed once already.
- **What counts as a problem.** The map leaves the prompt's judgment open. Records hold
  measurements only: step wall time, errored calls, identical commands, largest outputs,
  cache gaps with pacer waits, blockers and verbatim signals. A cache gap is elapsed
  time, not a proven cache miss.

## Done when

On `agent@gogo`, a Heartbeat run with unread records creates one consumer task that
launches only after other launchable work, writes at least one proposal to a target
repository's unreviewed section without duplicating an existing one, and adds the
records it handled to `consumed.md`. A second Heartbeat run with nothing unread creates
no task.

[map]: 5-observation-consumer.md
