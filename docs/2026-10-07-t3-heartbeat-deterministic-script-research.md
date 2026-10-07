# T3 worker runs: what deterministic scripts could replace

Status: research findings. No scripts were written; each proposal below is separate
work. This is the T3 counterpart of the Kandev study at
`~/prg/kandev-config/docs/2026-10-07-heartbeat-deterministic-script-research.md`.

Measured 2026-10-07 from the pacer's launch records under
`~/.local/state/kandev-pacer/t3/`, the T3 state database at `~/.t3/userdata/state.sqlite`
(copied, read-only), the Claude Code transcripts under
`~/.claude/projects/-home-agent--t3-worktrees-*-pacer-*/` and the Codex rollouts under
`~/.codex/sessions/2026/10/`. Token figures are the `usage` block of each Claude assistant
message and the `token_count` events Codex stores per model request.

## What "the T3 heartbeat" is

In Kandev mode the pacer calls a webhook and a model run (the `[Auto] Heartbeat`
automation) picks the next work item, creates the Kandev task and makes the scheduling
commit. In T3 mode there is no such model run. `kandev_pacer.t3.T3Pacer.tick` does the
whole heartbeat deterministically:

1. reads each configured `TASKS.md` through Hai's `hai_tasks` API,
2. takes the first issue under Scheduled or Ordered backlog whose dependencies are all
   under Completed / Accepted,
3. moves it to Scheduled with a revision check, commits only `TASKS.md` on `main`,
4. builds a prompt (issue text, root `AGENTS.md`, the tracker's `## Rules` section and a
   fixed scope paragraph; 5,900 to 10,200 characters, about 1,800 tokens) and
5. launches a T3 thread in a fresh `pacer-<thread ID>` worktree.

The Kandev heartbeat webhook is not configured in `~/.config/kandev-pacer/config-t3.toml`,
and the pacer log shows the last webhook call on 2026-10-04, the day T3 mode went live.
So everything the Kandev study proposed for sections 1 to 4 and 7 of the Heartbeat prompt
(capacity, ID resolution, tracker parsing, external IDs, the scheduling commit, archive)
already runs as code in T3 mode. There is nothing left to script on the pacer side of
the launch.

The model work that remains is inside each launched worker thread. Its prompt tells it to
move the issue to In Progress, implement, validate, merge to `main`, move the issue to
Completed / Accepted and remove the worktree. This document measures how much of that
work is tracker and Git bookkeeping that a script could do the same way every time.

## Summary

- The pacer launched 53 worker threads between 2026-10-04 and 2026-10-07 across 14
  repositories (37 Claude Fable, 16 Codex gpt-6.1-sol). 52 threads other than this one
  were analysed; 48 of their issues are now under Completed / Accepted.
- A worker run costs a median of 2.07 million prompt tokens over 26 model requests and 32
  tool calls, and takes a median of 6 minutes of wall time. 96 percent of prompt tokens
  are cached reads; the median uncached input is 84,000 tokens and the median output
  20,000 tokens.
- Half of the tool calls in a median run are tracker or Git bookkeeping, not
  implementation. Across all runs, 47 percent of tool calls edit or commit `TASKS.md` or
  run Git plumbing, and a further 25 percent re-read files the prompt already contains or
  that a script could summarise. Requests whose tool calls are only bookkeeping account
  for 43 percent of Claude's model requests, 41 percent of its prompt tokens and 50
  percent of its uncached input.
- Every Claude run but one wrote its own inline Python to rewrite `TASKS.md`, 88 such
  scripts in 36 runs. Only 15 of 36 Claude runs touched `hai_tasks` at all, even though
  the prompt asks for it; Codex runs spent calls searching for a `hai_tasks` MCP tool
  that does not exist and reading Hai's source to learn the API.
- About 12 of 52 runs hit a `TASKS.md` merge conflict when merging to `main` or rebasing, and
  resolved it by hand with `git checkout --ours` or a fresh inline script. The conflict is
  structural: two workers finishing in the same hour both move a bullet in the same file.
- No run removed its own worktree. 39 `pacer-*` worktrees and branches remain under
  `~/.t3/worktrees/` (4.8 GB), and worktree setup copies shell dotfiles into each
  worktree as untracked files, which one run then committed with `git add -A` and had to
  revert.

## Measurements

### Population

