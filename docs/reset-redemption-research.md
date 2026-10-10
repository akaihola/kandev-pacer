# Subscription reset redemption research

Researched 2026-10-10 for [task 37](tasks/37-research-reset-redemption.md).
No live redemption, OAuth probe, or request replay was performed. Runtime code is
unchanged. Public documentation establishes the user flows; Codex source establishes
its client request. Claude's request is corroborated only by T3 Code, not by an
authenticated official-client capture.

## 1. Official user flows and reset types

| Client | Steps and availability |
| --- | --- |
| Claude web / Desktop | Open Settings > Usage. In Resets, choose **Reset for free**, then confirm with the same button. A limit message can also offer it. Check the offer's type and expiry first. |
| Claude Mobile | Redemption button unavailable; use web or Desktop. |
| Claude Code terminal / IDE | Redemption button unavailable; use web or Desktop on the same account. Shared account usage makes the refill available to Code too. |
| Codex app | Settings > Usage, select the available reset / Full reset summary, review expiry, confirm. |
| Codex web | Settings > Usage in the correct account/workspace, select the available reset, review expiry, confirm. |
| Codex CLI | Run `/usage`, select a reset from the picker, review its details, confirm. The help page calls this Settings > Usage; the CLI source gives the concrete command. |

Claude offers session or weekly resets; the displayed offer determines the affected
limit. Redemption can happen before exhaustion and cannot be undone. Codex offers
full banked resets, distinct from automatically applied global resets and purchased
usage credits. Availability and expiry depend on the offer. These are documented
flows, not a claim that every installed client version exposes the feature.
Sources: [Claude help][claude-help], [Codex help][codex-help],
[CLI commands][commands], [CLI picker][codex-usage].

## 2. Requests compared with the pacer

### Codex: official source matches

The official backend client sends JSON to
`POST https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume`.
Its alternate Codex API path is `/api/codex/rate-limit-reset-credits/consume`.
The body contains `redeem_request_id` and optionally `credit_id`; selecting a
particular credit supplies both. Authentication includes a Bearer token and
`ChatGPT-Account-Id`. HTTP header names are case insensitive, so the pacer's lowercase
header is equivalent. The pacer's path and fields match this source. This does not
prove that Antti's account/token is eligible or that a future server keeps the contract.
Sources: [backend request][codex-request], [headers][codex-client],
[account-auth and selected-credit tests][codex-tests], [local implementation][resets].

The app-server method is `account/rateLimitResetCredit/consume`, with
`redeemRequestId` and optional `creditId`. Those are client protocol fields, not the
HTTP JSON field names. T3's Codex integration uses this protocol; its hub integration
also sends the HTTP request directly. Sources: [Codex tests][codex-tests],
[T3 hub redemption][t3-hub]. Closed app/web implementations were not captured, so
this is evidence for the open CLI/app-server rather than every official frontend.

### Claude: matching third-party implementation, official request unresolved

T3 sends:

```text
POST https://api.anthropic.com/api/organizations/{organizationUuid}/reset_rate_limits
Authorization: Bearer <Claude Code access token>
anthropic-beta: oauth-2025-04-20
Content-Type: application/json

{"program":"cedar_ember","grant_id":"<grant>","request_id":"<saved UUID>"}
```

It uses a `claude-cli/<version> (external, cli)` user agent. The pacer uses the same
host, path, fields, OAuth beta header and organization identity pattern. T3 reads
`claudeAiOauth.accessToken` from `.credentials.json` and `oauthAccount.organizationUuid`
from `.claude.json`. On macOS this T3 implementation declines the feature because
credentials live in the keychain. Source: [T3 Claude implementation][t3-claude].

T3's comments say it follows the CLI. That is a third-party assertion and conflicts
with treating a Code redemption button as publicly supported: Claude help says the
button is unavailable in Code. Neither source proves what claude.ai sends, whether
its web hostname differs, or whether it uses cookies/CSRF rather than Bearer OAuth.
The collaborative browser opened Settings > Usage but redirected to sign-in on
2026-10-10. No authenticated offer or network capture was available. Keep the
pacer's Claude endpoint labelled unverified until an authorized capture or provider
confirmation resolves this. Sources: [Claude help][claude-help], [T3 code][t3-claude].

## 3. Result codes and failures

| Evidence | Field and known values | Consequence for `resets.py` |
| --- | --- | --- |
| Official Codex backend enum | `code`: `reset`, `already_redeemed`, `nothing_to_reset`, `no_credit`; `windows_reset` is an integer defaulting to zero | All four are handled. `windows_reset` is ignored. The union of both providers' codes should not be described as Codex's contract. |
| T3 Claude schema | `result`: `reset`, `already_used`, `not_limited`, `ineligible`, `cooldown`, `unavailable` | `unavailable` is missing from the named outcomes; the unknown-result fallback nevertheless retains the saved ID and withholds work. |
| T3 Claude HTTP handling | 401/403 require sign-in; 429 means rate limiting; other bad HTTP statuses / malformed replies are failures | `_post` converts all exceptions into the same unconfirmed error and loses status/body diagnostics. |

