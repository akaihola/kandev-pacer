#!/usr/bin/env -S uv run
"""Compatibility entry point for the packaged CLI."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from kandev_pacer import runtime

if __name__ == "__main__":
    raise SystemExit(runtime.main() or 0)

# Existing local integrations import `pacer` and patch its runtime state.
sys.modules[__name__] = runtime
