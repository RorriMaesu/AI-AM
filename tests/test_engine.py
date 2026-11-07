import asyncio
import copy
import pytest
from core.actions import calculate
from core.config import load_config
from core.engine import MindEngine, RuntimeLease
from database.db_manager import MindStore

def decision(**changes):
    value = {"action": "reply", "reply": "I can account for that.", "query": "", "expression": "",
             "evidence_ids": [], "uncertainty": 0.2, "rationale": "Grounded in the supplied event.",
             "memory_updates": [], "goal_updates": []}
    value.update(changes)
    return value

class ScriptBackend:
    def __init__(self, decisions=None, fail=False, invalid_chitta=False):
        self.decisions = list(decisions or [decision()])
        self.calls = []
        self.fail = fail
        self.invalid_chitta = invalid_chitta
        self.closed = False
    async def infer(self, role, payload):
        self.calls.append((role, copy.deepcopy(payload)))
        if self.fail:
            raise RuntimeError("Local inference unavailable")
        if role == "manas":
            return {"observations": [payload["current_event"]["content"]], "interpretations": ["An incoming event"],
                    "uncertainties": [], "retrieval_query": payload["current_event"]["content"], "salience": 0.5}, 20
        if role == "chitta":
            return {"selected_memory_ids": ["invented-id"] if self.invalid_chitta else [m["id"] for m in payload["memories"]],
                    "associations": [], "contradictions": [], "missing_context": []}, 20
        if role == "ahamkara":
            return {"self_relevance": "Accountable for my own outputs.", "agency": "The user supplied this message.",
                    "capability_limits": ["No unscheduled external actions."], "commitments": ["Preserve corrections."]}, 20
        result = self.decisions.pop(0) if len(self.decisions) > 1 else self.decisions[0]
        return copy.deepcopy(result(payload) if callable(result) else result), 20
    async def list_models(self):
        return ["local:small", "local:large"]
    async def aclose(self):
        self.closed = True

@pytest.fixture
def parts(tmp_path):
    config = load_config(data_dir=str(tmp_path), demo=True)
    config["max_rounds"] = 2
    store = MindStore(config["db_path"])
    yield config, store
    store.close()

def run_event(config, store, backend, text="Remember the blue notebook.", kind="user"):
    async def work():
        engine = MindEngine(config, store, backend)
        event = await engine.submit(text) if kind == "user" else await engine.reflect(text)
        claimed = store.claim_next()
        assert claimed["id"] == event["id"]
        await engine.process_event(claimed)
        return engine, event
    return asyncio.run(work())

def test_four_functions_one_committed_response(parts):
    config, store = parts
    backend = ScriptBackend()
    engine, event = run_event(config, store, backend)
    assert [r for r, _ in backend.calls] == ["manas", "chitta", "ahamkara", "buddhi"]
    assert store.get_event(event["id"])["status"] == "complete"
    assert len([e for e in store.list_events() if e["kind"] == "assistant"]) == 1
    assert engine.state["state_version"] == 1
    assert engine.state["metrics"]["tokens"] == 80
    assert store.retrieve("blue notebook")

def test_buddhi_receives_original_memory_directly(parts):
    config, store = parts
    event = store.append_event("user", "The blue notebook is on the shelf.", source="user")
    memory = store.add_memory("The blue notebook is on the shelf.", "user_statement", "user", [event["id"]], 1.0)
    backend = ScriptBackend()
    run_event(config, store, backend, "blue notebook")
    payload = next(p for r, p in backend.calls if r == "buddhi")
    assert memory["id"] in [m["id"] for m in payload["memories"]]
    assert any("shelf" in m["content"] for m in payload["memories"])

def test_restart_preserves_identity_and_events(parts):
    config, store = parts
    def promise(payload):
        return decision(commitment_updates=[{"operation": "create", "id": None,
                        "description": "Preserve corrections.",
                        "source_event_ids": [payload["current_event"]["id"]]}])
    engine, event = run_event(config, store, ScriptBackend([promise]),
                              "Please commit to preserving corrections.")
    restored = MindEngine(config, store, ScriptBackend())
    assert restored.state["state_version"] == engine.state["state_version"]
    assert restored.state["self_model"]["commitments"] == ["Preserve corrections."]
    assert store.get_event(event["id"])["content"] == "Please commit to preserving corrections."
    assert restored.state["status"] == "idle"