Sources: [Codex types][codex-types], [Codex mock response tests][codex-tests],
[T3 Claude schema and HTTP handling][t3-claude], [pacer `_post`][resets]. Mock tests
establish client behavior, not an exhaustive server error catalogue. No official
Claude response schema was found in the checked help page; T3's list remains
corroboration. No HTTP success status or error shape should be invented beyond that.

T3 maps `already_used` to already redeemed, `not_limited` to nothing to reset, and
`ineligible` to no credit. It treats `cooldown` as settled and `unavailable` as
unconfirmed. The pacer clears pending state on its settled negative results; it
keeps state for unknown results and transport failures. Both success handling and
HTTP failure classification need provider-specific review before live verification.

## 4. Does the saved request ID prevent spending twice?

Idempotency means retrying a timed-out request performs the original redemption
once, rather than spending another reset. Codex's UI explicitly keeps both its UUID
and selected credit ID after failure and offers **Try again** with those same
values. It accepts `already_redeemed` as success. This is strong evidence of intended
retry behavior. Source: [Codex retry handler][codex-usage].

T3 likewise keeps an account's pending UUID after an unconfirmed error and calls
Claude's `request_id` an idempotency key. Its coordinator is in-memory; the pacer's
on-disk record survives restart. Sources: [T3 coordinator][t3-coordinator],
[T3 Claude claim][t3-claude], [pacer save/lock/retry][resets].

Neither inspected provider help page specifies server deduplication scope, retention
time, behavior after credit expiry, or whether a key is bound to the grant/body.
Claude has only third-party evidence. Thus the absolute promise in
`reset-aware-pacing.md` is stronger than the evidence. Keep the same account, credit
and request ID across an uncertain attempt; never generate a replacement ID merely
because a reply was lost. Do not test replay against a multi-reset grant on a live
account just to establish this property. A replay could spend a second reset if the
assumption is wrong. Offline duplicate-request tests verify the pacer's behavior;
provider documentation or an authorized sandbox is needed to verify the server.

## 5. OAuth compatibility and provider terms

Codex's official tests require ChatGPT authentication and explicitly verify the
Bearer token plus account header. An API key alone is rejected for redemption by
the app-server. This supports using the CLI's subscription login for this endpoint;
it does not validate this account live. Source: [Codex auth tests][codex-tests].

Claude token reuse is implemented in T3 but untested here. A successful OAuth usage
GET would not prove permission or acceptance for a redemption POST. Anthropic's
consumer terms section 3 restrict automated access unless using an API key or
explicit permission. Its Code guidance reserves subscription OAuth for native
Anthropic applications and restricts third-party credential handling. I found no
explicit permission for a separate pacer redeeming subscription resets. Use the
native manual flow unless Anthropic confirms this use is permitted. An API key is
not a substitute credential for an account's subscription reset.
Sources: [Anthropic consumer terms][anthropic-terms], [Code credential rules][claude-legal].

OpenAI's Europe terms prohibit bypassing limits and programmatic data extraction.
They do not explicitly authorize this pacer's private-endpoint redemption. Applying
a legitimately granted reset is the documented feature, but official CLI support
is not a blanket grant for unattended third-party use. This is an inference from
the checked text, not legal clearance. Business accounts may have different terms;
check the account's agreement before enabling automation. Sources:
[OpenAI Europe terms][openai-terms], [Codex auth implementation][codex-tests].

## 6. Window effects and timestamps

Claude preserves the scheduled weekly rollover. The help page distinguishes session
and weekly offers but does not say a weekly offer also clears the five-hour window.
Do not assume it does. Capture both windows independently. Source: [Claude help][claude-help].

Codex full resets refresh both five-hour and weekly windows and replace the old
weekly reset date. The next week is around seven days after resuming use, not a
second refill on the former rollover. A reset remains banked if no eligible window
is refreshed. Source: [Codex help][codex-help].

The Codex consume response reports `windows_reset`, not new reset timestamps.
After success the client fetches usage; the backend's window `reset_at` becomes
`resets_at` in each `RateLimitWindow`. Read the returned primary and secondary
windows, durations and used percentages; do not calculate exact timestamps from
POST time. Source: [response types][codex-types], [window conversion][codex-client].
Claude's exact post-redemption timestamps remain uncaptured. The pacer already
refreshes quota, but `_confirmed` only checks a lower selected-credit count and
available shared weekly quota. That does not prove the five-hour window changed,
or that disappearance from a filtered credit list means consumption rather than
expiry/ineligibility. Source: [pacer confirmation][resets].

