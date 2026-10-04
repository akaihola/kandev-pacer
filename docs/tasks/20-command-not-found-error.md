```
❯ cd ~/prg/kandev-pacer && set -a && source ~/.config/secrets/kandev-pacer.env && set +a && \
uv run \
  --extra t3 \
  --frozen kandev-pacer \
  --config ~/.config/kandev-pacer/config-t3.toml \
  --agent-providers-raw 'codex-acp=__api_key__ hermes-acp=__api_key__' \
  --apikey-policy cap \
  --dump-order
 : command not found
[
  {
    "tracker": "/home/agent/prg/kandev-pacer/TASKS.md",
    "issue": {
      "id": "simple-1ea5eea8-5a65-4a87-a84f-3f2ab01533b4",
      "sourceReference": "[19]",
      "identifier": "HAI-19",
      "title": "UV source path documentation unclear",
      "description": "In `docs/t3-pacing.md`:\n\n    The UV source points to `../hai`. Change that source path if Hai lives elsewhere.\n\nThis needs more explanation.",
      "status": "backlog",
      "priority": "none",
      "labels": [],
      "assigneeId": null,
      "projectId": null,
      "cycleId": null,
      "createdAt": "2026-10-04T18:44:41.879Z",
      "updatedAt": "2026-10-04T18:44:41.879Z",
      "sourceSection": "Ordered backlog",
      "accepted": false,
      "dependencies": [],
      "sourcePath": "docs/tasks/19-uv-source-path-documentation-unclear.md"
    }
  }
]
```

## Resolution, 2026-10-04

The secrets file contained a valid assignment, had mode `0600`, and had no CRLF
line endings, BOM, or shell metacharacters in the value. Sourcing it and running
`uv --version` worked in both Bash and Zsh. The full pacer `--dump-order` command
also completed without a shell error in clean and interactive shells.

The saved failing Zsh history showed an extra escaped space after `set +a &&`.
A minimal Zsh reproduction with a space after the continuation backslash emitted
exactly ` : command not found`, then successfully ran the next line. The backslash
escaped the space rather than the newline, so the shell tried to execute a command
named one space. This explains why the message appeared before successful pacer
output. The transcript in the original report does not show that trailing space.

Use the existing `--secrets-file` loader with the single-line command in
[shell troubleshooting](../t3-pacing.md#shell-command-not-found). That command was
verified against the user's current configuration and private secrets file; it
returned the eligible issues without launching work. Secret values were not printed.
`T3_THREAD_AUTH_TOKEN` remains a process-environment setting.

No application code change is needed for this shell input error.
