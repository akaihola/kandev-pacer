# T3 cutover rehearsal, 2026-10-18

## Cutover blockers

No cutover blocker was reproduced. All six scenarios passed on the disposable
v0.1.0 server built from [56]. No product fix is proposed.

The restart test covered a graceful SIGTERM restart with quota available,
not a crash, simultaneous recovery of several threads, or a quota gate closing
during downtime. Automatic continuation bypassed fresh pacer admission, but
remained counted as one paced thread. That distinction matters for the restart
recommendation at the end.

## First attempt, 2026-10-18 05:12–05:13 EEST

The first attempt stopped during worktree setup under the original instruction,
"stop if swap use grows". Swap rose from 3,627,272 to 3,703,516 KiB, an increase
of 76,244 KiB, while MemAvailable remained above 8 GiB. The source of that
increase was not established. No database was copied and no server, pacer or
provider turn started. All six scenarios were recorded as not run. The temporary
worktree and directory were removed.

Commit `5691f04` preserves that original report. The rerun below supersedes its
unverified outcomes and provisional recommendation. Main commit `a195a3e`
changed [57]'s stop rule to MemAvailable below 2 GiB or swap growth greater than
512 MiB from the start of the step. Rebase preserved both the new rule and the
first attempt's record.

## Rerun, 2026-10-18 08:06–08:17 EEST

### Versions and isolation

- Hook code `91cfe8521fe26753dd9fa34db4ba35c100147b43`, based on stable
  v0.1.0, `81493efb71e8e8244f95d3e9f71d8369654ab151`. Read [56]'s completed
  05:11 EEST update and the branch's pacing-hook README before running.
- Pacer executed from canonical main with an isolated virtual environment.
  Main before the final merge was `227a353`; its source, dependency manifest
  and lockfile have no changes from the first attempt's `d879ee2`.
- Temporary root `/tmp/pacer57-rerun-nrd3p4pg`, mode 0700. Dev server bound only
  to `127.0.0.1:41283`, with base directory `$ROOT/base`. The live endpoint was
  never contacted. No live process was restarted.
- Two real stopped threads were selected from the backup. Both had completed
  latest V1 turns, no active turn, no pending approval/input and matching pacer
  launch records. They were used only for reconciliation, never provider work.
- Provider turns used a throwaway Git repository at `$ROOT/scratch`, or its
  worktrees under `$ROOT/base/worktrees/scratch/`. Six provider turns in total,
  including the interrupted turn and its continuation. All remaining runs
  completed before cleanup.

Initial `free -m`:

```text
               total        used        free      shared  buff/cache   available
Mem:           11859        4222         169         104        7888        7637
Swap:          13055        3394        9661
```

A separate monitor sampled `/proc/meminfo` every second through setup, server
operation and cleanup. It would terminate only captured rehearsal process
groups if either revised threshold was crossed. A single baseline was retained
through the whole run, which is stricter than resetting it for each step.
No threshold was crossed:

```text
start_swap_kib      3474412
max_swap_kib        3682028
swap_growth_kib      207616  # 202.75 MiB
min_available_kib   6195560  # 5.91 GiB
```

### Setup commands and data preparation

Commands below use `ROOT` for the temporary directory named above. The actual
worktree creation used that absolute path. Dependency directories were links
to [56]'s already-installed checkout; there was no dependency install or full
typecheck of T3.

```bash
export SHELL="$(command -v bash)"
free -m
git rebase main
ROOT=$(mktemp -d -t pacer57-rerun-XXXXXXXX)
chmod 700 "$ROOT"
git -C /home/agent/repos/ai/t3code worktree add --quiet --detach \
  "$ROOT/t3code" 91cfe8521fe26753dd9fa34db4ba35c100147b43
```

For the root and each existing `apps/*/node_modules` and
`packages/*/node_modules`, Python linked the matching directory from
`/home/agent/.t3/worktrees/t3code/t3code-9f66b19e` into the temporary checkout.
Data directories were created normally, never linked.

The backup used Python's SQLite backup API with an explicitly read-only source:

```python
source = sqlite3.connect(
    "file:/home/agent/.t3/userdata/state.sqlite?mode=ro", uri=True
)
copy = sqlite3.connect(root / "base/userdata/state.sqlite")
source.backup(copy)
source.close()
```

Backup took **13.201 seconds**. Only the copy was pruned. Selected thread IDs
were `46718bef-79ae-494f-9e9e-93586ac7d86a` and
`f79c9366-cca4-4561-b65a-657c1d2dff35`. The pruning procedure was:

