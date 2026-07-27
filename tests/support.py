"""Payload fixtures shaped like the kandev API responses.

Builders, not pytest fixtures: a test usually wants several of these with different
fields, which reads better as a call than as a fixture per shape. The fake kandev the
client/runtime tests run against lives in conftest.py.
"""


def step(id, name, otc=None, oe=None, ots=None, start=False, stage="custom",
         pos=0, profile="", prompt="", signal=False):
    events = {}
    if oe:
        events["on_enter"] = [{"type": k} for k in oe]
    if otc:
        events["on_turn_complete"] = [{"type": k} for k in otc]
    if ots:
        events["on_turn_start"] = [{"type": k} for k in ots]
    return {"id": id, "name": name, "events": events, "is_start_step": start,
            "stage_type": stage, "position": pos, "agent_profile_id": profile,
            "prompt": prompt, "auto_advance_requires_signal": signal}


def sess(id, state, started="1"):
    # `started_at` is the real field name in GET /tasks/:id/sessions -- there is no `created_at`.
    return {"id": id, "state": state, "started_at": started}


def task(id, step="ip", pos=0, prio="medium", created="2026-01-01T00:00:00Z", parent=None):
    t = {"id": id, "workflow_step_id": step, "position": pos,
         "priority": prio, "created_at": created}
    if parent:
        t["parent_id"] = parent
    return t
