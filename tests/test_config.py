import pytest

from kandev_pacer.config import Settings, load_settings


def test_quota_source_defaults_to_direct(monkeypatch):
    monkeypatch.delenv("PACER_QUOTA_SOURCE", raising=False)
    assert Settings().quota_source == "direct"


@pytest.mark.parametrize("source", ["direct", "pebble"])
def test_quota_source_environment(monkeypatch, source):
    monkeypatch.setenv("PACER_QUOTA_SOURCE", source)
    assert Settings().quota_source == source


def test_quota_source_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("PACER_QUOTA_SOURCE", "unknown")
    with pytest.raises(ValueError, match="PACER_QUOTA_SOURCE"):
        Settings()


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


def test_t3_settings_from_toml(tmp_path):
    path = tmp_path / "pacer.toml"
    path.write_text("""
[kandev-pacer]
t3_runtime_mode = "auto-accept-edits"
t3_interaction_mode = "plan"
[kandev-pacer.t3_model]
instanceId = "claudeAgent"
model = "claude-fable"
[kandev-pacer.t3_model.options]
reasoningEffort = "high"
""")
    settings = load_settings(path)
    assert settings.t3_model.instanceId == "claudeAgent"
    assert settings.t3_model.options == {"reasoningEffort": "high"}
    assert settings.t3_runtime_mode == "auto-accept-edits"
    assert settings.t3_interaction_mode == "plan"


def test_t3_automatic_from_environment(monkeypatch):
    monkeypatch.setenv("PACER_T3_MODEL", "automatic")
    monkeypatch.setenv("PACER_T3_MODELS", '[{"instanceId":"codex","model":"gpt-test"}]')
    settings = Settings()
    assert settings.t3_model == "automatic"
    assert settings.t3_models[0].model == "gpt-test"


@pytest.mark.parametrize(
    "values",
    [
        {"PACER_T3_MODEL": "automatic"},
        {"PACER_T3_MODEL": {"instanceId": "unknown", "model": "test"}},
        {"PACER_T3_MODEL": {"instanceId": "codex", "model": " "}},
        {"PACER_T3_RUNTIME_MODE": "invalid"},
        {"PACER_T3_INTERACTION_MODE": "invalid"},
    ],
)
def test_invalid_t3_launch_settings(values):
    with pytest.raises(ValueError, match="PACER_T3"):
        Settings(**values)


def test_t3_model_json_command_line_override(tmp_path):
    settings = load_settings(
        tmp_path / "missing.toml",
        {
            "PACER_T3_MODEL": '{"instanceId":"codex","model":"gpt-test"}',
            "PACER_T3_MODELS": '[{"instanceId":"claudeAgent","model":"claude-test"}]',
        },
    )
    assert settings.t3_model.model == "gpt-test"
    assert settings.t3_models[0].instanceId == "claudeAgent"
