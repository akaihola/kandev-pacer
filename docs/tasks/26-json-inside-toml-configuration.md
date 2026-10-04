The configuration TOML file now expects JSON as the value for the `t3_trackers` setting.

Use TOML syntax instead.

## Implementation evidence, 2026-10-04

Merged as `2fb33a1` into `main`. `t3_trackers` accepts TOML arrays of inline
tables and `[[kandev-pacer.t3_trackers]]` arrays of tables. Existing JSON
strings remain supported, including environment and CLI overrides.

Validation: 89 focused tests passed; the full suite passed 707 tests with 20
live checks skipped. Darker, Graylint and whitespace checks passed.
The T3 configuration guide now uses native TOML. Running pacers need a restart
to load the new parser; no running process or operator configuration was changed.
