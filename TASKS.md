# Issue tracking for kandev-pacer

Rules for TASKS.md usage are at the bottom of the file.

## Unverified proposals

- [47] Detect stale pending launches after a T3 project is recreated
  <!-- hai:{"id":"simple-093c3118-831d-4092-837f-e7ed189c124e","createdAt":"2026-10-11T18:38:38.041Z","updatedAt":"2026-10-11T18:38:38.041Z"} -->

- [41] Verify Claude reset contract and supervise useful reset trials
  <!-- hai:{"id":"simple-75487633-ff73-4817-ac73-5364758dc38e","createdAt":"2026-10-10T10:59:20.476Z","updatedAt":"2026-10-10T10:59:20.476Z"} -->

- [40] Make reset redemption outcomes and diagnostics provider-specific
  <!-- hai:{"id":"simple-01626a87-ecf4-4d88-b00d-ba3c08a3686a","createdAt":"2026-10-10T10:59:20.368Z","updatedAt":"2026-10-10T10:59:20.368Z"} -->

## Ordered backlog

- [56] Prepare the T3 upgrade with the pacing-hook patch for deployment from
  atom
  <!-- hai:{"id":"simple-b13db05a-2c9d-4505-8cd5-1d1b843a5d44","labels":["steward"],"createdAt":"2026-10-17T23:10:11.429Z","updatedAt":"2026-10-17T23:10:11.429Z"} -->

## Scheduled

## In Progress

- [16] Learn from session observation records: a lowest-priority Kandev task the
  Heartbeat creates when unread records exist, writing observations and proposed
  solution prompts to the right repository's unreviewed section.
    - Depends on: [5]

- [*] The `Queue` button injected by the userscript into task sessions doesn't work. It
  always pops up this error: `Queue failed: task session identity is unavailable`.

- [15] Recover expired Claude credentials through Claude Code, then check fresh quota
  before resuming tasks.

## Completed / Accepted

- [ ] [55] Gather acceptance evidence for the 34 review-queue items
      <!-- hai:{"id":"simple-1589de5a-10e5-4287-9a0c-16c6baf5af62","labels":["steward"],"createdAt":"2026-10-17T17:06:23.520Z","updatedAt":"2026-10-17T17:32:51.307Z"} -->

- [ ] [54] Remove the tailnet hostname from all files and from history
      <!-- hai:{"id":"simple-0f558179-3e1d-4acd-bef4-ec9028dff735","labels":["steward"],"createdAt":"2026-10-17T12:54:00.321Z","updatedAt":"2026-10-17T13:02:43.560Z"} -->

- [ ] [53] Hold unresolved V1 launch records before quota and capacity checks
      <!-- hai:{"id":"simple-3065dc85-78d7-4c10-a98f-a9b47b58e061","labels":["steward"],"createdAt":"2026-10-17T11:05:14.552Z","updatedAt":"2026-10-17T11:34:06.613Z"} -->

- [ ] [52] A tracker that fails to parse stops pacing for every project
      <!-- hai:{"id":"simple-a77c430b-d72d-4f30-a910-9534cc450248","labels":["steward"],"createdAt":"2026-10-17T07:07:03.309Z","updatedAt":"2026-10-17T07:31:51.091Z"} -->

- [ ] [51] Count only paced threads against concurrency_total
      <!-- hai:{"id":"simple-8fd046bd-0a6c-4041-acb3-c0c670ac04e6","labels":["steward"],"createdAt":"2026-10-17T07:07:02.465Z","updatedAt":"2026-10-17T07:28:30.588Z"} -->

- [ ] [49] Port T3 mode to T3's V2 protocol: pacedRuns discovery and queued-run.admit
    - Depends on: [50]

      <!-- hai:{"id":"simple-da78b7a4-bee2-4ffe-86eb-a294b70b2388","labels":["steward"],"createdAt":"2026-10-16T16:43:07.027Z","updatedAt":"2026-10-17T07:24:40.167Z"} -->

