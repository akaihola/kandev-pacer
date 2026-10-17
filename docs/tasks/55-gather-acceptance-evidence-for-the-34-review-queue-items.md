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
