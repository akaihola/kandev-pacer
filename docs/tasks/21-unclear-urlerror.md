There needs to be a more clear explanation in the error of how to fix it and where to look.

```
❯ cd ~/prg/kandev-pacer && set -a && source ~/.config/secrets/kandev-pacer.env && set +a && \
uv run \
  --extra t3 \
  --frozen kandev-pacer \
  --config ~/.config/kandev-pacer/config-t3.toml \
  --agent-providers-raw 'codex-acp=__api_key__ hermes-acp=__api_key__' \
  --apikey-policy cap
 : command not found
      Built hai-tasks @ file:///home/agent/prg/hai
      Built kandev-pacer @ file:///home/agent/prg/kandev-pacer
Uninstalled 2 packages in 2ms
Installed 2 packages in 6ms
T3 tick withheld (URLError); inspect tracker and launch records
```