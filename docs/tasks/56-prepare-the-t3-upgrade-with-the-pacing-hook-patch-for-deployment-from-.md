# Prepare the T3 upgrade with the pacing-hook patch for deployment from atom

Steward issue, 2026-10-18: vault [12], moved here because the vault's tracker
has been blocked since 2026-10-17 15:15 by uncommitted changes that only Antti
can commit. It is step 2 of the pacing-hook sequence (decisions D29–D31 in
`/home/agent/my-knowledge/agents/steward/decisions.md`). The agent prepares;
Antti deploys from atom. The text below is vault [12]'s, unchanged apart from
this paragraph and the ground rules at the end.

## Prerequisites in other trackers

Report blocked and stop unless both are merged on their `main`:

- kandev-pacer [49], T3 mode on the V2 protocol. Without it the upgrade stops all
  pacing.
- skills-akaihola [6], the `t3-start-thread` helper on V2. Without it the
  steward's scheduler and agent thread messaging stop.

## Work

1. Rebase the pacing-hook commit onto upstream v0.1.0 or the newest 0.1.1
   nightly, whichever the branch README recommends. v0.1.0 (2026-10-15) is the
   first stable V2 release and the same commit as nightly
   0.0.46-nightly.20261015.3176; there is no stable 0.0.46. It is 281 commits
   past the hook's base `5cbe82a870`, and 9 of them change
   `apps/server/src/orchestration-v2/Orchestrator.ts`. Branch
   `t3code/implement-pacing-hook` in `/home/agent/repos/ai/t3code`, local to gogo:
   code `ea944594ce`, docs `c45bcb2e63`.
2. Run `Pacing.test.ts`, `PacedQueue.test.ts` and
   `~/repos/ai/t3code-lowmem/t3-lowmem tc --importers` (the low-memory typecheck a
   rebase needs; see the README).
3. Export the rebased code commit with `git format-patch -1`. The branch exists
   only on gogo, so name how atom gets the patch; if no route is documented, say
   so in the closing message as an open question for Antti.
4. Record in the branch's `docs/pacing-hook/README.md`: the new hash, the base,
   test results, and Antti's deployment checklist:
   - Back up `~/.t3/userdata/state.sqlite` first; the V2 server imports the V1
     database on its first start.
   - Install the helper from skills-akaihola [6] before the upgrade.
   - kandev-pacer `main` speaks only V2 since `8deba37` ([49]; `05986f7` before
     [54] rewrote kandev-pacer's history on 2026-10-17), and the running
     pacer speaks only V1. So restart the pacer on `main` right after the
     upgrade, not before it: a restart before the upgrade stops all pacing, and
     so does an upgrade without a restart. The steward's review (vault [14])
     found one blocking defect, kandev-pacer [53]: upgrade only once [53] is
     merged on `main`.
   - Keep the pacer's T3 server URL and its state directory
     (`~/.local/state/kandev-pacer/t3/`), so that imported threads reconcile
     with the existing launch records. Just before the upgrade, check that no
     launch record is `prepared`, `scheduled` or `submitted`; inspect any that
     is before removing anything.
   - Since v0.1.0, "Continue threads after restarts" is on by default
  (t3code #18461): a thread cut off by a server restart continues by
  itself, up to 3 times, without pacer admission. Ask Antti whether to
  switch it off before the upgrade. Keep `autoResumeLimitedThreads` and
  `snoozeLimitedThreads` off (their defaults): runs resumed at a reset
  would bypass the pacer.
- Only one server may open `~/.t3/userdata` (t3code #16102; a second one
  exits with code 78), so stop every other T3 server and desktop app
  first. V1 clients cannot connect to a V2 server: the mobile app needs
  2.0.
- The import copies `state.sqlite` (774 MB on 2026-10-17) to
  `statev2.sqlite` and migrates the copy. It does not carry over live
  provider sessions, checkpoints, tool activity, approvals or plans
  (t3code #14871).
- After the upgrade: check that the t3code project's "Setup Worktree" override
     (`t3-lowmem setup`) still wins and runs on worktree creation; send a paced
     prompt from the web and the mobile client; expect D31's one-time "Submitting
     message" stall on threads imported from 0.0.45.

## Acceptance

- The rebased commit and patch file exist, the tests and typecheck pass, and the
  README records hash, base, results and the checklist.
- Nothing is deployed; gogo's live T3 and its database are untouched.

## Ground rules

- The work happens in `/home/agent/repos/ai/t3code` on branch
  `t3code/implement-pacing-hook`. Rebase in a separate worktree of that
  repository (its README and `~/repos/ai/t3code-lowmem/t3-lowmem` say how),
  never in its main checkout, and reach it with `git -C` and absolute paths.
- Keep memory in check: gogo has 12 GB. No full `apps/server` typecheck at
  `--checkers 1` (it pushed gogo into swap on 2026-10-15); use the low-memory
  typecheck named above, and stop if swap use grows.
- In this repository, change only this description file and the issue's
  tracker moves. Don't push, deploy or restart anything, and don't touch the
  running T3 server, its `~/.t3/userdata`, the running pacer or its state.
- Record the outcome, including a blocked one, in a `## Result, YYYY-MM-DD`
  section here as well as in the branch README.


## Result, 2026-10-18

Blocked by the required memory stop condition. Keep [56] In Progress; acceptance
is not met, and [57] must not use this rebase as a validated build yet.

Both prerequisites are merged on local main: kandev-pacer [49] at `8deba37`
and skills-akaihola [6] at `2aadff5`. The deployment blocker [53] is also merged,
at `3daa054` after [54]'s history rewrite.

The code was rebased in the existing separate t3code worktree
`/home/agent/.t3/worktrees/t3code/t3code-9f66b19e`, on the required branch.
The new code commit is `91cfe8521fe26753dd9fa34db4ba35c100147b43`, based on
stable v0.1.0, `81493efb71e8e8244f95d3e9f71d8369654ab151`. The branch README
had no preference for a newer nightly. The original branch is preserved at
`backup/pacing-hook-before-hai-56`. One conflict in Stop handling was resolved
by keeping upstream's buffered-notification cancellation and the hook's
return of parked runs to user control. Range-diff shows only that adaptation.

The capped dependency refresh, `t3-lowmem run vp i -- --yes`, was stopped by
a host-swap monitor when usage rose from 3,357,788 KiB to 3,358,300 KiB. The
monitor returned exit 75. The source of that increase is unproven, but the
issue requires stopping on growth. No low-memory scopes remained running.
The install log and monitor are in
`/home/agent/.local/state/t3-upgrade-prep/2026-10-18/`.

Neither focused test file nor the low-memory importer typecheck has run on
this rebase. No patch was exported. The branch README records the blocked
result, hashes, resume commands and Antti's full deployment checklist.
Resume the install and validation only with stable memory headroom, using
`tc --importers --base v0.1.0` to scope the typecheck to the private patch.
Export and verify the code patch after validation. No atom transfer route
was found in the hook or low-memory documentation; Antti must choose one.

Nothing was pushed, deployed or restarted. The live T3 server, its database,
the running pacer and its state were untouched.