- [ ] [50] Update the tests to Hai's sourceState field
      <!-- hai:{"id":"simple-0032e3fe-2aeb-4c7b-856b-3c018aa9513c","labels":["steward"],"createdAt":"2026-10-17T07:07:01.094Z","updatedAt":"2026-10-17T07:16:16.361Z"} -->

- [x] [*] Use Opus 5.5 with Extra High reasoning for paced Claude tasks instead of Fable 5.1.
  <!-- hai:{"id":"simple-748c4fdf-5b8a-434f-bb5f-eee2687a0402","createdAt":"2026-10-11T20:32:51.333Z","updatedAt":"2026-10-15T20:35:57.874Z"} -->

- [x] [48] Pace Hai sources using workflow stages
  <!-- hai:{"id":"simple-d6ad5b8b-190d-4e2d-a4b0-d2d05257ae6e","createdAt":"2026-10-15T16:26:13.622Z","updatedAt":"2026-10-16T16:00:12.423Z"} -->

- [x] [46] New issue title not focused
  <!-- hai:{"id":"simple-63e6a154-30ce-4ee2-9fe5-404e753790f5","priority":"none","labels":[],"createdAt":"2026-10-10T19:40:23.453Z","updatedAt":"2026-10-11T14:05:39.779Z"} -->

- [x] [44] A tracker dropped from the config or T3 stops pacing for every project
  <!-- hai:{"id":"simple-3b44f53e-4b5a-4948-8f65-7003544a3dfb","createdAt":"2026-10-10T19:31:24.931Z","updatedAt":"2026-10-11T19:31:48.375Z"} -->

- [x] [43] A missing launched thread stops pacing for every project
  <!-- hai:{"id":"simple-1f8ce88a-69b8-4ee1-b03d-37b4e2793501","createdAt":"2026-10-10T19:31:24.723Z","updatedAt":"2026-10-11T18:34:48.211Z"} -->

- [x] [36] Keep long-lived Claude resets when the weekly rollover is too close to use the
  refill.
    - Depends on: [34]
  <!-- hai:{"updatedAt":"2026-10-10T19:42:51.523Z"} -->

- [x] [35] Redeem Codex resets as early in the week as possible instead of pacing toward an
  even split or saving them for their last week.
    - Depends on: [34]
  <!-- hai:{"updatedAt":"2026-10-10T19:37:24.558Z"} -->

- [x] [34] Measure how fast agents can empty a weekly allowance, per provider, from quota
  readings.
  <!-- hai:{"updatedAt":"2026-10-10T19:32:02.618Z"} -->

- [x] [42] Name the `hai-tasks` CLI and its path in the T3 worker prompt.
  <!-- hai:{"id":"simple-e5032b10-0e74-4f0f-b56c-1a32930f34fc","createdAt":"2026-10-10T11:06:14.580Z","updatedAt":"2026-10-10T19:32:32.397Z"} -->

- [x] [37] Research how to redeem banked resets for the Claude and Codex subscriptions, and
  how to verify the pacer's untested redemption requests without wasting a reset.
  <!-- hai:{"updatedAt":"2026-10-10T19:30:34.611Z"} -->

- [x] [38] Heavy console output
  <!-- hai:{"id":"simple-603d6a8b-d4a1-4ed1-869f-4f8c0439ef5e","priority":"none","labels":[],"createdAt":"2026-10-10T08:55:49.236Z","updatedAt":"2026-10-10T19:28:09.663Z"} -->

- [x] [33] Unclear "withheld" message
  <!-- hai:{"id":"simple-a1d698f2-1b36-4d9d-8019-cebd176aa083","priority":"none","labels":[],"createdAt":"2026-10-07T19:50:25.573Z","updatedAt":"2026-10-10T19:27:21.755Z"} -->

- [x] [39] One failed launch stops pacing for every project
  <!-- hai:{"updatedAt":"2026-10-11T04:11:30.487Z"} -->

