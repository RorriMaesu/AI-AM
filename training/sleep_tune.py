"""Consolidate evidence-backed memory without generating or activating weights.

The historical filename is retained for existing launch commands. This command
performs a database operation; it is not a model-training implementation.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Consolidate AI-AM memories. No model weights are trained."
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args(argv)
    try:
        from core.config import load_config
        from database.db_manager import MindStore

        config = load_config(path=args.config, data_dir=args.data_dir, demo=args.demo)
        store = MindStore(config["db_path"])
        result = store.consolidate()
        print(json.dumps({
            "operation": "memory_consolidation",
            "status": "completed",
            "weights_trained": False,
            "adapters_activated": False,
            "result": result,
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({
            "operation": "memory_consolidation",
            "status": "failed",
            "weights_trained": False,
            "adapters_activated": False,
            "error": str(exc),
        }, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