| Runs 2026-10-04 to 2026-10-07 | Value |
| --- | --- |
| Launch records | 53 (52 analysed, 1 is this run) |
| Claude Fable 5.1 / Codex gpt-6.1-sol | 36 / 16 analysed |
| Repositories | 14 of the 18 configured trackers |
| Issues now under Completed / Accepted | 48 of 52 |
| Median prompt tokens per run (sum over model requests) | 2,070,868 |
| Median uncached input tokens per run | 84,494 |
| Median output tokens per run | 20,485 |
| Median model requests per run | 26 |
| Median tool calls per run | 32 |
| Median wall time per run, single-turn runs | 5.9 min (p90 11 min) |
| Total prompt tokens, all runs | 125 million |
| Cached-read share of prompt tokens | 96.1 % |

Claude runs are heavier than Codex runs: median 2.19 million against 1.40 million
prompt tokens, 33 against 22 tool calls, 7.6 against 3.7 minutes. Part of that is
Claude's own per-request overhead (two system-reminder blocks, the Serena manual it is
told to read, `ToolSearch` calls), part is that the Codex runs were mostly small
kandev-pacer and hai documentation fixes.

The four issues that did not complete: one Codex run moved the issue to another
repository's tracker on purpose; one kasvimuseo-deployment run stopped with the issue
under In Progress; one multi-agent-demo run left the issue under Scheduled after its
turn; one kandev-pacer run was still in progress when measured.

### Tool calls by category, all runs

Each shell command was classified by a regular expression; `Edit`, `Write` and
`apply_patch` count as implementation. The categories are coarse but stable.

| Category | Calls | Share | Typical command |
| --- | --- | --- | --- |
| Tracker bookkeeping | 501 | 29.3 % | inline Python rewriting `TASKS.md`, `git commit -- TASKS.md`, `hai_tasks` reads |
| Orientation reads | 422 | 24.6 % | `cat AGENTS.md`, `sed -n 1,40p TASKS.md`, `ls`, `grep` over docs |
| Git flow | 310 | 18.1 % | `git status`, `git log`, `git merge --ff-only pacer-…`, `git rebase main` |
| Validation | 251 | 14.7 % | `uv run pytest`, `ruff`, `npm test`, `deno task check` |
| Implementation, shell | 97 | 5.7 % | `cat > file <<EOF`, inline Python patching source |
| Implementation, edit tools | 44 | 2.6 % | `Edit`, `Write`, `apply_patch`, Serena `replace_content` |
| Tool discovery | 71 | 4.1 % | `ToolSearch`, Serena `initial_instructions`, Codex `ALL_TOOLS.filter(/hai_tasks/)` |

The median run spends 14 of its 32 tool calls on tracker and Git bookkeeping. The
orientation share is inflated by the prompt itself: 19 of 36 Claude runs re-read
`AGENTS.md` from disk although the prompt contains it verbatim, and every Claude run
re-read `TASKS.md` with `cat`, `sed` or `grep` (median 3 times) before and after editing
it.

### Where the tokens go, Claude runs

Each model request was labelled by the tool calls it issued: bookkeeping only,
implementation or validation, or no tool call (a text-only reply or the final report).

| Request kind | Requests | Prompt tokens | Uncached input | Output |
| --- | --- | --- | --- | --- |
| Bookkeeping only | 450 (42.9 %) | 38.5 M (41.2 %) | 1.86 M (50.4 %) | 435 k (45.9 %) |
| Implementation or validation | 535 (51.0 %) | 48.1 M (51.4 %) | 1.71 M (46.4 %) | 473 k (49.9 %) |
| No tool call | 63 (6.0 %) | 6.9 M (7.4 %) | 0.12 M (3.2 %) | 40 k (4.2 %) |

