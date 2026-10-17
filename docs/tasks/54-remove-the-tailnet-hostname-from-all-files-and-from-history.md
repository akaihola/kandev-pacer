# Remove the tailnet hostname from all files and from history

Antti asked for this on 2026-10-17, after filemill [96] did the same there.
kandev-pacer is public on GitHub, and gogo's tailnet hostname (the `<tailnet-host>` name
that `HAI_ALLOWED_HOSTS` in `~/.config/hai/hai.env` holds) appears in
`tests/userscript.test.mjs` (lines 85 and 100) and in about 155 commits. Never
write the name itself into a file, commit message, task doc or closing message;
call it `<tailnet-host>`. Find it with Git grep and Git log pickaxe searches for the tailnet domain suffix.

## Keep the userscript easy to update

`userscript/kandev-queue-button.user.js` contains no hostname: `@include` matches
Kandev by port. Keep it that way. Antti updates it by replacing the installed script
in his browser's userscript manager with this file
(`docs/queueing-user-prompts.md`), and that must keep working unchanged. Don't add
`@updateURL` or `@downloadURL`, and don't put a host in the script or its docs. In
the test, use a neutral host such as `kandev.example.test`.

## Work

1. Check `git worktree list` and the T3 threads working in this repository. If a
   worktree's branch has commits that are not on `main`, name it and keep it
   working.
2. Replace the name in current files and commit on `main`. Run
   `node --test tests/userscript.test.mjs` and `uv run pytest -q`.
3. Back up: `git bundle create <backup dir>/kandev-pacer-before-tailnet-rewrite-YYYYMMDD.bundle --all`.
4. Rewrite every ref with `git filter-repo --replace-text` in a fresh clone, keeping
   the mapping file outside the repository (it contains the name). Then make this
   checkout match it, including the worktrees' branches. The pacer runs from this
   checkout: keep `.venv/` and untracked files, and don't restart the pacer.
5. Verify: Git log pickaxe and Git grep across every reachable commit for the tailnet domain suffix
   find nothing. Also check the remote-tracking refs: GitHub has only `main` and
   `feature/parallelize-agents-w-1pn`.
6. Record in this description file the old→new hashes of every commit that a
   `docs/tasks/` file or `TASKS.md` cites.

Do not push, and do not touch atom or GitHub.

## For Antti (after this lands)

- Force-push only the branches GitHub has:
  `git -C ~/prg/kandev-pacer push --force origin main refs/remotes/origin/feature/parallelize-agents-w-1pn:refs/heads/feature/parallelize-agents-w-1pn`,
  plus tags if any exist (`git tag` is empty today). Not `--all`: that would publish
  every local `pacer-*` branch.
- On atom: `git fetch`, then `git reset --hard origin/main`; rebase or recreate
  local branches.
- GitHub keeps old commits reachable through pull request refs and caches; only
  GitHub Support can purge those.

Acceptance: steps 1–6 done, the userscript unchanged except where it must be, nothing
pushed, and the closing message lists what Antti has to do.

## Implementation evidence, 2026-10-17

- Checked all 13 worktrees. Only `feature/t3code-feasibility` had commits
  absent from `main`, 18 before and after. Kept all branches and checkouts.
- T3's inspect helper could not connect. Read the local T3 SQLite projections
  without modifying them. This issue's thread was the only running thread;
  no other thread had pending approval or input. Sent no messages.
- Replaced the two test fixture URLs with `kandev.example.test`. The userscript
  and `docs/queueing-user-prompts.md` are byte-for-byte unchanged.
- Committed the current-file cleanup on `main` before rewriting history.
- Created and verified the all-refs bundle at
  `/home/agent/.local/state/kandev-pacer-tailnet-rewrite-20261017/kandev-pacer-before-tailnet-rewrite-20261017.bundle`.
  This private backup intentionally retains the old history. Do not publish it.
- Used a fresh `--mirror --no-local` clone and `git filter-repo --replace-text`
  plus `--replace-message`. Its `--force` override was needed because the
  mirror includes the existing stash. The private replacement file lives
  outside the repository beside the backup. Also redacted the suffix in
  historical task instructions so the requested suffix searches return nothing.
- Installed rewritten objects and mapped all 220 original refs, including
  remote-tracking refs, recovery/checkpoint refs and the stash. Updated all
  worktree indexes and tracked files. Kept `.venv/` and untracked files;
  checked their identity/content against the saved pre-rewrite inventory.
- Mapped recovery pointers, expired original reflogs and pruned unreachable
  original objects. Original objects remain recoverable from the private bundle.
- Verified Git log pickaxe and Git grep over all 757 reachable commits return
  no suffix matches. Local origin tracking refs retain only `main`,
  `feature/parallelize-agents-w-1pn`, and symbolic `HEAD`. There are no tags.
  Did not contact GitHub or atom, push anything, or restart the pacer.
- `node --test tests/userscript.test.mjs`: 64 passed.
- `uv run pytest -q`: 941 passed, 20 skipped, 1 failed. The failure is
  `tests/test_t3.py::test_startup_and_dump_order_skip_unreadable_tracker[False]`.
  It expects one `project2` launch but records none. It repeats in isolation
  and in a separate export of the pre-rewrite Python code, using the same
  environment. Python source and Python tests are unchanged by this issue.
  Left this pre-existing failure outside this issue's scope.

