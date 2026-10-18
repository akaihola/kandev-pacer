---
depends_on: []
---

# Record banked resets and redemption outcomes in files the steward can read

Rewritten on 2026-10-18 at Antti's request, so that the outcomes serve what the
steward (`~/my-knowledge/agents/steward/`) needs from reset redemption. It stays an
unverified proposal until Antti revisits it. The 2026-10-10 version, filed from the
[reset research](../reset-redemption-research.md) (task 37), is in Git history;
the last section says what changed and why.

## What the steward needs

The steward keeps every allowance in use, banked resets included, and routes work
to the subscription whose spare quota would expire first. For that it needs, per
provider, each banked reset with its expiry, what the pacer plans to do with it,
and what happened when the pacer tried to redeem one. It uses the answers to:

- record banked resets every tick in its `quota.jsonl`, and count them as unused
  quota when a weekly window resets within 36 hours (its spare-quota rule);
- keep launchable work queued for a provider whose allowance is about to run out
  with a reset waiting, since the pacer redeems only while work is waiting;
- raise an urgent message for Antti when a reset will expire before the plan
  spends it, when a pending redemption keeps withholding a provider, or when an
  outcome needs him (sign in again, review an unknown result);
- report resets spent, refused and expired per subscription in its weekly report;
- give [41]'s supervised trials their evidence record.

Today its only source is the pacer's tmux status lines, which it reads with an
unsandboxed `tmux capture-pane` into its `scripts/quota-line.py`. They fall short
in four ways.

- The count disappears in the states that matter. Lines name resets as "N resets
  usable" or "N resets planned". A held reset reads "N resets held", an exhausted
  allowance with a reset waiting reads "CEILING (reset budget exhausted)" with no
  count, and a pending or failed redemption reads "quota unavailable,
  WITHHOLDING". The steward's script records 0 for all three. Claude's one reset
  will likely read "held" from about 14:45 UTC on 2026-10-18, one burn time
  (~22 h) before its rollover on 2026-10-19 13:00 UTC.
- Expiry dates appear nowhere, although the pacer reads them. The steward's
  2026-W42 report could only guess Claude's (2026-10-22) from secondary sources.
- A failed redemption in T3 mode logs the same "quota unavailable, WITHHOLDING"
  line as a failed usage read. The reason, such as `reset not applied
  (ineligible)` or a lost reply, is dropped. Only success gets a line of its own,
  and scrollback is gone after a restart.
- Nothing separates a pacer redemption from other changes. Codex's weekly window
  restarted on the evening of 2026-10-17 with all three resets still listed. Antti
  can also redeem by hand in claude.ai, Claude Desktop or Codex's `/usage`, and
  T3's usage panel on t3code `main` can redeem one too, outside the pacer's lock.
  A grant that vanishes with no pacer attempt on record was spent elsewhere or
  expired.

## What to build

1. **Reset status file.** Every tick, for every configured provider, rewrite one
   JSON file beside `burn.json`, for example
   `~/.local/state/kandev-pacer/reset-status.json`, with its path configurable
   like `PACER_BURN_STATE`. Per provider it holds the time of the quota read it
   reflects; each usable grant's alias, count, expiry or none, and offer type when
   the provider reports one; and the plan state with its details:
   - `none`: no usable grant;
   - `planned`: Claude, with the depletion target;
   - `usable`: Codex, no weekly speed cap;
   - `held`: with the time to rollover and the burn time;
   - `due`: the shared weekly allowance is used up and a reset waits for work;
   - `pending`: a saved request, with its age, attempt count and last outcome.

   A failed read writes `null` for that provider, never 0. The names are a
   suggestion; document whatever ships.
2. **Attempt log.** Append one JSON line to `reset-attempts.jsonl` in the same
   directory for every redemption POST, and one when a later read confirms or
   rejects it. A POST line holds the UTC time, provider, account alias, grant and
   request aliases, the endpoint with the organization ID replaced by its alias,
   the HTTP status or transport error type, the outcome class and the provider's
   own code, Codex's `windows_reset`, the five-hour and weekly windows and grant
   counts from the read before, and whether the saved request was kept. A
   confirmation line holds the same windows and counts after, and its verdict.
   Aliases are short hashes. Exact IDs stay only in the private recovery records
   in `reset_state_dir`. Never rewrite or truncate the log.
