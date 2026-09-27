# Consumer prompt acceptance scenarios

These are synthetic observations for an agent-run check, not production findings.
Use disposable Git repositories. Interpret them with the consumer prompt in
kandev-config and inspect the proposal and ledger commits afterwards.

| Record | Evidence | Existing proposal | Expected result |
| --- | --- | --- | --- |
| exact | Task `exact`, session `s1`: three identical `pytest` commands fail with `command not found`; project declares uv | Run tests through uv; evidence already names `exact/s1` and three failures | No proposal diff; acknowledge |
| similar | Task `similar`, session `s2`: two identical `pytest` calls fail with `command not found`; project declares uv | Same root cause as `exact` | Merge this evidence into that proposal without losing `exact/s1`; acknowledge |
| quiet | Task `quiet`, session `s3`: one successful test call, no errors or blockers | None | No proposal; acknowledge |
| multiple | Task `multiple`, session `s4`: workflow prompt tells an agent to run unavailable `pytest`; project instructions also say plain `pytest` despite declaring uv | None in kandev-config | Write a workflow-prompt proposal to kandev-config and merge project evidence in its tracker; acknowledge only after both commits |
| failed | Task `failed`, session `s5`: repeated project command failure | Target tracker has an unrelated dirty edit | Preserve dirty edit; leave record unread |
| self | Title `[Auto] Learn from session observations` | Not applicable | Do not interpret; add skipped reason when acknowledging the real batch |

Each proposal must give the task/session evidence and a proposed solution prompt.
Keep it under `## Unverified proposals`; preserve the Ordered backlog and every
other section. For a global-skill variant, use the target's `docs/ideas/` convention.

After committing proposals, simulate failure before ledger acknowledgement. Repeat
proposal lookup: the exact and merged evidence must already be present. There must
be no second proposal or extra proposal commit. Acknowledge the successful records;
verify a single local ledger commit and no observation-file modification. The failed
record must remain unread. Then simulate a ledger commit failure and retry using the
same frozen batch; recovery must not stage unrelated files or create another task.
