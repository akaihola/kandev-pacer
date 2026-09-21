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

## Implementation review

The review repaired these collection cases:

- Cache gaps now retain transcript identity and never bridge separate transcripts.
- Unattributed usage sums unassigned usage events from the same source. It no longer
  subtracts Kandev step totals from ccusage totals or produces floating-point
  subtraction residue for cost.
- Hermes costs and call counts accumulate across fully bounded matching sessions.
  A missing export retains metadata and other available exports; an unfinished
  session's lifetime totals are excluded from the archive window.
- Codex exit status comes from the output envelope, excluding quoted status lines
  after its `Output:` delimiter. Non-string command arguments cannot break the
  repeated-command table.
- A turn at a step boundary is counted once. Sweep validates an existing record's
  archive identity before committing it or advancing its watermark.

The focused suite now passes 36 tests. The full review run before adding the final
boundary regression reported 41 failures, 263 passes and 19 skips. The 41 failing
node IDs exactly match the unchanged baseline after normalizing checkout paths.
Repository formatting, lint and type checks pass. The Nix module parses.

All ten tracker issues occur under exactly one heading. Issue 4 occurs only under
`In progress`; removing its entry and link makes TASKS.md byte-identical to main.
The timer branch remains separate and has no activation or rebuild changes.

The same three real tasks were collected again into `/tmp/learn-review-final/`,
using the normal CLI with an alternate vault and state directory. Their complete
records are under `pages/PKB/Kandev Session Observations/2026-09/` with the filenames
listed above. Calls and actual costs remain unchanged. All available unattributed
usage values are zero; Codex actual cost remains unknown. Claude has two cache-gap
rows, Codex six, and OpenCode none. These are elapsed-gap observations, not inferred
cache misses.

The original vault records remain byte-identical and were not overwritten. Their
`unattributed_usage` fields predate the repair: Codex and OpenCode contain differences
between ccusage and Kandev totals, and Claude cost contains floating-point residue.
Use the review reruns for those fields. This limitation is recorded here because the
immutable-record requirement prevents replacing those published files.

The wording scan of the reruns still finds only the two copies of `should` in the
required verbatim Codex signal quoted above. The other two records have none of the
listed words. No collector-authored judgment was added. The verbatim/word-exclusion
conflict remains a confirmed acceptance blocker; it cannot truthfully be reported
as a passed whole-record check.


## Deployment

Merged the collector into main with `--no-ff` (`2a71693`) and the timer into
nixos-config master with `--no-ff` (`5f22982`). The collector's development-tree
uv environment was refreshed by running its installed entry point. All 36 focused
tests pass from `~/prg/kandev-pacer`; help, dry sweep and digest also run there.
The dry sweep still lists no tasks and does not initialize production state.

No installed user service directly runs kandev-pacer or kandev-learn, so no restart
applies. No known vempai.men deployment exists for this collector. The Nix timer
is merged but is not activated; Antti performs rebuild and activation.

Unrelated main-worktree edits were preserved in named stashes to leave both main
worktrees clean. Neither stash was applied or dropped:

- kandev-pacer: `08a0d0a829fa91d42b8fda023ef571ffcbb4c486`, containing AGENTS.md
  and docs/patching-task-metadata.md.
- nixos-config: `b997327e4fb2d45440bbf1afe0648427d4e7b4c3`, containing
  home-manager/dotfiles/.config/coding-agents/sandbox.md.

Both have the message `pre-kandev-learn-merge: preserve unrelated work`.
Recover a stash with `git stash apply <hash>` in its repository when ready to
resume that work. Stashes remain local.

### End-user checks

From `~/prg/kandev-pacer`:

```bash
uv run --frozen kandev-learn sweep --dry-run
uv run --frozen kandev-learn 051b4ce1-51d6-4b48-89ee-71ec36f09860
uv run --frozen kandev-learn digest --since 2026-09-01
```

These show pending archive identities, return the existing Claude evidence record
without replacing it, and print the aggregate table. See the implementation-review
section above for the immutable records' known limitations.

After Antti activates the Nix configuration, verify the scheduled collector with:

```bash
systemctl --user list-timers kandev-learn.timer
systemctl --user status kandev-learn.timer
journalctl --user -u kandev-learn.service -n 30 --no-pager
```

The timer lists a 15-minute schedule. Its first sweep establishes the archive
boundary; subsequently archiving a task produces a new record on a later sweep.

## Split-output deployment, 2026-09-21

Merged with `--no-ff` as `d1ce12c`, after rebasing onto current main. The tracker
conflict was resolved from main's text with only this issue moved. Implementation
is in `82f4af3`, null-message extraction repair in `0888ef6`, and end-user checks
in `0230fb9`. See [Verify split output](session-observations.md#verify-split-output).

The active `kandev-learn.service` runs `uv run --frozen kandev-learn sweep` from
`~/prg/kandev-pacer`. Restarted it at 09:53:25 EEST; it finished at 09:53:26 with
`Result=success` and `ExecMainStatus=0`. The 15-minute `kandev-learn.timer` remains
active. No new JSON observations were produced by that run, so split-format output
was verified with fixtures rather than claimed from a new production observation.
No unit changes or Nix activation were needed.

After rebase, all 66 focused tests passed, as did formatting, diff-scoped lint/type
checks and whitespace checks. The full default-settings suite in review had
437 passes, 20 skips and the existing quota-default failure in
`test_direct_quota_source_remains_the_default`, previously reproduced on main.
Local server settings must be isolated when running the full suite.

Both worktrees were clean after merging. Only this issue was moved to Completed.
The unrelated Fable issue already appears under Scheduled and Completed in main;
that duplicate was preserved to keep every other issue unchanged.