## 7. Verification without wasting a reset

There is no documented dry-run endpoint in the inspected sources. Do not try a
fake credit, a supposedly empty account, or a pre-limit POST as a harmless probe.
Claude allows early use; Codex can refresh just one eligible window. A POST can
therefore spend a useful reset even when the weekly meter is below 100%.

Before any live attempt:

1. Run the existing offline reset tests. Add mocked HTTP contract tests in the
   follow-up covering each provider's outcomes, HTTP failures, response loss and
   restart with the same saved body/ID. Match the official Codex source and label
   Claude fixtures as T3-derived until a capture exists.
2. Prepare a redacted evidence record: UTC time, client version, account alias,
   endpoint, method, body field names, stable aliases for credit/request IDs, HTTP
   status, response body, and before/after raw quota and grant readings. Keep exact
   IDs only in private recovery state. Never log tokens, cookies, refresh tokens,
   Authorization headers or an unsanitized HAR file. `_post` currently lacks this
   evidence logging; implement and test it before a pacer-driven trial.
3. Arrange one intended redemption per provider when shared weekly usage reaches
   100%, useful work is waiting, and expiry/rollover leaves time to spend the refill.
   Capture the five-hour meter too. If it is empty, this trial cannot demonstrate
   whether Claude's weekly reset clears it. Obtain Antti's explicit go-ahead for
   the chosen provider/offer/time. No approval was obtained during this research.

For a native-client capture after approval, pause all account-sharing pacers and
other redemption clients. In a signed-in browser open developer tools > Network,
enable Preserve log, then open Usage and record the pre-reset windows/offer. Antti
confirms exactly once. Inspect the resulting POST's URL, headers, JSON, HTTP status
and response, then refresh Usage and record both windows and remaining grants.
Save only sanitized evidence. Do not click **Copy as cURL** and replay it. This
verifies the native request and window effect without an extra reset, but does not
prove that the pacer's OAuth credential works against a cookie-authenticated path.

For a pacer-driven trial, first resolve provider permission and the Claude request
contract. Use one supervised pacer process and the shared durable reset state;
prevent unattended repeat POSTs, allow one selected-credit POST, then perform
read-only quota polling to accommodate dashboard delay. Disable repeat redemption
before collecting results. Record the exact private recovery ID before sending.
If the reply is lost, stop POSTs, preserve state and reconcile grant/window reads;
do not retry live merely to prove deduplication. Resuming with uncertain state
requires an explicit reviewed recovery decision.

Accept a trial only if the expected credit decrement and affected windows agree,
the account/workspace is unchanged, and the provider's timestamps match the observed
flow. A 2xx or `reset` alone is insufficient. If native capture shows a different
contract, correct it in the follow-up before a pacer attempt. Support investigation
may need time/timezone and a Usage screenshot; do not promise replacement resets.
Source: [Codex troubleshooting][codex-help].

## Follow-up scope and evidence limits

File follow-ups for provider-specific results and redacted HTTP evidence; correcting
the unconditional retry promise in `reset-aware-pacing.md`; and permission, Claude
offer typing and supervised verification. Preserve unknown-result withholding.
Session-only Claude offers must not silently become weekly pacing budget: the
current grant parser does not retain a reset type. No public source here establishes
that every `cedar_ember` grant refreshes weekly usage.

[claude-help]: https://support.claude.com/en/articles/17007452-what-is-a-limit-reset
[codex-help]: https://help.openai.com/en/articles/20001498-how-banked-codex-resets-work
[commands]: https://learn.chatgpt.com/docs/developer-commands#built-in-slash-commands
[anthropic-terms]: https://www.anthropic.com/legal/consumer-terms
[claude-legal]: https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use
[openai-terms]: https://openai.com/policies/terms-of-use/
[codex-request]: https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/backend-client/src/client/rate_limit_resets.rs
[codex-types]: https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/backend-client/src/types.rs#L103-L119
[codex-client]: https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/backend-client/src/client.rs
[codex-tests]: https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/app-server/tests/suite/v2/rate_limit_reset_credits.rs
[codex-usage]: https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/tui/src/chatwidget/usage.rs
[t3-claude]: https://github.com/pingdotgg/t3code/blob/c77a7b7eebd6ea8a3a63bbcaaa7c1707fe65c86e/apps/server/src/provider/claudeResetCredits.ts
[t3-coordinator]: https://github.com/pingdotgg/t3code/blob/c77a7b7eebd6ea8a3a63bbcaaa7c1707fe65c86e/apps/server/src/provider/resetCreditCoordinator.ts
[t3-hub]: https://github.com/pingdotgg/t3code/blob/c77a7b7eebd6ea8a3a63bbcaaa7c1707fe65c86e/apps/server/src/usage/cliproxyApi.ts
[resets]: ../src/kandev_pacer/resets.py
