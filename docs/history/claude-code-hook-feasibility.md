# Can the pacer be a Claude Code hook?

Assessed 2026-08-02 against **Claude Code 2.1.220**, Anthropic's published docs
(`code.claude.com/docs`, `platform.claude.com/docs`), and this machine's own `~/.claude`
state. Claims marked **inference** are reasoning, not sources.

Most mechanical claims below are read directly out of the shipped 2.1.220 executable
(`~/.local/share/claude/versions/2.1.220`, an unstripped ELF), because the published docs
are thinner than the binary on exactly the details a pacer depends on. Symbol names are
minified and will not survive a version bump; the described *behaviour* is the durable part.

The question: could pacing live one level down — a hook that sits and waits until
`headroom > 0`, knows about the prompt cache, and can be overridden by a magic keyword —
so that every Claude Code session gets paced, not just kandev tasks?

> **Read the install situation first.** This machine has *two* Claude Code installs, and
> the older one shadows the newer on `PATH`:
>
> | path | resolves to | version |
> | --- | --- | --- |
> | `~/.bun/bin/claude` (PATH pos. 8) | `~/.bun/install/global/node_modules/@anthropic-ai/claude-code/cli.js` | **2.1.37** |
> | `~/.local/bin/claude` (PATH pos. 10) | `~/.local/share/claude/versions/2.1.220` | **2.1.220** |
>
> `~/.local` is the native installer that self-updates; the bun copy is a stale
> `bun install -g` that never moved. They differ on things a pacer depends on — 2.1.37 has
> **no `asyncRewake`** and **no `allowedHttpHookUrls`**, and it gives `UserPromptSubmit`
> hooks a 600 s budget where 2.1.220 gives 30 s. One `settings.json`, two engines, silently
> different behaviour. Remove the shadow (`bun remove -g @anthropic-ai/claude-code`) before
> building anything on this.

## Verdict

**Yes as a brake, and yes as a thin client of a pacing service — but not as a replacement
for the pacer.**

A hook can hold a turn at the right boundary on almost every Claude Code frontend, with a
keyword override and cache-aware release. Better, Claude Code ships an **`http` hook type**
that POSTs the hook input to a URL, so the business logic does not have to live in a shell
script on every machine — it can live in one service (§7). Four things still stop it being
the whole pacer:

1. **A hook fails open, in two distinct ways.** A hook that hits its timeout is *cancelled*
   and the turn proceeds; an `http` hook that gets a non-2xx, a connection error or a
   timeout is recorded as a **non-blocking** error and the turn proceeds (§2). Both failure
   modes release work rather than holding it.
2. **A hook is a brake, not a starter.** It holds work someone else launched. It cannot
   launch the next backlog task when the gate opens, which is most of what `kandev-pacer`
   does (§6). `asyncRewake` narrows this gap without closing it.
3. **The web frontend does not run it.** User-level `~/.claude/settings.json` hooks are
   explicitly not carried into cloud sessions — only repo-committed and org-managed ones
   (§5).
4. **Hooks are blind to quota.** No hook event receives usage on stdin; headroom has to be
   sourced separately, and the best sources are either stale or undocumented (§3).

What it *is* good for is the thing the current pacer cannot do at all: stopping an
interactive session — yours, in a terminal, at 11pm — from eating the window the paced
backlog was meant to burn down steadily.

The one finding that changes a conclusion already recorded in [`pacing-designs.md`] is the
cache arithmetic in §4: Claude Code on a subscription now requests the **one-hour** TTL
automatically, not the five-minute one, which inverts the "keeping the cache warm costs as
much as the miss" trade-off.

## 1. Where a hook can gate a turn

2.1.220 registers **31** hook events (read from `HOOK_EVENT_REGISTRY`): `PreToolUse`,
`PostToolUse`, `PostToolUseFailure`, `PostToolBatch`, `PermissionDenied`,
`PermissionRequest`, `Notification`, `Stop`, `SubagentStop`, `StopFailure`, `TeammateIdle`,
`TaskCreated`, `TaskCompleted`, `UserPromptSubmit`, `UserPromptExpansion`, `SessionStart`,
`SessionEnd`, `Setup`, `SubagentStart`, `PreCompact`, `PostCompact`, `ConfigChange`,
`CwdChanged`, `FileChanged`, `DirectoryAdded`, `InstructionsLoaded`, `Elicitation`,
`ElicitationResult`, `WorktreeCreate`, `WorktreeRemove`, `MessageDisplay`.

