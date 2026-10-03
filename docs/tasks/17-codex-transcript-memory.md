---
depends_on: []
---

# Stop `kandev-learn sweep` from loading every Codex transcript into memory

Implemented and deployed on 2026-10-03. The timer is running again; measured
verification is recorded below. The incident description preserves the original state.

## What happened

On 2026-10-03 at 11:54 the `kandev-learn.timer` run of `kandev-learn sweep` grew to
4.3 GiB of RAM in under three minutes. gogo has 11 GiB of RAM, and Kandev and its agents
already used most of it, so the kernel started pushing everything else out to swap.
Load went from 6 to 59, almost every process sat waiting on the disk, and gogo was
unusable until the sweep was stopped. Stopping it freed 4.8 GiB at once.

Two things now guard gogo, both set up from nixos-config
(`docs/in-progress/cap-kandev-learn-memory.md` there):

- `kandev-learn.service` has `MemoryMax=1G` and `MemorySwapMax=0`. A sweep that goes
  over 1 GiB is killed (systemd reports `Result: oom-kill`) instead of taking the host
  down.
- `kandev-learn.timer` is **stopped** on `agent@gogo`. Start it again only after this
  task lands, because every sweep would currently be killed (see below).

## Why it uses so much memory

The two tasks pending in `~/.local/state/kandev-learn/state.json` (`e5ae1474…` and
`3d4c93ff…`) both ran on `codex-acp`. For a Codex session, `collect_transcripts()` in
`src/kandev_pacer/learn_sources.py` has no index from session to file, so it does this:

```python
files = (home / ".codex/sessions").rglob("*.jsonl")
for file in files:
    entries = [e for e in loader._load_entries(file) if isinstance(e, dict)]
    ...
    cwd = next((e.get("payload", {}).get("cwd") for e in entries
                if e.get("type") == "session_meta"), None)
    if cwd != path:
        continue
```

It parses **every** file in `~/.codex/sessions`, completely, and only then looks at the
`session_meta` entry to see whether the file belongs to this task. On 2026-10-03 that
directory held 1702 files, 4.6 GB in total.

`_load_entries()` in `src/kandev_pacer/vendor/claude_session.py` makes it worse:

```python
for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
    entries.append(json.loads(line))
```

For one file, that holds three copies at once: the whole file as one string, the list of
line strings, and the parsed dicts. Parsed JSON in Python takes several times the bytes
of the text, and a string with even one non-ASCII character can take up to 4 bytes per
character.

One file is huge:
`~/.codex/sessions/2026/09/25/rollout-2026-09-25T12-42-16-01a0d7f1-b26f-7ca0-8d47-bf2a7cc5c943.jsonl`
is 1.17 GB (last written 2026-09-27). The next largest is 250 MB. Parsing that one file
alone accounts for the 4.3 GiB. So **any** sweep that includes a Codex task blows up,
however small that task's own transcript is.

The two Codex tasks stay in `pending` until a sweep finishes them, and `sweep()` retries
`pending` before anything new. Under the 1 GiB cap every sweep is now killed at the same
place, so no observations are recorded at all until this is fixed.

## Change

1. **Check the Codex file's owner before loading it.** In a Codex rollout the first line
   is the `session_meta` entry (checked on the 1.17 GB file). Read lines only until the
   first `session_meta`, take its `payload.cwd` and `payload.id`, and skip the file when
   the cwd is not the session's `workspace_path`. The current code also uses the *first*
   `session_meta` (`next(...)`), so this keeps the same answer.
2. **Skip files that cannot overlap the session window.** If a file's mtime is before the
   session's start, nothing in it can fall inside the window. This uses one `stat()` call
   and is time-zone safe. Do not compare against the timestamp in the file name: it is
   local time (`T12-42-16` above), while the entries use UTC (`09:42:16Z`).
3. **Stream lines instead of `read_text()`.** Iterate over the open file one line at a
   time, and keep only entries whose timestamp is inside `[start, end]` (plus whatever
   the caller needs from outside the window, such as `session_meta`). Compute the
   `full_window` flag while streaming instead of from a full list. The Claude and pi
   branches call `_load_entries()` too and get the same benefit.

`vendor/claude_session.py` is vendored code. Prefer a streaming loader in
`learn_sources.py` over editing the vendored copy, unless the vendored copy is already
diverged on purpose.

## Done when

- A test shows a Codex file whose `session_meta` cwd does not match is not read past its
  `session_meta` line. One way: make every later line invalid JSON, or count
  `json.loads` calls.
- A test shows entries outside the session window are not kept, and that `full_window`
  is still computed correctly.
- On `agent@gogo`, one manual run with the cap in place succeeds and finishes the two
  pending Codex tasks:

  ```bash
  systemctl --user start kandev-learn.service
  systemctl --user show kandev-learn.service -p Result -p MemoryPeak
  ```

  `Result=success`, with `MemoryPeak` well under 1 GiB. Record the number in this file.
