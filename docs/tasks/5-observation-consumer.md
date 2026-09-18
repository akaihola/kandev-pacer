---
depends_on: []
wayfinder: map
---

# What consumes the `kandev-learn` session observation records

A wayfinder map. Tickets live in `5-observation-consumer/issues/`; each is one
decision. The map is an index: it gists a decision and links the ticket that holds it.

## Destination

A locked decision on what consumes the observation records under
`~/my-knowledge/pages/PKB/Kandev Session Observations/`: who reads them, where that
reader lives, what it produces, when it runs, and what it may write. A later planning
task implements the consumer from these decisions. Decisions, not code.

## Notes

- Collector docs: `docs/session-observations.md`,
  `docs/session-observations-verification.md`. The collector stays measurement only;
  interpretation belongs to the consumer.
- Eight records exist (2026-09-11 to 2026-09-13). Three predate the unattributed-usage
  repair; see the verification doc.
- The five `[*]` readability items in `TASKS.md` (short numbers and timestamps, session
  ID once, filename UUID stated once, step names instead of IDs, quoted prompt and final
  response) are taken as given. Tickets factor them in; they are not re-decided here.
- Workflow step prompts do not live in this repo. `make-paced` reads and updates them
  through kandev's `PUT /api/v1/workflow/steps/{id}`. A "proposal to change a workflow
  step" is a proposal against kandev data.
- The vault `~/my-knowledge` is Tyko's knowledge base: `HEARTBEAT.md` runs on Haiku
  with Opus sub-agents, `LEARN.md` routes captures to `memory/daily/` and
  `pages/PKB/Improvement backlog.md`, and its `AGENTS.md` says ask before spending or
  external communication, never push, one agent mutates.
- Skills for grilling tickets: `/grilling`, `/domain-modeling`. Grilling tickets are
  human in the loop; an agent may write a *proposed* answer but must not resolve them.
- Do not edit the immutable records or `~/my-knowledge` from this effort.

## Decisions so far

<!-- one line per closed ticket: gist, then the link for detail -->

- [What in a record can a consumer rely on today?](5-observation-consumer/issues/01-record-facts-a-consumer-can-rely-on.md)
  — frontmatter is the interface; cost is null for Codex, cache gaps are not misses,
  steps are IDs, the collector cannot start where the pacer config is invalid.
- [What does the Tyko vault already offer a consumer?](5-observation-consumer/issues/06-tyko-fit.md)
  — heartbeat-plus-Opus-sub-agent fits; the vault already planned a session-review
  successor with deterministic pre-scans; unread digests have been dropped before.
- [Must the readability items land before the consumer?](5-observation-consumer/issues/07-readability-dependency.md)
  — yes for a human or model reader, no for a frontmatter reader; glob by directory,
  never parse filenames.
- [Who is the consumer's audience?](5-observation-consumer/issues/02-audience.md)
  — Antti, via a periodic review; problems are raised for collaborating on a mitigation.
- [What does the consumer emit?](5-observation-consumer/issues/04-output.md)
  — observations plus a proposed solution prompt into a target repo's "Unverified
  proposals" registry section; kandev-config, nixos-config or the task's project repo.
- [Where does the consumer live?](5-observation-consumer/issues/03-home.md)
  — a Kandev task the Heartbeat creates, lowest priority, skipped when nothing is new.
- [When does the consumer run?](5-observation-consumer/issues/08-cadence.md)
  — whenever the Heartbeat runs and unconsumed records exist; leftover quota only.
- [How is "lowest priority" expressed to the pacer?](5-observation-consumer/issues/12-lowest-priority-mechanism.md)
  — column, then drag position, then priority; recommend a title-based last-place rule.
- [Where is "Unverified proposals" in each target repo?](5-observation-consumer/issues/10-registry-convention.md)
  — follow each repo's `AGENTS.md`; kandev-config and this repo now have the section.
- [What may the consumer write?](5-observation-consumer/issues/09-write-boundary.md)
  — commit to the default branch; drop exact duplicates, merge similar proposals;
  never read its own records; never create tasks from a proposal.
- [How does the consumer know which records are already learned from?](5-observation-consumer/issues/11-consumed-ledger.md)
  — a list file at `Kandev Session Observations/consumed.md` in the vault, one line
  per record read or skipped, written by the consumer and read by the Heartbeat.

## Not yet specified

Nothing. Every decision this map set out to make is made. What is left is
implementation, described under Handoff below.

One thing to revisit after the first real run: whether findings should feed back
into the collector, because the review keeps wanting a measurement that is not
there. That is new work, not a gap in this map.

## Handoff

The way is clear. A planning task implements the consumer from the decisions above.
It has three parts, in this order.

1. **The consumer task's prompt**, which is the bulk of the work. It reads the
   leftover records, finds problems worth Antti's attention, and writes observations
   plus a proposed solution prompt into the right repository, following that
   repository's `AGENTS.md`. It dedupes and merges against existing proposals, skips
   records of its own past runs, and appends every record it handled to the list
   file with a local vault commit. Decide the batch cap here; there are 127 records
   waiting.
2. **The Heartbeat change** in `~/prg/kandev-config`: create the consumer task at the
   lowest Kandev priority when, and only when, the record folders hold something the
   list file does not.
3. **The pacer change** in this repository: recognise the consumer task and sort it
   after every other launchable task of the same provider, so it spends only
   leftover quota and shows up correctly in `--dump-order`.

Depends on the five readability items for `kandev-learn` records, all currently under
`## In progress` in `TASKS.md`. A consumer that reads the Markdown body wants step
names and the quoted prompt and final response. It must still cope with the older
records, which keep their original shape forever.

## Out of scope

- Feeding cost and cache-gap facts into pacer scheduling: the pacer already reads the
  live sources, records arrive after archive, and gaps are not misses. See
  [Should cost and cache-gap facts feed pacer scheduling?](5-observation-consumer/issues/05-pacer-feed.md).
- Changing what the collector measures. Belongs to `kandev-learn` work, not here.
- Implementing the consumer. The plan handoff after this map does that.
- The five readability items themselves. Already tracked as `[*]` items in `TASKS.md`.
