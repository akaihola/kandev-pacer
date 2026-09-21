"""Check configuration isolation in a fresh pytest process, before project imports."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("lowercase", [False, True])
def test_collection_isolates_offline_settings_and_preserves_live(
    tmp_path, live, lowercase
):
    config = tmp_path / ".config" / "kandev-pacer" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[kandev-pacer]\ninterval = 77\nquota_source = "pebble"\n')
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("PACER_", "KANDEV_"))
    }
    environment.update(
        KANDEV_URL="http://operator.invalid:1234",
        PACER_QUOTA_SOURCE="pebble",
        PACER_HEARTBEAT_WEBHOOK_URL="http://operator.invalid/hook",
        PACER_HEARTBEAT_WEBHOOK_SECRET="test-secret" if live else "",
    )
    if lowercase:
        environment = {
            key.lower() if key.startswith(("PACER_", "KANDEV_")) else key: value
            for key, value in environment.items()
        }
    if live:
        environment["PACER_LIVE_KANDEV"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

live = bool(os.environ.get("PACER_LIVE_KANDEV"))
original_environment = dict(os.environ)
original_is_file = Path.is_file

class CheckSettings:
    def pytest_collection_finish(self, session):
        from kandev_pacer.config import settings
        assert session.items
        assert bool(os.environ.get("PACER_LIVE_KANDEV")) == live
        assert settings.interval == (77 if live else 30)
        assert settings.quota_source == ("pebble" if live else "direct")
        assert settings.url == (
            "http://operator.invalid:1234" if live else "http://127.0.0.1:38429"
        )
        assert bool(settings.heartbeat_webhook_url) == live

with patch.object(Path, "home", return_value=Path(sys.argv[1])):
    status = pytest.main(
        ["--collect-only", "-q", "tests/test_contract_live.py"],
        plugins=[CheckSettings()],
    )
assert dict(os.environ) == original_environment
assert Path.is_file is original_is_file
raise SystemExit(status)
""",
            str(tmp_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
