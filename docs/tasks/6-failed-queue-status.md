---
depends_on: []
---

# `Queue status failed` after 0.94-dev upgrade

## Original report

The pacer fails to read queue status through `message.queue.get` and treats the
queue as empty. This is separate from
[task 7, the userscript Queue button failure](7-queue-button-session-identifiers.md),
which affects writing to the queue.

```
queue status failed for session b02d0f53-da34-4812-bf5a-255c63bb90cc (message.queue.get failed: task_id, session_id, and session_incarnation_id are required) -> treating as empty
queue status failed for session ff74dde1-3783-48a9-973c-c12c6b14e9ee (message.queue.get failed: task_id, session_id, and session_incarnation_id are required) -> treating as empty
queue status failed for session 72c48786-f009-4e61-a1ff-f951aee40a2b (message.queue.get failed: task_id, session_id, and session_incarnation_id are required) -> treating as empty
queue status failed for session 53ffebeb-c567-4490-b10a-524c03d0d84b (message.queue.get failed: task_id, session_id, and session_incarnation_id are required) -> treating as empty
queue status failed for session c2d62fd4-8bb0-43f6-84f6-fcb9f22ead01 (message.queue.get failed: task_id, session_id, and session_incarnation_id are required) -> treating as empty
queue status failed for session e09985f1-750f-4d51-84f4-fc2589baab12 (message.queue.get failed: task_id, session_id, and session_incarnation_id are required) -> treating as empty
[anthropic (Fable)] headroom=+0.106 open, nothing to launch (0/3 running, 11 held after a failed launch)
[openai] headroom=+0.733 open, nothing to launch (2/3 running, 11 held after a failed launch)
heartbeat: cooldown, retry in 1701s
```

## Investigation, 2026-09-19

The reported output combines a stale pacer process, an incomplete queue API
migration, and separate launch-capacity failures. Restarting alone is insufficient.
This is a proposal; no runtime code or running process was changed.

### Confirmed queue failures

- Pacer PID `2058240` started on September 14 at 20:06 EEST, from
  `/home/agent/prg/kandev-pacer`, under `uv run --frozen kandev-pacer`.
  Its output goes to `/dev/pts/9`; there is no `kandev-pacer` user service.
  Its editable installation points to that checkout. Python does not reload
  imported modules when a later merge changes their files.
- Queue fix `9ff6161`, merged as `0e34119` on September 18, added the three
  outgoing identifiers. The process predates this fix and still sends only
  `session_id`. A read-only replay of that payload reproduced the exact error.
- The merged fix also reads the wrong REST field. The live backend,
  `v0.94.0-224-gd2ef8fe21`, returns `queue_incarnation_id` in both session lists
  and session details. Queue WebSocket requests call that same value
  `session_incarnation_id`. These names must be mapped, not treated as identical.
- For reported session `ff74dde1-3783-48a9-973c-c12c6b14e9ee`, the REST field is
  present and the correctly mapped `message.queue.get` succeeds with count `0`.
  The current pacer instead raises `KeyError: 'session_incarnation_id'`.
- `client.ws_drain()` still sends only `session_id`. The backend requires the
  full identity for drain too, so repairing reads alone leaves prompt release broken.
- Userscript fix `1f86084`, merged as `e11ecf5`, makes the same REST-field mistake
  in `queueTarget()`. Its outgoing field name is correct.

The backend source checkout at `/home/agent/repos/ai/kandev` matches the live
version's commit. Relevant sources are `apps/backend/internal/task/dto/dto.go`
at lines 353 and 450, and
`apps/backend/internal/orchestrator/handlers/queue_operations_handlers.go`
at lines 92 and 343. Both drain and status validate the full identity.

### Past Kandev tasks

- `6e871e6b-45b5-44e4-86b6-ea4fa71e499e`, **Fix failed queue statuses**,
  reported deployment complete after merging `0e34119`, with no service restart.
  That did not update the already running terminal process.
- `b224e8f2-59b4-4d58-9115-bfdefdbc2c60`, **Fix Queue button session identifiers**,
  supplied the userscript fix and similarly reported completion from focused tests.