1. Disable foreign-key enforcement in the disposable copy.
2. Keep only their two thread IDs in every table with a `thread_id` column.
3. Keep only their project IDs in `projection_projects` and only events whose
   `stream_id` matches a kept thread or project in `orchestration_events`.
4. Empty every `auth_*` table, `orchestration_command_receipts`,
   `provider_session_runtime` and `projection_pending_approvals`.
5. Commit, vacuum and close the database. The final seed was **1,912,832 bytes**.

The copied launch records retained their original issue IDs, V1 commands and
thread IDs. Only their scratch copies' `url` and `tracker` were changed to the
dev endpoint and scratch tracker. Corresponding finished fixture issues were
placed under Completed / Accepted in the scratch tracker. No saved launch was
retargeted in live state.

The throwaway repository was initialized on `main` with two runnable issues,
"Reply REHEARSAL_ONE only" and "Reply REHEARSAL_TWO only", plus the two completed
import fixtures. Its instructions prohibited file changes, tracker updates and
messages to other threads. Both candidates were temporarily moved to Unverified
proposals after the order dump, allowing reconciliation and the migration hold
to be checked before a provider launch. Each was then released separately.
All tracker changes were committed so the scratch checkout stayed clean.

The server command was:

```bash
node "$ROOT/t3code/apps/server/src/bin.ts" \
  --base-dir "$ROOT/base" --host 127.0.0.1 --port 41283 --no-browser
```

First process startup to listening took **20.106 seconds**. The trace span
`server.startup.orchestration-v2.legacy-v1.import-shells` took **188.364 ms**.
The migration span took 235.025 ms. These timings are for the pruned seed,
not an estimate for the full live database. Background transcript hydration
completed about 1.3 seconds after listening:

```text
[08:09:24.678] Listening on http://127.0.0.1:41283
[08:09:24.973] Imported legacy v1 thread shells
  { importedThreadCount: 2, importedMessageCount: 3 }
[08:09:25.380] V2 orchestration recovery completed
  { terminalizedRuns: 0, stoppedSessions: 0, closedRequests: 0,
    retiredEffects: 0, requeuedEffects: 0 }
[08:09:25.983] Hydrated legacy v1 thread transcripts
  { importedThreadCount: 2, importedMessageCount: 12 }
```

A bearer token was issued with the dev checkout's CLI and captured without
printing it:

```bash
node "$ROOT/t3code/apps/server/src/bin.ts" auth session issue \
  --base-dir "$ROOT/base" --ttl 1h --label pacer57-isolated --token-only
```

Generated environments removed inherited `PACER_*`, `T3CODE_*`, `T3_THREAD_*`
and `VITE_*` overrides. They explicitly supplied the isolated base directory and
bearer token. `UV_PROJECT_ENVIRONMENT=$ROOT/venv` prevented changes to the live
pacer's environment. The effective scratch settings were:

```toml
[kandev-pacer]
mode = "t3"
t3_url = "http://127.0.0.1:41283"
t3_base_dir = "/tmp/pacer57-rerun-nrd3p4pg/base"
t3_state_dir = "/tmp/pacer57-rerun-nrd3p4pg/state"
t3_base_branch = "main"
providers_raw = "openai"
concurrency = 1
concurrency_total = 4
interval = 2
t3_model = { instanceId = "codex", model = "gpt-6.1-sol", options = { reasoningEffort = "low", serviceTier = "priority" } }
t3_runtime_mode = "full-access"
t3_interaction_mode = "default"
burn_state_path = "/tmp/pacer57-rerun-nrd3p4pg/burn.json"
reset_state_dir = "/tmp/pacer57-rerun-nrd3p4pg/resets"
log_path = ""
[[kandev-pacer.t3_trackers]]
tracker = "/tmp/pacer57-rerun-nrd3p4pg/scratch/TASKS.md"
```

Concurrency matches the live config. The actual scratch file also contained
`agent_providers_raw = "codex=api_key"`, an ineffective bypass spelling. The
real quota gate remained enabled and open, as the logs below show. No reset
was redeemed. The server environment initially used `/bin/bash`, which is absent
on gogo, and emitted nonfatal shell-initialization warnings. Use the resolved
Bash path shown above when repeating setup; on this host it is
`/home/agent/.local/bin/bash`. These setup mistakes did not prevent any scenario.

From canonical main, the actual pacer invocations were:

```bash
uv run --extra t3 --frozen --no-dev kandev-pacer \
  --config "$ROOT/config.toml" --dump-order
uv run --extra t3 --frozen --no-dev kandev-pacer \
  --config "$ROOT/config.toml"
```

