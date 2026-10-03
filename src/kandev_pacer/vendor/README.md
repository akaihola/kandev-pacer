# Session loaders

Vendored from the user's skills-akaihola marketplace at revision
`87188b6697d99614da332dc2635fc780b23106c6`:

- `claude-session-logs/scripts/analyze-session.py` → `claude_session.py`
- `pi-session-logs/scripts/analyze-pi-session.py` → `pi_session.py`

The collector imports `_content_blocks`; `learn_sources.py` streams session windows.
Each observation includes the imported file's SHA-256. The CLI display functions
are retained with the upstream source but are not invoked by the collector.
