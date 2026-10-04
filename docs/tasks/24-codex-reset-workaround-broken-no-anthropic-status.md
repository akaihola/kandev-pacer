The output looks very different from Kandev mode. The `  --agent-providers-raw 'codex-acp=__api_key__ hermes-acp=__api_key__' --apikey-policy cap` parameters disable pacing of Codex in Kandev mode, which is convenient if there are resets remaining to be used. And why is nothing printed about Anthropic pacing situation?

```
❯ cd ~/prg/kandev-pacer && set -a && source ~/.config/secrets/kandev-pacer.env && set +a && \
uv run \
  --extra t3 \
  --frozen kandev-pacer \
  --config ~/.config/kandev-pacer/config-t3.toml \
  --agent-providers-raw 'codex-acp=__api_key__ hermes-acp=__api_key__' \
  --apikey-policy cap
      Built kandev-pacer @ file:///home/agent/prg/kandev-pacer
      Built hai-tasks @ file:///home/agent/prg/hai
Uninstalled 2 packages in 1ms
Installed 2 packages in 4ms
T3 [openai] paused
T3 [openai] paused
```

## Implementation plan

- Honor the explicit `codex-acp=__api_key__` override in T3 mode. Keep provider and total concurrency limits, and honor `--apikey-policy withhold`. This changes pacing only, not T3 authentication.
- Print quota status for every configured provider each tick, including scoped windows, headroom, running counts and resume estimates. Report quota read failures and identify providers with no tracker parent.
- Verify admissions, retries, bypass capacity limits and provider status with offline integration tests.

## Completion evidence, 2026-10-04

Implemented the override and provider status reporting in T3 mode. Instance-name overrides take precedence over the legacy Codex alias. Quota bypass retains all concurrency limits and the withhold policy, including launch retries. Unsupported active providers still block admission. See [T3 pacing documentation](../t3-pacing.md#quota-status-and-manual-resets).

Offline verification: 56 T3 integration tests passed; the full suite passed with 684 tests and 20 live tests skipped. `darker --check`, `graylint`, Ruff and `ty check` passed. No live tasks were launched. Restart the running pacer to load the change.