Only a handful sit anywhere near a pacing decision:

| event | fires | blocks? | fit |
| --- | --- | --- | --- |
| `UserPromptSubmit` | on prompt submit, before processing | yes | **the right boundary** |
| `PreToolUse` | before each tool call, incl. in subagents | yes | paces *within* a turn |
| `Stop` | when Claude finishes responding | yes — but blocking *forces continuation* | wrong direction |
| `StopFailure` | turn ended on an API error; matcher includes `rate_limit` | no | the only quota-aware event, and observe-only |
| `UserPromptExpansion` | slash-command / MCP-prompt expansion only | yes | not a general prompt path (§4) |

**`UserPromptSubmit` is the correct gate**, for the same reason the kandev pacer gates
launches: one prompt is one turn, and it is the boundary *between* turns, where a pause
costs nothing already running. It also carries the raw `prompt` on stdin — verified, the
hook input is built as `{...base, hook_event_name:"UserPromptSubmit", prompt, session_title}`
— which is what makes the keyword override work.

`PreToolUse` is the tempting alternative: it fires per tool call, inside subagents, where
the tokens actually go, and it keeps the general 600 s budget. Two reasons not to make it
the primary gate:

- Blocking there stalls an agent **mid-turn**, holding whatever it holds — a worktree, a
  half-applied edit, a running server — for the length of the wait.
- A mid-turn pause runs the cache clock *inside* a turn, the one place it otherwise never
  runs (§4). It turns a warm session cold for no gain.

There is **no event that fires before each model request**. That is the mirror image of what
we already recorded against design 11, the model API proxy: a proxy sees every request but
cannot tell which ones form one turn; hooks see turns and tool calls but never requests.
Neither can gate the actual unit of spend.

## 2. Blocking mechanics, and the two ways it fails open

- **Hooks are synchronous.** Matching hooks run in parallel with each other, but the agent
  waits for the set. The Agent SDK docs say it plainly: "By default, the agent waits for
  your hook to return before proceeding."
- **Timeouts.** `timeout` is per handler, in seconds, applied as
  `handler.timeout*1000 : default`. The general default is **600 s** (`Hm=600000`), but the
  `UserPromptSubmit` runner passes **30 s** (`frd=30000`) as its default. So the event with
  the right semantics has the *shortest* default budget. **Always set `timeout`
  explicitly** — it is the difference between a 30-second and a 10-minute hold.
- **No general ceiling.** There is a clamp of `max(1500, min(t, 60000))` in the binary, but
  it is `SESSION_END_HOOK_TIMEOUT_MS_DEFAULT` — scoped to `SessionEnd` and overridable by
  `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS`. It does **not** apply to `command` or `http`
  hooks on other events. Nothing else in the binary caps them.
- **Failure mode A — timeout releases the turn.** The abort signal is composed as
  `wN(callerSignal, {timeoutMs})`; when it fires the handler yields
  `{outcome:"cancelled"}`, which carries no blocking decision. The changelog corroborates
  from the other side: a fix for "a hook callback timeout being misreported to the model as
  a user rejection". **A pacer that simply sleeps too long releases the turn it was meant to
  hold.**
- **Failure mode B — a broken `http` hook releases the turn.** Non-2xx, connection error and
  timeout all yield `hook_non_blocking_error` with `outcome:"non_blocking_error"`. So a
  pacing service that is down, slow, or returns 500 is indistinguishable from no pacer at
  all. **The service must return 2xx for its decision to be honoured.**
- **It is auditable after the fact.** `hook_cancelled` transcript attachments carry
  `timedOut: true` plus `timeoutMs` when the hook hit its timeout; user-Esc cancellations
  lack those fields. So you can measure how often the pacer failed open instead of guessing.
- **Esc interrupts the wait**, because that same signal composition includes the caller's
  abort controller. A sleeping pacer stays interruptible — the difference between a brake
  and a hang.
