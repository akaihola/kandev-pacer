---
depends_on: []
---

# Research how to redeem resets for both subscriptions

## Original request

2026-10-10: "Also a research task for how you could redeem the resets for both
subscriptions."

## Why

[`resets.py`] already sends redemption requests for Claude and Codex, but they have never
run against the live services. Task 32's completion note says no live reset was
redeemed. If a request is wrong, the pacer withholds that provider at 100% weekly usage
and never gets the refill. That fails safe, but the reset's value is lost without anyone
noticing.

## Questions

1. How does a person redeem a reset in each official client? List the steps for the
   Claude app or web, Claude Code, and the Codex app, CLI and web. Which reset types
   exist? Claude has session and weekly resets. Codex has full banked resets.
2. Which requests do the official clients send to redeem? Compare them with
   `resets.py`:
    - Codex: `POST …/wham/rate-limit-reset-credits/consume` with `credit_id`,
      `redeem_request_id` and the `chatgpt-account-id` header.
    - Claude: `POST /api/organizations/{uuid}/reset_rate_limits` with
      `program: cedar_ember`, `grant_id` and `request_id`.
3. Which result codes can come back? The code expects `reset`, `already_redeemed`,
   `already_used`, `nothing_to_reset`, `no_credit`, `not_limited`, `ineligible` and
   `cooldown`. Anything else is treated as unconfirmed and retried.
4. Does reusing a saved request ID really stop a retry from spending a second reset?
5. Can the OAuth logins that Claude Code and Codex CLI store be used for these
   endpoints? Do the provider terms allow automated redemption?
6. What does a redeemed reset do to the windows? Does a Claude weekly reset also clear
   the five-hour session? Which new window timestamps does Codex report?
7. How can the pacer's requests be verified without wasting a reset? For example, one
   supervised redemption at 100% weekly usage with the request and response logged.

## Sources to check

- Provider help pages: [Claude limit resets] and [Codex banked resets].
- The open-source [Codex CLI].
- [T3 Code], which task 32 named as the model for reading reset information.
- The requests claude.ai sends when you redeem from Settings > Usage on your own
  account, captured with browser developer tools.

## Output

- A research note under `docs/` that answers each question and cites its source.
- Corrections to `resets.py` and [reset-aware-pacing.md] if the real flow differs,
  filed as follow-up tasks.
- Don't redeem a live reset without Antti's go-ahead.

[`resets.py`]: ../../src/kandev_pacer/resets.py
[reset-aware-pacing.md]: ../reset-aware-pacing.md
[Claude limit resets]: https://support.claude.com/en/articles/17007452-what-is-a-limit-reset
[Codex banked resets]: https://help.openai.com/en/articles/20001498-how-banked-codex-resets-work
[Codex CLI]: https://github.com/openai/codex
[T3 Code]: https://github.com/pingdotgg/t3code
