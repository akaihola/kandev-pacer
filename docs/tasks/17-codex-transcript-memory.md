---
depends_on: []
---

# Stop `kandev-learn sweep` from loading every Codex transcript into memory

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
