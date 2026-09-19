---
depends_on: []
---

# Fix userscript Queue button session identifiers after the 0.94-dev upgrade

## Original request

After the 0.94-dev upgrade, I am getting `Queue failed: task_id, session_id, and
session_incarnation_id are required` when I try to use the `Queue` button from the
userscript in task ff3350a2-60a3-48b1-9631-796e46e23e28. Investigate and fix.

## Related task

[Task 6](6-failed-queue-status.md) covers the pacer reading queue status. This
request covers the userscript writing to the queue through its Queue button.
