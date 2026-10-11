When the `New issue` dialog opens, I expect the focus be in the Title field so I can start directly writing the title.

## Implementation evidence, 2026-10-11

The dialog belongs to Hai, not the pacer. Hai commit `013b04f` focuses the
Title input after the native dialog opens, following the project picker pattern.
Merged into Hai main and deployed through its user-service install script.

Both new Chromium regressions failed before the fix and pass afterwards. They
cover the New issue button and C shortcut, immediate typing, reopening, Tab to
Description, and restoring button focus on close. `npm run check` passed 33
Python tests, 90 client/API tests, TypeScript, the production build, and all
61 Chromium workflows. Log: `/tmp/hai-46-check.log`.

Loopback, private HTTP, and private HTTPS health checks passed; their HTML and
assets exactly match the deployed build. The collaborative browser confirmed
Title focus through both the button and C shortcut on private HTTPS using the
browser-local backend, without saving a test issue. Hai's unrelated dirty
tracker and description files were preserved. Issue 46 awaits user acceptance.
