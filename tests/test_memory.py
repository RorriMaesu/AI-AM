"""Behavioral persistence tests. Every database belongs to pytest's tmp_path."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from database.db_manager import MindStore


@pytest.fixture
def store(tmp_path):
    instance = MindStore(tmp_path / "mind.sqlite3")
    yield instance
    instance.close()


def remember(store, text, kind="user_statement", source="user", supersedes=None):
    event = store.append_event("user", text, source="user")
    return store.add_memory(
        text, kind, source, [event["id"]], confidence=0.8, supersedes=supersedes
    )


def test_input_and_state_survive_restart(tmp_path):
    path = tmp_path / "persistent.db"
    first = MindStore(path)
    event = first.enqueue("Remember the meeting at noon.")
    first.save_state({"self_model": {"commitments": ["meeting at noon"]}, "state_version": 2})
    first.close()
    second = MindStore(path)
    assert second.get_event(event["id"])["status"] == "pending"
    assert second.load_state()["self_model"]["commitments"] == ["meeting at noon"]
    second.close()


def test_correction_retains_history_and_changes_retrieval_after_restart(tmp_path):
    path = tmp_path / "corrections.db"
    first = MindStore(path)
    old = remember(first, "The appointment is Tuesday.")
    current = remember(first, "The appointment is Thursday.", supersedes=old["id"])
    assert first.get_memory(old["id"])["superseded_by"] == current["id"]
    first.close()
    second = MindStore(path)
    results = second.retrieve("appointment")
    assert [item["id"] for item in results] == [current["id"]]
    assert second.get_memory(current["id"])["supersedes"] == old["id"]
    assert len(second.retrieve("appointment", include_superseded=True)) == 2
    second.close()


def test_retrieval_separates_imagined_and_observed_events(store):
    observed = remember(store, "The blue door opened.", kind="observation", source="sensor")
    imagined = remember(store, "Imagine the blue door was locked.", kind="reflection", source="manas")
    results = {item["id"]: item for item in store.retrieve("blue door")}
    assert results[observed["id"]]["kind"] == "observation"
    assert results[imagined["id"]]["kind"] == "reflection"
    assert results[observed["id"]]["source_event_ids"] == observed["source_event_ids"]
    assert results[imagined["id"]]["source_event_ids"] == imagined["source_event_ids"]


def test_retrieval_does_not_return_zero_relevance_memory(store):
    remember(store, "The telescope is on the balcony.")
    assert store.retrieve("cabbage pancakes") == []
    assert store.retrieve("what is it") == []
    assert len(store.retrieve("")) == 1
    assert store.retrieve("telescope", limit=0) == []


def test_retrieval_is_deterministic_and_treats_query_as_data(store):
    memory = remember(store, "The telescope is on the balcony.")
    first = store.retrieve("telescope")
    assert first == store.retrieve("telescope")
    store.retrieve("telescope' OR 1=1; DROP TABLE mind_memories;--")
    assert store.get_memory(memory["id"])["content"] == memory["content"]


def test_claims_are_exclusive_and_user_input_has_priority(store):
    reflection = store.enqueue("Consolidate recent observations.", kind="reflection", source="system")
    inputs = [store.enqueue(f"User request {index}") for index in range(12)]
    first = store.claim_next()
    assert first["id"] == inputs[0]["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = [first] + list(pool.map(lambda _: store.claim_next(), range(12)))
    assert len({event["id"] for event in claims}) == 13
    assert {event["id"] for event in claims} == {reflection["id"], *(event["id"] for event in inputs)}
    assert store.claim_next() is None


def test_recover_interrupted_input_and_retry_failure(store):
    event = store.enqueue("Keep this request across a failed model call.")
    assert store.claim_next()["id"] == event["id"]
    assert store.recover_pending() == 1
    assert store.claim_next()["id"] == event["id"]
    store.fail_event(event["id"], "inference timeout")
    failed = store.get_event(event["id"])
    assert failed["content"] == event["content"]
    assert failed["status"] == "failed"
    assert failed["error"] == "inference timeout"
    assert store.claim_next() is None
    retried = store.retry_event(event["id"])
    assert retried["status"] == "pending"
    assert retried["error"] is None
    assert store.claim_next()["id"] == event["id"]


def test_cycle_commits_reply_state_trace_and_memory_together(store):
    event = store.enqueue("My favorite color is violet.")
    store.claim_next()
    memory = {
        "content": "The user said their favorite color is violet.",
        "kind": "user_statement", "source": "chitta",
        "source_event_ids": [event["id"]], "confidence": 0.9,
    }
    state = {"state_version": 1, "self_model": {"name": "AI-AM"}}
    trace = [{"role": "chitta", "evidence": [event["id"]]}]
    answer = store.commit_cycle(event["id"], state, "I will remember that.", [memory], trace)
    assert answer["source"] == "buddhi"
    assert answer["episode_id"] == event["id"]
    assert answer["metadata"]["trace"] == trace
    assert store.get_event(event["id"])["status"] == "complete"
    assert store.load_state() == state
    assert store.get_memory(answer["metadata"]["memory_ids"][0])["source_event_ids"] == [event["id"]]


def test_invalid_evidence_rolls_back_entire_cycle(store):
    event = store.enqueue("Remember a verified observation.")
    store.claim_next()
    store.save_state({"state_version": 0})
    good = {
        "content": "A sourced observation.", "kind": "observation", "source": "sensor",
        "source_event_ids": [event["id"]],
    }
    bad = dict(good, content="Unsupported claim.", source_event_ids=["invented-event-id"])
    with pytest.raises(ValueError, match="Unknown source event"):
        store.commit_cycle(event["id"], {"state_version": 1}, "Done.", [good, bad], [])
    assert store.load_state() == {"state_version": 0}
    assert store.stats()["memories"] == 0
    assert store.stats()["events"] == 1
    assert store.get_event(event["id"])["status"] == "processing"


def test_invalid_state_rolls_back_memory_and_reply(store):
    event = store.enqueue("Keep the old state if serialization fails.")
    store.save_state({"state_version": 0})
    proposal = {
        "content": "This must not be partially committed.",
        "kind": "inference", "source": "buddhi", "source_event_ids": [event["id"]],
    }
    with pytest.raises(ValueError):
        store.commit_cycle(event["id"], {"bad_number": float("nan")}, "Done.", [proposal], [])
    assert store.stats()["events"] == 1
    assert store.stats()["memories"] == 0
    assert store.load_state() == {"state_version": 0}


def test_cycle_commit_is_idempotent_without_duplicating_history(store):
    event = store.enqueue("Say hello.")
    first = store.commit_cycle(event["id"], {"state_version": 1}, "Hello.", [], [])
    second = store.commit_cycle(event["id"], {"state_version": 99}, "Hello.", [], [])
    assert second["id"] == first["id"]
    assert store.stats()["events"] == 2
    assert store.load_state() == {"state_version": 1}
    with pytest.raises(ValueError, match="already completed"):
        store.commit_cycle(event["id"], {}, "Different reply.", [], [])


def test_completed_inputs_cannot_be_retried_or_failed(store):
    event = store.enqueue("Finish this once.")
    store.commit_cycle(event["id"], {}, "Finished.", [], [])
    with pytest.raises(ValueError):
        store.retry_event(event["id"])
    with pytest.raises(ValueError):
        store.fail_event(event["id"], "late failure")


@pytest.mark.parametrize("confidence", [-1, 1.01, float("nan"), float("inf"), True, "0.5"])
def test_confidence_is_validated(store, confidence):
    event = store.append_event("user", "A reported observation.")
    with pytest.raises(ValueError):
        store.add_memory("An observation.", "observation", "sensor", [event["id"]], confidence)
    assert store.stats()["memories"] == 0


def test_memories_require_real_event_provenance(store):
    with pytest.raises(ValueError, match="at least one source"):
        store.add_memory("A fabricated memory.", "inference", "chitta", [])
    with pytest.raises(ValueError, match="Unknown source"):
        store.add_memory("A fabricated memory.", "inference", "chitta", ["missing"])
    with pytest.raises(ValueError, match="Invalid memory kind"):
        store.add_memory("A fabricated memory.", "absolute_truth", "chitta", ["missing"])


def test_correction_reference_validation_preserves_existing_memory(store):
    old = remember(store, "The appointment is Tuesday.")
    newer = remember(store, "The appointment is Thursday.", supersedes=old["id"])
    event = store.append_event("user", "Another correction.")
    for invalid_target in ["does-not-exist", old["id"]]:
        with pytest.raises(ValueError):
            store.add_memory(
                "Another correction.", "user_statement", "user",
                [event["id"]], supersedes=invalid_target,
            )
    assert store.get_memory(old["id"])["superseded_by"] == newer["id"]
    assert store.stats()["memories"] == 2


def test_consolidation_deduplicates_without_deleting_or_crossing_sources(store):
    old = remember(store, "  The   telescope is ready.  ")
    newest = remember(store, "The telescope is ready.")
    sensor = remember(store, "The telescope is ready.", kind="observation", source="sensor")
    result = store.consolidate()
    assert result["duplicates_superseded"] == 1
    assert result["weights_updated"] is False
    assert store.stats()["memories"] == 3
    assert store.stats()["active_memories"] == 2
    assert store.get_memory(old["id"])["superseded_by"] == newest["id"]
    assert store.get_memory(sensor["id"])["superseded_by"] is None
    assert store.consolidate()["duplicates_superseded"] == 0


def test_legacy_import_is_once_only_and_preserves_original_tables(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE memory_nodes (
                node_id TEXT PRIMARY KEY, content TEXT, embedding BLOB,
                baseline_arousal REAL, timestamp TEXT
            )
        """)
        conn.execute("CREATE TABLE cognitive_edges (source_node TEXT, target_node TEXT)")
        conn.execute(
            "INSERT INTO memory_nodes VALUES(?,?,?,?,?)",
            ("old-1", "I imagined a lighthouse.", b"original-bytes", 0.95, "2026-05-24 09:00:00"),
        )
        conn.execute("INSERT INTO cognitive_edges VALUES('old-1','old-1')")
    first = MindStore(path)
    imported = first.retrieve("lighthouse")[0]
    assert imported["kind"] == "legacy_unverified"
    assert imported["confidence"] == 0
    assert imported["source"] == "legacy"
    source = first.get_event(imported["source_event_ids"][0])
    assert source["metadata"]["legacy_node_id"] == "old-1"
    assert first.stats()["legacy_imported"] == 1
    first.close()
    second = MindStore(path)
    assert second.stats()["memories"] == 1
    assert second.stats()["events"] == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT embedding FROM memory_nodes").fetchone()[0] == b"original-bytes"
        assert conn.execute("SELECT COUNT(*) FROM cognitive_edges").fetchone()[0] == 1
    second.close()


