This error should really tell what specifically is wrong with the configuration. In this case `parent_thread` was missing from a tracker.

```
❯ cd ~/prg/kandev-pacer && set -a && source ~/.config/secrets/kandev-pacer.env && set +a && \
uv run \
  --extra t3 \
  --frozen kandev-pacer \
  --config ~/.config/kandev-pacer/config-t3.toml \
  --agent-providers-raw 'codex-acp=__api_key__ hermes-acp=__api_key__' \
  --apikey-policy cap
T3 startup failed (KeyError); check configuration and Hai installation
```

## Implementation evidence, 2026-10-04

Issue 28 already replaced the missing `parent_thread` lookup with validation and
allowed fully configured trackers without a parent. This change identifies the
tracker entry in those errors, lists missing launch settings, and explains that
setting `parent_thread` supplies inherited defaults. Missing or empty `tracker`
paths, invalid entries, inaccessible paths, directories, duplicate trackers, and
empty parent IDs also receive specific startup messages. Unexpected exception
contents remain hidden to avoid logging credentials.

Validation: 86 T3 tests passed; the full suite passed with 729 tests and 20 live
contract tests skipped. `darker --check`, `graylint`, and `git diff --check` passed.
The added startup tests verify actionable errors, no dispatch on invalid
configuration, and no raw credential-bearing values in logs.
