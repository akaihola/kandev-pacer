The ValueError should give a clear explanation of where to look, what is wrong, and how to fix it. 

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
Installed 2 packages in 7ms
T3 startup failed (ValueError); check configuration and Hai installation
```

## Resolution, 2026-10-04

After changing the endpoint to the server's Tailscale address, startup requires
`T3_THREAD_AUTH_TOKEN` in the process environment. The client already rejected a
missing token, but the startup handler hid its explanation behind `ValueError`.

The client now marks its safe configuration errors explicitly. Startup logs show
those explanations, including how to issue and export a token and when to use
`--base-dir`. Invalid URLs and empty token issuance also give configuration guidance.
Known tracker admission errors are shown at startup too. Unclassified exceptions
retain the generic message because their contents may contain credentials.

See [connection setup](../t3-pacing.md#connection-errors) for the token command and
endpoint settings. Tests cover the missing-token startup path, safe tracker errors,
malformed URL redaction, and hiding unclassified exception contents.
