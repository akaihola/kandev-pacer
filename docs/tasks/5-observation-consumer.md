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

## Not yet specified

- The prompt and output contract of a model-driven review pass: which record fields it
  reads, what a "finding" looks like, how it avoids restating the tables. Waits on the
  audience and output tickets.
- How a proposal reaches a kandev workflow step: hand-applied by Antti, a `make-paced`
  flag, or a kandev task. Waits on the output and boundary tickets.
- Whether findings should feed back into the collector (new measurements the review
  keeps wanting). Waits on the first real review.

## Out of scope

- Feeding cost and cache-gap facts into pacer scheduling: the pacer already reads the
  live sources, records arrive after archive, and gaps are not misses. See
  [Should cost and cache-gap facts feed pacer scheduling?](5-observation-consumer/issues/05-pacer-feed.md).
- Changing what the collector measures. Belongs to `kandev-learn` work, not here.
- Implementing the consumer. The plan handoff after this map does that.
- The five readability items themselves. Already tracked as `[*]` items in `TASKS.md`.
