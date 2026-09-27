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
`prompts/observation-consumer.md`. The latter sits outside the generated `workspaces/`
tree, which `export.py` deletes and rebuilds. Heartbeat embeds the full prompt and frozen
batch in a task bound to kandev-config on the writable Paced Kanban workflow. Proposals
may go to other repositories according to the original task and the finding's scope.
The agent reads each target's rules and writes only unreviewed proposal documentation.

Checks run during implementation:

- 337 focused tests passed across the consumer, selection, runtime, two-subscription,
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

Deployment remains for the merge/deploy step. Deploy the pacer helper and ordering
first, then apply the kandev-config prompt change with its repository procedure. Verify
one real batch creates one low-priority consumer, repeat while open creates none, the
consumer commits a justified unreviewed proposal, and a fully consumed input creates
none. With a capped backlog, the next completed batch may legitimately create one new
consumer. Keep production proposal commits separate from this issue's tracker move.
