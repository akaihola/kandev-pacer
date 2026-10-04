The "T3 withheld" error should give a hint on how to fix the situation.

```
❯ cd ~/prg/kandev-pacer && set -a && source ~/.config/secrets/kandev-pacer.env && set +a && \
uv run \
  --extra t3 \
  --frozen kandev-pacer \
  --config ~/.config/kandev-pacer/config-t3.toml \
  --agent-providers-raw 'codex-acp=__api_key__ hermes-acp=__api_key__' \
  --apikey-policy cap
 : command not found
      Built kandev-pacer @ file:///home/agent/prg/kandev-pacer
      Built hai-tasks @ file:///home/agent/prg/hai
Uninstalled 2 packages in 1ms
Installed 2 packages in 4ms
T3 withheld: Running thread has unknown provider; withholding work
```

## Resolution, 2026-10-04

A read-only live shell snapshot showed active threads with
`modelSelection.instanceId` values `claudeAgent` and `codex`, without a `provider`
field. The pacer only read the legacy `provider` field, causing supported threads
to appear unknown. The checked T3 0.0.44 contracts define `instanceId` as the current
field and decode legacy `provider` selections into that shape.

Both capacity checks and launch admission now accept the current field, with legacy
compatibility. An explicit `instanceId` takes precedence. Unsupported active
selections still block launches, but the error identifies the thread and selection,
lists supported instances, gives recovery steps, and explains that Kandev's agent
provider overrides do not configure T3 instances. See
[provider troubleshooting](../t3-pacing.md#unknown-providers).

Tests cover launching and capacity counting with both default instance IDs,
legacy selections, missing and unsupported selections, field precedence, and
blocks caused by running work, pending approval, or background activity. No live
thread was launched or stopped during investigation.
