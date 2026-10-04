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