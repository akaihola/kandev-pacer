---
depends_on: []
---

# Fall back to a configured agent profile when a provider gate stays closed

## Original request

Fall back to another agent profile when a provider's gate has been closed for
longer than `PACER_FALLBACK_AFTER` (seconds, default `0` = off). Map with
`PACER_FALLBACK_PROFILES`, e.g. `"Luna low=Sonnet low"`, resolved by name through
`GET /api/v1/agents`. The fallback is used only for `intent:start` launches and is
logged as `fallback` in the tick record. Deprioritized 2026-09-11: with Max 20x and
ChatGPT Pro, exhaustion is no longer the common case. Keep it for a provider that is
down rather than spent.
