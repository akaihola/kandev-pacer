# What does the Tyko vault already offer a consumer?

Type: research
Status: resolved
Blocked by:

## Question

What in `~/my-knowledge` (heartbeat, LEARN routing, research queue, skills, tracker,
past projects) supports or constrains a consumer living there? Include any earlier
session-review effort and why it ended.

## Answer

Resolved 2026-09-13 by reading `~/my-knowledge` (AGENTS.md, HEARTBEAT.md, LEARN.md,
memory/open-threads.md, pages/PKB/Improvement backlog.md, docs/agents/skills.md) and
the vault git log.

**Supports a consumer there:**
- The records are already in the vault and committed locally; Tyko sees them without
  a push. Heartbeat item one does a `git pull`, which is harmless for local commits.
- `HEARTBEAT.md` runs on Haiku and spawns Opus sub-agents for anything heavy, tracks
  "last seen" state in `memory/heartbeat-state.json`, and reports to Antti only when
  useful. That is the exact shape of a periodic review pass.
- `LEARN.md` routes captures to `memory/daily/YYYY-MM/` and to
  `pages/PKB/Improvement backlog.md`, whose rule is "bigger items get discussed with
  Antti first". A review note has a home without a new convention.
- The vault records a cancelled "Nightly Pi session review" and names its successor:
  a review covering Pi, Claude, Codex, T3 Code, Kandev and Hermes sessions "with Luna
  low and deterministic pre-scans", to be a new project "when Antti wants it". The
  observation records are that deterministic pre-scan.

**Constrains it:**
- Vault `AGENTS.md`: ask before spending or external communication, never push, one
  agent mutates while others draft. A consumer that spends model calls on a timer
  needs an explicit standing approval recorded somewhere Tyko reads.
- The vault's engineering tracker is GitHub issues on `akaihola/my-knowledge`, not
  this repo's `TASKS.md`. Proposals against `make-paced` workflows would cross repos.
- The vault's "AI agent intelligence loop" produced daily digests nobody acted on and
  was dropped on 2026-09-11. A digest with no reader dies; the consumer must produce
  something Antti or an agent acts on.
- `pages/PKB/reviews/` exists but holds one unrelated HTML review. No note template
  exists for session reviews.
