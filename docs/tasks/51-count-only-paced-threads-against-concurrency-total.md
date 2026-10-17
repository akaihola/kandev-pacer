# Count only paced threads against `concurrency_total`

Approved by Antti on 2026-10-17 (steward proposal P9, decision D32 in
`/home/agent/my-knowledge/agents/steward/decisions.md`).

`concurrency_total` counts every active T3 thread: Antti's interactive threads,
steward runs, a thread waiting for user input, and a thread whose background task
is still running (`activity()` in `t3.py`). From 2026-10-15 20:05 UTC to
2026-10-17 10:05 local, the pacer launched nothing. The count stood at 3–4 of 2,
held by threads doing no paced work, while Claude's weekly quota stood at 27% used
with 67% of the week elapsed, and Codex at 2% with 27% elapsed. As a stopgap,
Antti raised `concurrency_total` to 4 on 2026-10-17.

- Count only threads with a launch record from this pacer against
  `concurrency_total` and the per-provider `concurrency`.
- Keep a separate, optional limit on all active threads (for example
  `t3_active_limit`) to protect interactive use. Leave it unset by default.
- The status line shows both counts, for example `1/2 paced, 4 active`.
- Document it in `docs/t3-pacing.md`; add tests for: interactive threads only,
  mixed, waiting for user input, background task.

Acceptance: with two interactive threads active and `concurrency_total = 2`, the
pacer still launches up to two paced issues.
