---
depends_on: []
---

# Enrol workflows through the step update API instead of writing the Kandev database

## Original request

Enrol workflows through kandev's step update endpoint instead of writing
`~/.kandev/data/kandev.db`. `PUT /api/v1/workflow/steps/{id}` takes every step field,
`stage_type` included, and needs no login, so `make-paced` drops sqlite, `--db` and
`KANDEV_DB`; the README's "no API" claim goes with them.
