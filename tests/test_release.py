"""Regression checks for the non-destructive test and consolidation commands."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_protects_runtime_directories(tmp_path):
    runner = load_script(ROOT / "tests" / "run_tests.py", "isolated_runner")
    runner.ROOT = tmp_path / "repo"
    for path in [runner.ROOT, runner.ROOT / "config",
                 runner.ROOT / "database" / "nested", runner.ROOT / "adapters"]:
        with pytest.raises(ValueError):
            runner.validate_temp_parent(str(path))


def test_runner_keeps_existing_files_and_passes_isolated_environment(tmp_path, monkeypatch):
    runner = load_script(ROOT / "tests" / "run_tests.py", "isolated_runner_environment")
    parent = tmp_path / "scratch"
    parent.mkdir()
    marker = parent / "keep.txt"
    marker.write_text("existing data", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        env = kwargs["env"]
        runtime = Path(env["AIAM_DATA_DIR"])
        assert parent in runtime.parents
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
        assert command[command.index("--basetemp") + 1].endswith("pytest")
        assert "-B" in command
        assert "no:cacheprovider" in command
        assert Path(env["TMP"]).exists()
        return types.SimpleNamespace(returncode=7)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.main(["--temp-dir", str(parent)]) == 7
    assert len(calls) == 1
    assert marker.read_text(encoding="utf-8") == "existing data"
    assert list(parent.iterdir()) == [marker]


def test_consolidation_command_reports_no_weight_training(monkeypatch, capsys):
    command = load_script(ROOT / "training" / "sleep_tune.py", "consolidation_cli")
    import core.config
    import database.db_manager

    monkeypatch.setattr(core.config, "load_config", lambda **kwargs: {"db_path": "unused"})
    monkeypatch.setattr(database.db_manager, "MindStore", lambda path: types.SimpleNamespace(
        consolidate=lambda: {"deduplicated": 2}
    ))
    assert command.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["operation"] == "memory_consolidation"
    assert output["status"] == "completed"
    assert output["weights_trained"] is False
    assert output["adapters_activated"] is False
    assert output["result"]["deduplicated"] == 2


def test_consolidation_failure_has_nonzero_exit(monkeypatch, capsys):
    command = load_script(ROOT / "training" / "sleep_tune.py", "consolidation_cli_failure")
    import core.config

    def bad_config(**kwargs):
        raise ValueError("invalid configuration")

    monkeypatch.setattr(core.config, "load_config", bad_config)
    assert command.main([]) == 1
    output = json.loads(capsys.readouterr().err)
    assert output["status"] == "failed"
    assert output["weights_trained"] is False
    assert output["adapters_activated"] is False
