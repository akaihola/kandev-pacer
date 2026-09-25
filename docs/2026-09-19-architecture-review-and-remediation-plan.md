Architecture review and remediation plan, 2026-09-19

Review ready for user review. This document authorizes no implementation, deployment, workflow change, or new task. Reviewed commit: b3450492e552b82235ab76d0126f78e9583a9e2c. Task: 63a0e64b-e791-44c0-a0dc-3e3242585e44. Session: 2cabadb2-a18d-400f-9329-26da22c079e6. Kandev metadata confirms Astra Medium and Priority Kanban. Repository and tracker files were left unchanged.

**Architecture and purpose**

Keep the present single-process architecture. This is a local admission controller for low-priority agent turns, with a separate archive collector and a small browser integration. Its purpose does not justify federation, a service framework, a general plugin system, or a scheduling rewrite. The material problems are inconsistent checks before spending a turn, lossy persistence identity, and weak integration tests between otherwise well-tested helpers.

| Area | Responsibility and boundaries | Evidence |
| --- | --- | --- |
| Entry points and packaging | Python 3.11+, uv_build, console commands kandev-pacer, make-paced, kandev-learn; python -m kandev_pacer delegates to runtime. Runtime dependencies are pydantic-settings and websocket-client. | pyproject.toml:1–20; src/kandev_pacer/**main**.py:1 |
| Configuration | Settings validates TOML, private env-file, process environment and CLI values. A mutable global settings object is initialized during import; runtime.main updates it after CLI parsing. | config.py:22,259,298; runtime.py:1396 |
| Workflow model | Board.scan enrolls workflows with work-tagged routing steps, derives queue/admission targets, profile overrides and ordering. lint_workflow diagnoses auto-start bypasses. Board is frozen at startup. | workflow.py:23–149,240; runtime.py:1426–1452 |
| Quota | provider reads local OAuth credentials and undocumented provider usage endpoints, or a Pebble JSON snapshot; direct reads have separate positive/negative caches. quota normalizes windows, splits model scopes and evaluates burn line/backstop/reopening. Credentials are read, never refreshed or written. | provider.py:177–269,305–520; quota.py:141–204,276–344 |
| Scheduling | Pacer reads board tasks, their sessions, agents and sidebar settings; selection applies drains-first, pull/manual order and warm-cache deadlines. Runtime gates providers and performs actions through client. Bookings, failures, stalls, pending signals and webhook cooldown are in memory. | runtime.py:220–265,342–410,549–660,738–1078; selection.py:75–278,387–546 |
| Kandev integration | HTTP reads and step PUTs, fresh WebSocket connections for launch, queue, state and completion actions, HTTP webhook for refill. There is no pacer-owned server. Request deadlines distinguish refusal from uncertain delivery, but some callers discard that distinction in accounting. | client.py:26–82,169–291 |
| Enrollment | make-paced computes a dry-run diff, removes auto-start actions, sets stage tags and supplies missing signal instructions. Applying sends one PUT per step. Partial application is explicitly recoverable by rerunning; unpace does not restore auto-start. | make_paced.py:47–170,185–204,269–313 |
| Progress heuristic | Git HEAD and dirty-state readings from session worktrees drive stall counting, blocking and optional completion signaling. Assumes local worktrees and that disk changes indicate task progress. | progress.py:33–95; selection.py:453–495; runtime.py:1016–1187 |
| Observations | kandev-learn reads Kandev SQLite with mode=ro and source deadlines, gathers five agents' transcripts, computes source-labeled measurements and publishes immutable Markdown with JSON frontmatter. Sweep state, locks and a commit journal support retries. Git uses a temporary index and compare-and-swap HEAD update, meaning it refuses to publish on top of a changed HEAD. | learn.py:209–525,527–911; learn_sources.py:41–88,280–707 |
| Browser queue | Userscript mirrors composer serialization, expands saved prompt references, resolves session identity and sends message.queue.add. Node VM tests cover serialization and primary-session lookup. | userscript/kandev-queue-button.user.js:28–363; tests/userscript.test.mjs |
| Analysis and vendoring | scripts/cache_waste.py wraps the packaged report. Collector reuses its parsing/attribution functions, not the cost simulation. Claude/Pi loaders are vendored at a recorded upstream revision and hashed into records. | cache_waste.py:1–56,253–376,458; learn_sources.py:19–20,641–707; vendor/README.md |

Representative paths traced end to end:

1. Normal admission: runtime.main → load_settings/Board.scan → Pacer.read → provider.read/windows_by_provider → gate_groups/verdict_for → select_action → attempt fresh reads → launch_target/pick_intent → client.ws_launch → booking/error log.
2. Queued prompt: composer document → buildPayload → queueTarget → queueMessage → Kandev queue; later queued_counts → select_action/drain_session → attempt → ws_drain → booking. Kandev's own auto-drain behavior remains an external limitation.
3. Closed gate and fallback: run_provider timer → candidate selection → launch fallback profile resolution → WebSocket payload. This path currently bypasses important normal-path checks.
4. Stalled task: worktree fingerprint → note_stall → give_up → optional mcp.step_complete → pending_signals reconciliation or BLOCKED.
5. Enrollment and withdrawal: workflow/step reads → plan_changes or plan_unpace → step_update → PUT. The running pacer's cached Board does not observe these writes.
6. Archive recovery: sweep watermark/pending IDs → write_task → collect_task and transcript adapters → render_record/publish → commit_records journal → state advancement. Formatting changes the identity checked on retries.

**Review coverage and checks**

Inventoried all 87 tracked files. Inspected every first-party production module, the used vendor loader functions and vendor provenance, all test-module areas and fixtures, the userscript and Node tests, build/lint/type/test settings and lockfile, the script entry point, README and AGENTS.md, TASKS.md, all 14 top-level task documents and all 12 consumer decision tickets. Reviewed current design/operations documents and the conclusions, boundaries and historical status of the three alternatives documents, federation proposal and prior review. Historical evidence was not treated as a statement about today's deployed services.

No tracked CI workflow, container deployment, service unit or release automation exists in this checkout. Deployment is described as a manually started pacer with Nix-managed configuration elsewhere; the collector timer is also maintained in nixos-config. The dated collector verification document records an activation handoff, not proof of current activation. See docs/tasks/1-quota-from-pebble-collector.md:78, docs/tasks/2-heartbeat-webhook.md:77 and docs/session-observations-verification.md, Deployment.

Checks were offline and non-destructive:

- Clean tracked working tree before and after review.
- Frozen uv setup could not complete because GitHub DNS was unavailable for the locked graylint Git dependency. Used the existing /home/agent/prg/kandev-pacer/.venv interpreter with PYTHONPATH pointed at this checkout; this validates source behavior, not a clean locked installation.
- Ordinary pytest first failed during settings import because the host TOML configured a webhook without an available secret. Overriding that URL alone produced 64 failures, 320 passes and 20 skips because other host settings still affected the tests.
- A bootstrap cleared PACER\_\* and KANDEV URL environment overrides and reset the imported global settings to Settings defaults before invoking pytest. Result: 383 passed, 1 failed, 20 skipped in 14.21 seconds. The remaining failure is tests/test_provider.py:305, Settings().quota_source is None rather than direct.
- Offline Node userscript test command passed. Whole-tree Ruff and ty checks passed.
- Coverage instrumentation reported 70% overall, with runtime 87%, selection 100% and workflow 100%. This is an approximate diagnostic, not a release threshold: settings was imported before coverage began, and retained vendor/report CLI code lowers the aggregate. The uncovered runtime branches include fallback and most runtime.main behavior.
- Temporary probes using FakeBoard, temporary SQLite/files/Git repositories and mocked network calls reproduced the defects below. No real task launch, queue mutation, credential read request, provider call, webhook, enrollment, vault commit or deployment ran.
- Temporary evidence: /tmp/pacer-review-probes.py, /tmp/pacer-review-isolated.txt, /tmp/pacer-review-pytest.txt, /tmp/pacer-review-ruff.txt and /tmp/pacer-review-ty.txt. These are session artifacts; the concrete results are recorded below so the plan does not depend on their retention.

**Prioritized findings**

C = confirmed by source plus a focused reproduction or existing failing test. R = architectural risk with a concrete path, but no production incident verified in this review. Effort is a rough implementation estimate including focused tests: S under one developer day, M one to three days, L several days. Change risk describes remediation risk, not severity.

| Rank / ID | Finding | Impact | Trigger likelihood | Effort / change risk | Dependencies |
| --- | --- | --- | --- | --- | --- |
| 1 / F1 | Single-provider selection bypasses provider/API-key policy | Unauthorized metered spend or wrong quota gate | High when mixed profiles use a single unscoped gate | M / medium | None; shares action resolution with F4 |
| 2 / F2 | Observation formatting breaks immutable identity and retry recovery | Collector retry stops; old records can be duplicated; distinct archives can collide | High on reread/retry of newly formatted records | M / medium, migration required | None |
| 3 / F3 | Reservations do not consume capacity; fresh eligibility is not rechecked | Excess concurrent launches and stale-state actions | Medium/high during delayed starts, timeouts or concurrent UI changes | M–L / medium | F1 action attribution |
| 4 / F4 | Fallback can bypass closed gates/caps and sends a profile ID as a step ID | Ungated spend, wrong launch routing, repeated refusals/blocking | Certain on affected branches when fallback is enabled; default off | M / medium | F1, F3 |
| 5 / F5 | Quota source selection is broken; ingestion loses scope and accepts unknown-empty data | Pebble never selected through configuration; outages or wrong gating | Validator defect universal; ingestion cases conditional | S–M / low-medium | None; repair before relying on Pebble |
| 6 / F6 | Dirty boolean is treated as proof of no progress | Active work blocked or prematurely signaled complete | Medium for work spanning multiple uncommitted turns or non-code tasks | M / medium | Fresh-state checks in F3 |
| 7 / F7 | Workflow enrollment/profile rules remain stale until restart | Unpaced boards still launched; edits do not change gate attribution | High whenever workflows change while pacer runs | M / medium | F1 action resolution |
| 8 / F8 | Import-time settings cross module/CLI boundaries; tests depend on operator config | Offline tools and tests fail before CLI overrides; regressions are obscured | Confirmed in this environment | M / medium | Small test isolation prerequisite first |
| 9 / F9 | Queue failures and browser async edits are not represented safely | Wrong prompt/session, lost draft text, hung Queue button, possible pacing bypass | Medium during network delay/session switching | M / medium | F1 for drain attribution |
| 10 / F10 | Log rotation and evidence consumers disagree | Size cap ineffective; incomplete/misleading evidence for tuning | Rotation requires volume; attribution gaps routine on uncertain launches | S–M / low-medium | F3 outcome representation |

F1. Confirmed selection bypass. runtime.py:759–764 marks the single-provider, single-class path plain, and :896 passes allowed=None. selection.py:153–156 interprets that as allowing every task. Pacer.read has already marked API-key tasks None under withhold, but that exclusion is lost. A fake board with metered task first, subscription task second and PACER_APIKEY_POLICY=withhold logged the metered task as withheld and then launched it. The same shortcut can let the subscription path select an API-key task under cap, outside its intended separate cap. Fix the authorization check, not just the log.

Related R: attribution is based on task/current-step metadata, not necessarily the session being drained or already running. selection.py:197–240 chooses the current step override, while launch_target at :176 can choose a different admission step; runtime.py:534 counts every session of a task against that task's proposed launch provider. next_action ignores its provider_of callback's session argument at runtime.py:572. A task with sessions from different providers, or an admission target with a profile override, can be gated/counted against the wrong subscription. Verify with fixtures for those exact shapes and supported Kandev profile precedence before changing compatibility behavior.

F2. Confirmed identity loss. learn.render_record at learn.py:527–537 applies formatted_record to the entire object before writing frontmatter. formatted_record at :666–688 truncates archived_at and removes timezone. write_task at :731–746 and sweep at :888–893 compare the stored value with the exact database value. Probe: 2026-09-19T10:00:00.123456Z was stored as 2026-09-19T10:00:00.123; a second write_task raised observation identity conflict. A crash after publication but before state advancement can therefore wedge pending work. record_path at :691–698 also truncates identity in filenames; two rearchives in the same millisecond can collide. Older full-precision filenames are not found by the new path lookup, creating a duplicate risk on explicit recollection/backfill.

This also contradicts the claim that readability changes leave the machine interface intact. Costs and timings in frontmatter are rounded, so digest aggregates rounded data. Existing sweep recovery tests replace write_task with fake_write, tests/test_learn.py:45–55,84–107, bypassing render_record. They miss the real serialize/retry boundary. This is a regression in completed task 4 plus the completed precision/readability items, not a new collector design.

F3. Confirmed accounting and revalidation gaps. running_of at runtime.py:534–547 counts visible STARTING/RUNNING sessions only; bookings are skip keys and log counts, not reserved capacity. With concurrency=1, no visible starts and two waiting tasks, two ticks launched a then b while the first booking remained. Within a tick, uncertain actions return launched={} at :1001 and :1063; :928 tests truthiness, so an uncertain send does not increment \_spent for the total cap. Most successful real launches are expected to time out at the refusal window, per client.py:255–263, making this important.

attempt at runtime.py:970–985 refreshes the task step and sessions, but launch at :1023 only compares identity. It does not rerun is_launchable. A probe changed the same session to RUNNING and wip_admitted to false after selection; the pacer still sent session.launch. Actual duplicate execution depends on server rejection/side-effect ordering, so that downstream effect is R; the unsafe request is C. Malformed JSON after a WebSocket send also escapes \_ws_call as a generic exception, client.py:219–231, and gets classified as a refusal, although the request may have acted. Restart loses reservations and there is no singleton guard. These are further R cases for recovery testing, already anticipated historically in docs/two-subscriptions.md:188–211.

F4. Confirmed fallback defects. The fallback branch at runtime.py:841–877 runs before both cap checks at :878–893, never checks the destination provider's verdict, and does not increment \_spent. For an existing session, launch skips profile substitution because only intent=start enters :1042, but still launches on the closed source gate. A probe with 99% usage and an idle existing session emitted workflow_step on that same session despite a closed gate and no usable fallback mapping. For a sessionless task, :1045 overwrites the workflow target variable with the fallback profile ID. Captured payload had workflow_step_id=pb and agent_profile_id=pb, where pb was the profile, not a step.

config.py:171–180 also misparses multiple multiword mappings. Luna low=Sonnet low, Astra Medium=Sonnet low becomes {'Luna low': 'Sonnet low, Astra', 'Medium': 'Sonnet low'}. tests/test_two_subscriptions.py:45 checks parsing with a single-word second source and has no fallback execution test. Task 8 explicitly limits fallback to intent:start; preserve that requirement.

F5. Confirmed quota boundary defects. config.py:219–224 validates quota_source but never returns value. Settings(PACER_QUOTA_SOURCE='pebble').quota_source is None, and provider.\_read_source at provider.py:481–498 silently takes direct mode. This defeats completed task 1's deployment premise and is caught by the existing default test once host config is isolated.

Once source selection is fixed, pebble_fetch at provider.py:243–264 carries label/duration/utilization but no scope. A 7d (Fable) snapshot becomes scope=None, so all Claude models inherit the Fable limit, reversing the scoped-gate improvement. Empty services produce agents=\[\] without errors; quota.provider_missing at quota.py:342–344 returns false when the whole provider set is empty. A probe of that envelope produced gate=open. A direct HTTP JSON object {} likewise becomes an apparently present provider with no windows and opens. Dropped unknown/partial windows can also loosen gating, provider.py:91–147,357–385 and quota.py:276–296.

Distinguish a verified, legitimate unused account from an unrecognized response. Do not require all historical window labels on every account. R: malformed one-provider values can throw outside QuotaUnavailable and stop both providers; future-dated Pebble snapshots pass the one-sided age check. Exercise these with fixtures before claiming independent provider failure handling.

F6. Confirmed weak progress evidence; unsafe inference is a risk. progress.worktree_state at progress.py:48–61 returns only HEAD and bool(status). Two different uncommitted file contents produced the same fingerprint. A dirty worktree can keep changing for several turns and still accumulate stalls. fingerprint at :80–95 silently excludes unreadable worktrees if another is readable. launch at runtime.py:1026 records a stall before knowing a turn was accepted, so attempts can also pollute the count. give_up at :1095–1146 can block, or signal completion based only on primary/waiting/signal-required checks. No disk changes do not establish that a research task, review or blocked task is complete. Task 3 already acknowledges the real-blocker risk. Retain its default block policy and explicitly revisit the strength of evidence before relying on signal mode.

F7. Confirmed stale enrollment. Board is documented as fixed for process lifetime, workflow.py:53–60, and only rebuilt in runtime.main at :1434. make-paced --unpace clears tags, make_paced.py:132–157, but Pacer keeps old scope/work_steps/profile overrides. Its fresh task read does not refresh workflow definitions. Therefore “no longer paced” at make_paced.py:305 and README.md:74 is not true for an already-running pacer. Reimport, admission-target edits and profile changes have the same stale-definition problem. Per-step PUT enrollment also has a partial-application window and can overwrite concurrent edits to events or an amended prompt, :185–204. That is a bounded R, not a reason to restore direct database writes.

F8. Confirmed configuration/test coupling. config.py:298 loads operator config during import. learn → learn_sources → cache_waste → quota → config couples archive inspection to pacing secrets, URLs and valid pacing settings. runtime.main's --config/CLI overrides happen too late to rescue an invalid default configuration. This review reproduced pytest import failure from an unrelated missing webhook secret; task 5 decision ticket 01 already documents the collector variant. Tests/conftest.py:318 replaces network seams but not operator settings, producing dozens of misleading failures under the host configuration. Pacer(config=...) also mixes injected values with global settings used by client/provider/selection. README's “pure” claim at :349 means no I/O in the functions, but imports and global dependencies still prevent true configuration independence. No checked-in CI catches the known quota-source failure automatically.

F9. Confirmed gaps, with user-visible failures requiring the stated timing. queued_counts at runtime.py:149–172 turns errors into an empty queue. select_action at selection.py:142–170 then launches a workflow prompt, despite its own explanation that doing so when a prompt is pending can cause an additional ungated turn. Task 6 repaired identity fields but not this failure policy.

In the userscript, onQueue at :321–334 captures a document, awaits several network calls and then deletes the entire current editor content. Edits typed while awaiting acknowledgement can be erased. queueMessage at :273–298 has no deadline or close-without-response handler and does not guard JSON parsing, so Queue can remain disabled indefinitely. queueTarget at :302–314 always chooses primary rather than the composer’s selected session. That was deliberately tested for task 7, but docs describe queuing on the session the user had open; decide the product contract explicitly. The code does not reject queuing while a turn is running, although docs/queueing-user-prompts.md:68–112 documents automatic, unpaced drain triggers. These are local safety and clarity repairs, not a browser framework rewrite.

F10. Confirmed logging/evidence mismatch. PacerLogHandler at runtime.py:77–86 adds a size trigger to a date-suffixed TimedRotatingFileHandler. In this Python 3.14 runtime, repeated size rollovers in the same date interval encounter the same existing backup destination. A temporary 20-byte limit probe retained all five records in a 110-byte active file and an empty dated backup. The advertised size bound is not enforced. Other Python versions need testing rather than assuming identical collision behavior.

cache_waste.pacer_launches at cache_waste.py:253–281 reads only the active file, ignores rotated files, and records launches only when tick.launched includes task and session. Uncertain launch ticks contain no identity, while runtime.note_uncertain at :1189–1206 has no session/provider fields. Normal delayed acknowledgements therefore disappear from this measurement path. window_records at quota.py:208–222 uses one alpha and does not encode inactive 5-hour windows, whereas quota_verdict uses alpha_5h/interactive_hours. A scoped class withheld tick is merged by provider in cache_waste.py:267–273; it can mark another class's wait as withheld. This weakens evidence for quota/cache tuning and immutable observations even if launch control is correct.

**Ordered remediation plan**

The first small step makes verification trustworthy. Then fix source selection and the spending boundary, restore collector retry safety, and only then improve operational evidence or tuning. Fallback stays disabled until it passes the same checks as normal launches. Each step is a reviewable change; implementation requires a separate authorization.

 1. Establish isolated regression execution and capture the failures, F8 plus F1–F5.

    - Change tests/conftest.py and configuration test setup so unit tests cannot load real credentials, webhook settings, provider selection or default operator TOML. Add fixtures at the settings source boundary, before importing runtime. Keep explicitly enabled live contract tests separate.
    - Add focused reproductions for policy filtering, multi-tick capacity, uncertain same-tick capacity, fallback payloads and actual render/write/retry. These should initially fail for the right reason.
    - Prerequisite: none. Benefit: a stable signal for the subsequent patches instead of accepting the historical “41 existing failures” as normal.
    - Acceptance: same offline results with empty settings and with a deliberately incompatible fake operator configuration; no network or real log writes. Baseline must expose the known quota_source failure.
    - Verification: full pytest plus Node tests; record interpreter/dependency versions. No production migration. Revert test isolation independently if necessary, keeping defect reproductions.

 2. Repair quota-source configuration and ingestion as one boundary, F5.

    - Return the validated source value. Preserve Pebble model scope; read/validate the snapshot once per tick and separate per-provider missing/invalid data. Validate finite nonnegative utilization and positive durations, timestamp format and bounded snapshot age/skew. Distinguish documented unused-window cases from unknown-empty/schema drift.
    - Touch config.py, provider.py, quota.py and source/contract tests. Update README quota-source instructions and add a dated follow-up to task 1 without rewriting its historical request.
    - Prerequisite: step 1. Benefit: the configured collector actually operates and cannot silently remove a binding limit.
    - Acceptance: TOML/env/CLI selecting pebble makes no direct-provider call; direct remains default; Fable data constrains only relevant model classes; an absent requested service withholds its work; malformed one-provider data leaves a healthy provider usable; valid unused accounts have an explicit tested policy.
    - Verification: integration from load_settings through provider.read and quota_verdict, including missing/all-empty, partial, unknown, invalid-number and future/stale snapshots. Upgrade live check must exercise the configured source, not always direct credentials.
    - Rollout/rollback: retain direct as an explicit operator alternative; verify source choice in non-secret startup output before enabling the existing collector configuration. Never silently fall back to direct on invalid collector data. No data migration.

 3. Make every action pass the same provider/policy and capacity checks, F1 and F3.

    - Remove the unfiltered plain selection shortcut. Resolve the actual intended target step/profile, selected session and provider/scopes before checking permission. For drains, use the holding session's billing/model identity; for running counts, use each running session's identity. Keep unresolved identity conservative.
    - Count visible running sessions plus unobserved reservations without counting a visible session twice. Represent “a request may have spent a turn” explicitly, independent of whether logging metadata is an empty dict. Apply both provider and total caps across actions and ticks.
    - Immediately before send, rerun session-state, admission, step, enrollment and profile checks from refreshed data. Treat protocol/decode failures after sending as uncertain. Keep server-side duplicate-start behavior as an additional safeguard, not the only safeguard.
    - Touch runtime.py, selection.py, client.py and their fake-board tests. A small resolved-action record shared by launch/drain/fallback is justified; do not split the application into services.
    - Prerequisites: steps 1–2. Benefit: no metered or closed-provider work escapes the policy, and latency cannot create extra launch slots.
    - Acceptance: single-provider withhold never selects API-key work; cap mode has the same accounting as multi-provider mode; unconfigured/unresolved providers do not slip through; mixed-session tasks use the correct gate; cap=1 stays one while starts remain invisible; uncertain first-provider action consumes the total slot; changed RUNNING/WIP/profile data prevents send.
    - Verification: sequence tests spanning several ticks and same-tick provider order, refusals versus lost/malformed replies, queue drains and session-incarnation changes. Validate profile precedence against a supported Kandev fixture or disposable instance before release.
    - Recovery: add a per-instance process lock and a conservative restart reconciliation strategy for unobserved launches. Prefer a small versioned pending-action journal or explicitly bounded startup hold after verifying Kandev's deduplication contract. Do not build distributed leases for one local process.
    - Rollout/rollback: one pacer process, stop/start once, retain pending-action state; rollback must not erase uncertain reservations or trigger immediate retries.

 4. Repair fallback on top of the unified action path, F4.

    - Keep workflow_step_id and fallback agent_profile_id in separate variables. Reject fallback for all non-start intents. Parse explicit comma-separated mappings with spaces inside names; reject malformed or ambiguous mappings and duplicate profile-name matches.
    - Resolve the destination profile before gating, then use its provider/scopes, API-key policy, reservation and both caps. An unavailable destination must withhold rather than resume the exhausted source session. Log source/destination profile, provider and outcome, including uncertain sends.
    - Touch config.py, runtime.py and tests/test_two_subscriptions.py; clarify task 8 and README fallback behavior.
    - Prerequisite: step 3. Benefit: the optional escape path cannot override the rules it exists to respect.
    - Acceptance: no fallback on existing sessions; exact step/profile IDs in payload; no launch with destination gate closed/blind/full; no same-tick total-cap overflow; multiword mappings round-trip; a safe destination starts once.
    - Verification: end-to-end fake WebSocket payload and accounting assertions, not parser tests alone.
    - Rollout/rollback: default remains off. Enable only after fixture and disposable-instance checks; rollback by disabling fallback, not by restoring the unsafe bypass.

 5. Restore exact collector identity and replay safety, F2.

    - Separate machine record serialization from human display formatting. Retain exact archive identity and measurement precision in frontmatter; format timestamps/numbers only in rendered prose. Use a schema version and collision-resistant identity-to-path mapping while preserving readable filenames.
    - Recognize existing full-precision filenames and the later truncated-format records before creating a new file. Add an identity index or compatibility lookup only as large as needed. Never rewrite immutable records.
    - For truncated identities, match against the database and task only when unambiguous. Record unresolved collisions for manual reconciliation; do not guess which archive a file represents. Keep pending sweep/commit journal data until safely resolved.
    - Touch learn.py, tests/test_learn.py and docs/session-observations.md; add dated correction notes to task 4/readability evidence and task 5's machine-interface assumptions.
    - Prerequisite: step 1 only; this is independent of scheduler steps and should ship promptly. Benefit: retries recover instead of blocking the observation pipeline, and digest totals retain source precision.
    - Acceptance: actual collect/render/publish/read repeated twice is a no-op; failure after publish, after commit creation and after HEAD update can all retry; old and new filenames do not create duplicates; distinct sub-millisecond archives do not collide; unrelated index/worktree files remain unchanged.
    - Verification: real temporary SQLite plus Git fixtures, with real render_record in the recovery path; mixed schema records and ambiguous migration cases; exact machine metrics versus rounded display.
    - Migration/rollback: back up state and journals, produce a read-only compatibility report first, keep old files byte-identical. Ship a dual-reader before new-format writes. Do not roll back to a reader that rejects the new schema while new records exist.

 6. Make stall evidence conservative, F6.

    - Compare bounded actual worktree change evidence, not just a dirty bit. Treat partial unreadability as unknown rather than “unchanged.” Tie increments to confirmed completed turns, not launch attempts, and to the selected relevant session/worktrees.
    - A byte-level diff/untracked-content digest with explicit bounds is one option; if bounds or reads fail, withhold the conclusion of no progress. Avoid an unbounded scan on every tick.
    - Keep block as default. Signal mode must either require stronger completion evidence available from Kandev or explicitly restrict its use to workflows where advancing after no changes is approved. No-code work alone must not be evidence of completion.
    - Touch progress.py, runtime.py, selection.py, progress/stall tests, README troubleshooting and task 3's risk/acceptance notes.
    - Prerequisite: step 3 fresh-state handling. Benefit: live work is not blocked or advanced because it has not committed.
    - Acceptance: different edits to an already-dirty file reset the stall count; failed sends do not increment completed-turn stalls; one unreadable repository prevents an affirmative no-progress conclusion; review/research fixtures do not signal completion merely for no disk change.
    - Verification: temporary Git repositories, multi-repo partial failures, blocked and no-code sessions, bounded scan failure, existing pending-signal cancellation cases.
    - Rollout/rollback: validate with diagnostics before enabling signal behavior; disable signal mode if uncertain. Resetting heuristic counts is acceptable, but retain pending remote-action reservations.

 7. Make workflow changes visible and enrollment operationally safe, F7.

    - Refresh enrolled workflow definitions on a bounded cadence and revalidate the candidate's workflow before spending. Withdraw work immediately when the fresh definition is not enrolled; discover new enrollment on the next successful scan. Fail closed for the affected workflow when its definition cannot be checked.
    - Re-read changed step fields before applying enrollment; detect edits made since planning when the API permits it. Retain minimal PUT bodies and the current retryable partial-apply design.
    - Touch workflow.py, runtime.py, make_paced.py and tests. Until refresh exists, correct help/README to require pausing/restarting the pacer around enrollment, unpace and reimport. Document the partial-apply recovery procedure.
    - Prerequisites: step 3 action revalidation. Benefit: opt-out actually stops new pacer starts and profile edits cannot leave stale quota attribution.
    - Acceptance: unpace while running produces no later launches for that workflow; changed admission/profile rules affect the next eligible action; unrelated workflows continue through one definition failure.
    - Verification: mutate fake definitions between ticks and between selection/send, partial PUT failures and concurrent prompt/events edits.
    - Rollback: documented stop/edit/restart procedure remains available; do not restore auto_start_agent implicitly. No database migration.

 8. Remove import-time configuration effects and enforce the repaired checks, F8.

    - Load configuration in each CLI entry point. Separate fixed vocabulary/pure math from settings construction so learn and cache analysis do not require pacer settings. Pass required settings explicitly at shared boundaries; remove the current mix of Pacer.config and global settings.
    - Ensure --help and --config work even when default operator configuration is invalid. Name unknown TOML keys and missing explicitly requested files instead of silently ignoring them. Keep private env-file precedence and permissions checks.
    - Touch config.py, runtime.py, client.py, provider.py, selection.py and the collector's quota-helper import boundary. Prefer a small dependency split over an abstract configuration framework.
    - Add a checked-in offline CI job or equivalent reproducible project check using the locked environment, Python support floor and current runtime, pytest, Node, Ruff and ty. Keep production live probes opt-in and make skipped required upgrade checks visible.
    - Prerequisite: step 1; stage this after behavior patches to avoid mixing structural movement with safety fixes.
    - Acceptance: learn digest/help and cache parsing import without pacer config; CLI overrides can repair defaults; independent Pacer configurations do not leak settings; the full suite passes from a clean checkout and from hostile fake operator settings.
    - Verification: subprocess CLI tests, two-config isolation, secret-valued validation errors that must not expose dummy secret contents, clean frozen install when network/cache permits.
    - Rollout/rollback: no setting renames or precedence changes unless explicitly documented; preserve command compatibility. Revert module movement separately from the behavior fixes.

 9. Make queue operations preserve user intent through failures, F9.

    - Carry unknown queue status separately from empty; withhold launch for the affected task/session until status is known, while allowing unrelated tasks to proceed.
    - Preserve task/session/incarnation identity across composer capture and acknowledgement. Choose and document whether Queue targets the selected chat session or explicitly the primary; label it if primary is intentional.
    - Add bounded fetch/WebSocket waits, close/error/decode handling and explicit uncertain acknowledgement behavior. Clear only an unchanged acknowledged draft, leaving later edits intact. Do not automatically resend after a lost acknowledgement without checking the queue.
    - Touch runtime.py, selection.py, userscript and Node tests; correct queue API documentation and task 6/7 follow-up notes. Warn or disable the pacer-specific Queue action in states the documented Kandev auto-drain behavior cannot pace.
    - Prerequisite: step 3 for drain attribution. Benefit: network delays cannot erase new text or turn an unreadable queue into an ungated workflow prompt.
    - Acceptance: edits during send survive; timeout/close/malformed response settles the button state; multi-session target is unambiguous; unknown queue status does not launch that task; other tasks remain runnable.
    - Verification: deterministic Node async tests plus browser fixture for editor/session switching; fake board queue-read failures. Verify current drain payload identity against a supported disposable Kandev contract before changing it.
    - Rollback: disable userscript injection and retain existing queued entries; no queue deletion or mass resend.

10. Repair observability, align ordering, and finish the operating documentation, F10 and lower-priority L1–L4.

    - Use a tested combined size/time rollover strategy with unique backup names and four retained backups; preserve existing logs when adopting it.
    - Log explicit accepted/uncertain/reconciled action identity with provider, session, target and scope. Consume active plus rotated logs, deduplicate, and tolerate a partially written final line. Record effective per-window alpha/active state and distinguish unavailable history from “not withheld.”
    - Make cache deadlines follow launch_session, not newest_session. Share the actual stable selection ordering with --dump-order or clearly label that command as base order and provide a separate read-only eligible-action explanation.
    - Correct current README/design claims listed below; add a concise architecture/operations map covering all three CLIs, state ownership, backup/recovery, single-process ownership, supported contract verification and deployment handoffs. Mark historical measurements as historical rather than deleting them.
    - Prerequisites: steps 3,5,8 for stable action/schema/config boundaries. Benefit: logs and diagnostics explain real decisions and can support later performance tuning.
    - Acceptance: multiple size rollovers in one date interval and timed rollover retain bounded files without losing records; delayed launches remain attributable after reconciliation; scoped/interactive-hour gate records agree with the verdict; primary-session deadlines and displayed order agree with selection.
    - Verification: tiny-threshold clock-controlled rotation tests, mixed old/new rotated JSONL fixtures, lost-ack reconciliation, scoped gates, archive observation attribution and primary/child ordering tests.
    - Migration/rollback: readers accept old log/record schema; retain backups and schema versions; disabling enhanced output must not alter admission. No rewriting existing observations.
    - Only after these checks pass, measure tick duration and API-call count on representative board sizes. Optimize repeated reads or introduce events only if measured latency warrants it.

**Documentation corrections and backlog reconciliation**

No tracker status moves or new task files are part of this review. TASKS.md currently has no items in Unverified proposals, Ordered backlog, Scheduled or In Progress; numbered tasks 1–14 are under Completed. Completion of earlier work does not prove every edge case is correct, and the previous tracker-maintenance decisions are preserved.

| Existing work | Review relationship and correction |
| --- | --- |
| Task 1, Pebble | F5 is a concrete repair to the completed source adapter. The documented scope mapping and missing-service behavior are not met. Do not create a competing quota-source design. |
| Task 2 and heartbeat waiver | Implemented. Uncertain launch outcomes currently lift cooldown through a different truthiness test than capacity uses; make the outcome contract explicit in step 3. Do not redo refill architecture. |
| Tasks 3 and 13 | Primary selection and signal reconciliation exist. F6 improves the quality of evidence; it is not a request to discard the accepted opt-in signal policy. |
| Task 4 and inline readability work | Collector exists; F2 repairs exact identity after formatting changes. Initial/final text is stored in bounded frontmatter but is not rendered as quoted per-session body sections; assess this small acceptance gap separately from the identity repair. |
| Task 5 and its 12 tickets | The decision map is complete; consumer implementation is explicitly handed off, not shipped. No consumed ledger or guaranteed last-place consumer scheduling is implemented here. Preserve the existing handoff rather than adding a second plan for it. |
| Tasks 6 and 7 | Queue get/add now carry task/session/incarnation identity. F9 concerns failure behavior and UI/session intent after that fix. Current drain contract was not live-verified. |
| Task 8 | F4 repairs violations of the completed intent:start-only fallback request. Keep default off. |
| Task 9 | Deadline ordering exists in select_action, but cache_deadlines uses newest rather than primary and dump_order prints base order. Rule 2 early release remains unimplemented and out of scope. |
| Task 10 | Private env-file layer exists. F8 removes import coupling while preserving permissions and precedence. |
| Task 11 | Interval default is 30. F10 repairs the combined rotation behavior and log readers; do not add a second rotation mechanism. |
| Task 12 | Gate tuning exists; F10 aligns logged window data with it. No evidence here supports changing the configured tuning values. |
| Task 14 | API-based enrollment is implemented. F7 improves freshness and partial-update operation; do not return to direct SQLite writes. |

Specific current-doc corrections:

- README.md:27 and docs/design.md:57 say a distant pause costs two ticks per hour. A 120-second maximum sleep is about 30 ticks/hour before I/O, not two. “Within five seconds” is only a sleep-policy bound; serial HTTP, queue WebSocket calls, worktree Git reads and refusal waits add latency.
- README.md:74 and make-paced output need the workflow-refresh/restart caveat until F7 ships.
- README.md:130–146 and runtime.dump_order disagree with cache-deadline-first launch ordering. docs/design.md:93–110 still says launches choose newest rather than primary.
- README.md:300-era cache discussion and docs/cache-aware-pacing.md's recommendation need to distinguish implemented deadline ordering from unimplemented early release. Do not interpret modelled savings as observed current improvements.
- docs/queueing-user-prompts.md:24,38 omits incarnation in add's example and says add does no session-existence validation, contradicting completed task 7 and the current userscript.
- docs/tasks/5-observation-consumer/issues/05-pacer-feed.md says the pacer reads task_usage_events every tick. runtime.read does not; SQLite usage reads belong to the collector/report. Correct with a dated note, preserving the decision against feeding archived costs into live scheduling.
- README layout omits learn, learn_sources, progress, cache_waste and vendoring. pyproject.toml:72 still says make_paced.py lives at the repo root.
- Keep docs/history/\* and docs/reviews/2026-07-25-post-review-fixes.md as historical evidence. Their old filenames, APIs and “24 tests pass” verdict do not establish current correctness. Keep the dated collector baseline results while adding the new isolated result rather than replacing history.

**Lower-priority findings and risks**

L1, cache ordering diagnostic mismatch, confirmed by source. runtime.py:581–606 uses the newest session's turn list while selection.py:56 chooses primary; runtime.py:1310–1335 never calls that deadline logic. Task 9's intended tie-break behavior should be tested with primary plus newer child. Whether the live sessions endpoint actually supplies turns or task_session_turns is unverified; if absent, deadline ordering silently has no effect. Verify with a recorded supported payload before adding a new endpoint.

L2, serial read cost, architectural risk rather than a measured outage. runtime.read performs one session HTTP read per scoped task, and queued_counts one new WebSocket connection per idle session. Each can wait up to its own timeout; runtime.run sleeps only after the tick finishes. A slow board lengthens effective cadence and snapshot age. The collector scans all task_sessions for overlap per task, learn.py:242–248, and scans Codex transcript files per session, learn_sources.py:431–442; vendored loaders read whole files. Source deadlines bound time but can permanently publish partial immutable records. Instrument before introducing concurrency or indexes. No throughput claim is made from this review.

L3, operational/security boundaries. client.py has no authentication mechanism for Kandev REST/WS; Origin is supplied from config, not an access-control credential. Treat its endpoint and local account as trusted. Verify listener/firewall/proxy configuration in the deployment repository; exposure was not checked here. Provider tokens and webhook secrets are sent only to configured/fixed destinations in the inspected code. Observation records intentionally retain bounded prompts/responses, repeated shell commands and verbatim blocker/signal text, learn.py:327–333,462–500. They belong in a private vault and Git history with an explicit retention/access policy. No exploit, credential disclosure or remote publication was observed. The collector's mode=ro database access, exporter copies, no-installer execution, path containment, atomic no-overwrite publication and temporary Git index are useful safeguards to retain.

L4, collector/runtime maintainability. runtime.run_provider is large because policy branches have accumulated, not because a framework is missing. Consolidate action authorization as part of the fixes. Keep the separation between measurement and interpretation. Retained vendor display code costs package/coverage volume but is not a priority; revision/hash provenance is more valuable than trimming it. The locked Git dependency is reproducible when its commit is available; an unavailable network/cache is a build prerequisite, not evidence that uv.lock is unpinned.

L5, order/fairness, hypothesis. Drains always outrank launches and warm tasks outrank cold tasks. A sustained stream can starve cold work, but no measured starvation trace was collected. Preserve the accepted policy until logs establish a problem. The consumer's “lowest priority” must eventually be enforced after cache preference as well as base order; reuse task 5's handoff for that work.

**Completion and limits**

The review and remediation plan are complete for user review. No fixes, tracker changes, deployments, new tasks or delegated sessions were created. Tests/probes used Bash with SHELL=/bin/bash.

This is a source-and-fixture review, not a production audit. Live Kandev/provider contract tests, real browser interaction, actual deployed Nix/service settings, credential refresh behavior, current external API documentation and production performance were not verified. Historical upstream claims remain dated evidence. The clean locked install was blocked by dependency-network access; the available Python 3.14 environment was used instead. No claim of a fully green suite or verified production recovery is made.

Start with isolated regression tests and the small quota-source repair, then close the shared action-authorization/capacity gaps and restore collector identity. Keep fallback disabled until its destination-gate and payload tests pass. These changes directly protect quota, operator intent and recoverability; broader scheduling or deployment redesign can wait.
