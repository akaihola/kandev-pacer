---
depends_on: []
---

# Recover expired Claude credentials through Claude Code

## Problem and intended result

The pacer can stop Claude work when the temporary access token expires, even if Claude
Code could renew it without a browser login. Because the pacer stops launching work,
Claude may never run to perform that renewal.

After this change, the pacer should make a limited attempt to recover through Claude
Code. It must read fresh quota before deciding whether work may resume. Claude Code
remains responsible for renewing and saving credentials.

See the [research report](../claude-credential-recovery.md) for the apps involved,
alternatives considered, and evidence collected on 2026-09-18.

## First, prove the recovery method

1. Identify the Claude version and credential directory used by Kandev and the pacer.
   A different `HOME` or `CLAUDE_CONFIG_DIR` can mean a different login. Confirm that
   `PACER_CREDENTIALS` points to the credentials the recovery process will update.
2. On a controlled expired-token case, try a bounded Claude-owned status or usage
   operation that does not ask the model to do work. Do not assume `claude auth status`
   renews the login. Record the command, version, exit status, and expiry before and
   after, without recording tokens or account details.
3. If that cannot renew the token, validate a minimal model request with tools, hooks,
   plugins, and MCP connections disabled through supported options. It must not run
   inside an existing task conversation. Record that it consumes subscription usage.
4. Verify success by rereading credentials and successfully fetching quota. A command
   exiting successfully is not enough. Stop every child process when the attempt ends.

These tests may let Claude Code update its own login file. They belong to implementation,
not the completed read-only research. Do not force expiry by editing live credentials.
If no bounded method works safely, record the exact failures and leave automatic
recovery disabled rather than adding a custom token refresher.

## Implement the smallest recovery path

- Add an opt-in Claude credential recovery setting, disabled by default. Document the
  selected command, timeout, cooldown, and whether it makes a model request.
- Trigger recovery only for a diagnosed expired Claude credential. A network outage,
  missing file, or unknown quota failure must not launch Claude repeatedly.
- Allow at most one recovery attempt at a time for a credential directory, including
  across pacer instances. Use a finite timeout and a cooldown after failure. This
  coordinates pacer attempts; Claude Code still owns token renewal and its file locks.
- Keep the pacer's credential access read-only. Do not copy refresh tokens to a new
  store, call the token-renewal endpoint directly, or restore an old credential backup.
- After recovery, reread credentials and bypass the cached quota failure once. Resume
  tasks only if a fresh quota read succeeds and the existing pacing rules allow work.
- Keep other providers running while Claude recovery is pending. Terminate the recovery
  process and its children on timeout or pacer shutdown.
- Report distinct outcomes: recovery attempted, renewed and quota readable, cooldown,
  timed out, or login still required. Never include secrets or raw credential output.

## Handle the configured quota source

Support the direct source first. The existing Pebble source is a file reader, so a
generic missing or stale snapshot must not trigger login recovery.

Before deployment, check which source is active. If it is Pebble, explicitly choose
either direct quota reads with this recovery enabled, or a separately scoped collector
change. Do not claim that adding recovery to the direct reader fixes Pebble deployments.
Do not silently change the user's source or start a second recovery owner.

## Documentation and checks

Update the README troubleshooting guidance and the comments in `provider.py` that
claim expiry cannot cause a real outage. Make recovery logs clearly distinguish the
old error from the current result. Link to the research report.

Use fake credentials and subprocesses in automated tests. Cover successful renewal
followed by a fresh quota read, unchanged credentials despite a successful command,
failed renewal, timeout and child cleanup, cooldown, concurrent attempts, and continued
operation of other providers. Verify that a fresh quota result which still requires
pausing does not release tasks. Verify that Pebble failures do not invoke direct recovery.

Run the relevant existing provider, configuration, and runtime tests, plus the project's
required checks. Automated tests must not renew a real login or contact a model.

## Done when

- The chosen command is demonstrated on the deployment's Claude version and credential
  store, with redacted evidence recorded here.
- A controlled expiry recovers without manual login, and tasks resume only after a
  fresh quota check allows them.
- An unrecoverable login stays paused with a useful message and limited retries.
- No recovery attempt leaves background processes running or writes credentials from
  pacer code.
- The deployment's quota source and recovery setting are documented. Observe at least
  one idle period spanning token expiry; report the result rather than assuming success.

Rollback is to disable the recovery setting. The pacer then returns to its existing
behavior of withholding work when quota cannot be read. Do not roll credentials back.

## Implementation gate check, 2026-09-21

Implementation is blocked before adding recovery code. No expired credential was
available to demonstrate renewal, and no recovery command has been proven. The
checks below establish deployment facts, not successful recovery.

### Task record recovered

The main checkout at `77b998d` had neither issue [15] nor its description. The
preserved stash `a81b24c` contains the original Scheduled bullet; its untracked
tree `7dd86c8` contains this description and the research report. Main commit
`9add66f` restores only those items. The stash remains intact. The feature branch
was rebased onto main and moved only [15] to In Progress in `9aa14cd`.

### Redacted deployment evidence