- **`statusMessage`** sets the spinner text while a hook runs. It is the only progress
  affordance a blocked user gets, so it should carry the wait: `paced — headroom -0.30,
  resumes ~14:05`.

### Blocking is gentler than the docs imply

The docs' "blocks prompt, erases it" reads as though a refused prompt is lost. In 2.1.220 it
is not. The block message is composed as:

```
suppressOriginalPrompt ? blockMessage : `${blockMessage}\n\nOriginal prompt: ${prompt}`
```

and `suppressOriginalPrompt` is an opt-in field on `UserPromptSubmit`'s hook output,
described as 'When decision is "block", omit the original prompt from the block message'.
**By default the user gets their prompt echoed back** and can resubmit it. That makes
refusal a far more acceptable pacing outcome than first assumed.

The full `UserPromptSubmit` output schema is `{additionalContext?, sessionTitle?,
suppressOriginalPrompt?}` alongside the universal `continue` / `stopReason` /
`systemMessage` / `suppressOutput`.

### The shape a correct pacer hook has to take

Because timeout means *release*, an unbounded wait is unsafe:

```
timeout: 600                       # explicit, never inherited
loop while headroom < 0 and budget remains:
    sleep min(half the remaining wait, 5..120s, budget left)   # poll_interval(), verbatim
if headroom >= 0:  allow
else:              {"continue": false, "stopReason": "paced — resumes ~14:05"}
```

`{"continue": false, "stopReason": ...}` is preferable to exit code 2: `stopReason` is
defined as the user-facing string and renders as `Operation stopped by hook: <reason>`. This
converts one unbounded wait into **repeated bounded blocks that fail closed** — short pauses
absorbed invisibly inside the sleep, long ones surfacing as an honest "come back at 14:05".

## 3. How a hook learns the headroom

**Hook stdin carries no quota state.** Every event gets `session_id`, `prompt_id`,
`transcript_path`, `cwd`, `permission_mode`, `effort`, `hook_event_name` (plus `agent_id` /
`agent_type` in subagents, `tool_name` / `tool_input` on tool events) — and nothing about
usage. Three sources, worst risk last:

**a) `~/.claude.json` → `cachedUsageUtilization`** — free, no network, no token. Verified on
this machine:

```json
{"fetchedAtMs": 1785648733679,
 "utilization": {"five_hour": {"utilization": 0,  "resets_at": "2026-08-02T10:30:00Z"},
                 "seven_day": {"utilization": 79, "resets_at": "2026-08-03T13:00:00Z"},
                 "seven_day_opus": null, ...}}
```

That is **the same shape `quota.py:parse_windows` already consumes** — a consumed percentage
and a reset instant per window — under different key names. The risk is staleness, and not
the harmless kind: while *this* session is paused, other sessions and machines keep
spending, so a stale reading is stale-*low* on consumption and the hook under-withholds.
`fetchedAtMs` bounds it; treat a reading older than a few minutes as a reason to fall back
or fail closed.

**b) The status line** — the *documented* path. A status line script receives
`rate_limits.five_hour.{used_percentage,resets_at}` and the same for `seven_day`, refreshed
every render at no token cost. Hooks do not get this; status lines do. So the sidecar
pattern is: the status line writes the block to a state file, the hook reads it. Documented
caveat, verbatim: `rate_limits` "appears only for Claude.ai subscribers (Pro/Max) after the
first API response in the session. Each window may be independently absent" — so it is
missing for the first gate decision of a fresh session, exactly when you want it.

**c) `GET https://api.anthropic.com/api/oauth/usage`**, authenticated with
`~/.claude/.credentials.json` → `claudeAiOauth.accessToken`. Found in the shipped bundle and
verified to return live data: `five_hour`, `seven_day`, `seven_day_opus`, `extra_usage`, and
a `limits[]` array of `{kind, percent, severity, resets_at}`. **Undocumented internal API** —
unstable by definition, and a hook using it cannot refresh an expired token.

*Inference:* this is very likely what stands behind kandev's `GET /api/v1/agents/usage`,
whose `utilization_pct` / `reset_at` map one-to-one onto `utilization` / `resets_at`. If so,
a hook pacer gates on the same number the current pacer does, and the two cannot disagree.

