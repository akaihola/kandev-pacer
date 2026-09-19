---
depends_on: []
---

# Default to a 30-second interval and rotate pacer logs weekly or at 50 MB

## Original request

Change the default `PACER_INTERVAL` from `120` to `30` and update the README
example; `agent@gogo` already runs 30 through nix. Rotate `pacer.jsonl` weekly with a
`RotatingFileHandler`-style size cap of 50 MB and 4 backups; the file was 407 MB and
unrotated. Update the "Logging" section's size estimate.