Checks ran on gogo as `agent`. Host process inspection required execution outside
the agent sandbox; sandbox process listings did not show the deployed services.

| Check | Observed result |
| --- | --- |
| Standalone executable | `/home/agent/.local/bin/claude`, resolves to version `2.1.278` |
| Kandev adapter's executable | `@anthropic-ai/claude-agent-sdk-linux-x64/claude` in the installed npm adapter tree, version `2.1.257` |
| Other running Claude | Standalone version `2.1.269` also present |
| Running pacer source | `PACER_QUOTA_SOURCE=pebble` |
| Running pacer credential override | `PACER_CREDENTIALS` unset |
| Shared credential directory | `HOME=/home/agent`, `CLAUDE_CONFIG_DIR` unset in inspected pacer/Claude processes |
| Pacer TOML | No credential-path or quota-source override |
| Credential store | `/home/agent/.claude/.credentials.json`, OAuth access and refresh fields present; contents not recorded |
| Check time | `2026-09-21T15:23:23Z` |
| Recorded access-token expiry | `2026-09-21T16:00:35.179Z`, still valid, about 37 minutes remaining |
| `claude auth --help` | Offers login, logout and status; does not document a refresh-only command |
| `claude auth status --help` | Describes authentication status and JSON/text output, not renewal |
| `/home/agent/.local/bin/claude auth status` | Exit 0 in 1.68 seconds; access token and expiry unchanged; token still unexpired |

Version/help/status commands used a 10-second timeout, disconnected stdin, `/tmp`
as their working directory, and a separate process group. Status stdout/stderr
went to `/dev/null`. Each group was cleaned up and its direct child reaped. No
model request was made. No token, account details, credential copy, or raw status
output was saved. Pacer code did not write credentials.

### Why the proof cannot pass yet

An unexpired token and a successful status command cannot demonstrate renewal.
Active Claude sessions use the same store and may renew it before expiry. Editing
the expiry, copying the refresh token, stopping other sessions, or logging out
would alter a working shared login and is outside this experiment. A model request
with the current token would consume usage without proving expired-token recovery.
No post-recovery quota read or expiry-spanning idle observation is claimed.

The next implementation attempt needs a naturally expired credential in this
deployment's store and the bounded command experiment described above. Start with
a non-model operation. If that fails, validate the supported isolation flags before
trying the documented minimal model fallback. Record before/after expiry and a
fresh quota result before choosing the production command. Do not treat the npm
adapter version as interchangeable with the standalone version.

The active Pebble deployment remains a separate constraint. Direct-reader recovery
must not run on its snapshot failures. Do not silently switch its source or claim
this deployment is repaired by a direct-reader change.

No provider, configuration, runtime or README behavior changes were made. The
task remains In Progress and automatic recovery remains unimplemented.

### Checks for the blocked implementation handoff

Using `uv run --frozen --offline`:

- Focused provider, configuration, runtime, quota and two-subscription tests:
  246 passed.
- Full `pytest -q`: 483 passed, 20 skipped. Live tests remained disabled.
- `darker --check`, `graylint`, `ruff check src tests` and `ty check src tests`:
  passed.

These validate the unchanged code baseline, not the proposed recovery behavior.
`git diff --check` passed. Comparing the complete tracker to main after removing
the unchanged [15] bullet from both versions confirms that no other text differs.
The pre-existing Fable issue appears under both Scheduled and Completed in main;
it was deliberately left untouched to keep this task's tracker change to [15].

## AI implementation review, 2026-09-21

Result: blocker confirmed; the feature is not complete or approved for deployment.
The branch contains the status move and evidence only. The recovery command,
opt-in settings, process management, fresh-quota integration, README/provider
updates and new recovery tests remain unimplemented.

At `2026-09-21T16:57:48Z`, a read-only check found the shared credential's expiry
had advanced to `2026-09-22T00:33:18.333Z`. It was still unexpired. This confirms
that the earlier expiry time is no longer a usable test opportunity. It does not
identify the writer, prove renewal from an expired state, or validate a command
started by the pacer. The review did not invoke Claude, make a model request,
or change credentials. The proof-first gate remains unmet.

The restored description and research report were compared byte-for-byte with
stash tree `7dd86c8`; both match. The tracker audit normalized wrapped bullets
and inspected all issue sections. Issue [15] occurs exactly once, under In
Progress. Removing its unchanged bullet from both tracker versions leaves
byte-identical text. The only duplicate found in both main and the branch is:

- `Show Fable in scoped gate logs even when it is the only class, and diagnose
  why waiting Fable tasks are not launchable.` Under Scheduled and Completed.

Therefore the requested whole-tracker uniqueness proof cannot pass against this
baseline. Removing that unrelated duplicate would violate the requirement that
every other issue retain main's text. This review leaves it unchanged and records
the conflict rather than claiming a passing audit. No duplicate was introduced
by this branch.

Review rerun: all 246 focused provider, configuration, runtime, quota and
two-subscription tests passed. `git diff --check` passed after the review note.
These checks cover the existing implementation, not the missing recovery feature.
