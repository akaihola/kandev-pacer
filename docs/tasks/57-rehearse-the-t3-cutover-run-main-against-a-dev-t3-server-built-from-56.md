---
depends-on: [56]
---

# Rehearse the T3 cutover: run `main` against a dev T3 server built from [56]

Steward issue, 2026-10-18. Goal: agentic-leverage. When Antti upgrades gogo's T3
to v0.1.0 with the pacing hook, this repository's `main` replaces the running V1
pacer at once (decision D29 in
`/home/agent/my-knowledge/agents/steward/decisions.md`). Nothing has run `main`
against a real V2 server yet: [49]'s tests use a fake, the steward's review
(vault [14]) was static and found [53], and the hook's end-to-end check on
2026-10-15 used a stand-in for the pacer (t3code branch
`t3code/implement-pacing-hook`, `docs/pacing-hook/README.md`, "Checked end to
end"). If the cutover fails, all pacing stops until someone debugs it. This
rehearsal finds out first, on a disposable server.

## Setup

1. Read [56]'s result and the branch README it updated. Start a dev server from
   the rebased branch in a separate t3code worktree, as the README's "Checked
   end to end" section describes, with its own base directory under `$TMPDIR`
   (`T3CODE_HOME` or `--base-dir`) and a free port on 127.0.0.1. If [56]
   reported blocked, use the branch as it is and say so.
2. Seed it from a read-only backup of `~/.t3/userdata/state.sqlite`, pruned as
   the README says: keep a few stopped pacer threads that have launch records,
   delete the auth and command-receipt rows. The dev server imports it on
   first start; time the import.
3. Copy the matching launch records from `~/.local/state/kandev-pacer/t3/`
   into a scratch state directory, and write a scratch pacer config under
   `$TMPDIR` that points at the dev server, that state directory and a scratch
   tracker: a throwaway git repository in `$TMPDIR` with a `TASKS.md` holding
   two trivial issues. Keep the live config's concurrency settings.

## Scenarios

Run `main` (`uv run --extra t3 --frozen kandev-pacer --config <scratch>`) and
record for each scenario what happened, with log excerpts:

1. `--dump-order` prints JSON, and startup reconciles the copied records with
   the imported threads: no relaunch of finished issues, no held tracker.
2. A saved launch record left in `submitted` is held for migration ([53]).
3. One scratch issue launches, its turn completes, and the pacer sees it.
4. An unpaced thread you start by hand doesn't take a pacer slot ([51]).
5. A prompt ending in `…`, sent to the dev server through the API, parks, and
   the pacer admits it with `queued-run.admit`.
6. Restart the dev server during a paced turn. Record what "Continue threads
   after restarts" (on by default since v0.1.0, t3code #18461) does to that
   turn and whether the pacer counts and observes it correctly. This answers
   the open cutover question of whether Antti should switch that setting off.

## Deliverable

`docs/reviews/2026-10-18-t3-cutover-rehearsal.md`: the setup commands, each
scenario's outcome (passed, failed or not run, and why), and every defect
with the evidence a fix issue needs. Put the defects that should block the
cutover at the top, and end with a recommendation on the restart setting.
Don't fix defects here: the steward files them. Record a short summary in a
`## Result, YYYY-MM-DD` section of this description file.

## Ground rules

- Never touch the live system: not the T3 server at `100.81.121.7:3773`, not
  `~/.t3/userdata` (read-only backup only), not the running pacer, its config
  `~/.config/kandev-pacer/config-t3.toml` or `~/.local/state/kandev-pacer/`
  (copy only). Only one server may open a base directory; never point the dev
  server at the live one.
- Provider turns only in the scratch repository, and at most about six short
  ones.
- gogo has 12 GB of memory. Check `free -m` before starting the dev server and
  run no full typecheck. Stop if `MemAvailable` in `/proc/meminfo` falls below
  2 GiB, or if swap use grows by more than 512 MiB over its level when the step
  started. (Revised by the steward on 2026-10-18 08:00, as for [56]. The first
  rule, "stop if swap use grows", stopped the first run during setup on a
  74.5 MiB rise: gogo keeps about 3.2 GiB of idle pages in swap, and other
  processes move that figure all the time. [56] peaked at 195 MiB growth with
  4.85 GiB available.)
- At the end, stop every process you started, remove the worktrees, and
  delete the database copies, which hold Antti's private conversations.
- In this repository, change only the report, this description file and the
  issue's tracker moves. Don't push or deploy.

## Acceptance

- Every scenario has an outcome with evidence, or the reason it could not run.
- No live state changed, and no copy of the database is left behind.

## Result, 2026-10-18

Rehearsal report completed with all six scenarios marked not run. Setup stopped
under this issue's memory rule when host swap increased by 76,244 KiB, from
3,627,272 to 3,703,516 KiB, after creating the disposable worktree. Available
memory remained above 8 GiB; the source of the swap growth is unproven. [56]'s
validation had completed, but its revised 512 MiB allowance does not amend
this issue's stricter stop rule.

See [the rehearsal report](../reviews/2026-10-18-t3-cutover-rehearsal.md) for
commands, measurements, each scenario's missing evidence and cleanup. No dev
server, database copy, pacer or provider turn was started. The temporary t3code
worktree and scratch directory were removed. Live state was unchanged.

This is not cutover approval. Main against the real V2 server and restart
accounting remain unverified. Provisionally disable "Continue threads after
restarts" at cutover until scenario 6 passes; that recommendation is based
on source inspection, not a reproduced defect. No setting was changed.
