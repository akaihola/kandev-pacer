# Idle heartbeat refill

## Behavior

An open provider with no running or launchable work can request one early heartbeat
refill after a successful proactive request made while work was still running.
This avoids waiting out the remaining cooldown when that work finishes.

This is not an unconditional cooldown bypass. Repeated empty-board attempts and
failed webhook requests remain throttled. The existing waiver after a normal task
launch is unchanged. A running heartbeat, exhausted shared capacity, a turn spent
this tick, or relevant pending bookings prevent the new idle retry.

## Implementation

`Pacer.maybe_heartbeat()` tracks one unused idle retry. It considers every open
provider and counts only work in open model-scope classes for this exception.
Normal prefetch counting, provider gates, queue ordering and launch limits stay
unchanged. Fresh bookings also block a retry after fallback or unconfirmed actions
that did not increment the normal per-tick turn counter.

Implementation: `a3bd3da`. Review repair: `2a4af0e`.

## End-user checks

Observe the pacer's output in tmux pane `%1`, or read recent lines with:

```bash
tmux capture-pane -p -t %1 -S -40
```

- After a successful proactive heartbeat while a provider still has running work,
  let that work finish. If an open provider has no work and capacity is available,
  expect one `heartbeat: ... has 0 launchable -> webhook 2xx` before the previous
  cooldown expires. This can happen while another provider remains paused.
- Leave the board unchanged. Following ticks should report cooldown rather than
  repeating the webhook. A 409 or failed request must also wait out cooldown.
- During a running heartbeat, a full shared limit, or a fallback launch in the same
  tick, expect no extra idle retry. Already queued tasks retain their normal order.

Use ordinary work already on the board; no synthetic production tasks or quota
changes are needed. These checks depend on the stated gate and workload conditions.

## Deployment, 2026-09-21

Merged into the main checkout with `--no-ff` as `4f630c8`. The issue was moved from
In Progress to Completed after deployment. The focused suite passed all 291 tests
from the merged checkout with isolated test settings; formatter and lint checks
passed during review. The regression reproduced +0.082 Fable headroom, -0.007
OpenAI headroom and 1674 seconds of remaining cooldown before the fix.

Restarted the terminal-owned pacer in pane `%1`, replacing PID 903669 with PID
2699351 from `/home/agent/prg/kandev-pacer`. Retained the `pebble` quota-source
override and loaded the existing private environment file. The replacement logged
successful startup and fresh provider ticks. Both gates were paused at verification,
so a live idle-retry webhook was not forced or claimed. Restarted the development-tree
`kandev-learn.service`; its one-shot run returned status 0 and Result=success.

Unrelated pending tracker edits and two credential-recovery documents were preserved
in Git stash `a81b24c2bd2921be4e18356b8dede27a743cd457`, named
`pre-deploy idle-heartbeat: preserve unrelated tracker and credential docs`.
They remain recoverable but are not applied to the main working tree. Review this
stash before reapplying it, since the heartbeat issue has since moved to Completed.

The unrelated Fable diagnostics issue already appears under both Scheduled and
Completed on main. This workflow preserved it to keep the tracker diff limited to
the heartbeat issue; it did not certify global tracker uniqueness.
