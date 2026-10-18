Steward issue, 2026-10-18. Goal: agentic-leverage. [56] rebased the pacing
hook onto T3 v0.1.0 (code commit `91cfe8521f`, base `81493efb71`) and
validated it with 77 tests and a scoped typecheck. Antti will deploy that
patch on gogo's live T3 server, where every thread and paced run lives. A
rebase across the V2 release can also break what the tests don't reach: an
upstream change to code the hook relies on (queued runs, turn dispatch,
stop and interrupt, provider restarts, settings) that compiles but changes
behaviour. [57] rehearses the cutover on a dev server; this issue is the
independent read of the rebase itself, before deployment.

## Work

1. Read [56]'s Result section and the branch README
   (`docs/pacing-hook/README.md` on t3code branch
   `t3code/implement-pacing-hook`). Use `git -C ~/repos/ai/t3code show`;
   check nothing out.
2. Compare the hook before and after the rebase:
   `git -C ~/repos/ai/t3code range-diff` between
   `backup/pacing-hook-before-hai-56` (on the 2026-10-11 base `5cbe82a870`)
   and `91cfe8521f` (on `v0.1.0`). Account for every hunk that changed
   beyond context, including the Stop-handling conflict [56] resolved.
3. List the upstream commits between `5cbe82a870` and `v0.1.0` that touch
   files or functions the hook changes or calls. For each, check that the
   hook's invariants still hold: a paced prompt (ending in `...` or `…`)
   parks until admitted; `pacedRuns` lists every parked run;
   `queued-run.admit` starts exactly one; stop, settle, delete and a server
   restart return a parked run to user control; unpaced prompts are
   unaffected.
4. The local checkout has v0.1.1 nightlies up to
   `v0.1.1-nightly.20261017.3238`; read anything newer with `gh` against
   `pingdotgg/t3code`. Say whether a fix after v0.1.0 in code the hook
   touches means Antti should deploy a newer tag, and if so whether the
   patch applies to it (`git archive` that tag into `$TMPDIR`, then
   `git apply --check`).
5. Confirm the patch file
   `~/.local/state/t3-upgrade-prep/2026-10-18/t3code-pacing-hook-v0.1.0.patch`
   matches `91cfe8521f` (its SHA-256 starts `4379ccc2`).

## Ground rules

- Read only in t3code: no checkout, worktree, fetch, install, build, test
  or dev server in `~/repos/ai/t3code` or any of its worktrees. [57] is
  using them, and gogo has 12 GB of memory.
- Never touch the live T3 server, `~/.t3/userdata`, the running pacer, its
  config `~/.config/kandev-pacer/config-t3.toml` or
  `~/.local/state/kandev-pacer/`.
- In this repository, change only the report, this description file and
  the issue's tracker moves. Don't push.

## Deliverable

`docs/reviews/2026-10-18-pacing-hook-rebase-review.md`. Open with a
go/no-go paragraph for deploying the patch, and a yes or no on deploying a
newer upstream tag instead. Then each finding with its severity (blocks
deployment, fix after deployment, or note), evidence (file and line on
both sides, upstream commit) and the smallest fix. Don't fix anything
here: the steward files the fix issues. Record a short summary in a
Result section of this file.

## Acceptance

- Every hunk of the range-diff and every overlapping upstream commit is
  accounted for.
- The go/no-go and the newer-tag answer each give their reasons.

## Result, 2026-10-18

Go: deploy the v0.1.0 patch. No newer tag. Full review:
`docs/reviews/2026-10-18-pacing-hook-rebase-review.md`.

- The range-diff changes one hunk of 25 beyond context: Stop. [56] resolved it
  correctly. Upstream `05e4f888a0` (#19099) now cancels buffered provider wakes
  at Stop, and those are agent messages the hook never paces. The only slip is
  a lost `Pacing patch:` comment prefix. The docs commit, the three new files
  and the other 24 hunks are identical apart from line offsets.
- The 13 upstream commits that touch files the hook changes and the 19 that
  touch code it calls or depends on leave the gate, the dispatch queue branch,
  edit, promote-to-steer, release, the admit checks and the post-commit drain
  as they were. They add no start path that can reach a paced run, and leave
  the `pacedRuns` tables and snapshot route unchanged.
- Nothing blocks deployment, and nothing needs a fix after it. All eight
  findings are notes. Two correct this issue's invariants: a server restart
  leaves a parked run admittable by the pacer, as the spec intends, and settle
  is refused while a parked run exists. One refines [56]'s checklist: since
  v0.1.0 a restart also wakes completed runs whose background work it cut
  (`eeaac49a9d`, #19291), on top of the continuation now on by default
  (`b9ad3daf8d`, #18461). Antti still decides that setting before cutover.
- No stable tag follows v0.1.0. No fix up to `v0.1.1-nightly.20261018.3285`
  touches code the hook relies on. The patch fails `git apply --check` on
  `v0.1.1-nightly.20261017.3238` in the contracts file (`86534db4d3`, #19559),
  so moving to 0.1.1 needs a rebase.
- The patch file's SHA-256 is `4379ccc2…`. It is byte-identical to
  `git format-patch -1 91cfe8521f` and applies cleanly to a temporary index
  of v0.1.0.

Read-only throughout. Nothing in t3code was checked out, fetched, built or
run. The live T3 server, its userdata, the running pacer and its state were
not touched. Nothing was pushed.
