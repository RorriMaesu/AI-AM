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
    engine, event = run_event(config, store, ScriptBackend())
    restored = MindEngine(config, store, ScriptBackend())
    assert restored.state["state_version"] == engine.state["state_version"]
    assert restored.state["self_model"]["commitments"] == ["Preserve corrections."]
    assert store.get_event(event["id"])["content"] == "Remember the blue notebook."
    assert restored.state["status"] == "idle"

def test_correction_supersedes_evidence(parts):
    config, store = parts
    old = store.append_event("user", "My notebook is blue.", source="user")
    prior = store.add_memory("My notebook is blue.", "user_statement", "user", [old["id"]], 1.0)
    def corrected(payload):
        return decision(reply="I recorded the correction.", evidence_ids=[prior["id"], payload["current_event"]["id"]],
                        memory_updates=[{"content": "The user's notebook is green.", "kind": "user_statement",
                        "source_event_ids": [payload["current_event"]["id"]], "confidence": 1.0, "supersedes": prior["id"]}])
    run_event(config, store, ScriptBackend([corrected]), "Correction: my notebook is green, not blue.")
    assert MindEngine(config, store, ScriptBackend()).state["state_version"] == 1
    assert prior["id"] not in [m["id"] for m in store.retrieve("notebook")]
    assert store.get_memory(prior["id"]) is not None

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
