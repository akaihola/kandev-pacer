# Gather acceptance evidence for the 34 review-queue items

Steward issue, 2026-10-17 (decisions D7, D10 and D37 in
`/home/agent/my-knowledge/agents/steward/decisions.md`). It takes over vault
[19] and the kandev-pacer part of vault [17], whose tracker is blocked. The
verdicts feed the steward's next digest, so that Antti can accept or reject
these items with less checking of his own.

## Items

Every `- [ ]` item under `## Completed / Accepted`:

- 28 legacy bullets whose Hai metadata has no `updatedAt` (it starts with 1970
  in `hai-tasks read`). They predate Hai timestamps, so judge them against
  today's `main`: find the commits that delivered each one (`git log -S`,
  `--grep`, its description under `docs/tasks/`) and check that the behaviour
  still exists. An item superseded by later work is "would accept" only when
  the later work covers its request; name the issue that superseded it.
- [49], [50], [51], [52] and [53]. For [49], [51] and [52], the pre-cutover
  review is the evidence for criterion 1:
  `git -C /home/agent/my-knowledge show 606b679:"agents/steward/inbox/2026-10-17 kandev-pacer V2 cutover review.md"`.
  Check criteria 2–4 for them.
- [54] rewrote history, so it is left to Antti; record its evidence anyway.

## Work

Run the full offline suite once on current `main`
(`uv run --extra t3 pytest -q`) and a targeted test wherever an issue names
one. Check criterion 3 with `git revert --no-commit` of an item's merge commits
in a temporary worktree (`git worktree add --detach "$TMPDIR/revert-check" main`,
removed afterwards); conflicts only in `TASKS.md` and `docs/tasks/` don't count.

For each item, record one line: issue, a one-line outcome, the evidence
(commits, files, test command with pass and fail counts) and a verdict under
`/home/agent/my-knowledge/agents/steward/acceptance-policy.md`:

- **would accept:** all four criteria hold; give the revert command;
- **left to Antti:** name the criterion or the "Always left to Antti" class;
- **not done:** say what is missing.

## Deliverable

A section `## Evidence, YYYY-MM-DD` in this description file, with the lines
grouped by verdict and the counts per verdict at the end.

## Ground rules

- Change no other issue's state or text, and never change `[ ]` to `[x]`: only
  Antti accepts.
- Don't push, and don't touch the vault or the running pacer.

## Acceptance

- Every listed item has a line with evidence and a verdict.
- Nothing outside this file and the issue's own tracker moves changed.

## Evidence, 2026-10-17

Checked against `main` at `2ba0930`. All hashes are the ones after the [54] history
rewrite. The pre-cutover review cites the old ones. Its [49] commit `05986f7` is now
`8deba37`, and its base `1a444a5` is now `f75c66f`; its [51] and [52] commits are now
`713b1d9` and `f10a156` (matched by subject).

How the checks ran:

- Every pytest run used an empty home (`HOME=$TMPDIR/home
  XDG_CONFIG_HOME=$TMPDIR/home/.config`), because the real
  `~/.config/kandev-pacer/config.toml` changes the test settings. Counts include
  parametrised cases.
- Full offline suite, `uv run --extra t3 pytest -q`: **7 failed, 935 passed, 20
  skipped**. All seven are [52]'s own regression tests
  (`test_unreadable_tracker_skips_only_itself_and_retains_records`, five cases, and
  `test_startup_and_dump_order_skip_unreadable_tracker`, two). Six fail because Hai
  `abbb1ac` ("read Prettier tracker layouts", 2026-10-17 17:17) now reads the wrapped
  link definition (`[1]:` with the path on the next line) that the tests use to make a
  tracker unreadable. With Hai exported from `abbb1ac^`, those six pass. The seventh,
  `test_startup_and_dump_order_skip_unreadable_tracker[False]`, depends on timing. To
  stop `run()` after one tick, it replaces the global `time.sleep`, but `subprocess`
  waits inside the tick call `time.sleep` too (11 calls in one tick at `concurrency =
  1`). It passes with gogo's real config (concurrency 3, total 4), and it also failed
  for [54]'s worker.
