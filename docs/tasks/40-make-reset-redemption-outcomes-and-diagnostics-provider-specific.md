---
depends_on: []
---

# Make reset redemption outcomes and diagnostics provider-specific

Research evidence: [task 37 research](../reset-redemption-research.md), 2026-10-10.

- Review `resets.py` against the pinned official Codex enum and T3's Claude schema.
  Name Claude `unavailable` explicitly as unconfirmed while retaining its saved ID.
  Keep unknown outcomes withheld; do not broaden success handling on speculation.
- Preserve redacted HTTP status and safe response details for diagnosis, distinguishing
  401/403, 429, transport failure and malformed JSON without logging credentials.
  Record the selected request/credit aliases and before/after quota for a supervised
  trial. Never log tokens, cookies or raw HAR files.
- Correct `docs/reset-aware-pacing.md`: ID reuse is intended retry behavior with
  strong Codex client evidence, not a verified unlimited server guarantee. State the
  Claude permission and contract gaps and link the research note.
- Review confirmation evidence: filtered grant disappearance can mean expiry rather
  than spending; weekly-only confirmation does not prove a full Codex reset.
- Validate with offline HTTP fixtures and response-loss/restart tests. No live POST
  belongs to this implementation without Antti's separate approval.