- [x] [*] Look at
  agent@gogo:prg/kandev-config/docs/2026-10-07-heartbeat-deterministic-script-research.md
  and implement the scripts. Then add a ~/prg/kandev-config/ task to adapt the heartbeat
  automation prompt to use the new scripts.
  <!-- hai:{"id":"simple-3b8c36c856df2af8","updatedAt":"2026-10-08T19:32:30.022Z"} -->

- [x] [*] Look at
  agent@gogo:prg/kandev-config/docs/2026-10-07-heartbeat-deterministic-script-research.md
  and do a similar analysis for the T3 heartbeat that Kandev-pacer is using
  <!-- hai:{"id":"simple-1fc55ce1c4d74535","updatedAt":"2026-10-08T16:14:54.366Z"} -->

- [x] [32] Add reset-aware pacing
  <!-- hai:{"id":"simple-a0c0911c-b11f-4e28-9b79-867593c381eb","priority":"none","labels":[],"createdAt":"2026-10-06T18:43:18.623Z","updatedAt":"2026-10-07T19:54:23.157Z"} -->

- [ ] [*] T3 pacer launched nothing for hours: T3 rejected the palindromi.fi [6] launch
      after a failed worktree Git step, every retry hit the leftover `pacer-<thread>`
      branch, and a later tracker merge made dispatch refuse the record. A submitted
      launch now needs only its issue to stay eligible. A rejected launch absent from T3
      retries with a new thread ID, up to three times. A pending launch waiting for
      capacity logs why nothing else launches.

- [x] [31] Dirty repo blocks all trackers
      <!-- hai:{"id":"simple-5569346b-76c6-4a78-ad00-4f62faa13598","priority":"none","labels":[],"createdAt":"2026-10-06T05:24:40.114Z","updatedAt":"2026-10-17T12:54:17.356Z"} -->

- [x] [29] Unclear tracker error
      <!-- hai:{"id":"simple-deb4a03b-3449-4cf8-8d19-6efb79ccd008","priority":"none","labels":[],"createdAt":"2026-10-05T19:03:41.798Z","updatedAt":"2026-10-05T19:32:13.326Z"} -->

- [x] [30] Unclear non-clean repository error
      <!-- hai:{"id":"simple-9657c54f-11fb-4412-a07f-b516ee58254c","priority":"none","labels":[],"createdAt":"2026-10-05T19:04:41.824Z","updatedAt":"2026-10-05T19:30:54.689Z"} -->

- [x] [27] Unclear error on configuration mistake
      <!-- hai:{"id":"simple-aeab5d07-0f79-4609-9483-4601cb894f99","priority":"none","labels":[],"createdAt":"2026-10-04T20:22:34.136Z","updatedAt":"2026-10-04T20:34:49.446Z"} -->

- [x] [28] Still requires `parent_thread`
      <!-- hai:{"id":"simple-60f79b37-10ad-4c2e-9817-fcb8e3f11912","priority":"none","labels":[],"createdAt":"2026-10-04T20:24:37.536Z","updatedAt":"2026-10-04T20:33:05.030Z"} -->

- [x] [26] JSON inside TOML configuration
      <!-- hai:{"id":"simple-d5668741-e698-4a8a-b21d-32a2ff4ea0aa","priority":"none","labels":[],"createdAt":"2026-10-04T20:10:15.514Z","updatedAt":"2026-10-04T20:22:41.668Z"} -->

- [ ] [*] Configure T3 model selection as fixed or automatic by pacing headroom, and
      override runtime and interaction modes in the configuration file.

- [x] [19] UV source path documentation unclear
      <!-- hai:{"id":"simple-1ea5eea8-5a65-4a87-a84f-3f2ab01533b4","priority":"none","labels":[],"createdAt":"2026-10-04T18:44:41.879Z","updatedAt":"2026-10-04T19:43:02.979Z"} -->

- [x] [24] Codex reset workaround broken, no Anthropic status
      <!-- hai:{"id":"simple-1afb0e60-8d2e-4995-85eb-314807cfd60b","priority":"none","labels":[],"createdAt":"2026-10-04T19:13:34.534Z","updatedAt":"2026-10-04T19:24:14.956Z"} -->

