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
