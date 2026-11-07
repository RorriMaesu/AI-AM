import json
from pathlib import Path
import pytest
import core.config as config

@pytest.fixture
def isolated_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("AIAM_DATA_DIR", raising=False)
    return tmp_path

def write_settings(root, value):
    path = root / "settings.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path

def test_cold_start_has_complete_settings_without_creating_files(isolated_root):
    result = config.load_config()
    assert result["backend"] == "ollama"
    assert result["max_rounds"] == 2
    assert result["max_output_tokens"] == 1600
    assert result["request_timeout"] == 180
    assert result["cycle_timeout"] == 600
    assert set(result["roles"]) == {"manas", "chitta", "ahamkara", "buddhi"}
    assert result["background_enabled"] is False
    assert result["self_model"]["name"] == "AI-AM"
    assert result["db_path"] == str(isolated_root / "data" / "chitta.db")
    assert list(isolated_root.iterdir()) == []

def test_legacy_settings_keep_local_model_but_not_fictional_human_identity(isolated_root):
    path = write_settings(isolated_root, {
        "llm_parameters": {
            "model_name": "qwen3:8b",
            "api_url": "http://localhost:11434/v1/chat/completions",
        },
        "ahamkara_mask": {"core_identity": "A singular human awareness"},
    })
    original = path.read_bytes()
    result = config.load_config(path)
    assert all(role["model"] == "qwen3:8b" for role in result["roles"].values())
    assert result["api_url"] == "http://localhost:11434/api/chat"
    assert "human" not in result["self_model"]["description"]
    assert path.read_bytes() == original

def test_data_directory_precedence_and_demo_isolation(isolated_root, monkeypatch):
    monkeypatch.setenv("AIAM_DATA_DIR", str(isolated_root / "env-data"))
    settings = write_settings(isolated_root, {"schema_version": 2, "data_dir": "config-data"})
    result = config.load_config(settings, demo=True)
    assert result["db_path"] == str(isolated_root / "env-data" / "demo-chitta.db")
    result = config.load_config(settings, data_dir=isolated_root / "explicit")
    assert result["data_dir"] == str(isolated_root / "explicit")
    assert not (isolated_root / "explicit").exists()

def test_v2_role_models_and_identity(isolated_root):
    path = write_settings(isolated_root, {
        "schema_version": 2,
        "roles": {"buddhi": {"model": "qwen3:14b", "temperature": 0.1}},
        "self_model": {"name": "Mira", "commitments": ["Keep sources attached."]},
    })
    result = config.load_config(path)
    assert result["roles"]["buddhi"]["model"] == "qwen3:14b"
    assert result["roles"]["manas"]["model"] == config.DEFAULT_MODEL
    assert result["self_model"]["name"] == "Mira"
    assert result["self_model"]["commitments"] == ["Keep sources attached."]

@pytest.mark.parametrize("url", [
    "https://api.example.com/api/chat",
    "http://192.168.1.20:11434/api/chat",
    "http://localhost.evil.example:11434/api/chat",
    "http://user:secret@localhost:11434/api/chat",
    "http://localhost:11434/api/chat?target=remote",
    "http://localhost:11434/api/chat#fragment",
    "file:///api/chat",
    "http://localhost:11434/not-ollama",
])
def test_remote_or_ambiguous_endpoints_are_rejected(url):
    with pytest.raises(config.ConfigurationError):
        config.validate_api_url(url)

@pytest.mark.parametrize("name", ["gemma4:cloud", "gpt-oss:120b-cloud", "cloud/name/cloud", "a b", "", "a" * 201])
def test_remote_or_invalid_model_names_are_rejected(name):
    with pytest.raises(config.ConfigurationError):
        config.validate_model_name(name)

def test_ipv6_loopback_is_supported():
    assert config.validate_api_url("http://[::1]:11434/v1/chat/completions") == "http://[::1]:11434/api/chat"

@pytest.mark.parametrize("value", [
    {"max_rounds": 0}, {"max_rounds": True}, {"num_ctx": "8192"},
    {"background_enabled": "false"}, {"schema_version": 3},
    {"roles": {"witness": {"model": "x"}}},
    {"roles": {"manas": {"temperature": float("nan")}}},
])
def test_invalid_settings_fail_clearly(isolated_root, value):
    path = write_settings(isolated_root, value)
    with pytest.raises(config.ConfigurationError):
        config.load_config(path)

def test_corruption_never_overwrites_config(isolated_root):
    path = isolated_root / "bad.json"
    path.write_text("{unfinished", encoding="utf-8")
    with pytest.raises(config.ConfigurationError, match="Cannot read configuration"):
        config.load_config(path)
    assert path.read_text(encoding="utf-8") == "{unfinished"

def test_missing_explicit_file_is_an_error(isolated_root):
    with pytest.raises(config.ConfigurationError, match="does not exist"):
        config.load_config(isolated_root / "missing.json")

def test_explicit_generation_budgets_override_live_defaults(isolated_root):
    path = write_settings(isolated_root, {
        "schema_version": 2, "max_output_tokens": 2048,
        "request_timeout": 240, "cycle_timeout": 720,
    })
    result = config.load_config(path)
    assert result["max_output_tokens"] == 2048
    assert result["request_timeout"] == 240
    assert result["cycle_timeout"] == 720
