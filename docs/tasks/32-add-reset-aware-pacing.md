Make both Kandev and T3 pacing aware of available resets. `kandev-pacer` must get remaining reset information for Codex and Claude, similar to how T3 Code obtains that information. It must then plan an optimal pace for how fast to use pre-reset token budget, and when to use the reset, in order to get a maximum number of token for the subscription with the resets. It must also take into account the different reset behavior between Codex and Claude (does a reset continue to current time period or start a new period). After this change, the `--agent-providers-raw 'codex-acp=__api_key__ hermes-acp=__api_key__' --apikey-policy cap` options still cause non-paced behavior, and without them the behavior should optimize for getting maximum tokens.

## Implementation plan, 2026-10-07

- Read Codex banked credits from `/backend-api/wham/rate-limit-reset-credits`.
  Read Claude `cedar_ember` grants with the OAuth usage request. Filter expired,
  paused, unusable and malformed grants. A failed optional Codex credit read must
  retain the ordinary quota reading.
- Share planning and redemption between Kandev and T3. Pace quota fractions,
  since providers do not publish an absolute remaining token count. With N usable
  resets, divide the weekly burn interval by N + 1 and shorten it to credit expiry
  when needed. Recompute after every confirmed reset and natural rollover.
- Claude retains its weekly rollover, so its grants can add allowance within this
  week. Codex starts new windows on redemption. Reserve credits expiring after the
  current weekly rollover until their final natural week, preserving free rollovers.
  Use the returned window timestamps rather than inventing post-reset dates.
- Redeem when the shared weekly allowance is exhausted and subscription work is
  waiting. Keep normal five-hour pacing, scoped gates and concurrency limits.
  Reset-assisted weekly segments aim at the full allowance, overriding weekly
  alpha and backstop reserves until the credits are used. Then ordinary pacing
  resumes. Never purchase additional resets or usage credits.
- Serialize redemption per provider account. Save a private request ID before the
  POST and reuse it after lost replies or restart, preventing one interrupted
  attempt from spending a second credit. Require fresh credit-count reduction and
  refreshed weekly allowance before admitting work. Failures withhold that provider.
- Keep API-key `cap` overrides non-paced and exclude their work from redemption.
  Keep inspection reads free of reset spending. Include queued Kandev prompts as
  waiting work. Redeem only through direct quota mode; Pebble lacks grant data.
- Cover provider parsing, expiry scheduling, Codex/Claude rollover differences,
  scoped and five-hour constraints, retries, stale confirmation, queued prompts,
  both launch paths and the bypass with offline tests. Run the full Python and
  userscript suites and the repository's differential formatting, lint and type checks.

Provider behavior checked against these primary sources on 2026-10-07:

- [Codex banked resets](https://help.openai.com/en/articles/20001498-how-banked-codex-resets-work)
- [Claude limit resets](https://support.claude.com/en/articles/17007452-what-is-a-limit-reset)

The target maximizes available weekly allowance under sustained runnable work.
Actual token throughput depends on task costs, provider limits and available work;
a pacer cannot guarantee that every expiring reset will be exhausted. Session-only
and unidentified reset types do not get a weekly acceleration plan.

## Plan refinement, 2026-10-07

The host configuration uses Pebble, so direct-only redemption would leave Kandev
without this feature. Pebble mode now supplements its snapshot with cached live
provider reads. While credits are available, live windows replace snapshot windows
to avoid acting on pre-reset usage. Optional live-read failure falls back to Pebble.
Redemption rechecks and confirms live quota regardless of the selected source.

## Completion evidence, 2026-10-07

Implemented shared reset-aware pacing and automatic redemption for Kandev and T3,
including Pebble's live reset supplement. API-key cap bypass remains non-paced.
Recovery records retain the same request ID across lost replies and restarts;
provider admission stays closed until fresh quota and credit reduction confirm.

Validation on the implementation tree: 773 Python tests passed, 20 opt-in live
checks skipped; all 64 userscript tests passed. Differential Darker formatting,
Graylint Ruff/type checks and whitespace checks passed. Rebase changed only tracker
and plan documents; source and tests were identical to the validated tree. The
merged main tree passed all 89 reset, quota and provider tests.

Merged on main as `7c2b46e`. Tracker changes use revision-checked
hai_tasks operations, preserve unrelated issues, and leave acceptance unchecked.
No live reset was redeemed. No kandev-pacer user service was registered in this
environment, and no running pacer was restarted. Existing processes need a restart
to import the merged code. Throughput remains bounded by runnable work and provider
short-window limits; the planner cannot guarantee using every expiring credit.