def test_correction_supersedes_evidence(parts):
    config, store = parts
    old = store.append_event("user", "My notebook is blue.", source="user")
    prior = store.add_memory("My notebook is blue.", "user_statement", "user", [old["id"]], 1.0)
    def corrected(payload):
        return decision(reply="I recorded the correction.", evidence_ids=[prior["id"], payload["current_event"]["id"]],
                        memory_updates=[{"content": payload["current_event"]["content"], "kind": "user_statement",
                        "source_event_ids": [payload["current_event"]["id"]], "confidence": 1.0, "supersedes": prior["id"]}])
    run_event(config, store, ScriptBackend([corrected]), "Correction: my notebook is green, not blue.")
    assert MindEngine(config, store, ScriptBackend()).state["state_version"] == 1
    assert prior["id"] not in [m["id"] for m in store.retrieve("notebook")]
    assert store.get_memory(prior["id"]) is not None

@pytest.mark.parametrize("proposed_content", [
    "The user's notebook is green.",
    "Correction: my notebook is green, not blue.",
])
def test_raw_user_correction_uses_current_quote_without_duplicate_capture(parts, proposed_content):
    config, store = parts
    old_event = store.append_event("user", "My notebook is blue.", source="user")
    old_memory = store.add_memory(old_event["content"], "user_statement", "user", [old_event["id"]], 1.0)
    current_quote = "Correction: my notebook is green, not blue."
    def corrected(payload):
        return decision(reply="I recorded the correction.",
                        evidence_ids=[old_memory["id"], payload["current_event"]["id"]],
                        memory_updates=[{"content": proposed_content, "kind": "user_statement",
                                         "source_event_ids": [old_event["id"], payload["current_event"]["id"]],
                                         "confidence": .6, "supersedes": old_memory["id"]}])
    engine, event = run_event(config, store, ScriptBackend([corrected]), current_quote)
    assert store.get_event(event["id"])["status"] == "complete"
    assert engine.state["state_version"] == store.load_state()["state_version"] == 1
    records = store.list_memories()
    assert len(records) == 2
    replacement = next(m for m in records if m["id"] != old_memory["id"])
    assert replacement["content"] == current_quote
    assert replacement["kind"] == "user_statement"
    assert replacement["source"] == "user"
    assert replacement["source_event_ids"] == [event["id"]]
    assert replacement["confidence"] == 1.0
    assert replacement["supersedes"] == old_memory["id"]
    previous = store.get_memory(old_memory["id"])
    assert previous["content"] == old_event["content"]
    assert previous["source_event_ids"] == [old_event["id"]]
    assert previous["superseded_by"] == replacement["id"]
    assert store.get_event(old_event["id"])["content"] == old_event["content"]
    assert [m["id"] for m in store.retrieve("notebook")] == [replacement["id"]]
    assistants = [e for e in store.list_events() if e["kind"] == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["metadata"]["memory_ids"] == [replacement["id"]]

@pytest.mark.parametrize("invalid_case", ["unknown_target", "reflection"])
def test_canonical_user_correction_preserves_target_and_foreground_guards(parts, invalid_case):
    config, store = parts
    old_event = store.append_event("user", "My notebook is blue.", source="user")
    old_memory = store.add_memory(old_event["content"], "user_statement", "user", [old_event["id"]], 1.0)
    def unsupported(payload):
        return decision(memory_updates=[{
            "content": "The user's notebook is green.", "kind": "user_statement",
            "source_event_ids": [payload["current_event"]["id"]], "confidence": .9,
            "supersedes": "unknown-memory" if invalid_case == "unknown_target" else old_memory["id"],
        }])
    _, event = run_event(config, store, ScriptBackend([unsupported]), "Reconsider my blue notebook.",
                         kind="reflection" if invalid_case == "reflection" else "user")
    assert store.get_event(event["id"])["status"] == "failed"
    assert store.get_memory(old_memory["id"])["superseded_by"] is None
    assert store.stats()["memories"] == 1
    assert not [e for e in store.list_events() if e["kind"] == "assistant"]

def test_failure_retains_retryable_input(parts):
    config, store = parts
    engine, event = run_event(config, store, ScriptBackend(fail=True))
    assert store.get_event(event["id"])["status"] == "failed"
    assert not [e for e in store.list_events() if e["kind"] == "assistant"]
    assert engine.state["state_version"] == 0
    asyncio.run(engine.retry(event["id"]))
    assert store.claim_next()["id"] == event["id"]

@pytest.mark.parametrize("bad_memory", [False, True])
def test_invented_evidence_rejected(parts, bad_memory):
    config, store = parts
    backend = ScriptBackend(invalid_chitta=True) if bad_memory else ScriptBackend([decision(evidence_ids=["invented"])])
    engine, event = run_event(config, store, backend)
    assert store.get_event(event["id"])["status"] == "failed"
    assert engine.state["state_version"] == 0

def test_observation_requires_tool_outcome(parts):
    config, store = parts
    def fabricated(payload):
        return decision(memory_updates=[{"content": "A sensor detected rain.", "kind": "observation",
                        "source_event_ids": [payload["current_event"]["id"]], "confidence": 1.0}])
    _, event = run_event(config, store, ScriptBackend([fabricated]))
    assert store.get_event(event["id"])["status"] == "failed"
    assert store.stats()["memories"] == 0

def test_reflection_stays_reflection(parts):
    config, store = parts
    def imagined(payload):
        return decision(memory_updates=[{"content": "Imagine a purple notebook.", "kind": "observation",
                        "source_event_ids": [payload["current_event"]["id"]], "confidence": 0.5}])
    _, event = run_event(config, store, ScriptBackend([imagined]), "Imagine a purple notebook.", "reflection")
    assert store.get_event(event["id"])["status"] == "complete"
    assert all(m["kind"] == "reflection" for m in store.retrieve("purple notebook"))

def test_recurrence_is_bounded(parts):
    config, store = parts
    backend = ScriptBackend([decision(action="retrieve", query="missing appointment evidence", reply="")])
    engine, event = run_event(config, store, backend)
    assert len(backend.calls) == 8
    assert store.get_event(event["id"])["status"] == "complete"
    assert "more evidence" in engine.state["workspace"]["decision"]["reply"]

def test_calculator_feedback(parts):
    config, store = parts
    def answer(payload):
        outcome = payload["workspace"]["tool_results"][0]
        assert outcome["result"] == 42
        return decision(reply="The result is 42.", evidence_ids=[outcome["event_id"]])
    backend = ScriptBackend([decision(action="calculate", expression="6*7", reply=""), answer])
    engine, event = run_event(config, store, backend, "Calculate 6 times 7.")
    assert store.get_event(event["id"])["status"] == "complete"
    assert engine.state["workspace"]["decision"]["reply"] == "The result is 42."
    assert [e for e in store.list_events() if e["kind"] == "tool"][0]["source"] == "calculator"

def test_python_fences_never_execute(parts):
    config, store = parts
    fence = chr(96) * 3
    reply = "Example only:\n" + fence + "python\nraise RuntimeError('never execute')\n" + fence
    engine, event = run_event(config, store, ScriptBackend([decision(reply=reply)]))
    assert store.get_event(event["id"])["status"] == "complete"
    assert engine.state["workspace"]["decision"]["reply"] == reply
    assert not [e for e in store.list_events() if e["kind"] == "tool"]

def test_queue_budget(parts):
    config, store = parts
    config["max_pending"] = 1
    async def work():
        engine = MindEngine(config, store, ScriptBackend())
        await engine.submit("First")
        with pytest.raises(OverflowError):
            await engine.submit("Second")
    asyncio.run(work())

@pytest.mark.parametrize("value", [123, "", "   ", "x"*8001])
def test_input_validation(parts, value):
    config, store = parts
    with pytest.raises(ValueError):
        asyncio.run(MindEngine(config, store, ScriptBackend()).submit(value))

def test_reflection_needs_purpose(parts):
    config, store = parts
    with pytest.raises(ValueError):
        asyncio.run(MindEngine(config, store, ScriptBackend()).reflect())

def test_role_model_persists(parts):
    config, store = parts
    config["backend"] = "ollama"
    engine = MindEngine(config, store, ScriptBackend())
    asyncio.run(engine.set_models({"buddhi": "local:large"}))
    restored = MindEngine(config, store, ScriptBackend())
    assert restored.config["roles"]["buddhi"]["model"] == "local:large"
    assert restored.config["roles"]["manas"]["model"] != "local:large"

def test_background_setting_persists(parts):
    config, store = parts
    engine = MindEngine(config, store, ScriptBackend())
    asyncio.run(engine.set_background(True))
    assert MindEngine(config, store, ScriptBackend()).state["background_enabled"] is True

def test_cancellation_preserves_event(parts):
    config, store = parts
    class Slow(ScriptBackend):
        async def infer(self, role, payload):
            await asyncio.sleep(30)
    async def work():
        engine = MindEngine(config, store, Slow())
        event = await engine.submit("Please remember this input.")
        task = asyncio.create_task(engine.process_event(store.claim_next()))
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.get_event(event["id"])["status"] == "failed"
    asyncio.run(work())

def test_subscriber_queue_bounded(parts):
    config, store = parts
    async def work():
        engine = MindEngine(config, store, ScriptBackend())
        q = engine.subscribe()
        for i in range(200):
            await engine._emit("test", {"i": i})
        assert q.qsize() == 128
        engine.unsubscribe(q)
    asyncio.run(work())

def test_runtime_lease_exclusive(tmp_path):
    one, two = RuntimeLease(tmp_path/"mind.db"), RuntimeLease(tmp_path/"mind.db")
    one.acquire()
    try:
        with pytest.raises(RuntimeError):
            two.acquire()
    finally:
        one.release()
    two.acquire()
    two.release()

def test_run_loop_shutdown(parts):
    config, store = parts
    async def work():
        backend = ScriptBackend()
        engine = MindEngine(config, store, backend)
        runner = asyncio.create_task(engine.run_forever())
        event = await engine.submit("Remember my notebook.")
        for _ in range(100):
            if store.get_event(event["id"])["status"] == "complete":
                break
            await asyncio.sleep(.01)
        assert store.get_event(event["id"])["status"] == "complete"
        await engine.stop()
        await runner
        assert backend.closed
    asyncio.run(work())

@pytest.mark.parametrize("expression,expected", [("6*7",42),("(8+4)/3",4),("-2**3",-8),("5%2",1)])
def test_calculator(expression, expected):
    assert calculate(expression) == expected

@pytest.mark.parametrize("expression", ["__import__('os').system('echo bad')","open('x')","2**999999999",
                                        "10**100","True+1","[1,2]","1/0","1e309","'hello'","x"*201])
def test_calculator_rejects_execution(expression):
    with pytest.raises(ValueError):
        calculate(expression)

def test_future_and_failed_inputs_are_not_conversation_history(parts):
    config, store = parts
    async def work():
        backend = ScriptBackend()
        engine = MindEngine(config, store, backend)
        past = store.append_event("user", "An earlier completed statement.", source="user")
        first = await engine.submit("Process this first.")
        future = await engine.submit("Secret from a future queued turn.")
        failed = store.append_event("user", "A failed attempt.", source="user", status="pending")
        store.fail_event(failed["id"], "previous failure")
        await engine.process_event(store.claim_next())
        for _, payload in backend.calls:
            ids = {e["id"] for e in payload["history"]}
            assert past["id"] in ids
            assert future["id"] not in ids
            assert failed["id"] not in ids
            assert payload["current_event"]["id"] == first["id"]
    asyncio.run(work())

def test_notification_failure_does_not_undo_commit(parts, monkeypatch):
    config, store = parts
    async def work():
        engine = MindEngine(config, store, ScriptBackend())
        event = await engine.submit("Preserve this completed turn.")
        original = engine._emit
        async def unavailable(kind, data):
            if kind == "cycle_completed":
                raise OSError("Observatory disconnected")
            await original(kind, data)
        monkeypatch.setattr(engine, "_emit", unavailable)
        await engine.process_event(store.claim_next())
        assert store.get_event(event["id"])["status"] == "complete"
        assert engine.state["state_version"] == store.load_state()["state_version"] == 1
        assert len([e for e in store.list_events() if e["kind"] == "assistant"]) == 1
        assert engine.state["metrics"]["errors"] == 0
    asyncio.run(work())

def test_worker_startup_error_signals_readiness_without_writing_state(parts, monkeypatch):
    config, store = parts
    async def work():
        engine = MindEngine(config, store, ScriptBackend())
        def occupied():
            raise RuntimeError("already owned")
        monkeypatch.setattr(engine._lease, "acquire", occupied)
        with pytest.raises(RuntimeError, match="already owned"):
            await engine.run_forever()
        assert engine.ready.is_set()
        assert isinstance(engine.startup_error, RuntimeError)
        assert engine.state["status"] == "stopped"
        assert store.load_state() is None
    asyncio.run(work())

@pytest.mark.parametrize("failure", ["save", "close"])
def test_cleanup_always_releases_runtime_lease(parts, monkeypatch, failure):
    config, store = parts
    async def work():
        backend = ScriptBackend()
        engine = MindEngine(config, store, backend)
        task = asyncio.create_task(engine.run_forever())
        await asyncio.wait_for(engine.ready.wait(), 2)
        assert engine.startup_error is None
        if failure == "save":
            def fail_save(state):
                raise OSError("cannot save")
            monkeypatch.setattr(store, "save_state", fail_save)
        else:
            async def fail_close():
                raise OSError("cannot close")
            monkeypatch.setattr(backend, "aclose", fail_close)
        await engine.stop()
        with pytest.raises(OSError):
            await task
        assert engine._lease.handle is None
        another = RuntimeLease(config["db_path"])
        another.acquire()
        another.release()
    asyncio.run(work())

def test_stopped_worker_cannot_be_reopened_by_pause(parts):
    config, store = parts
    async def work():
        engine = MindEngine(config, store, ScriptBackend())
        failed = await engine.submit("Failed input")
        store.fail_event(failed["id"], "offline")
        await engine.stop()
        for operation in (engine.pause, engine.resume,
                          lambda: engine.submit("new"), lambda: engine.reflect("topic"),
                          lambda: engine.retry(failed["id"])):
            with pytest.raises(RuntimeError):
                await operation()
        assert engine.state["status"] == "stopped"
        assert store.stats()["pending"] == 0
    asyncio.run(work())

def test_retry_respects_queue_capacity(parts):
    config, store = parts
    config["max_pending"] = 1
    async def work():
        engine = MindEngine(config, store, ScriptBackend())
        failed = await engine.submit("Failed input")
        store.fail_event(failed["id"], "offline")
        await engine.submit("Waiting")
        with pytest.raises(OverflowError):
            await engine.retry(failed["id"])
        assert store.get_event(failed["id"])["status"] == "failed"
    asyncio.run(work())

def test_generated_paraphrase_is_not_a_raw_statement(parts):
    config, store = parts
    def paraphrase(payload):
        return decision(memory_updates=[{"content": "The notebook is definitely red.",
                        "kind": "user_statement", "source_event_ids": [payload["current_event"]["id"]],
                        "confidence": .9}])
    _, event = run_event(config, store, ScriptBackend([paraphrase]), "The notebook might be red.")
    records = store.retrieve("notebook")
    assert any(m["content"] == "The notebook might be red." and m["kind"] == "user_statement" for m in records)
    assert any(m["content"] == "The notebook is definitely red." and m["kind"] == "inference" for m in records)
    assert store.get_event(event["id"])["status"] == "complete"

@pytest.mark.parametrize("old_sources,replacement_kind", [(True, "user_statement"), (False, "inference"), (False, "reflection"), (False, "commitment")])
def test_correction_cannot_replace_raw_evidence_with_an_inference(parts, old_sources, replacement_kind):
    config, store = parts
    old = store.append_event("user", "My notebook is blue.", source="user")
    memory = store.add_memory(old["content"], "user_statement", "user", [old["id"]], 1.0)
    def unsupported(payload):
        return decision(memory_updates=[{"content": old["content"] if old_sources else "The notebook must be green.",
                        "kind": replacement_kind,
                        "source_event_ids": [old["id"] if old_sources else payload["current_event"]["id"]],
                        "confidence": .9, "supersedes": memory["id"]}])
    _, event = run_event(config, store, ScriptBackend([unsupported]), "Where is my notebook?")
    assert store.get_event(event["id"])["status"] == "failed"
    assert memory["id"] in [m["id"] for m in store.retrieve("notebook")]
    assert not [e for e in store.list_events() if e["kind"] == "assistant"]

def test_demo_never_imports_live_legacy_history(parts, monkeypatch, tmp_path):
    config, store = parts
    legacy = tmp_path / "legacy.db"
    legacy.write_text("not opened")
    config["legacy_db_path"] = str(legacy)
    def forbidden(path):
        raise AssertionError("Demo must not import live history")
    monkeypatch.setattr(store, "import_legacy", forbidden)
    async def work():
        engine = MindEngine(config, store, ScriptBackend())
        task = asyncio.create_task(engine.run_forever())
        await asyncio.wait_for(engine.ready.wait(), 2)
        assert engine.startup_error is None
        await engine.stop()
        await task
    asyncio.run(work())

def test_paused_snapshot_during_foreground_processing(parts):
    config, store = parts
    async def work():
        engine = MindEngine(config, store, ScriptBackend())
        engine.state["status"] = "thinking"
        await engine.pause()
        assert engine.snapshot()["paused"] is True
        assert engine.snapshot()["status"] == "thinking"
    asyncio.run(work())