- `b15edec0-8143-4d83-9a52-e1079da858f0`, **Explain queued session launch
  cancellation**, distinguished Kandev's instance ceiling from pacer's per-provider
  and total limits. It also traced cancellation when a queued workflow destination
  changes. Raising the ceiling can reduce contention, but cannot repair queue identity
  or pacer's treatment of deferrals.

### Launch holds and heartbeat

The JSONL log contains 176 failure events since September 18 00:00 UTC through
the investigation's scan: 175 say `session ceiling deferred the workflow step launch`;
one says `session is not resumable: no executor record (state: CREATED)`.
Queue-read exceptions do not call `note_failure()` and therefore do not create
these records.

Kandev's `ceiling_seam3.go` persists a deferred workflow launch before returning
that error. `session_launch.go:673` propagates it as a failure, although other
launch paths can return `activation_disposition: queued` and
`activation_reason: session_capacity`. Pacer treats the error as a failed launch,
backs off, and can flag the task BLOCKED after repeated attempts. Kandev may already
own a queued launch for that task.

The displayed count is `len(self.failed)`, shared across providers, rather than
the number currently backing off in the displayed provider group. Expired records
remain until success or a task/session change removes them. Thus the two lines do
not mean eight blocked tasks, and may not mean four currently held tasks either.
The supplied excerpt has no timestamp or task IDs for those records, so the exact
four cannot be identified from it alone.

The heartbeat cooldown throttles attempts to refill the backlog. It neither retries
queue reads nor clears launch failures. Shortening it would not repair these defects.

### Proposed fix

1. Complete the queue contract migration in one patch. Read
   `queue_incarnation_id` from REST and send its value as
   `session_incarnation_id` for status, drain, and userscript admission.
   Reuse a small identity-payload helper for the Python queue operations; keep the
   fresh session read before drain. Never invent an incarnation or substitute the
   session ID. If the incarnation changes between selection and dispatch, skip and
   read again on the next tick.
2. Represent an unreadable queue as unknown. Withhold automatic launches for the
   affected task until its idle-session queues are known; continue pacing unaffected
   tasks. Treating an unknown queue as empty can launch a workflow prompt ahead of
   a queued user prompt. Aggregate repeated read errors rather than logging every
   session on every tick.
3. Handle capacity deferral separately from rejection. Prefer making Kandev's
   workflow-step launch return its existing structured queued response after the
   deferral is persisted. Pacer should recognize that response and the task's
   `metadata.deferred_launch.ceiling_deferred` marker, suppress duplicate launches,
   and reconcile on each fresh board read. Do not increment failure/stall counters
   or flag BLOCKED for waiting on capacity. A fixed booking timeout alone is
   insufficient because the backend queue can outlive it. For the current backend,
   confirm persisted deferral on a fresh read after the specific error; do not
   classify arbitrary errors by a loose substring match.
4. Report active backoff counts per provider/scope, with capacity-deferred work
   separate. Keep genuine rejection details and distinguish retained failure history
   from tasks actually being withheld. Decide explicitly how backend-deferred work
   counts toward pacer's committed capacity and heartbeat backlog before dispatching
   more work. Backend replay currently owns timing after deferral, so strict quota
   rechecking at replay would require a separate backend admission change.
5. Deploy only after the live read checks pass. Restart the actual terminal process
   using its existing configuration and credentials, and update the installed
   userscript. Log the startup source revision and process start time. Verify queue
   reads from the replacement process and exercise queue/drain on a designated test
   task. A merge or an absent systemd unit is not deployment evidence.

Keep the queue repair independently shippable. Capacity-response normalization is
a separate backend/pacer change; no need for a generic compatibility framework or
automatic hot reload.

### Verification and regression coverage

- Existing client, runtime, and selection tests: **175 passed** with user TOML
  configuration excluded. An initial run inherited the workstation's provider
  configuration and failed 56 tests; rerunning with isolated defaults removed those
  unrelated failures.
- Existing live `test_queue_get_answers_a_count`: **fails** with
  `KeyError: 'session_incarnation_id'` against the running server.
- Read-only old-payload probe: reproduces the reported validation error.
- Read-only correctly mapped payload: succeeds against a reported session.
- No live launch or drain was issued. Drain requirements were verified in the
  backend source matching the running build.