- I checked criterion 3 with `git revert --no-commit` in a detached worktree of `main`,
  one item at a time. Where a revert conflicted only in `TASKS.md` or `docs/tasks/`, I
  kept main's version and reverted the next commit. "Code conflicts" means conflicts in
  any other file.
- Following the 2026-10-17 calibration, the seven failures count only against [52],
  because they are its tests.
- I read the deployment class as a deployment still to do or never verified. A restart
  the worker already did and verified ([17]) does not put an item in this class. Under a
  stricter reading, [17] moves to "left to Antti".

### Would accept (6)

- [50] Tests use Hai's `sourceState` field. Done. The 8 tests that broke when Hai
  removed the `sourceSection` alias pass. Evidence: `42ba84a` test: use Hai sourceState
  field (only `tests/test_heartbeat.py`, `tests/test_t3.py`); `pytest
  tests/test_heartbeat.py::test_numbered_dependencies_accept_unaccepted_completion` plus
  the seven `test_t3.py` tests named in the issue → 8 passed; `grep -rn sourceSection
  src tests` → no matches. Its "full suite passes" criterion held after it landed (904
  passed at [49], 942 at [53]); today's 7 failures are [52]'s tests, broken by a later Hai change.
  Revert: `git revert 42ba84a` (clean).
- [51] Only paced threads count against `concurrency_total`. The pre-cutover review
  found it ready after checking interactive threads, steward runs, user input and
  background tasks. It stays in scope. The issue asked for the optional
  `t3_active_limit`, which is unset by default, plus the status line, docs and tests.
  Nothing is lost, since `t3_active_limit` can still count every active thread.
  Evidence: `713b1d9` fix(t3): count only paced threads toward caps
  (`src/kandev_pacer/t3.py`, `src/kandev_pacer/config.py`, `docs/t3-pacing.md`,
  `tests/test_t3.py`); its five tests (`test_interactive_threads_leave_two_paced_slots`,
  `test_optional_all_active_limit`, `test_mixed_providers_count_only_paced_threads`,
  `test_active_limit_reserves_accepted_parked_run_until_visible`,
  `test_all_active_limit_requires_positive_integer`) → 13 passed. It goes live only when
  Antti restarts the pacer. Revert: `git revert 713b1d9` (clean).
- [53] Unresolved V1 launch records are held before quota and capacity checks. Done.
  This fixes the review's finding F1. Evidence: `3daa054` fix(t3): hold V1 launches
  before admission (`src/kandev_pacer/t3.py`, 14 lines, plus `docs/t3-pacing.md`,
  `tests/test_t3.py`); `pytest tests/test_t3.py -k
  'saved_v1_launch_is_held_before_v2_dispatch or
  imported_v1_launch_is_observed_without_relaunch'` → 18 passed; the same tests with
  `t3.py` from `3daa054^` → 12 failed, 6 passed, which confirms the worker's claim that
  the 12 absent-thread cases catch the defect. Revert: `git revert 3daa054` (clean).
- [*] Observation series index from `kandev-learn sweep`. Done. Each sweep that writes
  new records rewrites `index.md` and commits it with them, reading the JSON sidecar
  when present and legacy frontmatter otherwise. The live sweep rewrote the vault index
  this way on 2026-10-04. Evidence: `5ded9f2` feat(learn): write the observation series
  index during sweep, merge `ead5643`; `write_index()` and `index_row()` in
  `src/kandev_pacer/learn.py`; `test_sweep_writes_index_with_records` and
  `test_split_sweep_recovers_publication_and_commit` → 2 passed. The vault's old
  `scripts/kandev-observations-index` still exists; the bullet doesn't ask to delete it.
  Revert: `git revert -m 1 ead5643` (conflicts only in `TASKS.md`).
