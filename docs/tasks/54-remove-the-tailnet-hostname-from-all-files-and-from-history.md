# Remove the tailnet hostname from all files and from history

Antti asked for this on 2026-10-17, after filemill [96] did the same there.
kandev-pacer is public on GitHub, and gogo's tailnet hostname (the `*.<tailnet-suffix>` name
that `HAI_ALLOWED_HOSTS` in `~/.config/hai/hai.env` holds) appears in
`tests/userscript.test.mjs` (lines 85 and 100) and in about 155 commits. Never
write the name itself into a file, commit message, task doc or closing message;
call it `<tailnet-host>`. Find it with `git grep -n 'ts\.net'` and
`git log --all --oneline -S'<tailnet-suffix>'`.

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
5. Verify: `git log --all -S'<tailnet-suffix>'` and `git grep 'ts\.net' $(git rev-list --all)`
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
