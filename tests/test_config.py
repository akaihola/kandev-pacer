import pytest

from kandev_pacer.config import load_settings


def secret_file(tmp_path, text, mode=0o600):
    path = tmp_path / "secrets.env"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_secrets_file_defaults_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("PACER_SECRETS_FILE", raising=False)
    assert load_settings(tmp_path / "missing.toml").secrets_file == ""


def test_secrets_file_is_filtered_and_environment_wins(monkeypatch, tmp_path):
    path = secret_file(
        tmp_path,
        "PACER_INTERVAL=7\nPACER_HEARTBEAT_WEBHOOK_SECRET=file-secret\nNOT_CONFIG=leak\n",
    )
    monkeypatch.setenv("PACER_INTERVAL", "9")
    settings = load_settings(tmp_path / "missing.toml", {"PACER_SECRETS_FILE": str(path)})
    assert settings.interval == 9
    assert settings.heartbeat_webhook_secret == "file-secret"
    assert not hasattr(settings, "NOT_CONFIG")


def test_command_line_overrides_secrets_file(tmp_path):
    path = secret_file(tmp_path, "PACER_INTERVAL=7\n")
    settings = load_settings(
        tmp_path / "missing.toml",
        {"PACER_SECRETS_FILE": str(path), "PACER_INTERVAL": 11},
    )
    assert settings.interval == 11


def test_secrets_file_rejects_insecure_permissions(tmp_path):
    path = secret_file(tmp_path, "PACER_INTERVAL=7\n", 0o640)
    with pytest.raises(ValueError, match="group- or world-readable"):
        load_settings(tmp_path / "missing.toml", {"PACER_SECRETS_FILE": str(path)})


def test_secrets_file_rejects_malformed_lines(tmp_path):
    path = secret_file(tmp_path, "not-an-assignment\n")
    with pytest.raises(ValueError, match="line 1"):
        load_settings(tmp_path / "missing.toml", {"PACER_SECRETS_FILE": str(path)})


def test_secrets_file_contents_are_not_logged(tmp_path, caplog):
    secret = "never-log-this-value"
    path = secret_file(tmp_path, f"PACER_HEARTBEAT_WEBHOOK_SECRET={secret}\n")
    load_settings(tmp_path / "missing.toml", {"PACER_SECRETS_FILE": str(path)})
    assert secret not in caplog.text
