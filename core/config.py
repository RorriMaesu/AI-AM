"""Validated local runtime settings; loading never mutates user state."""
import copy
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from core.contracts import ROLES

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gemma4:e4b"
DEFAULT_SELF_MODEL = {
    "name": "AI-AM",
    "description": "A local agent with four cooperating cognitive functions.",
    "capabilities": ["conversation", "durable memory", "arithmetic"],
    "commitments": [],
}

class ConfigurationError(ValueError):
    pass

def validate_model_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value):
        raise ConfigurationError("Model must be a local Ollama model tag of at most 200 characters.")
    lower = value.lower()
    if ":cloud" in lower or lower.endswith("-cloud") or lower.endswith("/cloud"):
        raise ConfigurationError("Cloud model tags are disabled. Select an installed local model.")
    return value

def validate_api_url(value):
    if not isinstance(value, str):
        raise ConfigurationError("api_url must be a local HTTP URL.")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ConfigurationError("api_url is malformed.") from exc
    if (parts.scheme != "http" or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ConfigurationError("Inference must use a loopback HTTP endpoint without credentials, query, or fragment.")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigurationError("api_url port is out of range.")
    path = parts.path.rstrip("/")
    if path in {"", "/v1", "/v1/chat/completions", "/api/chat"}:
        path = "/api/chat"
    else:
        raise ConfigurationError("Use Ollama /api/chat or the legacy /v1/chat/completions endpoint.")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

def _number(raw, name, default, low, high, integer=False):
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number.")
    if integer and not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an integer.")
    if not low <= value <= high:
        raise ConfigurationError(f"{name} must be between {low} and {high}.")
    return value

def _bool(raw, name, default):
    value = raw.get(name, default)
    if type(value) is not bool:
        raise ConfigurationError(f"{name} must be true or false.")
    return value

def _self_model(value):
    if not isinstance(value, dict):
        raise ConfigurationError("self_model must be an object.")
    result = copy.deepcopy(DEFAULT_SELF_MODEL)
    for key, limit in (("name", 80), ("description", 2000)):
        if key in value:
            if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > limit:
                raise ConfigurationError(f"self_model.{key} must be nonempty text up to {limit} characters.")
            result[key] = value[key].strip()
    for key in ("capabilities", "commitments"):
        if key in value:
            items = value[key]
            if (not isinstance(items, list) or len(items) > 24
                    or any(not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in items)):
                raise ConfigurationError(f"self_model.{key} must be a bounded list of nonempty text.")
            result[key] = [item.strip() for item in items]
    return result

def load_config(path=None, data_dir=None, demo=False):
    """Read v2 settings or known legacy settings, without writing files or downloading models."""
    config_path = Path(path) if path is not None else ROOT / "config" / "engine_config.json"
    raw = {}
    if config_path.exists():
        try:
            if config_path.stat().st_size > 1024 * 1024:
                raise ConfigurationError("Configuration exceeds 1 MB.")
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Cannot read configuration at {config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("Configuration must contain a JSON object.")
    elif path is not None:
        raise ConfigurationError(f"Configuration file does not exist: {config_path}")

    version = raw.get("schema_version")
    if version is not None and (type(version) is not int or version != 2):
        raise ConfigurationError("Unsupported schema_version; expected 2.")
    legacy = raw.get("llm_parameters", {})
    if not isinstance(legacy, dict):
        raise ConfigurationError("llm_parameters must be an object.")
    model = validate_model_name(raw.get("model", legacy.get("model_name", DEFAULT_MODEL)))
    role_values = raw.get("roles", {})
    if not isinstance(role_values, dict) or any(role not in ROLES for role in role_values):
        raise ConfigurationError("roles must contain only manas, chitta, ahamkara, and buddhi.")
    roles = {}
    for role in ROLES:
        options = role_values.get(role, {})
        if not isinstance(options, dict) or any(key not in {"model", "temperature"} for key in options):
            raise ConfigurationError(f"roles.{role} must contain model and/or temperature.")
        roles[role] = {
            "model": validate_model_name(options.get("model", model)),
            "temperature": _number(options, "temperature", 0.35 if role == "manas" else 0.2, 0, 1),
        }
    backend = "demo" if demo else raw.get("backend", "ollama")
    if backend not in {"ollama", "demo"}:
        raise ConfigurationError("backend must be ollama or demo.")
    selected_dir = data_dir or os.environ.get("AIAM_DATA_DIR") or raw.get("data_dir") or ROOT / "data"
    if not isinstance(selected_dir, (str, os.PathLike)):
        raise ConfigurationError("data_dir must be a filesystem path.")
    selected_dir = Path(selected_dir).expanduser()
    if not selected_dir.is_absolute():
        selected_dir = ROOT / selected_dir
    selected_dir = selected_dir.resolve()
    config = {
        "schema_version": 2,
        "data_dir": str(selected_dir),
        "db_path": str(selected_dir / ("demo-chitta.db" if backend == "demo" else "chitta.db")),
        "legacy_db_path": str(ROOT / "database" / "chitta_store.db"),
        "api_url": validate_api_url(raw.get("api_url", legacy.get("api_url", "http://127.0.0.1:11434/api/chat"))),
        "roles": roles,
        "num_ctx": _number(raw, "num_ctx", 8192, 1024, 65536, integer=True),
        "max_output_tokens": _number(raw, "max_output_tokens", 1600, 64, 4096, integer=True),
        "request_timeout": _number(raw, "request_timeout", 180, 1, 300),
        "cycle_timeout": _number(raw, "cycle_timeout", 600, 10, 900),
        "max_rounds": _number(raw, "max_rounds", 2, 1, 4, integer=True),
        "max_pending": _number(raw, "max_pending", 100, 1, 1000, integer=True),
        "background_enabled": _bool(raw, "background_enabled", False),
        "background_interval": _number(raw, "background_interval", 60, 15, 3600),
        "backend": backend,
        # Old human-identity instructions are not imported into the new self-model.
        "self_model": _self_model(raw.get("self_model", {})) if version == 2 else copy.deepcopy(DEFAULT_SELF_MODEL),
    }
    return config