- `systemctl --user start kandev-learn.timer` on `agent@gogo`, and the next timed run also
  succeeds.

## Note for the deploy step

The timer, the service unit and the memory cap are declared in nixos-config
(`home-manager/home-agent-gogo.nix`), not here. Nothing in nixos-config needs to change
for this task. Only restarting the timer is needed, and that is a manual step on
`agent@gogo`.

## Implementation and verification, 2026-10-03

Deployed `295e767` and `876a7d6` to `/home/agent/prg/kandev-pacer` on gogo.
`transcript_window()` now checks mtime before opening, rejects a Codex rollout at
its first mismatched `session_meta`, and retains only entries in the inclusive
session window. It computes `full_window` while streaming, including timestamps
on discarded records. Claude and Pi use the same loader. Vendored Python files
are unchanged.

The first production run, at 15:59:27 EEST, successfully recorded both pending
tasks but reached exactly 1 GiB, so it failed the memory acceptance check.
Profiling found a second full-store scan in the prepared `ccusage` executable:
Python's peak RSS was about 65 MiB, but file cache after `ccusage` was
957,116,416 bytes. The correction gives Codex `ccusage` a temporary `CODEX_HOME`
containing copies of only the owned, fully covered rollouts. Copies are needed
because this executable ignores symlinked logs. The temporary directory is
removed on success and failure; an empty aggregate-ID list skips the scan.

### Two-task replay and production results

The first run removed these tasks from production pending state and wrote valid
observations, with no transcript-source error notes:

| Task | Codex transcript IDs | Tool records |
| --- | --- | --- |
| `e5ae1474-fca8-46e2-af28-9fec15406e63` | `01a100e4-277d-70c2-8203-e40f173fdff1`, `01a100e4-9a5e-73b3-835b-ecc8ca6d4855` | 26 |
| `3d4c93ff-cf8a-4f66-9c53-14923abf5254` | `01a100e8-4ce4-7303-94cb-5089a1887285`, `01a100e8-b44d-75d1-b85c-36ad959625ee` | 27 |

After the correction, replayed that exact pending batch with fresh state and an
empty Git vault under `/tmp/kandev-issue17-replay`, using the production database
and transcript files. Production state and existing observations were not reset
or overwritten. The complete sweep ran through `systemd-run --user --wait --pipe`
with `MemoryMax=1G` and `MemorySwapMax=0`:

```bash
systemd-run --user --unit=kandev-learn-issue17-replay --wait --pipe \
  --working-directory=/home/agent/prg/kandev-pacer \
  --property=MemoryMax=1G --property=MemorySwapMax=0 \
  /run/current-system/sw/bin/uv run --frozen kandev-learn sweep \
  --state /tmp/kandev-issue17-replay/state \
  --vault /tmp/kandev-issue17-replay/vault
```

Result was `success`, exit status 0, reported peak **201.6 MiB**, runtime 4.648 s,
and swap 0 bytes. Both tasks left the replay pending list. Their fresh JSON
records match the production transcript IDs, tools, initial prompts, final
responses and usage totals exactly; session notes are empty.

The subsequent normal production service run, 16:08:07 to 16:08:23 EEST, also
returned `Result=success`, `ExecMainStatus=0`, with
**MemoryPeak=575229952 bytes (548.58 MiB / 0.5357 GiB)**. Its invocation ID was
`3bb8a5cc3e44495c8e1c85a216436ad1`. Limits remained 1,073,741,824 bytes of memory
and zero swap; production pending state was empty afterwards.

The timer was restarted with its existing 15-minute cadence. Its next natural
trigger was **16:23:39 EEST**, matching the new service invocation
`034ed083dee1432b91214c60323e68be`. That run finished at 16:23:54 with
`Result=success`, `ExecMainStatus=0`, and **MemoryPeak=502120448 bytes**,
about 479 MiB. Pending state was empty. The timer remained active with its next
run scheduled for 16:38:39 EEST. No timer configuration or Nix changes were made.

### Automated checks

The focused source/collector suite passed 111 tests. The full offline suite passed
582 tests with 20 live checks skipped. `uv run --frozen darker --check`,
`uv run --frozen graylint`, and `git diff --check` passed. Regressions cover an
iterator that fails if read beyond mismatched metadata, mtime rejection before
opening, window boundaries and coverage for all three agents, malformed records,
first-metadata identity, transcript-ID filtering, and isolated usage-scan cleanup.

Memory still scales with the selected session window and the largest individual
JSON line. Codex usage staging also requires temporary disk space for eligible
rollouts. The measured runs cover these two pending tasks and the subsequent
production batch, not every possible transcript size.