- [17] `kandev-learn sweep` no longer parses every Codex transcript in full. Done and
  deployed. The sweep streams transcripts line by line, skips files older than the
  session by mtime and drops a Codex rollout from another task at its `session_meta`
  line, and `ccusage` sees only the task's own rollouts. Evidence: `18fa5e1` fix(learn):
  stream transcript session windows, `1c6950b` fix(learn): scope Codex usage scans to
  owned logs, `588c08a` (deployment record); `transcript_window()` and
  `collect_transcripts()` in `src/kandev_pacer/learn_sources.py`; the 8 tests from those
  commits in `tests/test_learn_sources.py` → 27 passed; recorded MemoryPeak 548.58 MiB,
  then 479 MiB on the next timed run (cap 1 GiB). Its done-when included the gogo timer
  restart, which the worker did and verified on 2026-10-03 (see the deployment class
  note above). Revert: `git revert 1c6950b 18fa5e1` (clean).
- [5] Observation consumer decision map. Done as a decision. All 12 tickets are
  resolved. The 7 that needed Antti record "Decided by Antti" (2026-09-13 to 09-18).
  "Not yet specified" lists nothing, there is no **Needs a decision** marker, and a
  three-part implementation handoff exists. The implementation is [16], still In
  Progress, which the tracker rules allow. Evidence: `b30fbf7`, `fa28409`, `cca1dc7`,
  `065d198` and `5f70252`, which touch only `TASKS.md` and
  `docs/tasks/5-observation-consumer*`; no tests (a decision only). The work also
  created `TASKS.md` and an `AGENTS.md` pointer in kandev-config (`637720b` there),
  which ticket 10 records as Antti's request. Revert: `git revert 5f70252 065d198
  cca1dc7 fa28409 b30fbf7` (tracker files only) and `git -C ~/prg/kandev-config revert
  637720b`.

### Left to Antti (23)

Criterion 3 is the only obstacle for 11 of these legacy items: the palindromi.fi launch
fix, T3 model selection, Fable gate logs, [13], [12], the identity-conflict message,
step names, the filename UUID, session IDs, shorter values and [6]. Each one's later
work edits the same lines, so its own revert conflicts in code. If Antti waives
criterion 3 when the conflicts come only from later work, these 11 become "would
accept".

- [49] Port T3 mode to T3's V2 protocol. The pre-cutover review found it ready after F1,
  which [53] fixed. Evidence: `8deba37` feat(t3): admit paced runs over V2, merge
  `442e30b` (`src/kandev_pacer/t3.py`, `src/kandev_pacer/t3_client.py`,
  `docs/t3-pacing.md`, `docs/t3-pacing-hook.md`, tests); the worker's end-to-end run
  against a patched T3 dev server is recorded in this issue's description; the review's
  command `pytest tests/test_t3.py tests/test_t3_client.py` → 223 passed, 7 failed (all
  seven are [52]'s tests). Reverting `git revert -m 1 442e30b` alone gives code
  conflicts in `docs/t3-pacing.md`, `src/kandev_pacer/t3.py` and `tests/test_t3.py`.
  Reverting the stack newest first (`git revert 3daa054 f10a156 713b1d9`, then `git
  revert -m 1 442e30b`) conflicts only in `TASKS.md`. Class: worker reported V2
  failed-workspace cleanup as unverified (the request already called it unknown);
  criterion 3 holds only together with [51]–[53].
- [52] An unreadable tracker skips only itself. The pre-cutover review found it ready.
  The behaviour still works with current Hai: in a probe outside the repo that removed
  the link definition (which Hai still rejects), both a running and a restarted pacer
  logged `skipping unreadable tracker …` and launched the healthy tracker. But all 7 of
  its own regression tests fail on today's `main` (see above). Evidence: `f10a156`
  fix(t3): skip unreadable trackers independently (`src/kandev_pacer/t3.py`,
  `docs/t3-pacing.md`, `tests/test_t3.py`); its two tests → 7 failed with current Hai,
  and 6 passed, 1 failed with Hai from `abbb1ac^`. Revert: `git revert f10a156` (clean).
  Criterion 1, because the item's own tests fail.
- [54] Remove the tailnet hostname from all files and from history. Done as recorded.
  Today Git log pickaxe and Git grep over all 763 reachable commits find the tailnet
  domain suffix 0 times, and commit messages contain it 0 times; `refs/remotes/origin`
  holds only `main`, `feature/parallelize-agents-w-1pn` and `HEAD`; there are no tags;
  `userscript/` and `docs/queueing-user-prompts.md` are unchanged since `60b55a9`; `node
  --test tests/userscript.test.mjs` → 64 passed, 0 failed; `git bundle verify` of
  `~/.local/state/kandev-pacer-tailnet-rewrite-20261017/kandev-pacer-before-tailnet-rewrite-20261017.bundle`
  reports it okay. Commits: `ff40280` (after the rewrite it touches only tracker files)
  and `359e83f` (hash table). `git revert` cannot undo a history rewrite; the bundle is
  the way back. Criterion 4 (history rewrite) and the publishing class (the force push
  is Antti's; see "For Antti" above).
- [*] T3 pacer launched nothing for hours (palindromi.fi [6]). Done, then superseded by
  [39] (held failed launches) and moved to V2 by [49]; all three requested behaviours
  remain. Evidence: `3f9d424` fix(t3): stop one rejected launch from blocking all
  trackers, `89d2e3e`; superseded by `9724fb9` [39]; `T3Pacer.dispatch`
  (`REJECTION_LIMIT = 3`, a fresh thread ID and branch per rejection) and the
  pending-launch log line in `T3Pacer.tick` (`src/kandev_pacer/t3.py`); the 5 [39] tests
  that replaced the original ones → 5 passed. Revert `git revert 89d2e3e 3f9d424` → code
  conflicts in `tests/test_t3.py`, `src/kandev_pacer/t3.py`, `docs/t3-pacing.md`.
  Criterion 3.
- [*] Configure T3 model selection and runtime and interaction modes. Done. Launches use
  a fixed `t3_model`, or `"automatic"` over `t3_models` (the candidate with the most
  headroom wins), plus `t3_runtime_mode` and `t3_interaction_mode`, settable in TOML,
  environment or CLI. Evidence: `8725cc6` feat(t3): configure model selection and modes;
  `T3ModelSelection` in `src/kandev_pacer/config.py`, `model_candidates` and
  `launch_settings` in `src/kandev_pacer/t3.py`; 4 `tests/test_config.py` and 5
  `tests/test_t3.py` tests → 16 passed. Revert `git revert 8725cc6` → code conflicts in
  `config.py`, `t3.py`, `docs/t3-pacing.md` and both test files. Criterion 3.
- [*] Capacity-deferral handling (Kandev task dd6897a9). Done in Kandev mode. Deferred
  launches stay held across ticks and restarts without counting as failures. They
  reserve per-provider and total slots and count in heartbeat decisions. Evidence: merge
  `e98691b` (`c88775a`, `cf24f6d`), `789826d` (deployment record); `DeferredLaunch` and
  `Pacer.deferred_of` in `src/kandev_pacer/runtime.py`, `is_capacity_deferred` in
  `src/kandev_pacer/selection.py`, `docs/capacity-deferrals.md`; 21 tests → 38 passed.
  Revert `git revert -m 1 e98691b` → code conflicts in `config.py`, `runtime.py`,
  `tests/test_runtime.py`, `tests/test_selection.py`. Criterion 3; the worker says the
  live capacity replay was "not forced or claimed" (unverified).
- [*] Show Fable in scoped gate logs and diagnose waiting Fable tasks. Done in two
  passes. The log now names the scope even when Fable is the only class. The waiting
  Fable tasks sat on steps that never launch on their own, and `--dump-order` and an "N
  held: queue unreadable" note now say so. Evidence: merge `78fb6d1` (`08134ef`), merge
  `6bd76a6` (`ac414ff`, `93205fe`); `_group_label` and `dump_order` in
  `src/kandev_pacer/runtime.py`; 6 tests → 15 passed. Revert `git revert -m 1 6bd76a6`
  and `-m 1 78fb6d1` → code conflicts in `runtime.py`. Criterion 3.
- [*] Heartbeat waits out its cooldown while Fable has nothing to launch. Answered with
  a bounded fix. After a successful heartbeat sent while work was still running, an open
  provider that goes idle gets one early retry. Repeated or failed attempts stay
  throttled. Evidence: merge `f0757f0` (`952418f`, `034c4e7`), `dd497f8` (deployment
  record); `Pacer.maybe_heartbeat` and `_heartbeat_idle_retry` in
  `src/kandev_pacer/runtime.py`, `docs/heartbeat-idle-retry.md`; 7 tests → 19 passed.
  Revert `git revert -m 1 f0757f0` → code conflicts in `runtime.py`. Criterion 3; the
  worker says the live webhook retry was "not forced or claimed" (unverified).
- [*] Fix all test and linter failures (`PACER_QUOTA_SOURCE`). Done at the time. The
  validator that silently dropped `pebble` now returns it, and offline tests ignore the
  operator's TOML file and `PACER_`/`KANDEV_` variables. Today `ruff check` passes, but
  `ty check` reports 3 errors in `tests/test_resets.py:202-203` (from [32] and [35]) and
  the full suite has [52]'s 7 failures. Evidence: merge `3097689` (`e3fbceb`, `c18bba4`,
  `ace4ca9`), `767927d`; `Settings._known_quota_source` in `src/kandev_pacer/config.py`,
  `tests/conftest.py`; 9 tests → 9 passed. Revert `git revert -m 1 3097689` → code
  conflicts in `tests/conftest.py`, `tests/test_provider.py`. Criteria 1 (`main` is not
  clean again) and 3.
- [13] Launch the primary session, falling back to the newest only when no primary
  exists. Done. `launch_session` picks the newest `is_primary` session, and the newest
  session only when none is primary; launch intent, eligibility, action keys and stall
  identity use it. Evidence: merge `ee1996a` (`e14813a`), `d2e936d` (shipped in [3]'s
  merge `d9eef40`); `src/kandev_pacer/selection.py`; 3 tests → 3 passed. Revert `git
  revert d2e936d` and `-m 1 ee1996a` → code conflicts in `selection.py`,
  `tests/test_selection.py`. Criterion 3.
- [9] Order launchable tasks by prompt-cache deadline. The selector orders by deadline,
  but nothing shows that Kandev sends the `turns` data it reads; without that data the
  ordering silently does nothing. The deadline also comes from the newest session, while
  launches use the primary one ([13]). Evidence: `e00e6c6`, merge `d553e11`;
  `select_action` in `src/kandev_pacer/selection.py`, `Pacer.cache_deadlines` in
  `src/kandev_pacer/runtime.py`; 3 selector tests with hand-made deadlines → 3 passed.
  Revert `git revert -m 1 d553e11` → code conflicts in `runtime.py`, `selection.py`.
  Criteria 1 (unverified against live data) and 3.
- [10] Load secrets from a private env file. Meets the request. Settings load from TOML,
  then the secrets file, then the environment, then the CLI. Only `CONFIG_FIELDS` keys
  are read, the loader refuses the file when group or others can read it, and an error
  names a malformed line by number only. Evidence: `254b075`, `5089f8f`, `886c6db`,
  merge `37e6e5b`; `load_settings` in `src/kandev_pacer/config.py`; `pytest
  tests/test_config.py -k secrets` → 6 passed. Revert `git revert -m 1 37e6e5b` → code
  conflicts in `README.md`, `config.py`, `tests/test_config.py`. Class: secrets or
  credentials (it decides how the heartbeat webhook secret reaches the process and which
  file permissions guard it); also criterion 3.
- [12] Tune 5-hour gating and interactive hours. Meets the request; weekly and backstop
  gates remain, and [32] didn't touch the 5-hour rules. Ranges that wrap past midnight
  (`22-06`) are rejected. Evidence: `b58cca0`, merge `a25ddcf`; `_window_alpha`,
  `_window_active` and `quota_verdict` in `src/kandev_pacer/quota.py`, `alpha_5h` and
  `interactive_hours` in `src/kandev_pacer/config.py`; 4 tests → 4 passed. Revert `git
  revert -m 1 a25ddcf` → code conflicts in `README.md`, `config.py`, `quota.py`.
  Criterion 3.
- [3] Optionally signal completion for stalled primary sessions. The code meets the
  request, but gogo doesn't set `stall_action`, so the requested first test against a
  real blocked card and the live week never happened. Evidence: `d2e936d`, `0ddf4ac`,
  `d4c39d6`, `3d9b36b`, merge `d9eef40`; `Pacer.give_up` and `Pacer.check_signals` in
  `src/kandev_pacer/runtime.py`, `ws_step_complete` in `src/kandev_pacer/client.py`; 22
  tests → 22 passed. Revert `git revert -m 1 d9eef40` → code conflicts in `runtime.py`
  and three test files. Class: deployment (enabling it and the live check are still to
  do); also criterion 3.
- [*] Observation consumer couldn't create its lock file in a read-only directory.
  Partly done. Lock failures now name the path, the error number and how to recover, and
  the cause is documented (the executor's sandbox mounts `/home` read-only), but the
  consumer still can't write its state. Evidence: `97bc646`, merge `735ebb4`, `592c9b3`;
  `locked()` in `src/kandev_pacer/learn.py`, "Lock access failure, 2026-10-04" in
  `docs/tasks/16-observation-consumer.md`; 5 lock tests → 27 passed. Revert `git revert
  -m 1 735ebb4` → tracker files only. Class: the worker reported it partial ("Restoring
  production consumer write access still requires the scoped Kandev/host
  configuration").
- [*] Unclear "observation identity conflict" output. Done. The error names the record
  file, the database, the expected and stored task and `archived_at`, the field that
  differs, a precision note for legacy timestamps, and recovery steps. It goes slightly
  past the request, because legacy records with shortened timestamps and no sidecar now
  fail with this explanation where `85a5ead` accepted them. Evidence: merge `3eae6d6`
  (`9d0b493`, `73e44fe`), `28a2bad`; `checked_record()` and `read_record()` in
  `src/kandev_pacer/learn.py`; 5 tests → 18 passed. Revert `git revert 28a2bad` and `-m
  1 3eae6d6` → code conflict in `tests/test_learn.py` (none if `-m 1 ead5643` is
  reverted first). Criterion 3.
- [*] Step names instead of step IDs in observation files. Done and live. Names show in
  `Steps` and in the step column of the signals table, and `## Appendix: Step IDs` maps
  names to IDs; live vault files show both. Evidence: `d762553`, `1bf26bd`, merge
  `eb8168d`; `step_names()` and `render_record()` in `src/kandev_pacer/learn.py`; 3
  `tests/test_learn.py` tests → 3 passed. Revert `git revert -m 1 eb8168d` → code
  conflict in `learn.py` (`render_record`, rewritten later by `8ce0030`). Criterion 3.
