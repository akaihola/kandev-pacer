---
depends_on: []
---

# Load secrets from a private env file between TOML and process-environment settings

## Original request

Read secrets from an env file the pacer opens itself. Add `secrets_file`
(`PACER_SECRETS_FILE`, default empty) as a layer between the TOML file and the
process environment: `KEY=VALUE` lines, only keys in `CONFIG_FIELDS`, real
environment still wins. Refuse to start if the file is group- or world-readable,
and never log its contents. On `agent@gogo` the nix-generated `config.toml` points
it at `~/.config/secrets/kandev-pacer.env`, so starting the pacer no longer needs a
`source` first and a forgotten one cannot leave `heartbeat_webhook_url` without its
secret. Do not hardcode that path; it is a nixos-config convention.