For implementation, use REST-shaped fixtures containing `queue_incarnation_id`,
and make the fake server validate all three identifiers for both get and drain,
including stale-incarnation rejection. Add userscript coverage for the real REST
shape, unknown-queue launch suppression, incarnation replacement before drain,
and deferral replay/cancellation without duplicate launches or BLOCKED flags.
Run the live queue-read contract test as a required deployment check, not only the
unit suite. Test per-provider hold counts with both expired and active records.

## Implemented queue repair, 2026-09-20

The implementation uses REST `queue_incarnation_id` for status reads, drains,
and userscript admission. Outgoing requests retain `session_incarnation_id`.
Missing identities are rejected before sending, and missing or malformed counts
remain unknown. Unknown queue reads withhold affected automatic launches, including
fallbacks; unrelated tasks and known queued prompts can proceed. The final launch
check also catches prompts added after selection. Drains compare the fresh queue
incarnation with the tick snapshot before sending. Userscript version is 1.1.3.

The status line now labels its count as global retained failure records. It no
longer claims those records represent currently held tasks for each provider.
Capacity deferrals still follow the existing failure path. Their separate fix
proposal above remains unresolved; this repair does not change capacity accounting,
heartbeat behavior, backend replay, or historical BLOCKED tasks.

Verification before the diagnostic-only change: 244 focused tests passed, including
the Node userscript suite. The live queue-read contract test executed and passed.
Diff-scoped formatting and lint passed. The broader suite had 408 passes, 20 opt-in
live-test skips, and one unrelated failure:
`tests/test_provider.py::test_direct_quota_source_remains_the_default` expects
`Settings().quota_source == "direct"`, but receives `None`. That failure also
reproduces in a clean archive of main at `b345049`; it was not changed here.

The added diagnostic regression also passed after the wording change, and final
diff-scoped lint passed.

Deployment remains pending. Reidentify the running process, preserve its configuration
and credentials, and account for in-flight work before restarting. Update the
installed userscript too. Verify queue reads from the replacement process, and use
a designated test task for any live add/drain smoke test. No production process
was restarted and no live prompt was added or drained during this implementation.

## AI review, 2026-09-20

Review found and repaired a fallback selection defect. With a closed normal gate,
a first task with a pending or unreadable queue was repeatedly selected and skipped,
preventing unrelated fallback work from running. Fallback now filters those tasks
before selection and still rechecks the selected task immediately before dispatch.
It does not drain user prompts through the closed gate.

Both new regression cases failed before the repair and passed afterward. All 247
focused tests, the live read-only queue contract test, and diff-scoped lint passed.
The tracker audit found 21 distinct issues, each under exactly one heading; only
issue 6 moved relative to main. Deployment and the separate capacity-deferral
proposal remain pending.


## Deployment, 2026-09-20

Merged the reviewed branch into `/home/agent/prg/kandev-pacer` with `--no-ff`
as `896001a`. Issue 6 is back under Completed. The only pre-existing dirty file
in the main worktree was `TASKS.md`, containing new backlog items and formatting
edits; those edits were preserved without committing or discarding them.
Consequently main cannot be reported clean. The task worktree was clean.

Restarted the terminal-owned pacer in tmux pane `%1`, using the documented private
environment file without exposing its values. Previous PID `494620` exited before
replacement PID `707856` started at 02:25:18 EEST. The replacement runs from the
main checkout, logs successful startup and pacing ticks without the reported
queue-identity error, and successfully called the configured heartbeat webhook.
The read-only queue contract test also passed from the merged checkout.

Userscript 1.1.3 is available in the main checkout. No accessible installed browser
profile or userscript manager was found on this host; browser-side installation
could not be verified or updated. Update the script in the browser before testing
the Queue button. No live user prompt was added or drained for verification.

End-user checks on a designated test task:

- After updating the userscript to 1.1.3, type a prompt and click Queue. Confirm it
  appears in the queue without an identity error. When the provider gate opens,
  confirm the queued prompt runs once.
- With fallback already configured and the normal gate closed, a pending prompt on
  the first task should stay queued while another eligible task can still launch.
- When historical failures exist, the pacing status labels their count as global
  retained failure records; it does not claim each provider has that many held tasks.

The separate backend capacity-deferral proposal remains unresolved. The existing
quota-default test failure was reproduced on untouched main during implementation;
no unrelated quota behavior was changed for deployment.