- [*] The filename UUID is repeated at the top of the file. Done and live. Each file has
  the line ``Observation file ID: `<uuid>` (task ID).``, and
  `docs/session-observations.md` explains it. Evidence: `1342a7f`, merge `d5670f9`; the
  same 3 tests → 3 passed. Revert `git revert -m 1 d5670f9` → code conflicts in
  `docs/session-observations.md`, `learn.py`. Criterion 3.
- [*] The session ID is repeated over and over. Done. A file can hold more than one
  session, so each section starts with `## Session <id>` and the per-session tables lost
  their session column. Evidence: `002162c`, merge `3e949a3`; the same 3 tests → 3
  passed. Revert `git revert -m 1 3e949a3` → code conflicts in
  `docs/session-observations.md`, `learn.py`, `tests/test_learn.py`. Criterion 3.
- [*] Shorter values in observation files. Done and live. Durations and cost have 3
  decimals, timestamps are milliseconds in UTC without a zone, file names use the same
  format, and the JSON sidecar keeps full precision. Evidence: `09da92c`, merge
  `3987872`; `formatted_record()`, `format_timestamp()` and `record_path()` in
  `src/kandev_pacer/learn.py`; the same 3 tests → 3 passed. Revert `git revert -m 1
  3987872` → code conflicts in `docs/session-observations.md`, `learn.py`,
  `tests/test_learn.py`. Criterion 3.
