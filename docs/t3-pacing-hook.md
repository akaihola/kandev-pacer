# Admit paced prompts in T3 Code

Status: T3 Code has the hook as a private patch. The pacer doesn't use it yet, because its T3 mode still speaks the protocol of T3 0.0.44 and 0.0.45.

## What the hook does

In a patched T3 Code, a prompt that ends with `...` or `…` doesn't start right away. Spaces and newlines after the dots don't matter, and phones often turn `...` into `…`, so both count. T3 queues the prompt and waits until the pacer admits it. That lets you write the next prompt now, on any T3 client, and have it sent when the provider has allowance.

Only prompts a person sends are paced. Prompts from agents, the server and other threads start as usual, even if they end with dots, and so do continuations of a turn that already ran.

The T3 side lives in `/home/agent/repos/ai/t3code` on branch `t3code/implement-pacing-hook`. The code is one commit, carried as a patch on top of upstream. Its own docs are in `docs/pacing-hook/` on that branch. It needs T3 built from upstream `main` (the V2 orchestrator), so the installed 0.0.45 can't run it.

## What the pacer has to change first

Pointed at upstream `main`, T3 mode withholds every tick today, hook or no hook. The V2 shell snapshot no longer has `session`, `latestTurn`, `hasPendingApprovals` or `backgroundLiveness`, so `activity()` raises "Incomplete T3 snapshot". Launches fail too, because `thread.turn.start` is gone. Section 5 of the [research report][research] lists each broken assumption and its V2 replacement. In short:

- Work out activity from `status`, `activeRunId`, `activityRunStatus`, `pendingRuntimeRequest` and `pendingBackgroundTasks`, and read `lastError` at the top level.
- Launch new threads with `orchestration.launchThread`, which returns `{threadId, projection, resumed}`.

## Finding paced prompts

Call `orchestration.subscribeShell` with `{}` and take the first chunk whose `kind` is `snapshot`, as the pacer already does. A patched server adds `pacedRuns` to it:

```json
{"threadId": "…", "runId": "…", "modelSelection": {"instanceId": "claudeAgent", "model": "…"}, "requestedAt": "2026-10-12T09:30:00.000Z"}
```

- If `pacedRuns` is present, even as `[]`, the server supports the hook. If it's missing, the server is unpatched: don't admit anything, and log it once.
- The list is oldest first. It holds only prompts that are parked: queued on a thread that isn't archived and has nothing preparing, starting, running or waiting.
- It doesn't check usage limits. Skip a thread whose `threads[]` entry has `lastErrorClass` set to `"usage_limit"`, or T3 refuses the admission.
- A paced prompt is not activity. Don't count it against `concurrency`, or a waiting prompt blocks its own release.

## Admitting one

Send `orchestration.dispatchCommand` with:

```json
{"type": "queued-run.admit", "commandId": "pace-admit:<runId>:<attempt>", "threadId": "…", "runId": "…"}
```

The session needs the `orchestration:operate` scope. Save the `commandId` before sending, like the launch records do. Admit the oldest eligible prompt first, at most one per tick, and before pending launches, because a closed gate on a pending launch returns early from the tick.

What comes back:

| Reply | Meaning | What to do |
|---|---|---|
| Success `{sequence}` | T3 started the prompt. The run is usually `starting` already. | Count it as running until the shell shows that thread active. If after a few ticks it's neither listed nor active, drop the booking. That happens when T3 held the start, for example because the provider instance was turned off. |
| Failure with `queued-run.admit refused: busy`, `not_next` or `usage_limited` | Not now. | Retry on a later tick with a new `commandId`. |
| Failure with `queued-run.admit refused: not_waiting` or `inactive` | The user started, edited, removed or stopped the prompt, or archived the thread. | Drop it. |
| "Previously rejected" | This `commandId` was refused before. T3 remembers refusals per id. | Mint a new `commandId`. |
| No reply (timeout or lost socket) | Unknown. | Resend the same `commandId`. T3 answers from its stored result instead of starting the prompt a second time, so a retry can't burn a second turn. |

## Pacer-side rules

- Make sure prompts the pacer launches don't end with `...` or `…`. T3 stamps every WebSocket dispatch as sent by a user, so such a prompt would park and wait for the pacer to admit it.
- Add the provider of every listed prompt to the reset-redemption `waiting` set. Otherwise no reset is redeemed for a provider that only has paced prompts waiting.
- Prompts on providers the pacer can't map (Cursor, Grok, OpenCode, custom instance ids) can never be admitted. Log and skip them without withholding the rest of the tick.

## What the user sees and can do

Unchanged T3 clients show a parked prompt as a held queue: "Resume thread" on web, "Queue held after restart" with "Resume queue" on mobile. Pressing Resume parks it again. To send one without the pacer, edit away the trailing dots and then press Resume, or remove it from the queue.

[research]: 2026-10-12-t3-pacing-hook-pacer-research.md
