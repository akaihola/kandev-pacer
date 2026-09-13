# Issue tracking for kandev-pacer

Rules for TASKS.md usage are at the bottom of the file.

## Ordered backlog

- [*] Launch only the primary session. `pick_intent` in `selection.py` takes the newest
  session, which on a task with `survey-*` child sessions is a read-only subagent: in the
  48 h to 2026-09-06 21 of 60 launches went to such sessions and four cards were flagged
  stalled. Prefer the session with `is_primary`, then the newest primary-less session
  only when the task has no primary at all. One test per case in `test_selection.py`.

- [1] Read the quota from the pebble-quota collector as a configurable alternative
  source. The direct read of the Anthropic and Codex usage endpoints stays the default.
  On `agent@gogo` the collector's snapshot is used, which removes the blind hours the
  expiring Claude Code token causes at night.

- [3] Advance a finished-but-unsignalled task instead of flagging it BLOCKED. The stall
  detector gives up after `PACER_STALL_AFTER` launches that left the worktree untouched,
  which is right for a wedged card and wrong for a primary session that finished its work
  and never called `step_complete_kandev`. Add `PACER_STALL_ACTION` (`block`, the default,
  or `signal`), and on `signal` send kandev's `mcp.step_complete` on the agent's behalf
  when the stalled session is the task's primary session, is waiting for input, and its
  step requires the signal. Falls back to `block` when the step does not move.

- [*] Change the default `PACER_INTERVAL` from `120` to `30` and update the README
  example; `agent@gogo` already runs 30 through nix. Rotate `pacer.jsonl` weekly with a
  `RotatingFileHandler`-style size cap of 50 MB and 4 backups; the file was 407 MB and
  unrotated. Update the "Logging" section's size estimate.

- [*] Order launchable tasks by prompt-cache deadline while the gate is open (rule 1 of
  `docs/cache-aware-pacing.md`): the task whose session's last turn ended longest ago
  but is still inside its provider's cache TTL goes first. No knob. Keep pull order and
  drag order as the tie-break so `--dump-order` still explains the queue. Worth less
  since the 2026-09-11 subscription upgrade — a gate that stays open makes fewer
  cache-killing waits — but the rule has no knob and no failure mode.

- [*] Gate on the weekly windows and make the 5-hour rule soft. When the Claude gate
  paused, the 5-hour window bound it six times more often than the 7-day one, while the
  general weekly window ended two of the last five weeks at about half. Add
  `PACER_ALPHA_5H` (default `1.0`, so nothing changes until set) applied to 5-hour
  windows only, and `PACER_INTERACTIVE_HOURS` (e.g. `08-23`, default empty) outside of
  which the 5-hour rule is skipped and only the weekly windows and the backstop gate.
  Deprioritized 2026-09-11: on Max 20x the weekly window is far from binding. Keep it
  for the two knobs, which matter again if usage grows.

- [*] Fall back to another agent profile when a provider's gate has been closed for
  longer than `PACER_FALLBACK_AFTER` (seconds, default `0` = off). Map with
  `PACER_FALLBACK_PROFILES`, e.g. `"Luna low=Sonnet low"`, resolved by name through
  `GET /api/v1/agents`. The fallback is used only for `intent:start` launches and is
  logged as `fallback` in the tick record. Deprioritized 2026-09-11: with Max 20x and
  ChatGPT Pro, exhaustion is no longer the common case. Keep it for a provider that is
  down rather than spent.

## Scheduled

## In progress

## Completed

- [2] Call the Heartbeat automation's webhook when a provider is running out of work.
  The webhook address and secret are configuration; the values from kandev on
  `agent@gogo` go into the layered config. The automation's schedule has been removed in
  favour of that webhook, and nothing calls it yet, so the board refills only when the
  automation is fired by hand.

- [4] Collect archived session observations without model calls.

- [*] Enrol workflows through kandev's step update endpoint instead of writing
  `~/.kandev/data/kandev.db`. `PUT /api/v1/workflow/steps/{id}` takes every step field,
  `stage_type` included, and needs no login, so `make-paced` drops sqlite, `--db` and
  `KANDEV_DB`; the README's "no API" claim goes with them.

[1]: docs/tasks/1-quota-from-pebble-collector.md
[2]: docs/tasks/2-heartbeat-webhook.md
[3]: docs/tasks/3-stall-detector-signal.md
[4]: docs/tasks/4-session-observations.md
[*]: TASKS.md

---

## Rules

Here are the rules for TASKS.md usage:

### TASKS.md maintenance sessions

- Each backlog item must be prefixed with either
  - a numbered reference-style link (e.g. `[1]`) to a description file, or
  - `[*]` to indicate no description file is needed for a simple task.
- Link references are listed between `## Completed` and `## Rules`.
- If any issue is missing a link:
  - Create the first missing numbered description file in
    docs/tasks/<N-issue-description>.md and add the link

### Modifying issues

- Ensure dependencies between issues are correctly updated.
- State dependencies using
  - indented `- Depends on: [N]` bullets in TASKS.md, and
  - YAML frontmatter in description files.
- Ensure backlog order respects dependencies.

### Workflow for new issue completion

1. Choose issue and schedule work (typically by a heartbeat)
- Pick the first backlog issue with no dependency to any uncompleted issue.
- Move it under `## Scheduled` in `TASKS.md` and remove it from `## Ordered
  backlog` in the `main` branch and commit.

2. Work on the issue (typically by a task workflow)
- Move the issue under `## In progress` in `TASKS.md` in the worktree branch,
  ensure it's not in `## Ordered backlog`, and commit.
- Create or update, review and refine a plan in
  docs/tasks/<N-issue-description>.md in `main` if more description is needed
  than nicely fits in a bullet point. If you created a plan document, link to it
  using a new `[N]` reference-style link.
- Commit description file (if any) and TASKS.md in `main`.
- From now on, ensure worktree feature branch is always rebased on `main`.
- Implement the plan, and lint, test, review and refine the implementation in
  the worktree feature branch.

3. Merge and deploy (typically by last steps of a task workflow)
- Merge the rebased branch on `main`, and remove the worktree and branch.
- Move the issue from `## In progress` to `## Completed` in TASKS.md and commit.
- Do any deployment steps if defined in the general development worklow.