- [*] Queue in Quick Chat queues to the task underneath. Partly done. Queue in Quick
  Chat now refuses ("Quick Chat paced queueing is unavailable"), keeps the draft and
  sends nothing; it doesn't queue a new Quick Chat session. The worker wrote "pending
  user acceptance of the documented fallback", the four browser checks were not run, and
  version 1.1.5 was never installed in the browser. Beyond the request, ordinary task
  composers now queue to the session on screen instead of the primary one. Evidence:
  `3d6f87d`, merge `6a2ef5a`, `ceb013e`; `onQueue()` and `queueTarget()` in
  `userscript/kandev-queue-button.user.js`; `pytest tests/test_userscript.py` → 1
  passed, `node --test tests/userscript.test.mjs` → 64 passed. Revert `git revert
  ceb013e` and `-m 1 6a2ef5a` → tracker files only. Class: the worker reported it
  partial and unverified; also criteria 1 and 2.
- [7] Queue button session identifiers after the 0.94-dev upgrade. The code on `main` is
  fixed, but nobody confirmed it in the browser. 1.1.4's own identity check caused "task
  session identity is unavailable"; `28ffe4f` fixed that, but the `[*]` for that error
  (added by `63aee2a`) is still In Progress. Evidence: `419da7f` (merge `5dd5f5a`),
  `4df4e73`, `88854e8` (merge `ac9fa66`), `28ffe4f` (merge `35a7236`); `queueTarget()`
  sends `session_incarnation_id`; `node --test tests/userscript.test.mjs` → 64 passed,
  `test_userscript` → 1 passed. Revert `git revert -m 1 35a7236` (the first step) → code
  conflicts in the userscript and its test. Class: deployment unverified (browser
  activation still outstanding); also criterion 3.