What is *not* a source: the Admin API `usage_report` endpoints and the
`anthropic-ratelimit-*` response headers describe API-key/org limits (RPM, ITPM), not the
subscription's 5-hour and weekly windows. `~/.claude/projects/**/*.jsonl` and
`~/.claude/stats-cache.json` hold real per-request token counts, but deriving a window from
them (what `ccusage` does) sees only *this machine* and guesses the boundary — not the
account-wide view the gate needs.

## 4. The override, and what cannot be overridden

**Detection is trivial.** `UserPromptSubmit` receives the raw `prompt`, so `!nopace` is a
substring test.

**Stripping it is impossible.** Searched the 2.1.220 binary for every plausible
prompt-rewrite output field — `updatedPrompt`, `modifiedPrompt`, `replacePrompt`,
`newPrompt`, `expandedPrompt` — **all zero occurrences**. A `UserPromptSubmit` hook can
*add* context (`additionalContext`, 186 occurrences) or *block*, but it cannot alter the
text the model sees. So the magic keyword stays in the prompt. Pick one the model will
harmlessly ignore.

`UserPromptExpansion` is not a way around this. Its input is
`{expansion_type, command_name, command_args, command_source, prompt}` and it fires only for
slash-command and MCP-prompt expansion — not for ordinary typed prompts.

**Scoping the override across events.** If a `PreToolUse` gate is added later, the override
must survive into it. `session_id` is on every hook input, so the `UserPromptSubmit` hook
writes a marker keyed by session and `Stop` clears it — making the override turn-scoped
rather than permanent. Non-keyword channels also exist on every hook's stdin if you would
rather not put it in the prompt: `permission_mode` and `effort`.

## 5. The prompt cache — a correction to what we recorded before

[`pacing-designs.md`] concludes that quota pauses are unavoidable cache misses because
"keeping the cache warm would cost about as much as the miss". That was reasoned on a
**five-minute** TTL. It has changed.

- **TTL is sliding and refreshing is free**: "The cache is refreshed for no additional cost
  each time the cached content is used." Only idle time expires a prefix.
- **Prices:** 5-minute write **1.25×** base input, 1-hour write **2×**, read **0.1×**.
- **On a subscription, Claude Code requests the one-hour TTL automatically.** Verified
  across the five most recent sessions on this machine — every one shows
  `cache_creation.ephemeral_1h_input_tokens` non-zero against `ephemeral_5m_input_tokens: 0`.
  It drops to 5 minutes when spending usage credits rather than plan allowance.
  **Subagents always use the 5-minute TTL**; only the main conversation gets the hour.
- **The cache key includes model, effort level and working directory.** A pause during which
  the user switches model or effort has already lost the cache, whatever the clock says.

### What that means for pacing

The free-pause window is an hour, not five minutes, so most short pauses now cost nothing.
When a pause does cross the line the miss is dearer: re-establishing an expired one-hour
prefix costs `2.0×` where reading it would have cost `0.1×`. For a prefix of **P** tokens,
crossing expiry costs an extra **1.9 P** input tokens — on 100k of history, roughly 190k
tokens of quota spent on nothing.

So the instinct is right, and for a stronger reason than convenience: **if the gate is due
to reopen shortly after the cache is due to expire, release it shortly before instead.**
Releasing early costs a bounded headroom overshoot, repaid by the next tick of elapsed time.
Releasing late costs 1.9 P *and* inflates the consumed fraction the gate reads — a cache
miss makes the pacer's own number worse. Avoiding misses is aligned with pacing, not in
tension with it.

Guard rails:

- Applies only when the remaining wait is **comparable to** the remaining TTL. When headroom
  is hours-deep negative the cache is unsalvageable and the miss should be accepted, exactly
  as the old note said.
- The clock is derivable with no new plumbing: `transcript_path` is on every hook's stdin,
  and the last assistant record's `timestamp` plus its `cache_creation.ephemeral_1h_input_tokens`
  say when the prefix was last touched and which TTL applies.
