# Archived session observations

`kandev-learn` records measurements without model calls. It reads Kandev SQLite
in read-only mode and surviving agent transcripts. It never opens a session's
former workspace. Git diff counts come from `task_session_commits`.

```bash
uv run --frozen kandev-learn TASK_ID
uv run --frozen kandev-learn sweep --dry-run
uv run --frozen kandev-learn sweep
uv run --frozen kandev-learn digest --since 2026-09-01
```

Records live under
`~/my-knowledge/pages/PKB/Kandev Session Observations/YYYY-MM/`.
The filename uses the archive timestamp to milliseconds in UTC, without a timezone,
with punctuation made filename-safe, followed by the task ID. Existing files,
including partial records, are never replaced. Observation timestamps use the same
format. Durations use milliseconds and costs use USD to 1/1000 dollar.
Frontmatter is JSON, a YAML 1.2 subset, and includes numeric sources and coverage.
The digest reads that frontmatter and prints a single table with totals.

## Sources and units

- Kandev supplies task/session identities, recorded agent/model snapshots, step
  transitions, turn usage, signal metadata, blockers and commit counts.
- ccusage **20.0.19** supplies session token/cache totals. The prepared package is
  located under `~/.npm/_npx/`; runtime executes its CLI through Node without
  invoking an installer. Prepare that exact version separately before enabling
  the timer. Claude uses `session --id`; other agents use their session subcommand
  followed by local ID filtering. All calls pass `--offline --no-cost`.
- Claude and Pi adapters import the vendored skills-akaihola loaders. Their tool
  display functions print truncated prose, so the collector extracts call IDs,
  explicit error flags and output lengths from the loaded blocks. Codex needs
  its response-item adapter for these fields.
- OpenCode and Hermes use their CLI exporters against temporary SQLite backups.
  Original agent databases are never opened for writing. The OpenCode executable
  is resolved from `.bun/bin`, then `.npm-global/bin`, then PATH. Export stdout
  goes to a temporary file because the installed OpenCode binary drops piped
  output beyond 65,536 bytes on exit.
- Hermes exports one identified session with `--cwd`, `--session-id`,
  `--format trace` and `--no-redact`. No upload is performed. Its installed
  exporter substitutes export time for message timestamps; those timestamps
  are excluded. Session-level measurements require a matching metadata window.
- `kandev_pacer.cache_waste` is the existing measurement implementation moved
  into the package. `scripts/cache_waste.py` remains its command wrapper. The
  collector calls its readers and attribution helper; it does not call the
  report, cost estimates or release simulation.

Durations are seconds, rendered to milliseconds; tool-output size is UTF-8 bytes
of stored output, including any truncation already present in the source. Only explicit error flags or a
Codex command's recorded exit status mark errors. Repeated commands use exact
string equality and do not assert retry intent. The top ten outputs and command
counts cover all task sessions. Only repeated command text and the first 120
characters of errored output are retained; other tool input/output text is omitted.

Token fields retain their source's accounting. ccusage's input field excludes
its separately reported cache tokens; Kandev turn fields retain their contract's
values. Reasoning can be part of output and is never added to output a second
time. Per-step attribution uses complete contained turns or timestamped events;
crossing turns stay unassigned. Session-only costs are not divided across steps.
Costs use provider-reported Kandev rows, Pi message cost and Hermes actual session
cost. One Kandev subcent is USD 0.0001. Estimated rows are excluded. Missing cost
is unknown; a sum of available rows is labeled as having that coverage.

Cache gaps include elapsed time, helper TTL basis and observed cache counters.
Recorded pacer withholding is reported separately. A gap alone does not establish
a cache miss or its cause. Signals and blockers remain verbatim, even when their
source wording is interpretive; the collector adds no judgments.

## Sweep state and recovery

State is in `~/.local/state/kandev-learn/`. The first normal sweep establishes an
archive boundary without processing the existing archive. Dry-run prints that
proposed boundary and an empty task list without writing anything. Later sweeps
process up to five tasks, ordered by archive timestamp and ID. Boundary IDs retain
coverage across equal-timestamp batches. Unarchive/rearchive produces a new record.

`--backfill --batch-size N` explicitly processes older archives in batches with a
separate progress list. Do not use it during ordinary verification.

Runs serialize on `collector.lock`. Pending identities survive failures. Vault
commits hold `flock` on `~/my-knowledge/.git/kandev-learn.lock`, use a temporary
Git index and update only observation entries in the user's index. A commit journal
recovers interruption between the HEAD update and index synchronization. A concurrent
HEAD or index conflict stops the sweep with pending state retained. Hooks are disabled,
unrelated staged/unstaged files remain intact, and nothing is pushed.

Missing transcripts, stores and source timeouts produce `no transcript` or
`no data for <agent>` inside an exit-0 partial record. Output, state or Git failures
exit nonzero. Default source deadline is 45 seconds; a task has a shared budget of
three source deadlines. `--home`, `--db`, `--vault`, `--state`, `--source-timeout`
and `--batch-size` allow fixture environments without touching production paths.

The Nix user timer runs every 15 minutes from `~/prg/kandev-pacer` using the frozen
uv environment. No new Python dependencies were introduced; uv.lock pins the existing
dependencies. Antti performs rebuild and activation after the branch is integrated.

## Checks

```bash
uv run pytest tests/test_learn.py tests/test_learn_sources.py
env -u PACER_LIVE_KANDEV uv run pytest
uv run darker --check
uv run graylint
```

Fixtures cover five formats, path encoding, time-window filtering, explicit errors,
byte sizes, actual costs, cache attribution, same-day rearchive, equal timestamps,
backfill batches, dry-run, commit recovery and preservation of the vault's index.
See [verification evidence](session-observations-verification.md) for real runs and
remaining source limitations.
