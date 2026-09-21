---
depends_on: []
---

# Fix userscript Queue button session identifiers after the 0.94-dev upgrade

## Original request

After the 0.94-dev upgrade, I am getting `Queue failed: task_id, session_id, and
session_incarnation_id are required` when I try to use the `Queue` button from the
userscript in task ff3350a2-60a3-48b1-9631-796e46e23e28. Investigate and fix.

## Related task

[Task 6](6-failed-queue-status.md) covers the pacer reading queue status. This
request covers the userscript writing to the queue through its Queue button.

## Recurrence investigation, 2026-09-21

The reported `task session identity is unavailable` error is reproducible with
userscript 1.1.2 from commit `1f86084` and the live REST response shape captured in
`tests/fixtures/queue/sessions.json`. That script reads `session_incarnation_id`,
but REST supplies `queue_incarnation_id`. The WebSocket request must send that
value as `session_incarnation_id`.

The live gogo endpoint `GET /api/v1/tasks/:id/sessions` supplies the identity.
Kandev's `TaskSessionSummaryDTO` and frontend `listTaskSessions()` confirm this
path. The Python client already uses it correctly. Repository version 1.1.3 fixed
the userscript field mapping in `a550b88`. Task 6's deployment record says the
browser installation could not be updated or verified. An outdated installed
copy explains the recurrence, but the user's installed version remains unverified.

Version 1.1.4 retains the mapping and primary-session selection order. It checks
HTTP status, the sessions envelope, string identities and task ownership before
opening the queue socket. Invalid identity preserves the prompt and restores the
button. A server rejection also preserves the prompt. No fallback queue mutation,
session creation, launch or drain is attempted.

Update the browser's installed userscript to 1.1.4 and reload the page using the
instructions in [Queue deployment](../queueing-user-prompts.md#queue-identity-after-094-dev).
No installed browser copy was changed, and no live prompt was added or drained.

### Review evidence

- Earlier 1.1.2 script plus the captured fixture reproduces the exact reported error.
- 28 Node tests pass, including full Queue clicks, API errors, invalid identity,
  primary-session selection, server rejection and existing composer serialization.
- Live session-list and queue-read contract tests pass, using read-only requests.
- `uv run darker --check`, `uv run graylint`, JavaScript syntax checks and
  `git diff --check` pass.
- Full pytest with default settings isolated from the host's personal configuration:
  456 passed, 20 skipped, 1 failed. The failure is
  `test_direct_quota_source_remains_the_default`: `Settings().quota_source` is
  `None` instead of `"direct"`. The identical test fails on an untouched `main`
  archive. Its validator lacks a return for valid values; quota code is outside
  this task's scope and remains unchanged.

### AI review, 2026-09-21

Rebased onto main `90278cf` to retain its newly scheduled test/lint issue.
Verified the tracker equals main byte-for-byte after moving only `[7]` to
In Progress. The moved issue occurs exactly once. Normalizing wrapped bullets
reveals one existing duplicate in both main and this branch: "Show Fable in
scoped gate logs even when it is the only class, and diagnose why waiting Fable
tasks are not launchable" appears under Scheduled and Completed. The requested
tracker-wide uniqueness proof is therefore blocked by main's existing state.
Removing that duplicate would violate the requirement to change only this issue.

Repeated the old-script reproduction and the current-script check against the
same fixture. All 28 Node tests and 222 focused Python tests pass, covering
userscript, API client, selection and runtime behavior. Both live read-only
contract checks, darker, graylint and JavaScript syntax checks pass. The unrelated
quota-default failure still reproduces; its source and test match main exactly.
No additional in-scope code defect was found.

Browser deployment remains unverified. The host's Chromium configuration contains
only `Crash Reports`, with no installed profile or extension manifest. The normal
Chrome and Firefox profile locations are absent. This does not establish which
userscript version is installed in the user's browser; the old-version diagnosis
remains supported by reproduction rather than inspection of that browser.