- [x] [20] `Command not found` error
      <!-- hai:{"id":"simple-b2087b50-35fc-4c18-8a87-3826565d0d60","priority":"none","labels":[],"createdAt":"2026-10-04T18:45:46.258Z","updatedAt":"2026-10-04T19:11:10.780Z"} -->

- [x] [23] Unclear "unknown provider" error
      <!-- hai:{"id":"simple-573cb9ef-fda8-47a8-904c-de914d45738a","priority":"none","labels":[],"createdAt":"2026-10-04T18:56:27.672Z","updatedAt":"2026-10-04T19:06:31.589Z"} -->

- [x] [22] Unclear ValueError
      <!-- hai:{"id":"simple-04f2f10f-b85d-4318-8db4-1ea72176c3e1","priority":"none","labels":[],"createdAt":"2026-10-04T18:54:18.448Z","updatedAt":"2026-10-04T19:03:05.774Z"} -->

- [x] [21] Unclear URLError
      <!-- hai:{"id":"simple-0ad93f1d-3593-46f0-b76c-5cea0e0cba6f","priority":"none","labels":[],"createdAt":"2026-10-04T18:47:10.449Z","updatedAt":"2026-10-17T12:54:19.078Z"} -->

- [x] [18] Pace TASKS.md work through Hai's Python API and launch admitted tasks in T3
      Code.
    - Hai's `hai_tasks` Python module is ready to use; the API dependency is satisfied.

      <!-- hai:{"updatedAt":"2026-10-04T19:25:53.624Z"} -->

- [ ] [*] Write `pages/PKB/Kandev Session Observations/index.md` from
      `kandev-learn sweep` in the same commit as new records, reading the JSON sidecar
      when present. It replaces the vault's one-off `scripts/kandev-observations-index`,
      whose output went stale.

- [ ] [*] The `Queue` button injected by the userscript into the Quick Chat window
      queues the prompt to the task beneath the Quick Chat pop-up, not as a new quick
      chat session. Fix this.

- [ ] [*] In task 0cab15fe-556c-4837-b04b-83e50e52c4a9, the observation consumer
      couldn't create a lock file due to a read-only directory. Investigate and fix this
      problem.

- [ ] [17] Stop `kandev-learn sweep` from parsing every Codex transcript in full before
      checking which task it belongs to; one 1.17 GB rollout makes each sweep take 4+
      GiB.

- [ ] [*] Kandev task dd6897a9-b54e-4ad4-b9b5-b2372292f4b1 stated: "Capacity-deferral
      handling remains separate follow-up work." If that hasn't been implemented, do it.

- [ ] [*] Show Fable in scoped gate logs even when it is the only class, and diagnose
      why waiting Fable tasks are not launchable.

- [ ] [7] Fix userscript Queue button session identifiers after the 0.94-dev upgrade.

- [ ] [*] Fix all test and linter failures, e.g. the `PACER_QUOTA_SOURCE` default test
      failure if it still occurs.

- [ ] [*] It's hard to understand what this output means:

    ```
    ❯ set -a; source ~/.config/secrets/kandev-pacer.env; set +a; uv run --frozen kandev-learn b224e8f2-59b4-4d58-9115-bfdefdbc2c60
    kandev-learn: observation identity conflict
    ❯ set -a; source ~/.config/secrets/kandev-pacer.env; set +a; uv run --frozen kandev-learn 6e871e6b-45b5-44e4-86b6-ea4fa71e499e
    kandev-learn: observation identity conflict
    ```

- [ ] [*] `kandev-learn sweep` now stores the JSON as frontmatter in the session
      observation Markdown file. Store the JSON in a separate file alongside the
      Markdown instead, but extract the initial user prompt (excluding system prompts,
      tools etc.) and the final answer (both truncated if long) in the Markdown file as
      well.

- [ ] [*] I see this:

    ```
    [anthropic (Fable)] headroom=+0.082 open, nothing to launch (0/3 running)
    [openai] headroom=-0.007 PAUSED (0 running), resumes in ~1h 14m 02s (~22:34), next tick in 30s
    heartbeat: cooldown, retry in 1674s
    ```

    In this situation, shouldn't heartbeat be executed immediately because Fable has
    nothing to launch?

