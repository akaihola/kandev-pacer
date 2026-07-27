"""Shared plumbing for the test modules: import path, runner, payload fixtures.

Each test module mirrors one application module (`test_quota.py` -> `kandev_pacer/quota.py`,
and so on) and stays runnable on its own, so the pieces every module needs live here rather
than being copied four times.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# `kandev_pacer` from src/, `make_paced` from the repo root. Tests run as standalone
# scripts (uv run tests/test_quota.py), so neither is on the path by default.
for entry in (str(ROOT), str(ROOT / "src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)


def run_tests(namespace):
    """Run every test_* callable in `namespace`, in name order. Returns the count."""
    tests = [(n, f) for n, f in sorted(namespace.items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"ok {name}")
    print(f"\n{len(tests)} passed")
    return len(tests)


# --- payload fixtures, shaped like the kandev API responses ---

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
