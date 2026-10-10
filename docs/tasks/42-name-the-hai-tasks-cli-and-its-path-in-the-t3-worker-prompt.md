# Name the `hai-tasks` CLI and its path in the T3 worker prompt

## Why

The T3 prompt builder in `src/kandev_pacer/t3.py` tells workers: "Use hai_tasks
for tracker updates with revision checks." Workers read `hai_tasks` as a tool
name. No such tool exists. `hai_tasks` is Hai's Python package, `hai-tasks` is
its CLI, and neither is on the workers' `PATH`.

On 2026-10-06 a palindromi.fi worker (T3 thread
`3ccc4d7e-ed76-454f-8464-238ecb69be71`) found no tool, edited `TASKS.md` by hand
and skipped Hai's revision checks. Workers in at least five repositories have
since saved "no hai_tasks tool, edit by hand" to their agent memory.

## Change

- Resolve the CLI once when T3 mode starts: `shutil.which("hai-tasks")`, else
  the `hai-tasks` script next to `sys.executable`. The `t3` extra installs that
  script, and the pacer runs through `uv run --extra t3`, so the fallback is
  `.venv/bin/hai-tasks`. If neither exists, refuse to start T3 mode with a
  message naming both places it looked. T3 mode cannot work without `hai_tasks`
  anyway.
- Replace the sentence with one that names the resolved command and says what
  it is, for example: "`hai_tasks` is Hai's Python package, not a tool. Update
  the tracker with `<path> read <repo>`, then `<path> update <repo> <id>
  --revision <rev> --json <patch>`. Load the `hai-tasks` skill if it is listed."
- Describe the command lookup in `docs/t3-pacing.md`.

This task does not depend on the related Hai and nixos-config tasks. Once
nixos-config puts `hai-tasks` on `PATH`
(`~/repos/nixos-config/docs/backlog/deploy-hai-tasks-skill.md`), the `which`
lookup finds it first.

## Verification

- Unit tests: the prompt contains the resolved path; the fallback is used when
  `PATH` has no `hai-tasks`; startup fails with the message when neither exists.
- Run the full suite with `HOME` pointing at an empty directory. Otherwise
  `~/.config/kandev-pacer/config.toml` leaks into the tests and about 43 fail.