- Must not fire if model or effort changed during the pause — the prefix is already gone.
- A **keepalive** would be strictly better than early release: a hit costs 0.1 P against a
  1.9 P miss, some 19× less. **A hook cannot do this** — it has no way to make the harness
  send a request. Only the harness or a proxy could. Worth recording as the thing that would
  make pausing nearly free, and as an argument that this belongs *in* Claude Code.

One claim we could not source: whether cache reads are discounted inside the *subscription*
windows the way they are exempted from API ITPM limits. The cost docs say a cached session
"still draws usage for the whole conversation" and flag cache misses as a usage driver,
implying reads count at reduced weight rather than not at all — but that is **inference**.
It changes the size of the numbers above, not their direction.

## 6. Which frontends would actually be paced

"Stated" means an Anthropic doc says so; "inference" means it follows from a verified
mechanism.

| frontend | user-level hook runs? | basis |
| --- | --- | --- |
| CLI in a terminal | yes | stated |
| Desktop app | yes | stated — "Hooks and skills defined in settings apply to both"; `~/.claude/settings.json` is shared |
| VS Code extension | yes | stated — shares `~/.claude/settings.json`; spawns a real `claude` process |
| JetBrains | probably | **inference** — same architecture, unverified |
| Agent SDK (TS/Python) | yes | stated — filesystem hooks load unless `settingSources: []`, alongside programmatic `hooks` |
| kandev, Zed, other CLI/SDK drivers | yes | **inference with a verified mechanism** — hook loading happens inside the engine, not the UI; Zed's `claude-code-acp` bundles the Agent SDK, which spawns the bundled `cli.js` |
| **Web / cloud sessions** | **no** | **stated** — "user-level settings stay on your machine"; only the repo's `.claude/settings.json` and org server-managed settings run |
| Frontends that reimplement the agent loop | no | no Claude Code hook system exists in them |

Two things to be blunt about:

- **The web frontend is the hole in the plan.** Pacing a cloud session means committing the
  hook into each repository's `.claude/settings.json`, or pushing it through org-managed
  settings. There is also no shared machine state there, so no cross-session concurrency
  cap, and headroom must come from source (c). `CLAUDE_CODE_REMOTE` is `"true"` there, so a
  hook can at least tell where it is.
- **This is Claude-Code-wide, not agent-wide.** The objection recorded against design 10 in
  [`pacing-designs.md`] — that hook systems differ between Claude Code, Codex and Hermes —
  stands unchanged. Whether that is a win depends on whether the goal is "all my Claude Code
  work" (yes, big win) or "all agents kandev can run" (no, unchanged).

Hook entries **merge across settings levels** rather than overriding, so a pacer in
`~/.claude/settings.json` cannot be removed by a project — but `disableAllHooks` is a
user-settable escape hatch, and a hook that must not be removable has to be managed policy.
On this machine `~/.claude/settings.json` is a Nix store symlink, so installing anything
means editing the nixos-config.

## 7. The service architecture: `http` hooks

The strongest argument for building this is that the client half is a *native hook type*,
not a script to deploy on every machine. `HttpHookSchema` in 2.1.220:

```
url            URL to POST the hook input JSON to
timeout        seconds, for this specific request
headers        values may reference $VAR / ${VAR}
allowedEnvVars explicit allowlist of interpolatable env var names
if             conditional execution
```

So the whole client is settings JSON, and the business logic lives in one service:

```json
{ "UserPromptSubmit": [{ "hooks": [{
    "type": "http",
    "url": "http://<host>:8787/pace",
    "timeout": 600,
    "statusMessage": "paced — waiting for headroom",
    "headers": { "Authorization": "Bearer $PACER_TOKEN" },
    "allowedEnvVars": ["PACER_TOKEN"]
}]}]}
```

Verified runner behaviour: it POSTs the hook input as the body, honours
`allowedHttpHookUrls` patterns from managed settings (blocking non-matching URLs with a
warning), sets `maxRedirects: 0` and `validateStatus: () => !0` so it reads the body on any
status, and routes through a sandbox or env proxy when one exists. `httpHookAllowedEnvVars`
is the matching managed-settings key restricting header interpolation.

**Holding the turn, two mechanisms:**

