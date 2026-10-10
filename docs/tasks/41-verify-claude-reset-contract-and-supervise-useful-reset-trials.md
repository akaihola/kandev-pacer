---
depends_on: []
---

# Verify Claude reset contract and supervise useful reset trials

Research evidence: [task 37 research](../reset-redemption-research.md), 2026-10-10.
The collaborative browser reached Claude sign-in; no authenticated capture exists.

- Resolve permission for automated subscription redemption under the account's
  provider terms before a pacer-driven trial. Anthropic restricts automated access
  and subscription OAuth reuse; technical token compatibility is not permission.
- On Antti's own signed-in account, capture one intended native Claude redemption
  only after his go-ahead. Compare hostname, cookie/CSRF versus OAuth authentication,
  JSON fields, result/status and grant type with `resets.py` and T3's implementation.
- Establish whether `cedar_ember` grants include session-only offers. Retain verified
  offer typing in the grant parser and exclude session-only/unknown offers from
  weekly pacing. Verify whether a weekly offer clears the five-hour window.
- Use the research note's procedure for one useful trial per provider at 100% weekly
  usage with waiting work. First complete safe evidence logging. Pause competing
  redeemers, send at most one POST, record redacted evidence and poll reads only.
  Never replay a live request merely to prove deduplication, even with the same ID.
- Correct the provider adapter and pacing documentation if captured evidence differs.
  Validate corrections offline before an authorized pacer-driven trial. Preserve
  uncertain recovery state, and report unresolved server deduplication guarantees.