### 1. Order dump and imported-record reconciliation: passed

The dump exited 0 and parsed as a JSON array with two candidates. It did not
schedule or launch either. Startup then observed both imported threads as
`idle`, with no active run. Both records remained `phase=observed`, without
`hold`. Neither imported thread acquired a new run during the rehearsal.

```text
T3 task-7 ... on f79c9366-cca4-4561-b65a-657c1d2dff35 (codex): status idle
T3 simple-cdf92e3d-8a75-4367-8a79-dd5652c55f7b ... on 46718bef-79ae-494f-9e9e-93586ac7d86a (claudeAgent): status idle
T3 [openai] headroom=+0.990 open; ... (0/1 paced), 0/4 paced total, 0 active
```

### 2. V1 submitted-record migration hold: passed

A scratch-only copy of a V1 record was changed to `phase=submitted`, given an
absent thread ID and issue ID `migration-submitted-fixture`, and placed in the
scratch state directory. Its `bootstrap` command was retained. The next tick
saved a tracker hold before scheduling or launching anything:

```text
phase: submitted
hold.scope: tracker
hold.reason: Saved V1 launch needs inspection before retrying on V2
T3 HELD tracker ... since 2026-10-18 08:11: Saved V1 launch needs inspection before retrying on V2; issue migration-submitted-fixture thread 46307dc3-bb76-4ca4-ba2a-08ec4c37adca
```

Only that synthetic scratch fixture was removed afterward. The next tick
released the hold. No live record was removed or edited.

### 3. Scratch issue launch, completion and observation: passed

Released issue `simple-7d1c8072049d84ed` launched thread
`55557fac-ff26-4921-825b-c71d6c129ebb` in an isolated worktree. Its first run
started at 08:11:54.958 EEST and completed at 08:12:00.646. The pacer saved
`phase=observed`, with `status=completed` and no hold.

```text
T3 launch accepted: 55557fac-ff26-4921-825b-c71d6c129ebb
T3 simple-7d1c8072049d84ed ... (codex): status running, run running
T3 simple-7d1c8072049d84ed ... (codex): status completed
```

The worker was deliberately instructed not to update the scratch tracker, so
its issue stayed Scheduled. The saved launch record prevented relaunch anyway.

### 4. Manual unpaced thread does not consume a pacer slot: passed

Before releasing that issue, `orchestration.launchThread` started manual thread
`1179349c-61a2-4dc9-9382-0de625cda8cd` in the scratch repository, asking it to
sleep 20 seconds and reply. Both threads were observed with `status=running`
and non-null `activeRunId`. With provider concurrency still 1, the pacer counted
only its own launch. The manual turn completed at 08:12:15.563.

```text
T3 [openai] headroom=+0.990 open; ... (0/1 paced), 0/4 paced total, 1 active
T3 launch accepted: 55557fac-ff26-4921-825b-c71d6c129ebb
T3 [openai] headroom=+0.990 open; ... (1/1 paced), 1/4 paced total, 2 active
```

### 5. API ellipsis prompt parks and receives admission: passed

`orchestration.launchThread` created scratch thread
`3b9a8d03-ebca-4680-b1d7-f53ee65eb696` with initial message
"Reply PARKED_DONE only. Do not run tools or change files. …". Before admission,
its projection showed `status=queued`, `pacing=held`, and shell `pacedRuns`
listed `run:thread:3b9a8d03-ebca-4680-b1d7-f53ee65eb696:ordinal:1`.

The pacer admitted it and persisted this exact command shape:

```json
{"type":"queued-run.admit","commandId":"pace-admit:run:thread:3b9a8d03-ebca-4680-b1d7-f53ee65eb696:ordinal:1:1","threadId":"3b9a8d03-ebca-4680-b1d7-f53ee65eb696","runId":"run:thread:3b9a8d03-ebca-4680-b1d7-f53ee65eb696:ordinal:1"}
```

```text
T3 paced run admitted: run:thread:3b9a8d03-ebca-4680-b1d7-f53ee65eb696:ordinal:1
```

The run completed at 08:12:19.590 and its admission record became `done`.
API calls used main's bearer-ticket flow and V2 WebSocket transport, not a
stand-in pacer.

### 6. Restart during an admitted turn: passed

`server.getSettings` confirmed the isolated defaults:

```json
{"continueThreadsAfterServerUpdate":true,"autoResumeLimitedThreads":false,"snoozeLimitedThreads":false}
```

Sent a follow-up to the first paced thread with `orchestration.dispatchCommand`:

```json
{"type":"message.dispatch","createdBy":"user","creationSource":"mcp","commandId":"<fresh UUID>","threadId":"55557fac-ff26-4921-825b-c71d6c129ebb","messageId":"<fresh UUID>","text":"In this disposable scratch worktree only, run bash sleep 35 once, then reply RESTART_DONE. Do not read or edit files, update trackers, or send messages. …","attachments":[],"dispatchMode":{"type":"start_immediately"}}
```

The first two harness requests omitted `createdBy`, then used an invalid
`creationSource`. Schema validation rejected both before creating a run. The
corrected request above parked and received pacer admission. These were request
construction errors, not product defects or additional provider turns.

Once run ordinal 2 was `running`, SIGTERM was sent to the captured dev-server
PID. Only after that process exited was a replacement started on the same port
and isolated base directory. The pacer was left running throughout.

```text
[08:13:05.770] V2 orchestration shutdown reconciliation completed
  { terminalizedRuns: 1, stoppedSessions: 0, closedRequests: 0,
    retiredEffects: 0, requeuedEffects: 0 }
T3 withheld: T3 HTTP ticket request to http://127.0.0.1:41283 failed: connection refused.
[08:13:26.737] Listening on http://127.0.0.1:41283
```

Replacement process startup to listening took 17.040 seconds. T3 automatically
created run ordinal 3, linked to ordinal 2. Projection timestamps, in EEST:

| Run | Result | Started | Completed | Restart link |
| --- | --- | --- | --- | --- |
| First scratch issue, ordinal 1 | completed | 08:11:54.958 | 08:12:00.646 | none |
| Admitted follow-up, ordinal 2 | cancelled by shutdown | 08:12:47.327 | 08:13:05.744 | none |
| Automatic continuation, ordinal 3 | completed | 08:13:32.163 | 08:13:58.508 | ordinal 2 |
| Second scratch issue, separate thread | completed | 08:14:01.289 | 08:14:05.646 | none |

There was no admission command for ordinal 3. Only the API-marker run from
scenario 5 and ordinal 2 had admission records; both ended `done`. Main still
counted ordinal 3 through the existing launch record's thread ID:

```text
T3 simple-7d1c8072049d84ed ... (codex): status running, run running
T3 [openai] headroom=+0.990 open; ... (1/1 paced), 1/4 paced total, 1 active
T3 simple-7d1c8072049d84ed ... (codex): status completed
T3 [openai] headroom=+0.990 open; ... (0/1 paced), 0/4 paced total, 0 active
T3 launch accepted: 84e8980b-6482-40f7-aad2-4c26a4cc6cbe
```

The second issue was released during restart recovery. It was not launched
until ordinal 3 finished. This checks capacity accounting after recovery as
well as observation of the resumed thread. No pacer restart was needed.

### Defects and cleanup

No product defect was reproduced. No source code or deployment setting was
changed. The shell-path and API-payload corrections above are sufficient for
repeating the harness; they do not require a product fix issue.

After all remaining turns completed, sent SIGTERM to only the captured dev
server and pacer process groups. Captured their descendant PIDs before stopping
them and verified that none remained running. The memory-monitor process also
exited. Verified that port 41283 no longer accepted connections.

Removed both scratch provider worktrees with `git worktree remove --force`,
then removed the temporary t3code worktree from its parent repository. Deleted
the whole temporary root, including both SQLite databases, WAL/SHM files,
private imported records, authentication token, logs, scratch config and virtual
environment. Verified the directory was absent and the t3code Git worktree list
had no registration for it. Only the sanitized measurements and excerpts in
this report are retained. The pre-existing [56] worktree was untouched.

No live database was opened read-write. The live config and launch records were
read/copied only. No live server, pacer or deployment was changed. Nothing was
pushed. Validation consisted of the real-server scenarios above, tracker
structure checks and `git diff --check`; no full typecheck was run.

## Restart-setting recommendation

Leave "Continue threads after restarts" enabled for the cutover. In this
rehearsal it continued previously admitted work, main counted it as one paced
thread, the next issue waited for that slot, and the pacer observed completion
without restarting. This replaces the first attempt's provisional disable
recommendation.

Continuation does not perform a fresh quota admission. If Antti wants a server
restart to force every interrupted task back through the quota gate, turn this
setting off instead and expect manual recovery of interrupted work. The run
above establishes correct counting for this graceful-restart case, not quota
revalidation or crash recovery. Keep `autoResumeLimitedThreads` and
`snoozeLimitedThreads` off, as they were during the test.