- [6] Failed queue statuses after the 0.94-dev upgrade. Done, deployed and verified on
  2026-09-20. The worker restarted the pacer, its log shows ticks without the identity
  error, and the live read-only contract test passed. The pacer sends the session's
  `queue_incarnation_id` as `session_incarnation_id`, and an unreadable queue holds back
  only that task. Evidence: `ef466eb` (merge `020388a`), `4df4e73`, `ba27513`, `32c0827`
  (merge `b4c71f9`), `378b970`; `queue_identity()`, `ws_queue_count()` and `ws_drain()`
  in `src/kandev_pacer/client.py`, and `src/kandev_pacer/runtime.py`; 15 test IDs → 30
  passed, 1 skipped (the opt-in live test). Revert `git revert -m 1 b4c71f9` → code
  conflicts in `runtime.py`, the userscript, `docs/queueing-user-prompts.md` and four
  test files. Criterion 3.

### Not done (5)

- [8] Fall back to a configured agent profile when a provider gate stays closed. With
  `PACER_FALLBACK_AFTER=1`, no mapping and the gate closed at 95%, an idle task with a
  session still got `session.launch intent=workflow_step` on the same provider; with
  fallback off, nothing launched. The mapping `"Luna low=Sonnet low, Astra Medium=Sonnet
  low"` parses as `{'Luna low': 'Sonnet low, Astra', 'Medium': 'Sonnet low'}`. Evidence:
  `4bbfdbf`, `375726c`, `40094f5`, merge `e3cd4b9`; `Pacer.run_provider` and
  `Pacer.launch` in `src/kandev_pacer/runtime.py`, `Settings.fallback_profiles` in
  `src/kandev_pacer/config.py`; `pytest tests/test_two_subscriptions.py -k fallback` and
  2 runtime fallback tests → 5 passed (none covers these cases). Missing: a closed gate
  must not launch turns for tasks that have a session, and mappings with spaces must
  parse.