def test_recent_events_are_returned_in_conversation_order(store):
    ids = [store.append_event("user", f"Message {index}")["id"] for index in range(5)]
    assert [event["id"] for event in store.list_events(3)] == ids[-3:]
    assert store.list_events(0) == []


def test_store_closes_connections_and_supports_explicit_in_memory_store():
    store = MindStore(":memory:")
    event = store.enqueue("An isolated memory-only test.")
    assert store.get_event(event["id"])["content"] == event["content"]
    store.close()
    with pytest.raises(RuntimeError, match="closed"):
        store.stats()


def test_user_messages_are_data_and_never_sql(store):
    event = store.append_event("user", "Robert'); DROP TABLE mind_events;--")
    assert store.get_event(event["id"])["content"].endswith("--")
    assert store.stats()["events"] == 1


def test_external_legacy_import_is_read_only_and_incrementally_idempotent(tmp_path):
    legacy = tmp_path / "old" / "chitta_store.db"
    legacy.parent.mkdir()
    with sqlite3.connect(legacy) as conn:
        conn.execute("CREATE TABLE memory_nodes(node_id TEXT PRIMARY KEY,content TEXT)")
        conn.execute("INSERT INTO memory_nodes VALUES('one','A remembered lighthouse.')")
    before = legacy.read_bytes()
    current = MindStore(tmp_path / "new" / "chitta.db")
    assert current.import_legacy(legacy) == 1
    assert legacy.read_bytes() == before
    assert current.import_legacy(legacy) == 0
    with sqlite3.connect(legacy) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'mind_%'"
        ).fetchall() == []
        conn.execute("INSERT INTO memory_nodes VALUES('two','A second observation.')")
    assert current.import_legacy(legacy) == 1
    assert current.stats()["legacy_imported"] == 2
    assert current.stats()["memories"] == 2
    assert all(memory["kind"] == "legacy_unverified" for memory in current.list_memories())
    current.close()


def test_external_legacy_import_does_not_confuse_same_node_ids_from_different_files(tmp_path):
    current = MindStore(tmp_path / "current.db")
    for index in (1, 2):
        legacy = tmp_path / f"legacy-{index}.db"
        with sqlite3.connect(legacy) as conn:
            conn.execute("CREATE TABLE memory_nodes(node_id TEXT PRIMARY KEY,content TEXT)")
            conn.execute("INSERT INTO memory_nodes VALUES('same-id',?)", (f"Source {index}",))
        assert current.import_legacy(legacy) == 1
    assert current.stats()["memories"] == 2
    assert current.import_legacy(tmp_path / "current.db") == 0
    current.close()
