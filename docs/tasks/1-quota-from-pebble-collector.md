---
depends_on: []
---

# Read the quota from the pebble-quota collector

## Problem

`provider.py` reads each subscription's usage directly: the Anthropic endpoint with the
token in `~/.claude/.credentials.json`, the Codex endpoint with `~/.codex/auth.json`. The
pacer refuses to refresh either token, because Claude Code and Codex CLI own those files.
At night nobody runs Claude Code, the token expires, and the Claude gate goes **blind**:
on 2026-09-06 that was 00:00 to 02:00 and 10:00 to 12:30 with five or six launchable
tasks waiting. Blind means withheld, so the hours the weekly quota should burn are the
hours nothing runs.

The pebble-quota collector on `agent@gogo` (`~/prg/pebble-quota`, user service
`pebble-quota.service`) already reads both subscriptions every 300 s and writes
`~/.local/state/pebble-quota/latest.json`. It has the same three Claude windows and the
two Codex windows the pacer gates on. Two readers of the same credential are one too many.

## Change

Add a quota *source* setting. The direct read stays the default so nothing changes for a
pacer that does not have the collector.

| setting | env | default | meaning |
| --- | --- | --- | --- |
| `quota_source` | `PACER_QUOTA_SOURCE` | `direct` | `direct` reads the provider endpoints as today; `pebble` reads the collector's snapshot |
| `pebble_snapshot` | `PACER_PEBBLE_SNAPSHOT` | `~/.local/state/pebble-quota/latest.json` | the snapshot file, when the source is `pebble` |
| `pebble_max_age` | `PACER_PEBBLE_MAX_AGE` | `900` | seconds; an older `collected_at` is a blind read, not a stale gate |

On `agent@gogo`, `config.toml` gets `quota_source = "pebble"`.

## Mapping

The snapshot looks like this (abridged):

```json
{
  "collected_at": "2026-09-06T20:40:38Z",
  "services": {
    "claude": {"windows": {
      "5h":         {"duration_seconds": 18000,  "resets_at": "...", "used_fraction": 0.11},
      "7d":         {"duration_seconds": 604800, "resets_at": "...", "used_fraction": 0.43},
      "7d (Fable)": {"duration_seconds": 604800, "resets_at": "...", "used_fraction": 0.70}}},
    "codex":  {"windows": {
      "5h": {"...": "..."}, "7d": {"...": "..."}}}
  }
}
```

One function per provider turns it into the envelope `claude_windows` / `codex_windows`
already produce, so `quota.py` and `runtime.py` do not change:

| snapshot | envelope |
| --- | --- |
| `services.claude` | provider `anthropic` |
| `services.codex` | provider `openai` |
| `"5h"` | label `5-hour` |
| `"7d"` | label `7-day` |
| `"7d (Fable)"` | label `7-day (Fable)`, `weekly_scoped` for the Fable model |
| `used_fraction` | `c` |
| `resets_at` (ISO 8601) | `resets_at` (epoch seconds) |
| `duration_seconds` | `d` |

A missing service in the snapshot blinds only that provider, the same way an unreachable
endpoint does today. A missing file, unparsable JSON, or `collected_at` older than
`pebble_max_age` raises `QuotaUnavailable` with the file path in the message.

## Where it goes

- `provider.py`: `pebble_fetch(now, path=None)` beside `fetch` and `codex_fetch`, and
  `_read_source` dispatching on `settings.quota_source`. The 5-minute `_Cache` is
  bypassed for the snapshot: the file *is* the cache, and reading a 2 KB file per tick
  costs nothing.
- `config.py`: the three fields, validated (`quota_source` in `{"direct", "pebble"}`).
- `tests/test_provider.py`: one test per mapping row above, one for each failure shape,
  built from a snapshot dict in `conftest.py`.
- `tests/test_contract_live.py`: when `PACER_QUOTA_SOURCE=pebble`, assert the snapshot
  file exists and carries the two services and the five window labels.
- `README.md`: the configuration table and a short "Quota sources" paragraph.

## Done when

`uv run pytest` passes, and on `agent@gogo` a night passes with no `blind` ticks in
`pacer.jsonl` while `quota_source = "pebble"`.