- [1] Read quota from pebble-quota as an optional alternative. Source selection works
  and gogo uses it, but `7d (Fable)` maps to `scope=None`, so Fable's weekly limit gates
  every Claude model. In a single-provider run, a snapshot without the `claude` service
  opens the gate instead of marking it blind. The spec's live contract check and README
  "Quota sources" paragraph were never added. Evidence: `0d807f4`, `b322ecf`, merge
  `f73b8c9`, and `e3fbceb` (until 2026-09-21 the validator dropped `pebble`, so it was
  never selected); `pebble_fetch` and `_read_source` in `src/kandev_pacer/provider.py`;
  `pytest tests/test_provider.py -k 'pebble or quota_source'` → 7 passed. Missing: a
  scoped Fable window, a blind gate when the service is missing, the contract check and
  the README paragraph.
- [11] Default to a 30-second interval and rotate pacer logs weekly or at 50 MB. The 30
  s default and weekly rotation with 4 backups are in, but the 50 MB cap doesn't hold.
  After the first size rollover in a week, the dated backup name already exists, so
  later rollovers do nothing and the file grows until the weekly roll. With the cap set
  to 200 bytes, 40 records left a 1584-byte file and one backup. The live `pacer.jsonl`
  covers 2026-09-19 to 10-04 with no weekly roll, because each restart moves the roll to
  the last write plus 7 days. Evidence: `a1e4ef6`, `97ae150`, merge `b27f97b`;
  `PacerLogHandler` in `src/kandev_pacer/runtime.py`; `pytest tests/test_runtime.py -k
  'default_interval or log_rotation'` → 3 passed (the test never reaches a second
  rollover). Missing: an enforced size cap.
