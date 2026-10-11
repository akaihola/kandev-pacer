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

## Review follow-up, 2026-10-11

Independent review found that canceling Delete issue left focus on BODY because
the Title input was recreated without rerunning the initial focus effect.
Hai commit `9cef64f` reruns that effect when `deleting` changes. The regression
test checks Title focus, immediate typing, saving, and persistence after reload.

Validation passed 38 Python tests, 90 client/API tests, TypeScript, the production
build, and all 62 Chromium workflows using one worker. The first client/API run
hit the previously documented oversized-request EPIPE failure; the complete
rerun passed without changes to that test. The independent review supplies the
before-fix reproduction. Browser runs initially queued behind Filemill's long
shared-lock run, then ran under a separate Hai lock with isolated test data.

Merged into Hai main and deployed using its user-service install script. Health,
HTML, and asset checks passed on loopback, private HTTP, and private HTTPS.
The collaborative browser confirmed Title focus after canceling deletion in the
private HTTPS app using the browser-local backend. It disconnected before live
typing verification; typing and persistence passed in the automated regression.
No live issue was saved or deleted.

Logs and the previous build are retained in
`/home/agent/.local/state/hai-46-cancel-focus/`. Existing live tracker acceptance
and unrelated dirty files were preserved; this follow-up did not mark acceptance.
