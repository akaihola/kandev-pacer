# What does the consumer emit?

Type: grilling
Status: open
Blocked by: 02

## Question

What is the deliverable of one consumer run: a digest table surfaced to Antti, a
written review note with findings, proposals against `make-paced` workflow prompts,
steps or pacing knobs, or some combination? Name the artifact, where it is stored and
how Antti sees it.

## Proposed answer (needs Antti)

One review note per run, not a bare digest table. Contents: the digest totals for
the period, then at most a handful of findings, each anchored to a record file and a
field (step wall time, errored calls, identical commands, largest outputs, cache
gaps with pacer wait, blockers), each ending in one concrete proposal or "no action".
Proposals name a target: a kandev workflow step prompt (by workflow and step name),
a pacer setting, or a `TASKS.md` item for either repo. The note itself is the
artifact; Antti sees it by message from Tyko. Question to settle live: should
proposals against `make-paced` workflow prompts be written as ready-to-apply text, or
only as observations?
