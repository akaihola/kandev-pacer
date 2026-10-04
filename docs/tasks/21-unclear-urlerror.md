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

## Resolution, 2026-10-04

The configured endpoint was `http://127.0.0.1:3773`. Connections to it were refused.
T3 was listening on `100.81.121.7:3773`, and that address accepted connections.
The failure occurred while requesting the HTTP WebSocket ticket, before reading
T3's snapshot. Tracker repair was therefore the wrong troubleshooting advice.

Set `t3_url` to the server's listening address in the selected TOML, and check for
`PACER_T3_URL` overrides. Non-loopback addresses require `T3_THREAD_AUTH_TOKEN`,
issued from the running server's state directory. See
[connection configuration and troubleshooting](../t3-pacing.md#connection-errors).

HTTP ticket failures now report the endpoint origin, a safe connection reason or
HTTP status, the relevant settings, and the troubleshooting document. Raw exception
text and URL paths remain hidden to avoid exposing credentials. Regression tests
cover connection refusal, DNS, timeouts, TLS, authentication, server HTTP failures,
secret redaction, and logging without launching work.