3. **Outcome classes that say who acts**, per provider:
   - applied, once fresh quota confirms it: Codex `reset`, `already_redeemed`;
     Claude `reset`, `already_used`. Before that, awaiting confirmation;
   - not applied, settled: Codex `nothing_to_reset`, `no_credit`; Claude
     `not_limited`, `ineligible`, `cooldown`. The saved request is cleared;
   - unconfirmed, retrying the same request: timeout, transport failure, malformed
     JSON, HTTP 5xx, Claude `unavailable` named explicitly, and any unknown code;
   - rate limited: HTTP 429, retried later with the same request;
   - sign-in needed: HTTP 401 or 403. Antti has to act.

   Codex's codes follow the pinned official enum in the research note. Label
   Claude's as T3-derived until [41] captures the real contract. Don't widen what
   counts as applied. The T3 status line names the class and reason instead of
   "quota unavailable", and the ceiling line keeps the reset count.
4. **Confirmation that expiry can't fake.** A grant that is gone because its
   expiry passed is `expired`, not applied. Record both windows; a weekly drop
   alone doesn't show that a Codex reset refreshed the five-hour window. Other
   changes to the confirmation rule wait for [41]'s evidence.
5. **Docs.** In [reset-aware-pacing.md](../reset-aware-pacing.md), describe both
   files and their fields and say they hold no credentials. Correct "retrying asks
   about the same redemption instead of spending another credit": reusing the ID
   is intended retry behaviour with strong Codex client evidence and only
   third-party evidence for Claude, not a verified server guarantee. Link the
   research note, and update the status-line part of
   [t3-pacing.md](../t3-pacing.md).
6. **Offline tests.** HTTP fixtures for every outcome above for both providers; a
   lost reply and a restart that resends the same saved body and ID; held, due and
   pending states each keep their count in the status file; and a check that no
   token, cookie, Authorization header or organization UUID reaches either file or
   the log.

## Limits

- No live redemption POST, OAuth probe or request replay in this issue. A live
  trial needs Antti's separate approval and belongs to [41].
- Never log or store tokens, cookies, refresh tokens, Authorization headers or raw
  HAR files.
- Unknown outcomes stay withheld with the saved request ID.
- Out of scope: whether work blocked by a dirty checkout should count as waiting
  before a redemption, and a general machine-readable T3 decision log with a
  systemd unit. Both are separate steward proposals. If that log lands first,
  these records may live in it, as long as the fields above survive and nothing
  needs the tmux pane.
- When this lands, the steward switches `quota-line.py` to the status file. That
  change is the steward's own.

## Changes from the 2026-10-10 version

- **Title.** It named the mechanism, provider-specific outcomes. Those are now
  item 3, a means to the files the steward reads.
- **Kept.** The review against the Codex enum and T3's Claude schema, Claude
  `unavailable` named, unknown outcomes withheld, redacted HTTP status separating
  401/403, 429, transport failure and malformed JSON, the documentation fix,
  offline fixtures, no live POST without Antti's approval, and no credentials in
  any log.
- **Widened.** Aliases and before/after quota were to be recorded for a
  supervised trial only. Every attempt gets them now, because nothing else shows
  what happened to a reset.
- **New.** The status file and the `held`, `due` and `pending` states. The
  console is the steward's only source, and it drops the count in exactly those
  states. The outcome classes now say who has to act, to match the steward's
  urgent messages.
- **Narrowed.** The confirmation review keeps two offline fixes, expired grants
  and both windows recorded. Whether a Codex reset clears the five-hour window,
  or a Claude weekly offer does, needs a live capture and stays with [41].
- **Dropped.** Only the old title's framing. Claude's permission question and
  offer typing were already [41]'s, and the switch of the steward's own script
  stays outside this issue.