- [*] Store observation JSON in a sidecar and quote the prompt and final answer in the
  Markdown. The sidecar and the 1,000-character quotes are in, but the quoted "Initial
  user prompt" is text the agent harness injects. Of 589 vault sessions with a sidecar,
  545 quote injected text (298 `# AGENTS.md instructions`, 222 `<recommended_plugins>`,
  25 `<kandev-system>`), 42 show "No data." and only 2 show the real task prompt.
  Evidence: `8ce0030`, `9b3012c`, `af1361f`, merge `85a5ead`, `0d9c4c3`;
  `render_record()` and `collect_task()` in `src/kandev_pacer/learn.py`;
  `message_text()` in `src/kandev_pacer/learn_sources.py` filters by role only; 12
  fixture tests → 26 passed. Missing: skip the user-role messages the harness injects.
- [*] Quote the initial prompt and the final response in observation files. Superseded
  by the sidecar item above, with the same flaw: of 104 sessions recorded before the
  split, 103 quote `<recommended_plugins>` or `<kandev-system>` text and 1 is empty;
  final responses are captured. Evidence: `f85d0dc`, `c117acd`, merge `993efa4`;
  `TEXT_LIMIT` and `formatted_record()` in `src/kandev_pacer/learn.py`; 2 tests → 2
  passed. Missing: the prompt the session actually ran with.

### Follow-ups found

I filed none of these, because this issue may not change other issues.

- Point [52]'s fixtures at a malformation Hai still rejects (dropping the link
  definition works), and stop `test_startup_and_dump_order_skip_unreadable_tracker` from
  replacing the global `time.sleep`.
- `ty check` reports 3 errors in `tests/test_resets.py:202-203`.
- One fix would cover both prompt-quoting items, skipping the leading user-role messages
  the harness injects.
- The In Progress `[*]` "task session identity is unavailable" may need only a browser
  check of userscript 1.1.5, since `28ffe4f` fixed its cause.
- `docs/2026-09-19-architecture-review-and-remediation-plan.md` already lists the [8],
  [1], [11] and [9] defects.

### Counts

- Would accept: 6 ([50], [51], [53], the observation index, [17], [5])
- Left to Antti: 23
- Not done: 5 ([8], [1], [11] and the two prompt-quoting items)
- Total: 34
