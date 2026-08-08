"""The userscript mirrors kandev's composer serialization; the actual
assertions live in userscript.test.mjs (node:test), this just runs them."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_userscript() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("userscript.test.mjs"))],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
