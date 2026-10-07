# Deterministic Heartbeat helpers

These commands implement the script proposals in kandev-config's
`docs/2026-10-07-heartbeat-deterministic-script-research.md`. Selection, judging whether
work can ship independently, and writing acceptance criteria remain model work. The
Heartbeat prompt migration is a separate kandev-config backlog task.

Install `uv sync --extra heartbeat` in the base checkout. Like the T3 extra, this needs
Hai at the `../hai` source path in `pyproject.toml`; see [T3 setup](t3-pacing.md).
Use `--extra heartbeat` on subsequent frozen runs so UV keeps Hai installed.

## Preflight and selection

```bash
uv run --frozen --extra heartbeat --project /home/agent/prg/kandev-pacer heartbeat preflight
```

The compact JSON gives workspace-scoped capacity, resolved profile/workflow/executor
IDs, fresh quota fractions and the non-trivial profile choice, repository counts,
source paths, dirty flags, first candidates, coverage and rate windows. Heartbeat runs
are excluded from the 20-task capacity. Archived identities remain in collision checks
and rolling rate limits. Completed tracker issues count as finished with either checkbox.
The digest has two candidates per repository, reports how many it omitted, and is about
4 KB for the current nine repositories. Decisions use complete sources, never that
bounded display.

For a selected repository, read its full bullets, current instructions, revisions and
five recent commit subjects, open task titles and identities, and heartbeat tasks
older than three weeks:

```bash
uv run --frozen --extra heartbeat --project /home/agent/prg/kandev-pacer heartbeat \
  --repository /home/agent/prg/filemill preflight --all
```

Global options precede the action: `--db`, `--quota`, `--workspace` and repeatable
`--repository`. Defaults point to gogo's local Kandev database, pebble snapshot,
Default Workspace and the nine repositories in the current Heartbeat prompt.
The database opens read-only. No command writes SQL. Use a database belonging to the
same Kandev server configured with `KANDEV_URL`.

`TASKS.md` reads use Hai. A legacy missing proposals heading is supplied in memory
for reads; scheduling still requires a valid Hai tracker. Issue-file readers support
nixos-config's Markdown inbox and kasvimuseo's RST `Status`, `Claimed` and `Depends on`
fields. Missing dependencies block selection. Unknown statuses and unsplit RST intake
reports are visible errors. Equal six-word keys are collisions, including when one of
the bullets is finished. Exact open task titles on the same repository also count as
covered; the model must still check semantic duplicates with different titles.

The current rules supported automatically are nixos-config's one-task rolling seven-day
limit and my-knowledge's one-open-heartbeat limit. Both are read from current AGENTS.md
text. The expired nixos-config temporary open-task cap is not applied. New rule wording
or other work-item formats require review before using these helpers to schedule them.
The focused `--all` output includes Heartbeat rule sections for that review.

## Create and verify

Write the model's problem, relevant files and acceptance criteria to a local text file:

```bash
uv run --frozen --extra heartbeat --project /home/agent/prg/kandev-pacer heartbeat \
  create /home/agent/prg/filemill ITEM_KEY --description-file /tmp/heartbeat-description.txt \
  --kind simple
```

`--kind` selects `complex`, `simple` or `research`; `--trivial` selects Luna Medium.
Otherwise the lower seven-day used fraction chooses Astra High or Opus Medium, with
Luna Medium when both exceed 1.05. Missing, invalid or stale quota blocks creation.
The snapshot must carry a collector timestamp no more than 900 seconds old.

For `status: create`, call the direct Kandev `create_task_kandev` tool with the returned
`arguments` exactly. It includes the quoted source, stable identity, absolute local
path and `start_agent: false`. It omits parent and repository IDs. Then verify the
returned task before scheduling or creating another:

```bash
uv run --frozen --extra heartbeat --project /home/agent/prg/kandev-pacer heartbeat \
  verify TASK_ID 'heartbeat:filemill:ITEM_KEY' /home/agent/prg/filemill --workspace-id WORKSPACE_ID
```

