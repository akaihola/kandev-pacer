# Instructions for coding agents

Issues are tracked in `TASKS.md`.

## Writing Level

Target all prose — conversation, docs, code comments — at a self-taught developer
with ~2 years on a team and no formal CS background. Concretely:

- Use practical/industry vocabulary freely (PRs, staging, rollback, deploy, on-call,
  feature flags) — no glossing needed.
- When you use a formal/CS term (Big-O, invariant, race condition, back-pressure,
  idempotency), briefly anchor it to an observable, real-world effect the first time —
  e.g. "O(n²) — why that page crawls once a customer has thousands of rows."
- Justify with concrete examples and failure modes ("here's what breaks"), not proofs
  or first-principles derivation.
- Don't assume a textbook term is known just because the underlying thing is —
  name it, then ground it.
