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

## Implementation and verification, 2026-09-27

The consumer uses the reserved title `[Auto] Learn from session observations`.
`selection.select_action` puts it after normal eligible launches, after cache sorting.
Queued user prompts still drain first. `--dump-order` labels the consumer and states
that its display does not simulate live cache deadlines, gates or prompt drains.

`kandev_pacer.observation_consumer` provides three commands:

- `prepare --workspace ID --workflow ID --agent ID --executor ID` returns a complete
  `create_task_kandev` argument object or an empty/open/unsettled/blocked result. Run
  it again after creating the task to reconcile the result and verify its binding and
  `low` priority. It checks all pages of workspace tasks, freezes up to 20 records
  oldest first, and keeps the same identity across retries and newly arriving records.
- `route project|kandev|global ORIGINAL_TASK_ID` resolves the proposal repository.
  Project routing requires exactly one original-task repository, including archived
  tasks. Global and Kandev configuration findings do not require that binding.
- `acknowledge EXTERNAL_ID YYYY-MM/RECORD.md ...` commits handled names and self-skip
  reasons to the vault's `consumed.md`. Only acknowledge records whose proposals all
  succeeded or which produced no finding. The commit includes no observation files.

Run from any checkout, after this branch is deployed to the canonical project:

```bash
PACER_HEARTBEAT_WEBHOOK_URL= uv run --frozen --project /home/agent/prg/kandev-pacer python -m kandev_pacer.observation_consumer route kandev ORIGINAL_TASK_ID
```

The empty webhook URL applies only to this helper process. It avoids requiring the
webhook secret during import; this helper never invokes Heartbeat. Other pacer settings,
including the Kandev API URL, keep their configured values.

Creation state lives under `~/.local/state/kandev-observation-consumer/`. It is not a
second consumed ledger. Do not delete it to retry a timed-out create. An unsettled
creation or a closed task with unread batch records blocks replacement creation and
reports the task ID. Inspect and repair that task; never automatically release its
external ID. An interrupted ledger commit is recovered before its entries count as
consumed. Unrelated manual edits to the ledger stop the helper without overwriting them.

Malformed records remain unread and are reported. Self-records are never interpreted;
self-only input creates no task, preventing a consumer review loop. A future real batch
records their skip reasons. Record sidecars and legacy frontmatter remain unchanged.

The companion change in `~/prg/kandev-config` adds Heartbeat section 6 and
`prompts/observation-consumer.md`, committed as `1c0db21`. The latter sits outside the
generated `workspaces/` tree, which `export.py` deletes and rebuilds. Heartbeat embeds the full prompt and frozen
batch in a task bound to kandev-config on the writable Paced Kanban workflow. Proposals
may go to other repositories according to the original task and the finding's scope.
The agent reads each target's rules and writes only unreviewed proposal documentation.

Checks run during implementation:

- 375 focused tests passed after rebasing onto main's capacity-deferral changes,
  across the consumer, selection, runtime, two-subscription,
  Fable-window and collector suites. `uv run darker --check` and `uv run graylint` passed.
- Real Git fixtures covered ledger failures before and after publication, preservation
  of unrelated staged files, partial acknowledgement and repeated runs.
- The agent-run scenarios in `tests/fixtures/observation-consumer.md` used disposable
  repositories. Exact duplicate, similar merge, no finding, two target repositories,
  dirty target refusal, self-skip and proposal-before-ledger retry checks passed.
  These used synthetic evidence; they do not demonstrate model judgment on production
  observations or a production Heartbeat run.
- A read-only API check resolved this implementation task's repository to
  `/home/agent/prg/kandev-pacer`. A helper command run from `/tmp` succeeded using the
  worktree's package. A temporary export preserved the separate consumer prompt.
- The kandev-config apply dry run showed only the Heartbeat prompt addition. No live
  configuration was applied and no real observation was acknowledged in this step.
- Rebase conflicts in the tracker and appended tests were resolved using main's
  contents plus only this issue's move and new tests. Removing [16] from both tracker
  versions leaves byte-for-byte identical text. Both repositories have clean worktrees.

Deployment remains for the merge/deploy step. Deploy the pacer helper and ordering
first, then apply the kandev-config prompt change with its repository procedure. Verify
one real batch creates one low-priority consumer, repeat while open creates none, the
consumer commits a justified unreviewed proposal, and a fully consumed input creates
none. With a capped backlog, the next completed batch may legitimately create one new
consumer. Keep production proposal commits separate from this issue's tracker move.

## Implementation review, 2026-09-27

Two duplicate-suppression defects were reproduced and fixed. A change in the total
task count between API pages now stops preparation: a removal can move an unseen
consumer onto a page already read. A fully acknowledged pending batch is now retired
even when its Kandev task has been deleted, so the helper cannot recreate consumed work.
Uncertain or open tasks still retain their identity and suppress replacement creation.

All 377 focused tests pass after these fixes, as do `darker --check` and `graylint`.
Tracker verification found 29 issues, each under exactly one heading. Removing [16]
from this branch and main leaves identical tracker text. The companion configuration
commit was reviewed; production activation and the real-batch check remain deployment
steps as described above.

## Deployment and live verification, 2026-09-27

Merged the five feature commits with `--no-ff` as `e79b061`. The deployed tree passed
all 377 focused tests listed above; `darker --check` and `graylint` passed again.
Both pacer worktrees were clean after the merge.

Restarted the pacer in its existing tmux pane from `/home/agent/prg/kandev-pacer`,
keeping its private environment and pebble quota source. Restarted the development-tree
`kandev-learn.service`; its result was success with exit status 0, and its timer remains
active. After the helper and launch ordering were running, applied kandev-config commit
`1c0db21` through `uv run apply.py --apply`. The tool verified the live rows and export
against the repository; a second dry run reported no changes. Only Heartbeat's prompt
changed. Apply created backup `manual-1790470029183729266.db` and undo file
`~/.kandev/data/backups/kandev-config-undo-20260927T004709Z.json`.

Exercised the deployed helper and MCP create path manually with the first real frozen
batch of 20 records. This verifies the creation path, not an autonomous Heartbeat run.
It created task `809760aa-9423-4d58-9230-144f88337b84`, bound to kandev-config in
Paced Kanban, with external ID
`observation-consumer:4ccc6d0ed531cf231ac2c9ab03b08ae3ac9824aca0296ddaf4fdb3b43547e315`.
Two repeated preparations returned the same open task; API inspection found exactly
one consumer and verified its `low` priority and repository binding. `--dump-order`
put it last (position 70), with the expected explanation. Once the existing quota
collector refreshed its stale snapshot, the pacer launched it through the normal gate.
No quota bypass or forced launch was used.

### Live verification blocker

Consumer session `952942a3-377c-450a-943b-1ed33b06f26c` reported at
2026-09-27 01:04:56 UTC that its sandbox could not start Bash and automatic approval
review timed out twice on its required file read. It created no proposals or commits
and acknowledged no filenames. Read-only checks confirmed `consumed.md` was absent;
the task reached Review and retained low priority. A later helper preparation received
HTTP 503 from the task-list API and stopped without creating a replacement.

The deployment is active, but the real proposal/ledger check and autonomous Heartbeat
acceptance run remain unverified. Keep [16] In Progress until those checks pass. The
frozen batch and task identity remain intact; do not delete pending state or create a
replacement to work around the access failure. Restore the consumer's execution access,
resume the same task, then inspect its proposal commits and ledger entries. Re-run
preparation while it is open to verify suppression. The fully consumed-input/no-new-task
case passes focused fixtures; the production backlog still contains unread records.
