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