Reproduce the suffix checks without embedding the suffix in this file:

```bash
suffix='ts.'net
git log --all -S"$suffix"
git grep -F "$suffix" $(git rev-list --all)
```

## Cited commit hash migration

Collected commit citations from every historical `TASKS.md` and `docs/tasks/`
Markdown blob reachable before the rewrite, including branch-only history.
All 31 cited commits changed. Original citations remain historical evidence;
use this table to find their rewritten commits.

| Old commit | New commit |
| --- | --- |
| `036b2bf1b63e9314e98da056418c97341b7883dc` | `3daa05434f926434208e3c2292b777f8ae2a0871` |
| `05986f710db096eb9254563c09bff163949ec955` | `8deba372f5f2521769dfd7f371df97f5f7153930` |
| `05a25b36c84ced091aa18ebe0809a7e36086e5b4` | `032dfb4a2fd7db8b068200e9be6f9e41353a636b` |
| `06ddd866349029da69288506c828264ce9a2d072` | `735ebb40836bdf2ff8b3df1cf7de2ab0b73659e3` |
| `0e34119ef37c7fb0e9d8fea0a5e34e986a6f0ae9` | `020388a2b57cfaad777c0f80a5917e541b06d943` |
| `1a444a50e021c8606bce54a722a805182885c0db` | `f75c66f3792711825f8edc1f05083701234211f5` |
| `1f86084620a8a2bcb812f7d7e9f223b894d7014f` | `419da7f225a488e4cfb1000543058c5d586a2cf7` |
| `20a964dff1547500c5e7957796abb37eb5c7413c` | `37031011b9df88356f79899258de2a781f8ec9af` |
| `295e76792d6187331f6f070c78c77d76c7f29e43` | `18fa5e1b1c617883eebbda26df4ccb6a8010e87c` |
| `2fb33a154b4a81c514608008c3e10c13d14c63d2` | `b04b7930629911377baa5671358ee47bb25b2a12` |
| `40e6cf1e9b9a66d32bdc3aee905197c0778ab56a` | `153813aa664ea055c573ade008d7ccd6ed540b74` |
| `4f3c859f089504faf606a7e0098cd0a8776c87d7` | `075714a0465e9e34eafd694569afaf0431682739` |
| `77b998dc5f3119d693200d102c09d503f01bcd36` | `767927d74f33fe28ccc77ca87acbe8549e35a828` |
| `7c2b46ebeb744b522e47531e2bbbfe16e75dccda` | `56e990814e75ecbd089afffdb9a34c50cd9cd7cc` |
| `7dd86c8866301dbe61abe7600e94a953d62428e6` | `1f63f557dc16e60981e3d4d837504db27a332419` |
| `876a7d6878db94027fe36538ea74d067a2142474` | `1c6950b1acac20abc8030e2aac29a7fb001acec3` |
| `896001aec4872f4b4984fa08241a605309152f03` | `b4c71f9fc992af729da9da1a59d900cbe608ccc7` |
| `90278cf3b4930bc0be24612e958b12c9c27a3495` | `f9562aa1433296c3681025fe5e7563e8d5fd1643` |
| `990d23d0a4ac9ee565c87386c9d5b164272bf61f` | `1b14f7b9f2d0db94827696f308ef5e395bb21266` |
| `9aa14cd58bb4fec30db5826a41bf1f3ae2705370` | `c1f3fbedb9ca90505965a6fe10a6ff89303e744f` |
| `9add66f8993a8ae79f0745042abd81bd2f602902` | `eed14f62106692087dea7998888de29d6e116974` |
| `9ae5864719e0f880e73d273b1caf8f788a48ae2f` | `ac9fa66798ca3916945dd0ec19fa20dc85846218` |
| `9d327c05bfcde70419e85ff561f9a9d33d539391` | `d87d8b19a7a86b163a064a760f3dc2cbced9d3db` |
| `9ff6161392518ae7cc5f4a6c14846d84bc4a712f` | `ef466ebc3b994e555490689d81695106dd1060a9` |
| `a550b8898fb445f26b41750ec84fa665898e1832` | `4df4e73ab7240369bc0eacae9a60570df3467df4` |
| `a81b24c2bd2921be4e18356b8dede27a743cd457` | `2a071e9d580f9010978cf51c4f1cc390e0d50bc3` |
| `b3450492e552b82235ab76d0126f78e9583a9e2c` | `ab5d78a7596f3011edfada9b2bc3be14f067e3ed` |
| `d67176b46ae7fd30a4e192fa3657aa92887f9f64` | `ebc2f7f01d259c2c1d7060f291480d033831769c` |
| `e11ecf5ec8bdae5686e87cfce95548aa6f13a639` | `5dd5f5a8ad19e707d64da4a80a3a50241455c56e` |
| `e21604d4e976b3906a868bef48cd70ebf6b748aa` | `97bc6462f1dcce3d34da80dd209871098647932c` |
| `e79b061ab8aeefc80e001b4960baf311cf40d368` | `fba17dba0835e1e103d1a5a62905030b074a92a3` |