- [ ] [5] Decide the observation consumer's audience, home, output, cadence and write
      boundary. Completed decision map and implementation handoff.

- [ ] [8] Fall back to a configured agent profile when a provider gate stays closed.

- [ ] [9] Order launchable tasks by prompt-cache deadline, preserving queue tie-breaks.

- [ ] [*] In `kandev-learn` session observation files, quote the initial prompt used for
      the session, and the final response generated by the model. Truncate both to a
      reasonable length.

- [ ] [10] Load secrets from a private env file between TOML and process-environment
      settings.

- [ ] [1] Read quota from pebble-quota as an optional alternative to direct provider
      reads.

- [ ] [11] Default to a 30-second interval and rotate pacer logs weekly or at 50 MB.

- [ ] [12] Tune 5-hour gating and interactive hours while retaining weekly and backstop
      gates.

- [ ] [*] In `kandev-learn` session observation files, use the name of each step
      mentioned instead of the step ID, both in the `Steps` section table column 2, and
      in column 3 of the `Step signal summaries and blockers` table. Add an appendix
      mapping step names to step IDs.

- [ ] [13] Launch the primary session, falling back to the newest only when no primary
      exists.

- [ ] [*] There is a UUID in the `kandev-learn` session observation file name. That UUID
      is never mentioned in the file itself. It's unclear what the UUID represents.
      Repeat the UUID and its type (e.g. task, session ID, etc.) at the top of the file.

- [ ] [*] The session ID is repeated over and over in `kandev-learn` session observation
      files. Will there ever be more than one session per file? If not, just state the
      ID once at the top of the file. If yes, state the ID for each session at the top
      of each section.

- [ ] [*] In session observation files from `kandev-learn`, truncate long values for
      brevity when full precision is not needed. For example, use millisecond precision
      for durations, 1/1000 dollars for cost, and milliseconds without timezone for
      timestamps (also in file names).

- [ ] [6] After the 0.94-dev upgrade, I'm getting failed queue statuses.

- [ ] [3] Optionally signal completion for eligible stalled primary sessions; block if
      the step does not advance.
    - Depends on: [13]

- [x] [*] Waive the heartbeat cooldown once a task has been launched since the last
      attempt, so a full backlog does not leave the board dry for half of every cycle.
    - Depends on: [2]

      <!-- hai:{"id":"simple-88e03c0e1537dea8","updatedAt":"2026-10-06T05:26:03.871Z"} -->

- [x] [2] Call the Heartbeat webhook when an open provider gate is running out of work.
      <!-- hai:{"updatedAt":"2026-10-06T05:25:57.872Z"} -->

- [x] [4] Collect archived session observations without model calls.
      <!-- hai:{"updatedAt":"2026-10-06T05:25:39.325Z"} -->

- [x] [14] Enrol workflows through the step update API instead of writing the Kandev
      database.
      <!-- hai:{"updatedAt":"2026-10-06T05:25:28.371Z"} -->

[1]: docs/tasks/1-quota-from-pebble-collector.md
[2]: docs/tasks/2-heartbeat-webhook.md
[3]: docs/tasks/3-stall-detector-signal.md
[4]: docs/tasks/4-session-observations.md
[5]: docs/tasks/5-observation-consumer.md
[6]: docs/tasks/6-failed-queue-status.md
[7]: docs/tasks/7-queue-button-session-identifiers.md
[8]: docs/tasks/8-agent-profile-fallback.md
[9]: docs/tasks/9-prompt-cache-deadline-order.md
[10]: docs/tasks/10-secrets-env-file.md
[11]: docs/tasks/11-pacer-log-rotation.md
[12]: docs/tasks/12-gate-window-tuning.md
[13]: docs/tasks/13-primary-session-selection.md
[14]: docs/tasks/14-workflow-step-api.md
[15]: docs/tasks/15-claude-credential-recovery.md
[16]: docs/tasks/16-observation-consumer.md
[17]: docs/tasks/17-codex-transcript-memory.md
[18]: docs/tasks/18-hai-tasks-t3-code.md
[*]: TASKS.md