- **Short waits — hold the HTTP response.** The service simply does not reply until
  `headroom ≥ 0`. That is literally "sit and wait", with no polling. Bounded by the hook
  `timeout`; because a lapsed timeout *releases* the turn (§2), the service must own the
  deadline and answer at `timeout − ε` with a refusal rather than let it expire.
- **Long waits — refuse, then rewake.** Past the hold budget, answer `{"continue": false,
  "stopReason": ...}` and arm a rewake. The companion is a second hook with
  `asyncRewake: true` whose command long-polls the service and exits 2 when the gate opens.
  Schema, verbatim from 2.1.220: "If true, hook runs in background and wakes the model on
  exit code 2 (blocking error). Implies async." Two `@internal` siblings tune the surface:
  `rewakeMessage` (prefix of the system-reminder shown to *the model*, hook output appended
  after it) and `rewakeSummary` (one line shown to *the user*, defaults to "Stop hook
  feedback").

**The limit on rewake:** it wakes *the model* with a system reminder. It does not restore a
prompt that a refusal discarded. That makes it a good fit for unattended, agentic
continuation and a poor one for an interactive session whose prompt was refused — there the
honest UX is the echoed-back prompt of §2 and a manual resubmit. Whether a rewake can carry
enough context to restart refused work is the one thing to prototype before committing.

**What the service buys that a hook alone cannot:**

- **Cross-session concurrency.** `PACER_CONCURRENCY` becomes real, via leases. This is the
  single thing hooks fundamentally cannot do alone.
- **Ordering.** Without it, whoever polls first wins; with it, the pacer's queue discipline
  extends across every frontend.
- **One reading of usage.** One poll per interval instead of one per hook invocation, and
  every session sees the same number rather than its own stale copy.
- **`quota.py` runs unmodified.** It imports nothing but `config`, so `headroom`,
  `worst_headroom`, `resume_at`, `poll_interval` and `format_wait` are the service's brain
  as-is. The service *is* kandev-pacer's core with a second front door; the existing loop
  and the hooks become two clients of one gate.
- **One log**, in the existing JSONL format, across frontends.

**Costs and risks:** every prompt submit now waits on a network round-trip; a service that
is down releases every turn unpaced (§2, failure mode B) — pair it with a `command` hook
that fails closed if enforcement matters more than availability; and a cloud sandbox almost
certainly cannot reach a private/tailnet address, so the web gap of §6 remains.

## 8. What a hook cannot do that the pacer does

- **No queue, no ordering** without the service of §7. The pacer decides *which task goes
  next*; a hook decides only whether *this* session proceeds now.
- **No concurrency cap** without shared state, and on one machine only.
- **It brakes, it does not start.** When the gate opens the pacer *launches* the next
  backlog task; a hook can only release work something else launched. `asyncRewake` narrows
  this — it can wake a session when headroom returns — but it wakes a *conversation*, not a
  queue. Unattended overnight burn-down remains the pacer's job.
- **It fails open**, on timeout, on HTTP failure and on `disableAllHooks`. The pacer fails
  closed: a provider it cannot read means withhold.
- **No BLOCKED flagging, no launch-refusal retry** — though a hook can log its own.

## 9. If we build it

- **Gate `UserPromptSubmit`**, explicit `timeout`, bounded wait, `statusMessage` carrying
  the wait, refusal via `{"continue": false, "stopReason": ...}` leaving the original prompt
  echoed. Add a `PreToolUse` gate later for subagent spend, accepting the mid-turn cost.
- **`http` hook against a service** holding `quota.py`, leases and the log; `command` hook
  instead if failing closed on service outage matters more.
- **Headroom from `cachedUsageUtilization`** with a `fetchedAtMs` age check, falling back to
  a status-line-written state file, then the OAuth endpoint.
- **Override by keyword**, detected on `UserPromptSubmit`, persisted per `session_id` and
  cleared on `Stop`. It cannot be stripped from the prompt — choose one the model ignores.
- **Remove the shadowed 2.1.37 install first**, or the same config will behave two ways.

Together the halves cover the whole surface: **the hook is the brake on attended work, the
pacer is the throttle and starter for unattended work**, and they read the same number.

[`pacing-designs.md`]: pacing-designs.md
