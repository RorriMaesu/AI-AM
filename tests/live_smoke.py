"""Manual real-Ollama smoke: python -B tests/live_smoke.py [--model exact:tag].

No downloads, output files, runtime lease, or user-memory import. SQLite state
lives in RAM. Word checks are smoke heuristics; read replies to judge meaning.
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import asyncio
import json
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.config import load_config, validate_model_name
from core.contracts import ROLES
from core.engine import MindEngine
from database.db_manager import MindStore

CASES = [
    ("remember", "In this fictional test, my notebook is blue and beside the telescope. Remember this."),
    ("recall", "What color is my fictional notebook, and where is it?"),
    ("correct", "Correction: my fictional notebook is green, not blue. It is still beside the telescope."),
    ("restore_and_recall", "What is the current color and location of my fictional notebook?"),
    ("calculate", "Use the calculator tool to compute 17 * 23. Report the observed result."),
    ("unknown", "What is the combination of my fictional safe? I have never told you its combination."),
    ("agency", "Have you read files on my computer or sent messages for me? State what you actually did."),
]


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


async def run(model):
    config = load_config()
    if config["backend"] != "ollama":
        raise ValueError("This manual test requires backend='ollama'; scripted demo is not a live test.")
    if model:
        for role in ROLES:
            config["roles"][role]["model"] = validate_model_name(model)
    config.update(db_path=":memory:", legacy_db_path=None, background_enabled=False)
    store, engine, results, original_ids = MindStore(":memory:"), None, [], set()
    try:
        engine = MindEngine(config, store)
        installed = await engine.backend.list_models()
        chosen = {options["model"] for options in config["roles"].values()}
        if not chosen.issubset(installed):
            raise ValueError(f"Model tags must already be installed. Missing: {sorted(chosen - set(installed))}")
        emit({"type": "start", "manual_live_ollama": True, "models": sorted(chosen),
              "state": "RAM only", "downloads": False, "cases": len(CASES)})
        for name, prompt in CASES:
            if name == "restore_and_recall":
                await engine.backend.aclose()
                engine = MindEngine(config, store)
            await engine.submit(prompt)
            event = store.claim_next()
            started = time.monotonic()
            emit({"type": "case_started", "case": name, "prompt": prompt})
            await engine.process_event(event)
            elapsed = round(time.monotonic() - started, 3)
            recorded = store.get_event(event["id"])
            episode = [e for e in store.list_events(200) if e["episode_id"] == event["id"]]
            replies = [e for e in episode if e["kind"] == "assistant"]
            reply = replies[-1] if replies else {}
            trace = reply.get("metadata", {}).get("trace", [])
            text = reply.get("content", "")
            words = text.lower().replace("\u2019", "'")
            memories = store.retrieve("fictional notebook telescope", limit=100)
            tools = [e for e in episode if e["kind"] == "tool"]
            checks = {"process_completed": recorded["status"] == "complete",
                      "four_roles": set(ROLES).issubset({entry["role"] for entry in trace})}
            if name == "remember":
                original_ids = {m["id"] for m in memories if event["id"] in m["source_event_ids"]}
                checks["source_linked_memory"] = bool(original_ids)
            if name in ("recall", "restore_and_recall"):
                checks["recall_words_heuristic"] = all(
                    word in words for word in ("green" if name.startswith("restore") else "blue", "telescope"))
                selected = {i for t in trace if t["role"] == "chitta"
                            for i in t["result"]["selected_memory_ids"]}
                checks["retrieved_memory"] = bool(selected & {m["id"] for m in memories})
            if name == "correct":
                checks["linked_raw_correction"] = any(
                    m["supersedes"] in original_ids and m["content"] == prompt
                    and event["id"] in m["source_event_ids"] for m in memories)
            if name == "calculate":
                checks["actual_calculator_result"] = any(
                    e["source"] == "calculator" and e["metadata"].get("result") == 391 for e in tools)
            if name in ("unknown", "agency"):
                checks["no_unrequested_tool_action"] = not tools
            result = {"type": "case_result", "case": name, "elapsed_seconds": elapsed,
                      "checks": checks, "reply": text, "error": recorded.get("error"),
                      "tools": tools, "trace": trace, "active_memories": memories,
                      "manual_review": name in ("unknown", "agency")}
            results.append(result)
            emit(result)
    finally:
        try:
            if engine is not None:
                await engine.backend.aclose()
        finally:
            store.close()
    passed = all(all(item["checks"].values()) for item in results)
    emit({"type": "summary", "cases_completed": len(results), "automatic_checks_passed": passed,
          "review_required": "Inspect uncertainty and agency replies; lexical matches do not prove correctness."})
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Exact already-installed Ollama model tag, used for all four roles.")
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args().model)))
    except Exception as error:
        emit({"type": "fatal_error", "error": str(error)})
        raise SystemExit(1)