---

[19]: docs/tasks/19-uv-source-path-documentation-unclear.md

<!-- hai:reserved-numbers:1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56 -->

[20]: docs/tasks/20-command-not-found-error.md
[21]: docs/tasks/21-unclear-urlerror.md
[22]: docs/tasks/22-unclear-valueerror.md
[23]: docs/tasks/23-unclear-unknown-provider-error.md
[24]: docs/tasks/24-codex-reset-workaround-broken-no-anthropic-status.md
[26]: docs/tasks/26-json-inside-toml-configuration.md
[27]: docs/tasks/27-unclear-error-on-configuration-mistake.md
[28]: docs/tasks/28-still-requires-parent-thread.md
[29]: docs/tasks/29-unclear-tracker-error.md
[30]: docs/tasks/30-unclear-non-clean-repository-error.md
[31]: docs/tasks/31-dirty-repo-blocks-all-trackers.md
[32]: docs/tasks/32-add-reset-aware-pacing.md

[33]: docs/tasks/33-unclear-withheld-message.md
[34]: docs/tasks/34-measure-burn-time.md
[35]: docs/tasks/35-redeem-codex-resets-early.md
[36]: docs/tasks/36-keep-claude-resets-near-rollover.md
[37]: docs/tasks/37-research-reset-redemption.md

[38]: docs/tasks/38-heavy-console-output.md
[39]: docs/tasks/39-hold-failed-launch-pace-others.md

[40]: docs/tasks/40-make-reset-redemption-outcomes-and-diagnostics-provider-specific.md

[41]: docs/tasks/41-verify-claude-reset-contract-and-supervise-useful-reset-trials.md

[42]: docs/tasks/42-name-the-hai-tasks-cli-and-its-path-in-the-t3-worker-prompt.md

[43]: docs/tasks/43-a-missing-launched-thread-stops-pacing-for-every-project.md

[44]: docs/tasks/44-a-tracker-dropped-from-the-config-or-t3-stops-pacing-for-every-project.md

[46]: docs/tasks/46-new-issue-title-not-focused.md

[47]: docs/tasks/47-detect-stale-pending-launches-after-a-t3-project-is-recreated.md

[48]: docs/tasks/48-pace-hai-sources-using-workflow-stages.md

[49]: docs/tasks/49-port-t3-mode-to-t3-s-v2-protocol-pacedruns-discovery-and-queued-run-ad.md

[50]: docs/tasks/50-update-the-tests-to-hai-s-sourcestate-field.md

[51]: docs/tasks/51-count-only-paced-threads-against-concurrency-total.md

[52]: docs/tasks/52-a-tracker-that-fails-to-parse-stops-pacing-for-every-project.md

[53]: docs/tasks/53-hold-unresolved-v1-launch-records-before-quota-and-capacity-checks.md

[54]: docs/tasks/54-remove-the-tailnet-hostname-from-all-files-and-from-history.md

[55]: docs/tasks/55-gather-acceptance-evidence-for-the-34-review-queue-items.md

[56]: docs/tasks/56-prepare-the-t3-upgrade-with-the-pacing-hook-patch-for-deployment-from-.md

## Rules

Here are the rules for TASKS.md usage:

### Unverified proposals

- Bullets under `## Unverified proposals` are ideas that nobody has reviewed yet. They
  are not candidates for scheduling: a heartbeat never picks from this section. Antti
  reviews a proposal and moves it into `## Ordered backlog`, or deletes it.
- Before adding a proposal, read the whole section. Drop an exact duplicate. Merge a
  similar existing proposal with the new one into a single revision that combines the
  ideas of both.

### One heading per issue

At any time, each issue's bullet must be under exactly one `##` heading (for example
`## Ordered backlog` or `## In Progress`). It must never be under two headings at the
same time.

