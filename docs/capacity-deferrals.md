# Capacity deferrals: verification and deployment

The inline capacity-deferral follow-up in [TASKS.md](../TASKS.md#completed)
closes the gap recorded by Kandev task dd6897a9-b54e-4ad4-b9b5-b2372292f4b1.
See [the behavior and configuration notes](../README.md#session-capacity-deferrals).

## End-user verification

Use a designated test task when the backend session ceiling is already full.
Do not lower production limits or start extra agent work just to force this check.

1. Let pacer attempt an eligible task. Its output should report waiting for session
   capacity, with a separate capacity-deferred count. Across subsequent ticks the
   task should stay pending without duplicate launches, drained prompts, or a new
   BLOCKED flag caused by capacity waiting. Existing unrelated failure history remains.
2. Let backend capacity become available. Confirm the deferred task starts once and
   running sessions take over its reserved slot. Other work must still respect the
   configured provider and total caps.
3. If a deferred launch is cancelled and its backend marker disappears without a
   visible session, allow the existing booking timeout to pass. With an open quota
   gate, the task becomes eligible in normal queue order; queued prompts retain
   their precedence over workflow prompts. With a closed gate, it remains waiting.
4. While enough deferred work is pending to meet the heartbeat refill threshold,
   confirm it does not request more work just because the launchable count is zero.
   A deferral alone does not waive the heartbeat cooldown.

## Deployment on gogo, 2026-09-27

- Merged three feature commits with `--no-ff` as `7851732a2d81a3cfdc9e18dc4375aba752bbcaf8`
  in `/home/agent/prg/kandev-pacer`. Implementation is in `cd150cb`; fresh-admission
  review repairs are in `b4f13bc`.
- Before deployment, 319 focused tests and 530 full-suite tests passed; 20 live
  tests were skipped in the offline suite. Formatting, lint, type, and whitespace
  checks passed. Tracker validation found 29 issues, each under one heading.
- The first live check encountered Kandev HTTP 503 `persistence_unavailable`.
  After the backend recovered, all 18 selected live backend contract checks passed,
  including queue reads. The two direct-provider quota checks were deselected
  because this deployment uses the existing `pebble` quota source.
- Restarted the terminal-owned pacer in tmux pane `%1`. Old PID `2699351` exited
  before replacement PID `1717977` started at 00:18:47 UTC. Retained the private
  environment file and `PACER_QUOTA_SOURCE=pebble`. Use the installed Bash at
  `/home/agent/.local/bin/bash` on this Nix host; `/bin/bash` does not exist.
- Startup printed the merged source revision at 00:18:48 UTC. Fresh ticks included
  the new `deferred` field and normal provider gating; the heartbeat webhook
  returned 200. Initial deferred counts were zero, so live capacity replay was not
  forced or claimed. The capacity transitions are covered by offline regressions.
- Restarted `kandev-learn.service`, which runs from the development tree. It exited
  at 00:18:09 UTC with `Result=success` and `ExecMainStatus=0`.

Kandev still owns replay timing. Rechecking quota at the moment of backend replay
requires a separate backend admission change. Historical BLOCKED tasks were not reset.
