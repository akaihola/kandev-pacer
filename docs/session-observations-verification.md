# Observation verification, 2026-09-12

Three real archived tasks were collected with `kandev-learn <task-id>` into the
vault. No archive backfill, pacer loop change, push, rebuild or activation ran.

| Agent | Task | Recorded calls | Actual cost USD |
| --- | --- | ---: | ---: |
| Claude | 051b4ce1-51d6-4b48-89ee-71ec36f09860 | 132 | 24.2928 |
| Codex | ffef5ab6-ded5-4e4b-af95-aed0eacad222 | 51 | unknown |
| OpenCode | e795741e-b1b7-403e-b581-5930bd9dc2db | 35 | 0.3279 |

Costs cover available provider-reported rows. Codex's existing usage costs are
estimated and were excluded. All three runs found transcripts and ccusage totals.
The Codex task maps to two surviving transcript IDs in the same task-session
window. Usage windows were checked before accepting whole-session aggregates.

Vault commit `f5261c5` contains only these three records. All pre-existing index
entries were verified unchanged.

The record files under
`~/my-knowledge/pages/PKB/Kandev Session Observations/2026-09/` are:

- `2026-09-12-05-26-36.836376201+00-00 051b4ce1-51d6-4b48-89ee-71ec36f09860.md`
- `2026-09-11-20-29-03.426187375+00-00 ffef5ab6-ded5-4e4b-af95-aed0eacad222.md`
- `2026-09-11-18-38-26.342612871+00-00 e795741e-b1b7-403e-b581-5930bd9dc2db.md`

## Dry sweep

The production state has no high-water mark. `kandev-learn sweep --dry-run` printed:

```text
Initial archive boundary: 2026-09-12 05:26:36.836376201+00:00
Tasks: []
```

It did not create state or process the existing archive. `digest --since 2026-09-01`
was checked against the three records and printed a single table with a totals row.

## Source availability and wording

Pi transcript support ran on fixtures only: no archived Pi task session exists in
Kandev. Three archived Hermes task sessions exist, but none has a surviving matching
cwd/time window in the tyko database. A direct Hermes probe in a temporary output
vault recorded `no transcript`; Hermes tool/error parsing therefore ran on fixtures.

Collector-authored text is factual. The Claude and OpenCode records contain none
of the task's listed judgment words. The literal wording acceptance criterion is
not met for the Codex record: a required verbatim signal at
`2026-09-11 15:37:35.694370163+00:00` contains:

> and should any gogo write credential be allowed if the handoff is too cumbersome?

This appears once in frontmatter and once in the signal table. Removing or
rewriting it would violate the verbatim requirement. This conflict remains for
Review; it is not counted as a passed check.

## Checks and observed failures

The collector fixture suite covers all five formats, exact path encoding, actual
cost filtering, output bytes and excerpts, time windows, ambiguous matching,
cache helper use without its cost calculations, read-only databases, source
timeouts, same-day archive identity, dry sweep, equal timestamps, bounded backfill,
commit recovery, concurrent HEAD changes, digest totals and preservation of
unrelated staged/unstaged files.

The default existing suite has 41 failures. Running an unchanged HEAD archive from
`/tmp/learn-baseline` reproduces all 41, with 228 passes and 19 skips. No existing
pacer code or tests were changed to address those failures. The added collector
suite passes all 28 tests. The combined run reports 41 failures, 256 passes and
19 skips; the failures match the unchanged baseline. Both repository checks,
`uv run darker --check` and `uv run graylint`, pass. Ruff and type checks pass for the collector.

The wheel builds, and `kandev-learn --help` plus the cache helper import work in an
isolated uv environment outside the checkout. The Nix file parses without a rebuild.

Two environment issues were resolved within scope: uv initially lacked graylint in
a new temporary cache, so checks used the existing populated cache; OpenCode's local
wrapper pointed to an absent Bun binary, so exports used its installed npm binary.
Its piped stdout stopped at 65,536 bytes; capturing to a regular temporary file
preserved the complete JSON export.

Antti performs rebuild and activation after integration.