- Before you commit any change to TASKS.md, read the whole file and check that no bullet
  appears under two headings.

### TASKS.md maintenance sessions

- In `## Completed / Accepted`, `- [ ] [N] summary` or `- [ ] [*] summary` means
  completed work awaiting user acceptance. `- [x] [N] summary` or `- [x] [*] summary`
  means the user has accepted it. Only user acceptance changes `[ ]` to `[x]`;
  completion, tests and deployment do not.
- Move newly completed issues into `## Completed / Accepted` with `[ ]`.
- During the next maintenance run, remove only `[x]` issues from this section, plus
  their unused description files and reference-style links. Keep `[ ]` issues and any
  files or links still referenced by another issue.
- Each issue must retain either
    - a numbered reference-style link (e.g. `[1]`) to a description file, or
    - `[*]` to indicate no description file is needed for a simple task.
- Link references are listed between `## Completed / Accepted` and `## Rules`.
- Keep simple tasks inline with `[*]`. Move detailed requirements, rationale, examples
  and acceptance criteria into `docs/tasks/N-issue-description.md`, leaving a concise
  summary and numbered reference. Reuse an existing description for the same issue.
- New descriptions use the next unused number above the highest top-level task number in
  the tracker or `docs/tasks/`. Nested decision-ticket numbers are local to their parent
  task. Do not renumber existing tasks.
- Treat completed descriptions as historical requests and decisions. Keep dated
  evidence; distinguish it from current behavior when adding an update.
- Verify status moves against repository history and available Kandev evidence. A
  completed decision map does not mean its implementation handoff is implemented.
- Any completed tasks which haven't yet been moved from `## In Progress` to
  `## Completed / Accepted` should be moved there.
- Any in progress tasks which haven't yet been moved from `## Ordered backlog` or
  `## Scheduled` to `## In Progress` should be moved there.
- Ensure there are no duplicate sections, and that they are in the correct order:
  `## Unverified proposals` -> `## Ordered backlog` -> `## Scheduled` ->
  `## In Progress` -> `## Completed / Accepted` -> `## Rules`.

### Modifying issues

- Ensure dependencies between issues are correctly updated.
- State dependencies using
    - indented `- Depends on: [N]` bullets in TASKS.md, and
    - YAML frontmatter in description files.
- Ensure backlog order respects dependencies.
- When you move an issue, preserve its identifier, text and line wrapping. Change only
  the status checkbox when completion or user acceptance requires it. This keeps moves
  identifiable in Git and reduces avoidable conflicts.

### Workflow for new issue completion

1. Choose issue and schedule work (typically by a heartbeat)

- Pick the first backlog issue with no dependency to any uncompleted issue.
- Move it under `## Scheduled` in `TASKS.md` and remove it from `## Ordered backlog` in
  the `main` branch and commit.

2. Work on the issue (typically by a task workflow)

- Rebase the worktree feature branch on `main` before moving the issue, and keep it
  rebased afterwards.
- Move the issue under `## In Progress` in `TASKS.md` in the worktree branch, ensure
  it's not in `## Ordered backlog`, and commit.
- Create or update, review and refine a plan in docs/tasks/<N-issue-description>.md in
  `main` if more description is needed than nicely fits in a bullet point. If you
  created a plan document, link to it using a new `[N]` reference-style link.
- Commit the description file (if any) in `main`.
- Implement the plan, and lint, test, review and refine the implementation in the
  worktree feature branch.

3. Merge and deploy (typically by last steps of a task workflow)

- Merge the rebased branch on `main`, and remove the worktree and branch.
- Move the issue from `## In Progress` to `## Completed / Accepted` in TASKS.md with
  `[ ]`, pending user acceptance, and commit.
- Do any deployment steps if defined in the general development workflow.

When TASKS.md conflicts during a rebase or merge:

- Use `main`'s version of every section as the base.
- Apply again only the move of your own issue.
- Never restore, add again or re-word another issue's bullet from your side of the
  conflict.
- After resolving the conflict, read the whole file and ensure that each bullet is under
  exactly one `##` heading.
