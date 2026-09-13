# What in a record can a consumer rely on today?

Type: research
Status: resolved
Blocked by:

## Question

Which fields of an observation record are stable and trustworthy enough for a consumer
to build on, which are partial or provider-dependent, and how do the five readability
items change the file shape a consumer will see?

## Answer

Resolved 2026-09-13 from the two collector docs, the collector source and the eight
records.

**The JSON frontmatter is the machine interface.** Every Markdown table restates a
frontmatter field. Top level: `task`, `title`, `workflow`, `archived_at`, `totals`,
`diffs`, `blockers`, `sessions[]`. Per session: `agent`, `models`, `workspace_path`,
`start`, `end`, `steps[]` (step ID, wall seconds, preceding pacer wait, turns,
per-category token totals with a `source` string), `tools[]` (one row per call with
bytes and error flag), `tool_calls` histogram, `gaps[]`, `waits[]`, `signals[]`,
`unattributed_usage`, `usage_coverage`. Every number carries its source, so a
consumer can tell ccusage from kandev totals.

**Reliable now:** task and session identity, agent and model names, step wall time
and turn counts, tool histogram, output byte sizes, errored calls, identical
commands, diff counts, verbatim signals and blockers.

**Partial or provider-dependent:**
- Cost is `null` for Codex (estimated rows are excluded) and present for Claude and
  OpenCode. Cross-provider cost comparison is not possible from the records.
- Cache gaps are elapsed-time observations with a TTL basis. They do not establish a
  cache miss. Pacer withholding is a separate column.
- Steps are IDs. Names need a kandev lookup until readability item four adds the
  appendix.
- Pi and Hermes have no real transcript yet; their records say `no transcript`.
- The three 2026-09-11/12 records have pre-repair `unattributed_usage` values.
- Records carry noise rows a consumer must tolerate: a signal row with
  `{"error_message": "task tree archived"}` and an all-unknown row.
- Verbatim signal text can contain judgment words. The collector does not filter
  them; a consumer inherits them as quoted source text.
- The record is per task and can hold several sessions (Codex tasks map to two
  transcript IDs).

**How the readability items change the shape:** item one changes filenames (no
nanoseconds, no timezone), so a consumer must not parse the filename; `archived_at`
in frontmatter is the identity. Items two to five change only the Markdown body.
Item five adds the initial prompt and final response, which is the one field a
model-driven reader would want most and which no record has today.

**Environment finding:** `kandev-learn` imports the pacer's settings module at import
time, so it fails to start wherever the pacer config is invalid (seen here when a
heartbeat webhook URL is configured without its secret in the environment). A
consumer running outside this repo's environment should read the record files
directly rather than shell out to `kandev-learn digest`.