Only `status: open` confirms the identity and repository binding. A wrong binding or
finished identity is a blocker. After an uncertain create response, rerun `create` for
the same key. An existing open identity returns `status: verify` with its task ID;
verify it instead of creating another identity. Coverage is advisory until this API
verification succeeds. No helper creates a Kandev task itself.

## Schedule TASKS.md

```bash
uv run --frozen --extra heartbeat --project /home/agent/prg/kandev-pacer tasks-md \
  --source /home/agent/prg/filemill/TASKS.md list
uv run --frozen --extra heartbeat --project /home/agent/prg/kandev-pacer tasks-md \
  --source /home/agent/prg/filemill/TASKS.md move ITEM_KEY --revision REVISION --to Scheduled
uv run --frozen --extra heartbeat --project /home/agent/prg/kandev-pacer tasks-md \
  --source /home/agent/prg/filemill/TASKS.md check
```

`move` requires the caller's revision, a clean checkout including the index, and the
base branch, default `main`, override with `--base-branch master`. It only schedules an
eligible backlog issue, checks the complete tracker and commits only TASKS.md. Hai's
revision check catches a tracker or description changed since `list`. A failed Git
commit leaves the move visible for review; resolve the failure rather than blindly
retrying. Other issues retain their text and acceptance. Issue-file adapters are
read-only: use `--format md` or `--format rst` with an inbox directory for `list` or
`check`, and follow that repository's own claim workflow for writes.

## Pacer gate and cleanup

With the webhook configured, the pacer runs the gate before each otherwise eligible
webhook attempt. Known empty/covered backlogs and an `open` or `empty` observation
consumer skip the run without spending the webhook cooldown. Eligible ordinary work,
unread observations, failed consumer recovery, and unreadable sources still allow a
Heartbeat to handle the work or report the error. The gate never freezes an observation
batch or edits its ledger. Existing provider, capacity, running-Heartbeat and cooldown
guards still apply.

The pacer also archives successful Heartbeat automation runs on its next tick. Cleanup
requires the exact reserved title, successful automation evidence, no unfinished
automation run, no child tasks, and no active/starting/unknown sessions. It rechecks
sessions through Kandev before sending `task.archive`, then confirms `archived_at`.
Failed or cancelled runs without success evidence remain available for investigation.
The original prompt's archive step can coexist with this cleanup.

Preview cleanup without writing:

```bash
uv run --frozen --extra heartbeat --project /home/agent/prg/kandev-pacer heartbeat archive
```

`heartbeat archive --apply` uses the supported Kandev WebSocket API. Tests use fixture
SQLite databases and mocked API writes; production validation uses only reads.

| Environment setting | Default | Effect |
| --- | --- | --- |
| `PACER_HEARTBEAT_PREFLIGHT` | `true` | Gate webhook attempts on available work |
| `PACER_HEARTBEAT_ARCHIVE` | `true` | Archive proven finished Heartbeat runs per tick |
| `PACER_HEARTBEAT_DB` | `~/.kandev/data/kandev.db` | Local read-only database |
| `PACER_HEARTBEAT_WORKSPACE` | `Default Workspace` | Name used to resolve IDs and capacity |
| `PACER_HEARTBEAT_REPOSITORIES` | `[]` | JSON list of absolute paths; empty uses the nine defaults |
| `PACER_HEARTBEAT_VAULT` | `~/my-knowledge` | Observation source for the gate |

These fields also work in the existing TOML and CLI settings layers. The gate uses
`PACER_PEBBLE_SNAPSHOT` for quota. Disable the two new switches to restore the previous
webhook behavior. After merging, sync the heartbeat extra in the base checkout and
restart the manually launched Kandev pacer to load the change. T3 heartbeat research is
separate work; this implements the Kandev automation helpers.