Bookkeeping costs more uncached input than the implementation does because it happens
at both ends of the run, when the tool results it reads (`TASKS.md` dumps, Git output,
Hai's JSON) are new to the cache.

### How runs do the bookkeeping today

| Behaviour, 36 Claude runs | Runs | Count |
| --- | --- | --- |
| Rewrote `TASKS.md` with inline `python3 - <<'PY'` string replacement | 35 | 88 scripts |
| Used `hai_tasks` (CLI or import) for any tracker operation | 15 | 24 calls |
| Re-read `AGENTS.md` from disk | 19 | 50 reads |
| Re-read `TASKS.md` from disk | 36 | 141 reads |
| Ran `git rebase` | 12 | 30 calls |
| Ran `git merge` into `main` | 29 | 75 calls |
| Removed its worktree | 0 | 0 |
| Had at least one failing tool call | 25 | 59 failures |

Codex runs show the same shape with a different accent: six `ALL_TOOLS.filter(/hai_tasks/)`
probes for an MCP tool that does not exist, source reads of `hai_tasks/store.py` and
`markdown.py` to learn the API, then `PYTHONPATH=/home/agent/prg/hai python3 - <<'PY'`
with `TasksStore` to make the move. The two palindromi.fi Codex runs on the evening of
2026-10-06 resolved `TASKS.md` conflicts with `git checkout --ours TASKS.md` followed by
a second inline script.

### Three runs in detail

| Run | Repository | Model | Requests | Prompt tokens | Tool calls | Bookkeeping calls | Result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `69c80336` | pykofinder, CSV preview | Fable 5.1 | 25 | 2,266,048 | 24 | 11 | merged; one commit accidentally added T3's dotfiles and was reverted with `git rm --cached` |
| `4e75aa54` | kandev-pacer, error message | gpt-6.1-sol | 16 | 827,034 | 15 | 12 | merged; 3 of 15 calls were the actual fix, patch and test |
| `cbf7e7db` | hai, all-projects view | gpt-6.1-sol | 58 | 6,207,131 | 57 | 32 | merged; 10 orientation calls on Hai's own source, 32 bookkeeping calls |

Run `4e75aa54` is the clearest case. The first five calls read the prompt's own files
again and probed for a Hai tool. One call moved the bullet with `TasksStore` and
committed. One `apply_patch` and one test run were the work. The remaining calls were
`git diff`, a doc edit, the completion move with another inline script, the commit and a
`git worktree remove` of the worktree the process was running in, which Git refuses.

### Failure modes the bookkeeping causes

- **Merge conflicts in `TASKS.md`.** 10 Claude and 2 Codex runs saw `CONFLICT (content):
  Merge conflict in TASKS.md`. Two workers in the same repository finish within minutes
  of each other (kasvimuseo-deployment: eight issues between 01:00 and 02:48), each
  moves its own bullet, and the second to merge conflicts. The rules at the bottom of
  `TASKS.md` describe the resolution in prose, so each run re-derives it.
- **Spacing and format drift.** Inline replacements leave double blank lines or merge
  two bullets; filemill run `c5960bc0` needed a second commit "fix spacing around [77]".
  Hai's parser would have produced canonical output.
- **Worktree leftovers.** The prompt asks the worker to remove the worktree and branch,
  but the worker runs inside that worktree, so `git worktree remove` fails or is skipped.
  39 worktrees and 39 `pacer-*` branches remain; the pacer's own rejection recovery
  (docs/t3-pacing.md) also assumes leftover worktrees are cleaned by hand.
- **Dotfiles in the worktree.** Worktree setup copies `.bashrc`, `.zshrc`, `.gitconfig`,
  `.idea`, `.vscode` and others into every worktree as untracked files (this run's
  worktree has twelve of them). A worker that commits
  with `git add -A` commits them; run `69c80336` did and reverted.
- **Stuck sections.** One run left its issue under In Progress and one under Scheduled
  after the turn ended. The pacer never relaunches those by design, so they wait for a
  human to notice.

## Prompt instructions and what a script can take over

The worker prompt is built in `T3Pacer.prepare`. The table marks each instruction as
deterministic, judgement or mixed.

| Instruction | Kind | Today | Proposal |
| --- | --- | --- | --- |
| Rebase the worktree branch on `main` before moving the issue | deterministic | 12 runs rebase, most skip it; conflicts later | Script does the rebase as the first step, before the model starts |
| Move the issue to In Progress in the worktree, commit | deterministic | 88 inline scripts, 2 to 4 tool calls, re-reads of `TASKS.md` | `tasks-md start <issue>`: Hai update with revision check and a fixed commit, or the pacer makes this commit on the branch before launching |
| Read repository instructions and tracker rules | mixed | Prompt contains them, 19 runs re-read them anyway | Keep in prompt; drop the Rules section when a `tasks-md` command enforces them |
| Implement, lint, test, review | judgement | model | Stays with the model |
| Merge the rebased branch on `main` | deterministic | 75 merge calls, 12 conflicts, hand resolution | `tasks-md finish`: rebase, re-run validation hook if configured, `--ff-only` merge, conflict resolution of `TASKS.md` by re-applying only the own move |
| Move the issue to Completed / Accepted with `[ ]`, commit | deterministic | second inline script, second commit, spacing fixes | Same `finish` command |
| Remove the worktree and branch | deterministic | 0 runs succeed | Pacer-side: when a thread's turn completes and the branch is merged, remove worktree and branch |
| Report blockers and validation evidence | judgement | final message | Stays with the model |
| Do not check `[x]`, do not change unrelated work | deterministic | prose rule | `tasks-md check` in the `finish` command refuses a diff that touches another bullet |

### What stays with the model

Understanding the issue, deciding what to change, writing code and tests, running the
right validators, and writing the completion note. In the runs studied this is 51 percent
of model requests and about half of the tokens. The other half is the lifecycle that the
pacer already drives on the scheduling side.

## Proposed scripts

All of them belong in `kandev-pacer`, next to `t3.py`, run with
`uv run --frozen --project /home/agent/prg/kandev-pacer`, and use `hai_tasks` for every
tracker change so the revision check and canonical formatting apply. They never bypass
Hai to edit `TASKS.md`.

1. **`tasks-md start <tracker> <issue>`** — in the worktree: rebase on `main`, move the
   issue to In Progress through Hai, commit only `TASKS.md` with the fixed message. The
   worker prompt replaces its first paragraph of lifecycle prose with this one command,
   or the pacer runs it itself on the new branch before the model's first request.
   Replaces about 4 tool calls and 3 model requests per run.
2. **`tasks-md finish <tracker> <issue>`** — in the worktree: rebase on `main`, resolve a
   `TASKS.md` conflict by taking `main`'s file and re-applying only the own issue's move,
   move the issue to Completed / Accepted with `[ ]`, commit, fast-forward `main`, and
   print what it did. Refuses when the diff moves any other bullet or when the tree is
   dirty. Replaces 6 to 12 tool calls and the 12-run conflict tail.
3. **Prompt trim** — once items 1 and 2 exist, the prompt no longer needs the full
   `## Rules` section (about 100 lines, roughly 1,500 tokens per request, cached) nor the
   instruction to use `hai_tasks` directly. The prompt keeps `AGENTS.md`, the issue and
   the two commands.
4. **Pacer-side worktree cleanup** — after a launched thread's turn completes with the
   `pacer-*` branch merged into `main`, the pacer removes the worktree and branch and
   records that in the launch record. Also fixes the leftover-worktree recovery case in
   docs/t3-pacing.md. The worker should stop being asked to remove its own worktree.
5. **Worktree hygiene** — add the T3-copied dotfiles to the pacer's launch bootstrap as a
   `.git/info/exclude` write in the new worktree, or ask T3 for a per-project exclude,
   so `git add -A` in a worker cannot commit them.
6. **Stuck-issue report** — one pacer log line per tick for issues under In Progress or
   Scheduled whose thread is idle or stopped, so a human sees the two stuck cases from
   this week without opening T3.

## Expected effect

For a typical run the model would skip about 14 bookkeeping tool calls and 10 of its 26
requests. Prompt tokens per run would fall from about 2.1 million to roughly 1.2 million,
uncached input from 84,000 to about 45,000, and wall time by two to three minutes. The
larger gain is reliability: the tracker move and merge would be done the same way every
time from Hai's parser, so the 12 hand-resolved conflicts, the spacing-fix commits, the
two stuck issues and the 39 leftover worktrees stop recurring.

## Reproduction

Launch records give thread IDs, trackers, issues and models:

```bash
for f in ~/.local/state/kandev-pacer/t3/*.json; do
  jq -r '[.phase,.tracker,.issue,.command.threadId,.command.createdAt,.command.modelSelection.model]|@tsv' "$f"
done | sort -k5
```

Claude usage per run: in `~/.claude/projects/*pacer-<thread>/*.jsonl`, deduplicate
`assistant` entries by `message.id` and sum `usage.input_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens` and `output_tokens`; tool calls
are `tool_use` blocks in the same entries. Codex usage per run: the rollout under
`~/.codex/sessions/2026/10/` containing `Hai issue ID: <issue>`; take the last
`token_count` event's `total_token_usage`, and count `function_call` and
`custom_tool_call` items. Thread outcomes: `python3 -m hai_tasks read <tracker>` and
compare each issue's `sourceSection`. Leftover worktrees: `ls -d ~/.t3/worktrees/*/pacer-*`.
