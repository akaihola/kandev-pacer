# Pace Hai tasks in T3 Code

T3 mode reads local TASKS.md trackers through Hai's `hai_tasks` API and launches
eligible issues in isolated T3 worktrees. Kandev remains the default mode.

T3 mode needs Hai's Python package, `hai-tasks`, which provides the `hai_tasks`
module. This checkout tells uv to install it from a local Hai checkout. The setting
is in the root `pyproject.toml`, under the existing `[tool.uv.sources]` table:

```toml
[tool.uv.sources]
hai-tasks = { path = "../hai", editable = true }
```

The path is relative to the directory containing this `pyproject.toml`. `../hai`
means go up one directory, then into `hai`. For example, this layout works without
changing the setting:

```text
/home/me/prg/
  kandev-pacer/pyproject.toml
  hai/pyproject.toml
  hai/hai_tasks/
```

Point to the Hai repository root containing its `pyproject.toml`, not the
`hai_tasks/` module directory. If Hai lives elsewhere, edit only the `path` value
in that existing entry. For example:

```toml
hai-tasks = { path = "/home/me/prg/hai", editable = true }
```

Replace the example with your actual path. An absolute path also works from a T3
worktree, whose parent directory may differ from the main checkout's. A relative
path must be correct for each checkout where you run uv. `editable = true` makes
Python load Hai's code directly from that checkout, so edits there take effect
without reinstalling.

Run these commands from the kandev-pacer checkout root after checking the path:

```bash
uv sync --extra t3
uv run --extra t3 python -c 'import hai_tasks; print(hai_tasks.__file__)'
```

