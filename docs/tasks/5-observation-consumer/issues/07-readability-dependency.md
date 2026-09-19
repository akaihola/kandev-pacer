# Must the readability items land before the consumer?

Type: research
Status: resolved
Blocked by: 01

## Question

Do the five `[*]` readability items in `TASKS.md` block the consumer, and does the
consumer read the JSON frontmatter, the Markdown body, or both? Decide the dependency
direction so the plan handoff orders the work.

## Answer

Resolved 2026-09-13. The dependency analysis below is historical; all five
readability tasks are now completed, as recorded in the
[map handoff](../../5-observation-consumer.md#handoff).

- A machine consumer reads frontmatter only and is not blocked by any of the five
  items. It must glob the month directories and read `archived_at` instead of parsing
  filenames, because item one changes the filename format and old files keep theirs.
- A human or model reader reads the Markdown body and is blocked by items two to
  five in practice: step IDs, repeated session IDs and the missing prompt and final
  response make the body hard to judge, and a model pass would spend tokens on the
  raw tool list.
- Dependency direction: the five `[*]` items land first, then the consumer's first
  real run. They were small and ordered ahead of this map in `TASKS.md`; the
  consumer plan should list them under `depends_on`. The consumer must still accept
  the eight existing records in the old shape, since records are immutable.
