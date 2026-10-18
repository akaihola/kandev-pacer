# T3 cutover rehearsal, 2026-10-18

## Cutover blockers

The rehearsal stopped during setup under [57]'s memory rule. **Do not treat
this report as cutover validation.** None of the six server scenarios ran.
The integration and restart-accounting questions remain open. No product
defect was reproduced, and no fix was attempted.

Host swap usage increased by 76,244 KiB, about 74.5 MiB, after creating the
separate worktree. [57] says "stop if swap use grows". Its rule has no
512 MiB allowance. [56]'s revised allowance applies to that preparation task,
so it was not carried into this rehearsal. The measurements do not establish
which process caused the increase. Available memory remained above 8 GiB.

## Inputs and setup

Work ran on gogo at approximately 05:12–05:13 EEST on 2026-10-18.

- Pacer main and this worktree initially pointed to `d879ee2`. Rebase on local
  main reported that the branch was up to date. The later tracker-only commit
  did not change pacer code.
- Read [56]'s complete result and the hook branch README. Its 05:11 EEST update
  supersedes the earlier blocked result. The validated hook code is
  `91cfe8521fe26753dd9fa34db4ba35c100147b43`, based on stable v0.1.0,
  `81493efb71e8e8244f95d3e9f71d8369654ab151`.
- [56] records 40 passing pacing tests, 18 queue tests, 19 Stop tests and a
  passing importer-scoped typecheck. Those are preparation results, not
  rehearsal evidence. No tests or typechecks were run again here.
- Read only the live config fields needed to prepare isolation. Concurrency is
  `concurrency = 1`, `concurrency_total = 4`. No config was written. One saved
  launch record was inspected read-only to learn its schema; none was copied.

Commands used for initial checks:

```bash
export SHELL=/bin/bash
free -m
git branch --show-current
git log -1 --oneline
git rev-list --count HEAD..main
git rebase main
cat docs/tasks/56-prepare-the-t3-upgrade-with-the-pacing-hook-patch-for-deployment-from-.md
cat /home/agent/.t3/worktrees/t3code/t3code-9f66b19e/docs/pacing-hook/README.md
rg -n 'concurrency|mode|t3_|provider' /home/agent/.config/kandev-pacer/config-t3.toml
```

Initial `free -m` output:

```text
               total        used        free      shared  buff/cache   available
Mem:           11859        3825         182         134        8302        8033
Swap:          13055        3533        9522
```

Setup used Python's `tempfile.mkdtemp(prefix='pacer57-')`, producing
`/tmp/pacer57-h2uk3dgm`. Before checkout, it recorded `/proc/meminfo` values
in `memory-start.json` inside that temporary directory. The checkout command
was:

```bash
git -C /home/agent/repos/ai/t3code worktree add --detach \
  /tmp/pacer57-h2uk3dgm/t3code \
  91cfe8521fe26753dd9fa34db4ba35c100147b43
```

Python then linked the existing validated worktree's root, app and package
`node_modules` directories into matching locations in the temporary checkout.
No install, build, server or dependency command ran through those links.
Only temporary links were created; their targets were not modified.

The next memory check read `/proc/meminfo` again and printed:

```text
start_swap_kib 3627272 now_swap_kib 3703516 available_kib 8496792
```

The baseline had `SwapTotal=13368912`, `SwapFree=9741640` and
`MemAvailable=8269024`, all in KiB. Swap used is `SwapTotal - SwapFree`.
The increase triggered the stop before database backup or server startup.
These are two samples around setup, not continuous monitoring, so an
intermediate peak cannot be excluded.

No port was allocated. No isolated base directory, pruned seed, scratch
tracker, copied launch records or scratch config was created. Import timing
is unavailable because no import started. The requested pacer invocation
was not executed. On a future attempt it must use an isolated config and
state, with the concurrency values above:

```bash
uv run --extra t3 --frozen kandev-pacer --config "$scratch_config" --dump-order
uv run --extra t3 --frozen kandev-pacer --config "$scratch_config"
```

Those two commands describe the pending run, not commands executed here.
The backup, pruning, import and scratch-project steps remain pending too.

## Scenario outcomes

| Scenario | Outcome | Evidence and reason |
| --- | --- | --- |
| 1. JSON dump and reconciliation of imported finished threads | Not run | Memory stop occurred before a database backup, import, copied records or pacer startup. No JSON or reconciliation log exists. Duplicate prevention and absence of tracker holds are unverified. |
| 2. Saved `submitted` record held for migration, [53] | Not run | No scratch state was created. No migration hold was exercised against a real server. |
| 3. Scratch issue launches, completes and is observed | Not run | Neither server nor pacer started. Zero provider turns were submitted. |
| 4. Unpaced manual thread takes no pacer slot, [51] | Not run | No scratch project or manual thread was created. Slot accounting was not observed. |
| 5. API prompt ending in `…` parks and receives `queued-run.admit` | Not run | No API request was sent. Parking, discovery and admission remain unverified with main. |
| 6. Server restart during a paced turn | Not run | No dev server or paced turn existed to restart. Continuation and pacer accounting remain unverified. |

There are no server or pacer log excerpts because neither process started.
The setup and memory excerpts above are the evidence for the common blocker.
Existing fake-server tests and [56]'s focused server tests cannot establish
these integration outcomes.

## Cleanup and live-state boundary

Removed the detached temporary worktree with:

```bash
git -C /home/agent/repos/ai/t3code worktree remove --force \
  /tmp/pacer57-h2uk3dgm/t3code
```

Python's `shutil.rmtree` then removed the temporary root, including dependency
links and memory evidence, after transcribing the measurements above. Its
verification printed `temporary root removed True`. Git's registered
worktrees were checked afterward to confirm that temporary path was absent.

No database backup or copy was created, so no private database remains from
this attempt. No server, pacer or provider process was started; there was
nothing to stop. No request was made to the live T3 endpoint. The live
database was not opened, and the live config and launch state were only read.
No live process was restarted, no product code changed, and nothing was
pushed or deployed. The pre-existing [56] worktree was retained.

## Restart-setting recommendation

Provisionally turn off "Continue threads after restarts" for cutover until
scenario 6 passes. This is a conservative recommendation based on source,
not an observed integration failure. At the validated hook commit,
`packages/contracts/src/settings.ts` defaults
`continueThreadsAfterServerUpdate` to `true`.
`apps/server/src/orchestration-v2/RestartContinuation.ts` limits consecutive
continuations to three and dispatches `message.dispatch` with
`dispatchMode: { type: "start_immediately" }`, without asking the external
pacer for admission. How the pacer counts and observes that continuation
still needs the disposable-server test. Disabling it means interrupted work
needs manual attention after a restart. This rehearsal changed no setting.
