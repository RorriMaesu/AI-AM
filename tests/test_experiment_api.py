"""The experiment API keeps evaluator truth separate and validates every mutation."""
from copy import deepcopy
import pytest
from fastapi.testclient import TestClient
from core.orchestrator import create_app

ORIGIN = "http://127.0.0.1:8002"


class ExperimentEngine:
    def __init__(self):
        self.config = {"backend": "demo"}
        self.calls = []
        self.state = {"status": "idle", "backend": "demo"}

    def snapshot(self):
        return deepcopy(self.state)

    async def stop(self):
        self.state["status"] = "stopped"

    def experiment_snapshot(self, evaluator=False):
        self.calls.append(("view", evaluator))
        public = {"revision": 1, "observation": {"objects": [{"id": "key"}]}, "actions": []}
        if evaluator:
            public.update(world={"objects": {"cup": {"position": [6, 3]}}}, evaluator_only=True)
        return public

    async def experiment_reset(self, seed=0):
        self.calls.append(("reset", seed))
        return {"revision": 2}

    async def experiment_step(self, **kwargs):
        self.calls.append(("step", kwargs))
        return {"status": "succeeded"}

    async def experiment_intervene(self, change):
        self.calls.append(("intervene", change))
        return {"revision": 3}

    async def experiment_run(self, goal, steps=5):
        self.calls.append(("run", goal, steps))
        return {"id": "experiment-event", "status": "pending"}

    async def experiment_pause(self):
        self.calls.append(("pause",))
        return {"status": "paused"}


@pytest.fixture
def context():
    engine = ExperimentEngine()
    with TestClient(create_app(engine=engine, start_runtime=False), base_url=ORIGIN) as client:
        yield client, engine


def test_observer_truth_is_only_returned_from_explicit_evaluator_endpoint(context):
    client, engine = context
    public = client.get("/api/experiment")
    assert public.status_code == 200
    assert "world" not in public.json()
    assert "cup" not in public.text
    truth = client.get("/api/experiment/evaluator")
    assert truth.json()["evaluator_only"] is True
    assert truth.json()["world"]["objects"]["cup"]["position"] == [6, 3]
    assert engine.calls == [("view", False), ("view", True)]
    assert public.headers["cache-control"] == "no-store"


def test_manual_actions_are_attributed_to_user_and_pass_prediction(context):
    client, engine = context
    result = client.post("/api/experiment/step", json={
        "action": "pick_up", "target": "key", "prediction": "The key will be carried.", "confidence": 0.75
    })
    assert result.status_code == 200
    assert engine.calls[-1] == ("step", {
        "action": "pick_up", "target": "key", "prediction": "The key will be carried.",
        "confidence": 0.75, "actor": "user"
    })


@pytest.mark.parametrize("payload", [
    {"action": "shell"}, {"action": "move_forward", "actor": "self"},
    {"action": "look", "confidence": True}, {"action": "look", "confidence": -0.1},
    {"action": "look", "confidence": 1.1}, {"action": "look", "target": 123},
    {"action": "look", "prediction": "x" * 1001}
])
def test_invalid_step_is_never_executed(context, payload):
    client, engine = context
    assert client.post("/api/experiment/step", json=payload).status_code == 422
    assert not engine.calls


def test_bounded_run_reset_pause_and_intervention(context):
    client, engine = context
    assert client.post("/api/experiment/reset", json={"seed": 7}).status_code == 200
    assert client.post("/api/experiment/run", json={"goal": "  Find the key. ", "steps": 3}).status_code == 202
    assert engine.calls[-1] == ("run", "Find the key.", 3)
    assert client.post("/api/experiment/pause", json={}).status_code == 200
    change = {"kind": "move_object", "object_id": "key", "position": [3, 2]}
    assert client.post("/api/experiment/intervene", json=change).status_code == 200
    assert engine.calls[-1] == ("intervene", change)
    mapping = {"kind": "alter_controls", "mapping": {"turn_left": "turn_right", "turn_right": "turn_left"}}
    assert client.post("/api/experiment/intervene", json=mapping).status_code == 200
    assert engine.calls[-1] == ("intervene", mapping)


@pytest.mark.parametrize("endpoint,payload", [
    ("run", {"goal": " ", "steps": 2}), ("run", {"goal": "Find it", "steps": 11}),
    ("run", {"goal": "Find it", "steps": True}), ("run", {"goal": "Find it", "world": {"hidden": True}}),
    ("reset", {"seed": True}), ("pause", {"execute": True}),
    ("intervene", {"kind": "move_object", "object_id": "key", "position": [True, 2]}),
    ("intervene", {"kind": "move_object", "object_id": "key", "position": [3]}),
    ("intervene", {"kind": "move_object", "object_id": "key", "position": [3, 2], "mapping": {}}),
    ("intervene", {"kind": "alter_controls", "mapping": {"look": "shell"}}),
    ("intervene", {"kind": "alter_controls", "mapping": {}}),
])
def test_invalid_experiment_request_is_rejected(context, endpoint, payload):
    client, engine = context
    assert client.post("/api/experiment/" + endpoint, json=payload).status_code == 422
    assert not engine.calls


def test_experiment_mutations_share_origin_and_runtime_boundaries(context):
    client, engine = context
    assert client.post("/api/experiment/step", json={"action": "look"},
                       headers={"Origin": "https://example.com"}).status_code == 403
    client.app.state.stopping = True
    assert client.post("/api/experiment/run", json={"goal": "Explore."}).status_code == 503
    assert not engine.calls

def test_evaluator_events_are_hidden_from_public_event_routes(context):
    client, engine = context
    public = {"id": "public", "kind": "tool", "metadata": {}, "content": "Look completed"}
    private = {"id": "private", "kind": "experiment_intervention",
               "metadata": {"evaluator_only": True, "change": {"position": [6, 3]}},
               "content": "Supervisor moved hidden object"}
    class Store:
        def list_events(self, limit=50):
            return [public, private]
        def get_event(self, event_id):
            return private if event_id == "private" else public
    engine.store = Store()
    assert client.get("/api/events").json()["events"] == [public]
    assert client.get("/api/events/private").status_code == 404
    assert client.get("/api/events/public").status_code == 200
