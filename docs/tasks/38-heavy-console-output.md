The JSON lines on the console output of kandev-pacer are heavy for a human to read. Which parts are essential information and what could be hidden (if not all)?

```
❯ [ -n "$T3_THREAD_AUTH_TOKEN" ] && uv run --extra t3 --frozen kandev-pacer --config /home/agent/.config/kandev-pacer/config-t3.toml --agent-providers-raw 'codex=__api_key__ claudeAgent=__api_key__ hermes-acp=__api_key__' --apikey-policy cap
T3 ignores --agent-providers-raw entry hermes-acp=__api_key__: it matches no T3 provider instance. Use codex or claudeAgent, or the Kandev aliases codex-acp or claude-acp   
T3 f79c9366-cca4-4561-b65a-657c1d2dff35: {"id": "f79c9366-cca4-4561-b65a-657c1d2dff35", "branch": "pacer-f79c9366-cca4-4561-b65a-657c1d2dff35", "worktreePath": "/home/agent/.t3/worktrees/palindromi.fi/pacer-f79c9366-cca4-4561-b65a-657c1d2dff35", "session": {"threadId": "f79c9366-cca4-4561-b65a-657c1d2dff35", "status": "stopped", "providerName": "codex", "providerInstanceId": "codex", "runtimeMode": "full-access", "activeTurnId": null, "lastError": "This thread's workspace folder no longer exists or is not a directory: /home/agent/.t3/worktrees/palindromi.fi/pacer-f79c9366-cca4-4561-b65a-657c1d2dff35. Restore the folder at this path before retrying.", "updatedAt": "2026-10-07T04:12:00.586Z"}, "latestTurn": {"turnId": "01a112cd-9fa7-7a52-bba3-deb1009a9ebf", "state": "completed", "requestedAt": "2026-10-06T20:00:27.061Z", "startedAt": "2026-10-06T20:00:27.061Z", "completedAt": "2026-10-06T20:03:05.048Z", "assistantMessageId": "assistant:msg_08f49e251369b62b016ac553f7a48c81919f55f390c5eb4f3a"}, "hasPendingApprovals": false, "hasPendingUserInput": false}
T3 e06a9686-844b-468f-be02-1c6a587279c9: {"id": "e06a9686-844b-468f-be02-1c6a587279c9", "branch": "pacer-e06a9686-844b-468f-be02-1c6a587279c9", "worktreePath": "/home/agent/.t3/worktrees/filemill/pacer-e06a9686-844b-468f-be02-1c6a587279c9", "session": {"threadId": "e06a9686-844b-468f-be02-1c6a587279c9", "status": "stopped", "providerName": "claudeAgent", "providerInstanceId": "claudeAgent", "runtimeMode": "full-access", "activeTurnId": null, "lastError": null, "updatedAt": "2026-10-07T05:13:51.119Z"}, "latestTurn": {"turnId": "d82a00e7-5250-4574-9728-01f470bf5246", "state": "completed", "requestedAt": "2026-10-07T04:38:04.875Z", "startedAt": "2026-10-07T04:38:04.875Z", "completedAt": "2026-10-07T04:40:07.331Z", "assistantMessageId": "assistant:1958e959-4134-49ab-b0e7-7f16feff9b0d"}, "hasPendingApprovals": false, "hasPendingUserInput": false}
T3 withheld: Launch record does not match endpoint/trackers
T3 f79c9366-cca4-4561-b65a-657c1d2dff35: {"id": "f79c9366-cca4-4561-b65a-657c1d2dff35", "branch": "pacer-f79c9366-cca4-4561-b65a-657c1d2dff35", "worktreePath": "/home/agent/.t3/worktrees/palindromi.fi/pacer-f79c9366-cca4-4561-b65a-657c1d2dff35", "session": {"threadId": "f79c9366-cca4-4561-b65a-657c1d2dff35", "status": "stopped", "providerName": "codex", "providerInstanceId": "codex", "runtimeMode": "full-access", "activeTurnId": null, "lastError": "This thread's workspace folder no longer exists or is not a directory: /home/agent/.t3/worktrees/palindromi.fi/pacer-f79c9366-cca4-4561-b65a-657c1d2dff35. Restore the folder at this path before retrying.", "updatedAt": "2026-10-07T04:12:00.586Z"}, "latestTurn": {"turnId": "01a112cd-9fa7-7a52-bba3-deb1009a9ebf", "state": "completed", "requestedAt": "2026-10-06T20:00:27.061Z", "startedAt": "2026-10-06T20:00:27.061Z", "completedAt": "2026-10-06T20:03:05.048Z", "assistantMessageId": "assistant:msg_08f49e251369b62b016ac553f7a48c81919f55f390c5eb4f3a"}, "hasPendingApprovals": false, "hasPendingUserInput": false}
T3 e06a9686-844b-468f-be02-1c6a587279c9: {"id": "e06a9686-844b-468f-be02-1c6a587279c9", "branch": "pacer-e06a9686-844b-468f-be02-1c6a587279c9", "worktreePath": "/home/agent/.t3/worktrees/filemill/pacer-e06a9686-844b-468f-be02-1c6a587279c9", "session": {"threadId": "e06a9686-844b-468f-be02-1c6a587279c9", "status": "stopped", "providerName": "claudeAgent", "providerInstanceId": "claudeAgent", "runtimeMode": "full-access", "activeTurnId": null, "lastError": null, "updatedAt": "2026-10-07T05:13:51.119Z"}, "latestTurn": {"turnId": "d82a00e7-5250-4574-9728-01f470bf5246", "state": "completed", "requestedAt": "2026-10-07T04:38:04.875Z", "startedAt": "2026-10-07T04:38:04.875Z", "completedAt": "2026-10-07T04:40:07.331Z", "assistantMessageId": "assistant:1958e959-4134-49ab-b0e7-7f16feff9b0d"}, "hasPendingApprovals": false, "hasPendingUserInput": false}
T3 withheld: Launch record does not match endpoint/trackers
```

## Decision (2026-10-10)

The per-thread JSON came from the pacer logging the whole `thread` section of the launch
record after every tick. The same data is already saved to the record file, so printing it
again only told a person that "something was observed" at the cost of a 900-character
line, repeated for every thread on every tick.

What a person watching the console acts on:

- which issue and tracker the thread belongs to, and the thread ID to find it in T3
- the provider, because quota and capacity are per subscription
- `session.status` and `latestTurn.state`, which say whether work is running, done or
  stopped
- `hasPendingApprovals` / `hasPendingUserInput`, which mean the thread waits for a human
- `session.lastError`, the only free-text clue when something went wrong

What was hidden from INFO (still in the record file and in the DEBUG log):

- `id` and `session.threadId` (duplicates of the thread ID already in the line)
- `branch` and `worktreePath` (derivable from the thread ID and the project)
- `providerInstanceId`, `runtimeMode`, `activeTurnId`, `updatedAt`
- `latestTurn.turnId`, `requestedAt`, `startedAt`, `completedAt`, `assistantMessageId`

The INFO line is now `T3 <issue> from <tracker> on <thread ID> (<provider>): session
<status>, turn <state>[, needs approval][, needs input][; error: <lastError>]`. The
`T3 withheld: Launch record does not match endpoint/trackers` line in the example is a
separate issue, tracked as [33].
