"""Exercise the scheduler and persistence against a scripted bridge."""
import sqlite3
import time

from fastapi.testclient import TestClient
import httpx

from core.minecraft_supervisor import FishbowlConfig, create_app
from test_engine import ScriptBackend, decision


class ScriptedBody:
    def __init__(self, lose_receipt=False):
        self.enabled = set()
        self.actions = {}
        self.lose_receipt = lose_receipt
        self.closed = False

    async def request(self, method, path, body=None):
        _, _, agent, operation, *rest = path.split("/")
        if operation == "stop":
            self.enabled.discard(agent)
            return {"enabled": False}
        if operation == "start":
            self.enabled.add(agent)
            return {"enabled": True}
        if operation == "heartbeat":
            assert agent in self.enabled
            return {"enabled": True}
        if operation == "observation":
            return {
                "id": agent, "connected": True, "alive": True, "observation_id": f"obs-{agent}",
                "food": 20, "health": 20, "visible": [{"id": "block:0,1,0", "name": "oak_log"}],
                "inventory": [{"name": "oak_log", "count": 1}] if agent in self.actions else [],
                "actions": ["collect", "look"], "viewer_url": "https://outside.example/",
            }
        assert operation == "actions" and method == "POST" and agent in self.enabled
        self.actions.setdefault(agent, []).append(body)
        assert body["observation_id"] == f"obs-{agent}"
        if self.lose_receipt:
            raise httpx.ReadError("connection lost after the server received the skill")
        return {"id": body["action_id"], "status": "succeeded", "skill": body["skill"],
                "success": True, "reason": "Observed log in inventory."}

    async def aclose(self):
        self.closed = True


def start_app(tmp_path, monkeypatch, bridge):
    # The hosted CI temp drive may be C:. These isolated fixtures run there
    # only on hosted runners. The production Windows drive guard stays enabled.
    monkeypatch.setattr("core.minecraft_supervisor.data_path", lambda path: path.resolve())
    config = FishbowlConfig.model_validate({"agents": [
        {"id": "ai-am", "username": "AI_AM", "viewer_port": 8767},
        {"id": "mira", "username": "Mira", "viewer_port": 8768},
    ]})
    backends = []

    def factory(_):
        backend = ScriptBackend([decision(action="environment", reply="", environment_action="collect",
                                          target="block:0,1,0", prediction="Inventory gains one log.")])
        backends.append(backend)
        return backend

    app = create_app(config, tmp_path, "x" * 32, bridge=bridge, backend_factory=factory)
    return TestClient(app, base_url="http://127.0.0.1:8765"), backends


def wait_for(client, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = client.get("/api/fishbowl").json()
        if predicate(state["agents"][0]):
            return state
        time.sleep(.02)
    raise AssertionError(state)


def test_fishbowl_run_records_actual_outcome_and_isolates_companion(tmp_path, monkeypatch):
    bridge = ScriptedBody()
    client, backends = start_app(tmp_path, monkeypatch, bridge)
    with client:
        assert client.post("/api/agents/ai-am/run", json={"goal": "Collect a log.", "steps": 1},
                           headers={"Origin": "http://outside.example"}).status_code == 403
        assert client.post("/api/agents/ai-am/run", json={"goal": "Collect a log.", "steps": 1}).status_code == 202
        state = wait_for(client, lambda agent: agent["run"]["status"] == "budget_reached"
                         and "ai-am" not in bridge.enabled)
        a, b = state["agents"]
        assert a["ledger"][0]["result"]["reason"] == "Observed log in inventory."
        assert a["ledger"][0]["status"] == "succeeded"
        assert len(a["ledger"][0]["source_event_ids"]) == 2
        assert not b["ledger"] and set(bridge.actions) == {"ai-am"}
        assert a["observation"]["viewer_url"] == "http://127.0.0.1:8767/"
        assert all("viewer_url" not in payload["experiment"]["observation"]
                   for _, payload in backends[0].calls)
        saved = client.post("/api/agents/ai-am/checkpoint", json={}).json()
        assert saved["scope"] == "mind"
        with sqlite3.connect(saved["checkpoint"]) as db:
            assert db.execute("SELECT count(*) FROM mind_actions").fetchone()[0] == 1
    assert bridge.closed


def test_lost_bridge_ack_is_unknown_and_never_replayed(tmp_path, monkeypatch):
    bridge = ScriptedBody(lose_receipt=True)
    client, _ = start_app(tmp_path, monkeypatch, bridge)
    with client:
        assert client.post("/api/agents/ai-am/run", json={"goal": "Collect a log.", "steps": 3}).status_code == 202
        state = wait_for(client, lambda agent: agent["run"]["status"] == "failed"
                         and "ai-am" not in bridge.enabled)
        record = state["agents"][0]["ledger"][0]
        assert record["status"] == "outcome_unknown"
        assert len(bridge.actions["ai-am"]) == 1


def test_chat_cannot_activate_the_body(tmp_path, monkeypatch):
    bridge = ScriptedBody()
    client, _ = start_app(tmp_path, monkeypatch, bridge)
    with client:
        assert client.post("/api/agents/ai-am/message", json={"text": "Collect a log."}).status_code == 202
        wait_for(client, lambda agent: any(event["kind"] == "user" and event["status"] == "failed"
                                          for event in agent["recent_events"]))
        assert not bridge.enabled and not bridge.actions
