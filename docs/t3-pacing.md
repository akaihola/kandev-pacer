# Pace Hai tasks in T3 Code

T3 mode reads local TASKS.md trackers through Hai's `hai_tasks` API and launches
eligible issues in isolated T3 worktrees. Kandev remains the default mode.

Install Hai alongside this checkout, then install the optional dependency:

```bash
uv sync --extra t3
```

The UV source points to `../hai`. Change that source path if Hai lives elsewhere.
Run the pacer on the same filesystem as T3. Tracker paths must match T3 project
workspace roots, and each tracker needs an explicit parent thread in that project.
The parent supplies the model, model options, runtime mode, and interaction mode.
Codex maps to the OpenAI subscription; `claudeAgent` maps to Anthropic. Other
providers withhold launches. Configure subscription-backed provider sessions in T3;
pacer cannot detect an API-key override hidden in a provider's environment.

Example configuration, with real paths and parent IDs substituted:

```toml
[kandev-pacer]
mode = "t3"
t3_url = "http://127.0.0.1:3773"
t3_trackers = '[{"tracker":"/home/me/prg/project/TASKS.md","parent_thread":"T3_THREAD_ID"}]'
t3_base_branch = "main"
providers_raw = "openai anthropic"
concurrency = 1
concurrency_total = 2
```

```bash
uv run --extra t3 kandev-pacer --config /path/to/t3-pacer.toml --dump-order
uv run --extra t3 kandev-pacer --config /path/to/t3-pacer.toml
```

`--dump-order` reads eligible issues without scheduling or launching them. Trackers
are considered in configured order. Within each tracker, Scheduled precedes Ordered
backlog. Dependencies must be in Completed / Accepted, whether accepted or awaiting
acceptance. Missing dependencies block work. Proposals and in-progress issues are
not admitted. Numbered references are resolved only within their own tracker.

Local authentication uses `t3 auth session issue`, renewing the five-minute session
before expiry. Set `t3_base_dir` if the CLI and server use different state directories.
Remote endpoints require `T3_THREAD_AUTH_TOKEN`, supplied through the environment.
Tokens and WebSocket tickets are never written into launch records. The protocol
matches T3 0.0.44. On 2026-10-04 the local CLI reported that version; the Gogo HTTP
endpoint was reachable, but its installed version could not be rechecked over SSH
because authentication was refused. No live task launch was used for verification.

## Scheduling and recovery

Scheduling requires a clean checkout on `t3_base_branch`. Pacer saves a private
record, moves the issue to Scheduled through Hai's revision-checked API, and commits
only the tracker. T3 then creates a worktree from that branch. The worker receives the
issue description, root AGENTS.md, tracker rules, and instructions to use Hai for
tracker changes. Implementation, validation, merge, and completion follow those
repository rules. A finished turn never moves the tracker or checks acceptance.

The existing quota gates, model-specific windows, and concurrency limits apply.
Starting/running turns, pending approvals/input, and active background work count
against capacity across the T3 server. Unrecognized state or active provider blocks
new work. These counts do not include a separate Kandev server; avoid two independent
pacers admitting work against the same subscription without partitioning capacity.

Records live in `~/.local/state/kandev-pacer/t3`, configurable with
`PACER_T3_STATE_DIR`. Use the same directory after restart. A process lock allows one
pacer to own it. Do not run another pacer for the same trackers with a different state
directory.

Each record contains the canonical tracker path, Hai issue ID, revision, endpoint,
exact command, thread ID, and observed worktree/state. An interrupted request queries
T3 first, then resends the same command ID only when the thread has never been observed
or acknowledged. An acknowledged thread missing from T3 blocks admission for inspection.
Idle, failed, waiting, and completed threads retain their record and never relaunch
automatically. Continue those threads manually when needed.

Tracker conflicts, including a crash between Hai's update and saving its new revision,
withhold the pending launch. Inspect TASKS.md, Git history, the saved record, and T3
before repairing that record. Do not delete records to retry: that discards duplicate
launch protection. Endpoint or tracker changes also require reconciling existing
records before reusing the state directory.

Run the integration fixtures with `uv run --extra t3 pytest tests/test_t3.py` and the
transport tests with `uv run pytest tests/test_t3_client.py`. The default suite skips
Hai integration tests when the optional dependency is absent.
