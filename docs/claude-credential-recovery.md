# Why Claude quota reads stop, and how to recover

Research conducted on 2026-09-18; report written on 2026-09-20.
The research inspected code and documentation. It did not try a live token refresh.

## Recommendation

Let Claude Code renew its own login when needed. Then let the pacer read the updated
login file and check the quota again. Keep tasks paused until that check succeeds and
the normal pacing rules allow work.

This is a proposed change, not an existing setting. The exact Claude command still
needs testing on the machine running Kandev. See [task 15](tasks/15-claude-credential-recovery.md).

## What each app does

| Component | Its job here |
| --- | --- |
| Anthropic | Runs Claude and enforces the subscription's usage limits. |
| Claude Code | The command-line app that runs Claude work and manages its login. |
| Kandev | Organizes tasks and starts coding agents, including Claude Code through an adapter. |
| kandev-pacer | Decides when Kandev may start more work, based on remaining quota. |
| pebble-quota | An optional collector that saves quota readings in a file the pacer can read. |
| CodexBar | Another quota reader, used by Kandev's optional Provider Usage plugin. |

“Quota” means how much of the subscription's usage allowance has been used, and when
that allowance resets. Knowing that a user is logged in is not enough to know their quota.

## Why this happens

Claude Code stores login credentials in `~/.claude/.credentials.json` on this Linux
setup. Two parts matter:

- An **access token** is a temporary pass used to make requests.
- A **refresh token** lets Claude Code obtain a new pass without another browser login.

The pacer reads the access token. If its recorded expiry is less than a minute away,
the pacer stops before contacting Anthropic. It does not check whether the refresh
token still works. See `access_token()` in [provider.py](../src/kandev_pacer/provider.py).

This can leave the system waiting on itself:

1. The access token expires while Claude Code is idle.
2. The pacer cannot read quota, so it withholds new Claude work.
3. Claude Code does not run to renew the login.
4. Work stays paused until something else renews it, such as the user's login.

An expired access token therefore does **not** by itself mean a browser login is
required. A revoked or unusable refresh token would still require one.

The comment in `provider.py` assumes Claude will refresh early enough that withholding
cannot cause an outage. That assumption should be corrected when this is implemented.

## What the recovery message means

The line beginning `quota source recovered after 186 failed read(s)` means quota reads
have started working again. It then repeats the old error, including `WITHHOLDING`.
That last word describes the previous failure, not necessarily the current pacing
decision. See `note_provider_recoveries()` in [runtime.py](../src/kandev_pacer/runtime.py).

## Why not have the pacer renew the token itself?

Renewal can replace both tokens. If Claude Code and the pacer renew at the same time,
one can overwrite the other's new credentials. That can break a working login.
Making the pacer wait for its own lock does not help unless Claude Code follows the
same lock too.

Claude Code already handles its own credential writes. Anthropic's changelog records
fixes for concurrent renewal problems in versions 2.1.133 and 2.1.136. Keeping renewal
inside Claude avoids adding another independent writer.
[Source: Claude Code changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md).

## Can Kandev do this for us?

There are ways to ask Kandev for quota, but the inspected paths do not borrow an
already-running Claude session's connection:

- **Provider Usage plugin:** runs CodexBar separately and stores its readings. It tries
  an OAuth token read first, then a command-line fallback. It could supply quota to the
  pacer, but needs integration and testing of the version Kandev actually installs.
  [Source: plugin README](https://github.com/kdlbs/kandev-plugin-provider-usage).
- **Office agent utilization endpoint:** the inspected Kandev checkout has
  `GET /api/v1/office/agents/:id/utilization`. Its Claude client renews and writes the
  shared credential itself. Moving the request there would not remove the competing
  writer problem. This was found in checkout `77e3cfc9`, in
  `apps/backend/internal/office/agents/handler.go`, `internal/backendapp/usage_adapter.go`
  and `internal/agent/usage/client_claude.go`. The endpoint was not called because it
  can change credentials even though it is a GET request.

The old general `/api/v1/agents/usage` endpoint was removed in Kandev 0.83.0, as recorded
in this repository's [earlier integration research](history/kandev-plugin-feasibility.md).

## Which alternatives help?

| Option | What the research found |
| --- | --- |
| Run Claude Code to renew its login | Best direction, but the exact command needs local validation. |
| Run `claude auth status` | Shows login status; do not assume it renews anything. |
| Use `claude setup-token` | Its long-lived token supports model requests, not the quota endpoint. |
| Switch to pebble-quota | The inspected collector reads the same access token without renewing it. Moving the read alone cannot fix expiry. |
| Read Claude's status-line data | Supplies some quota fields after a model response, but does not solve idle recovery or document every model-specific window the pacer needs. |
| Use browser login cookies for quota | Avoids the Claude credential file, but adds separate browser login expiry and possible web access failures. |
| Give a separate collector its own login | Could avoid sharing credential writes, but adds another login to manage. Copying the existing refresh token is not a separate login. |

The completed [Pebble task](tasks/1-quota-from-pebble-collector.md) expected fewer blind
hours. Its source integration is useful, but that expectation is not evidence of token
renewal. The collector inspected on September 18 only read the access token in
`collector/pebble_quota.py`, in `collect_claude()`.

## Evidence and remaining uncertainty

CodexBar has a bounded recovery attempt that invokes Claude's `/status` path, waits for
credential changes, and limits repeated attempts. That is a useful example, not proof
that the same approach works on this Linux host. Its implementation focuses on macOS
Keychain storage.
[Source: delegated refresh implementation](https://github.com/steipete/CodexBar/blob/main/Sources/CodexBarCore/Providers/Claude/ClaudeOAuth/ClaudeOAuthDelegatedRefreshCoordinator.swift).

A first-hand Claude Code bug report measured that `claude auth status` did not renew
an expired token, while a small `claude -p` model request did. The measurement used
version 2.1.223. The standalone executable found locally was 2.1.276; Kandev may use a
different managed version. Neither was tested for recovery during this research.
[Source: upstream report](https://github.com/anthropics/claude-code/issues/84864).

Official documentation says `setup-token` creates a one-year token for model requests.
CodexBar documents that quota reads need the `user:profile` permission, which that token
does not provide. Claude's status-line documentation also says quota fields appear only
after the first API response and may be absent.
[Token documentation](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token),
[quota permissions](https://github.com/steipete/CodexBar/blob/main/docs/claude.md),
[status-line fields](https://code.claude.com/docs/en/statusline).

The next step is a controlled recovery test, followed by the small integration in
[task 15](tasks/15-claude-credential-recovery.md). No live recovery was attempted, no
credentials were changed, and no background processes were started by the research.
