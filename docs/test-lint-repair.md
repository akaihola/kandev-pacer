# Test and quota-source repair

## What changed

The configured quota source now takes effect. The validator previously replaced
both `direct` and `pebble` with `None`, silently falling back to direct provider
reads. The default remains `direct`; invalid values still fail validation.
The fix is in `49e0c1a`.

Offline tests now ignore operator TOML and inherited PACER/KANDEV settings,
including lowercase names. Tests can still supply explicit files and environment
overrides. Opt-in live tests retain operator settings, and pytest restores its
isolation patches at shutdown. Implementation is in `a411faa` and the review
repair is in `5c7f179`.

## User checks

Run from `~/prg/kandev-pacer`.

Check both configured sources without contacting a provider or starting the
pacing loop. The output must be `direct`, then `pebble`:

```bash
for source in direct pebble; do
  PACER_HEARTBEAT_WEBHOOK_URL='' PACER_QUOTA_SOURCE="$source" uv run --frozen python -c 'from kandev_pacer.config import settings; print(settings.quota_source)'
done
```

Run the focused regressions for defaults, source selection, invalid values,
offline isolation and live configuration preservation:

```bash
uv run --frozen pytest tests/test_config.py tests/test_provider.py tests/test_test_environment.py -q
```

Verify that inherited lowercase settings cannot break the offline suite:

```bash
pacer_quota_source=pebble pacer_heartbeat_webhook_url=http://operator.invalid/hook pacer_heartbeat_webhook_secret='' uv run --frozen pytest -ra
```

The live-mode regression only collects tests; it makes no service requests.
Actual live contracts remain opt-in as documented in README.md.

## Deployment and validation

Merged with `--no-ff` as `321c08b` on 2026-09-21. Both worktrees were clean
immediately after merge. Only this issue moved to Completed afterward.

Restarted the development-tree `kandev-learn.service` at 13:19:52 EEST. Its
one-shot run finished at 13:19:53 with `Result=success` and `ExecMainStatus=0`;
`kandev-learn.timer` remains active. No unit or dependency changes were needed.
This verifies the scheduled entry point, not a live quota-provider request.

The final review suite passed 483 tests with 20 documented opt-in live skips,
both normally and with lowercase quota/invalid heartbeat overrides. Ruff, ty,
Darker, Graylint and whitespace checks passed. The merged main tree also passed
all 43 focused checks and printed the expected values for both quota sources.

The tracker diff preserves every unrelated issue. Main already lists the Fable
gate-log issue under both Scheduled and Completed; that unrelated duplicate was
preserved, so no claim of global tracker uniqueness is made.
