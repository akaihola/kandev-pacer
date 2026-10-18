# Commit the In Progress move as soon as an issue starts

Approved by Antti on 2026-10-18 (steward decision 11, proposal P12 in
`/home/agent/my-knowledge/agents/steward/proposals.md`).

The T3 worker prompt tells the worker to move its issue to `## In Progress` with
`hai-tasks update` on the canonical tracker, which is the shared main checkout. In
some repositories the worker never commits that move there; filemill's Rules put it
on the worktree branch. If the worker then can't merge, the uncommitted `TASKS.md`
keeps the checkout dirty, and the pacer skips the whole tracker until someone
commits it.

Evidence, 2026-10-17: filemill [25]'s worker (thread `520cb8de`) moved [25] at 08:27
UTC, stopped at 08:49 on main's red server suite, and left the move uncommitted.
filemill's four launchable issues waited until a steward run committed it at 11:06
(`b675ffc7`); Codex had nothing else to run from 09:12. The next worker
(`d56fbcad`) left its move uncommitted again at 11:05. This repository's own workers
avoid it by committing `docs(tasks): start HAI-N` at once.

## Change

Pick one, and say why in the commit:

- **The pacer makes the move.** When it commits the schedule move
  (`docs(tasks): schedule HAI-N`), it also moves the issue to In Progress, or does so
  in a second path-limited commit right after the launch is accepted. The worker
  prompt then says the issue is already In Progress.
- **The prompt makes the worker commit it.** The worker commits the move on `main` at
  once, path-limited (`git commit -- TASKS.md`), before any other work.

Either way, a worker that stops early must not leave `TASKS.md` dirty in the main
checkout.

## Acceptance

- Tests: a launch leaves the main checkout clean; a worker that fails before
  merging leaves no uncommitted tracker change.
- `docs/t3-pacing.md` describes who moves the issue and when.
- Works for TASKS.md and directory (`docs/backlog`) sources.
