"""Run isolated tests without deleting an installation's state or memories."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def validate_temp_parent(value: str | None) -> Path | None:
    if value is None:
        return None
    parent = Path(value).expanduser().resolve()
    # Pytest must never receive one of these locations as its cleanup target.
    # All real cleanup is additionally restricted to our newly-created child.
    protected = [ROOT / "config", ROOT / "database", ROOT / "adapters"]
    if parent == ROOT or any(parent == p or p in parent.parents for p in protected):
        raise ValueError("Choose a temporary parent outside repository config, database, and adapters.")
    if parent.exists() and not parent.is_dir():
        raise ValueError("The temporary parent must be a directory.")
    parent.mkdir(parents=True, exist_ok=True)
    return parent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--temp-dir",
        help="Parent for a new disposable test directory; existing contents are preserved.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    try:
        parent = validate_temp_parent(args.temp_dir)
        with tempfile.TemporaryDirectory(prefix="ai-am-tests-", dir=parent) as temporary:
            test_root = Path(temporary).resolve()
            env = os.environ.copy()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
            env["TMP"] = str(test_root)
            env["TEMP"] = str(test_root)
            env["TMPDIR"] = str(test_root)
            env["AIAM_DATA_DIR"] = str(test_root / "runtime")
            env["HF_HOME"] = str(test_root / "cache" / "huggingface")
            env["PIP_CACHE_DIR"] = str(test_root / "cache" / "pip")
            command = [
                sys.executable, "-B", "-m", "pytest",
                "-p", "pytest_asyncio.plugin",
                "-p", "no:cacheprovider",
                "--basetemp", str(test_root / "pytest"),
                str(ROOT / "tests"),
            ]
            if args.verbose:
                command.append("-v")
            return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode
    except (OSError, ValueError) as exc:
        print(f"Unable to run isolated tests: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