The second command should print a file inside the Hai checkout you selected.
`uv sync` updates the environment and, when needed, `uv.lock`. After changing the
source path, run it without `--frozen` so the lockfile can record the new source.
If uv reports that the source directory or its `pyproject.toml` is missing, check
the path and that Hai is checked out there. See uv's
[path dependency documentation](https://docs.astral.sh/uv/concepts/projects/dependencies/#path).

Run the pacer on the same filesystem as T3. Tracker paths must match T3 project
workspace roots. Each tracker needs a parent thread in that project unless all
model and launch settings are explicitly configured.
The parent supplies the model, model options, runtime mode, and interaction mode
unless configuration overrides them.
Codex maps to the OpenAI subscription; `claudeAgent` maps to Anthropic. Other
providers withhold launches. Configure subscription-backed provider sessions in T3;
pacer cannot detect an API-key override hidden in a provider's environment.

Example configuration, with real paths and parent IDs substituted:

```toml
[kandev-pacer]
mode = "t3"
t3_url = "http://127.0.0.1:3773"
t3_base_branch = "main"
providers_raw = "openai anthropic"
concurrency = 1
concurrency_total = 2

[[kandev-pacer.t3_trackers]]
tracker = "/home/me/prg/project/TASKS.md"
parent_thread = "T3_THREAD_ID"
```

Repeat `[[kandev-pacer.t3_trackers]]` for each tracker. A TOML array of inline
tables also works. `PACER_T3_TRACKERS` environment and CLI overrides still use
a JSON array; existing JSON strings in TOML remain supported.

Tracker configuration errors identify the entry using a zero-based index, so
`t3_trackers[1].tracker` means the second tracker table's `tracker` field.
Missing or empty paths, paths that cannot be resolved, directories, duplicate
trackers, and empty parent IDs produce specific startup errors. Raw invalid
values and unexpected exception details are omitted from logs because they can
contain credentials.

If `parent_thread` is omitted while launch settings are missing, startup lists
each missing setting and explains how to fix it. For example:

```text
T3 startup failed: t3_trackers[0]: Trackers without parent_thread require t3_model, t3_runtime_mode, t3_interaction_mode; set parent_thread to inherit these settings, or configure them explicitly
```

## Model and launch settings

Omit `t3_model`, `t3_runtime_mode`, and `t3_interaction_mode` to inherit each
setting from the parent. `parent_thread` is optional when all three settings are
explicitly configured, with either a fixed model or automatic model selection.
Without a parent, the tracker must still match exactly one project in T3.
For example, use a tracker table containing only `tracker = "/path/to/TASKS.md"`
with either of the complete model and mode configurations below.
To always use a specific model, add these keys to the
existing `[kandev-pacer]` section:

```toml
t3_model = { instanceId = "codex", model = "gpt-5.3-codex", options = { reasoningEffort = "high" } }
t3_runtime_mode = "full-access"
t3_interaction_mode = "default"
```

Use a model ID available on your T3 instance. `instanceId` must be `codex` or
`claudeAgent`. Model options are optional and belong to the selected model;
overriding the model does not copy options from the parent.

For automatic selection, configure the models the pacer may use:

```toml
t3_model = "automatic"
t3_models = [
  { instanceId = "codex", model = "gpt-5.3-codex", options = { reasoningEffort = "high" } },
  { instanceId = "claudeAgent", model = "YOUR_CLAUDE_MODEL_ID" },
]
t3_runtime_mode = "full-access"
t3_interaction_mode = "default"
```

Automatic mode chooses the available candidate with the greatest headroom on its
tightest quota window. This is the model whose subscription usage is furthest
behind the configured target burn line. Model-specific windows count too.
Candidates must pass provider, quota, and concurrency checks; if none pass,
the tracker stays untouched. Equal headroom uses the first configured candidate.
A provider with quota bypass enabled has a neutral score of zero. Include all
candidate providers in `providers_raw`.

Runtime modes are `approval-required`, `auto-accept-edits`, `auto`, and
`full-access`. Interaction modes are `default` and `plan`. These overrides apply
to new threads and their first turns. Saved launch records keep their original
model and modes when retried, even after configuration changes.

Environment equivalents are `PACER_T3_MODEL`, `PACER_T3_MODELS`,
`PACER_T3_RUNTIME_MODE`, and `PACER_T3_INTERACTION_MODE`. Model tables and candidate
lists use JSON in environment variables and CLI overrides, such as
`--t3-model '{"instanceId":"codex","model":"gpt-5.3-codex"}'`.

## Running

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
Non-loopback endpoints require `T3_THREAD_AUTH_TOKEN`, supplied through the environment.
Tokens and WebSocket tickets are never written into launch records. The protocol
matches T3 0.0.44. On 2026-10-04 the local CLI reported that version; the Gogo HTTP
endpoint was reachable, but its installed version could not be rechecked over SSH
because authentication was refused. No live task launch was used for verification.

## Shell command not found

If a pasted multiline command prints ` : command not found` and then the pacer
starts, check for a space after a line-continuation backslash. The backslash must
be the final character before the newline. With a trailing space, Zsh treats that
space as a command name and executes `uv` separately on the next line. This is a
shell error, so the pacer's error handler cannot explain it.

Use the existing secrets-file loader and a single-line command to avoid sourcing
the file and relying on backslash continuations:

```bash
uv run --extra t3 --frozen kandev-pacer --config ~/.config/kandev-pacer/config-t3.toml --secrets-file ~/.config/secrets/kandev-pacer.env --dump-order
```

Remove `--dump-order` when ready to run the scheduler. The secrets file must use
literal `KEY=VALUE` lines without shell quotes, `export`, or command substitution,
and have mode `0600` or stricter. The loader reads pacer settings only;
`T3_THREAD_AUTH_TOKEN` must still be exported in the process environment for a
non-loopback endpoint. See [connection setup](#connection-errors).

## Connection errors

The pacer first requests an authenticated WebSocket ticket over HTTP. A connection
failure at this point happens before it can inspect or launch a T3 thread. Check
the server endpoint and authentication before changing tracker or launch records.

Set `t3_url` in the `[kandev-pacer]` section of the file selected by `--config`.
`PACER_T3_URL` in the process environment or configured secrets file overrides TOML;
check those sources too. The default `http://127.0.0.1:3773` works only when T3
listens on loopback. A server bound to a specific LAN or Tailscale address needs
that address even when the pacer runs on the same machine.

For example, task [21] was investigated on 2026-10-04. The user's
`~/.config/kandev-pacer/config-t3.toml` selected `http://127.0.0.1:3773`, which refused
connections. `ss -ltnp 'sport = :3773'` showed T3 listening on `100.81.121.7:3773`,
and that address accepted connections. For that installation, use:

```toml
[kandev-pacer]
mode = "t3"
t3_url = "http://100.81.121.7:3773"
```

Keep the other settings in the existing section. For any non-loopback URL, including
this same-machine Tailscale address, the pacer requires `T3_THREAD_AUTH_TOKEN`.
On the T3 server's machine, issue a token using the running server's state directory
and pass it privately to the pacer:

```bash
export T3_THREAD_AUTH_TOKEN="$(t3 auth session issue --ttl 1h --label kandev-pacer --token-only)"
uv run --extra t3 --frozen kandev-pacer --config ~/.config/kandev-pacer/config-t3.toml
```

If T3 uses a custom state directory, add `--base-dir /actual/t3/state-directory`
to the token command. Explicit tokens are not renewed by the pacer; issue a fresh
one before expiry. With a loopback URL, the pacer issues and renews its own token;
set `t3_base_dir` or `PACER_T3_BASE_DIR` when that server uses a custom directory.

- **Connection refused** means no server accepts connections at that address and
  port, or a firewall actively rejects them. Check T3 is running and its bind address.
- **Hostname lookup failed** means the configured hostname could not be resolved.
- **Connection timed out** means the connection or response did not arrive in time.
  Check network access, firewall, proxy settings, and server responsiveness.
- **TLS verification or handshake failed** means HTTPS could not establish a trusted
  connection. Check the hostname, certificate, and local certificate trust.
- **HTTP 401 or 403** means authentication was rejected. Check token expiry and that
  the token was issued from the running server's state directory.
- Other HTTP failures need inspection of the T3 server or reverse proxy logs.

The error reports the endpoint origin and a safe reason. It omits raw exception
text, response bodies, tokens, and URL paths because these can contain secrets.

## Unknown providers

T3's current snapshots identify a model selection with `instanceId`; older snapshots
use `provider`. The pacer accepts both, preferring `instanceId` when present. It maps
the default instances `codex` to OpenAI and `claudeAgent` to Anthropic, preserving the
selection when launching a child thread. Custom instance IDs and other providers
are not mapped automatically because the pacer cannot infer their subscription.

An active thread with an unsupported or missing selection blocks launches across
the T3 server, including threads outside the configured trackers. The error names
the thread ID, selection field and value. Open that thread in T3 and inspect its
provider. Let its work finish or stop it intentionally, and resolve pending
approvals, input, and background work. These also count as active work. Use a
supported subscription-backed provider instance for threads that the pacer launches.

`--agent-providers-raw` and `--apikey-policy` do not register T3 provider instances.
Setting `hermes-acp=__api_key__` therefore
does not exclude an active T3 Hermes thread from the server-wide capacity check.
Configure subscription providers with `providers_raw` or `PACER_PROVIDERS`, and
select a supported provider instance in each configured T3 parent thread.

## Scheduling and recovery

If a tracker matches zero or multiple T3 projects, the pacer logs its path,
the required `workspaceRoot`, and the match count. The required root is the
resolved directory containing `TASKS.md`. It must equal exactly one project's
`workspaceRoot` in the server selected by `t3_url`.

For zero matches, open or add that directory as a project in that T3 server,
or correct `t3_trackers[].tracker` to the intended project's `TASKS.md`.
For multiple matches, inspect the projects and resolve duplicate workspace roots.
This blocks new task launches for the current tick. Existing T3 threads continue
running, and the pacer keeps reporting quota status and retries next tick.
Trackers processed before the mismatch can still launch work on other ticks.

Scheduling requires a clean checkout on `t3_base_branch`. The checkout is the
directory containing the configured `TASKS.md`, before T3 creates a worktree.
If it has staged, unstaged, or untracked changes, the warning names that directory
and tracker and provides a `git -C ... status --short` command. Inspect the changes,
then commit or stash work you want to keep. Include untracked files when stashing
with `git stash push --include-untracked`. Retry scheduling once status is empty.
A dirty checkout, or one on another branch, blocks only its own tracker. The pacer
logs a warning naming the checkout and moves on to the next tracker in the same tick,
so other repositories keep launching work.

Pacer saves a private record, moves the issue to Scheduled through Hai's
revision-checked API, and commits only the tracker. T3 then creates a worktree from that branch. The worker receives the
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


## Quota status and manual resets

Each tick reports every provider in `providers_raw`, including providers with no
tracker parent. Status includes headroom, running counts, scoped windows such as
Fable, and estimated resume times. A provider needs a tracker parent using it or
a configured model candidate to launch work. With inherited or fixed selection,
the pacer waits for that model's gate. Automatic selection can switch providers
when another configured candidate is available.

To spend a Codex reset without burn-line pacing, the Kandev workaround also works
in T3 mode:

```bash
uv run --extra t3 --frozen kandev-pacer --config ~/.config/kandev-pacer/config-t3.toml --agent-providers-raw 'codex-acp=__api_key__ hermes-acp=__api_key__' --apikey-policy cap
```

`codex=__api_key__` also selects this bypass. `claudeAgent=__api_key__` bypasses
Anthropic pacing. T3 instance names take precedence over the legacy `codex-acp`
alias. The override changes pacing only; T3 authentication stays as configured.
The log says `quota bypass, concurrency only`. Provider and total concurrency
limits still apply, including pending launch retries. With `--apikey-policy
withhold`, bypassed providers cannot launch. Remove the override to restore quota
pacing after using the reset. `hermes-acp` has no effect in T3 mode because Hermes
is not a supported T3 provider instance.
