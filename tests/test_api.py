"""API boundary tests use an injected runtime, never the user's database or models."""
import asyncio
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core.orchestrator import MAX_BODY_BYTES, create_app

ORIGIN = "http://127.0.0.1:8002"
WS_ORIGIN = "ws://127.0.0.1:8002"
ROLES = ("manas", "chitta", "ahamkara", "buddhi")


class FakeStore:
    def __init__(self):
        self.events = [{"id": "e1", "kind": "user", "content": "Remember the blue door.",
                        "source": "user", "status": "complete", "metadata": {}}]
        self.memories = [{"id": "m1", "content": "The user described a blue door.",
                          "kind": "user_statement", "source": "user",
                          "source_event_ids": ["e1"], "confidence": 1.0}]

    def list_events(self, limit=50):
        return self.events[-limit:]

    def get_event(self, event_id):
        return next((e for e in self.events if e["id"] == event_id), None)

    def get_memory(self, memory_id):
        return next((m for m in self.memories if m["id"] == memory_id), None)

    def retrieve(self, query, limit=6):
        return [m for m in self.memories if not query or query.lower() in m["content"].lower()][:limit]


class FakeBackend:
    async def list_models(self):
        return ["local:small", "local:reason"]


class FakeEngine:
    def __init__(self):
        self.config = {"backend": "demo"}
        self.store = FakeStore()
        self.backend = FakeBackend()
        self.state = {
            "schema_version": 2, "status": "idle", "background_enabled": False,
            "roles": {role: {"model": "local:small"} for role in ROLES},
            "self_model": {"name": "AI-AM"}, "backend": "demo",
        }
        self.subscribers = set()
        self.started = False
        self.stop_called = False
        self.stopped = asyncio.Event()
        self.ready = asyncio.Event()
        self.startup_error = None

    def snapshot(self):
        return deepcopy(self.state)

    async def run_forever(self):
        self.started = True
        self.ready.set()
        await self.stopped.wait()

    async def stop(self):
        self.stop_called = True
        self.state["status"] = "stopped"
        self.stopped.set()

    async def submit(self, text):
        event = {"id": "e2", "kind": "user", "content": text, "status": "pending"}
        self.store.events.append(event)
        return event

    async def reflect(self, topic=""):
        return {"id": "r1", "kind": "reflection", "content": topic, "status": "pending"}

    async def retry(self, event_id):
        result = self.store.get_event(event_id)
        if result is None:
            raise KeyError(event_id)
        if result["status"] != "failed":
            raise ValueError("Only failed events can be retried.")
        result["status"] = "pending"
        return result

    async def consolidate(self):
        return {"deduplicated": 0, "memory_count": len(self.store.memories)}

    async def set_models(self, mapping):
        for role, model in mapping.items():
            self.state["roles"][role]["model"] = model

    async def set_background(self, enabled):
        self.state["background_enabled"] = enabled

    async def pause(self):
        self.state["status"] = "paused"

    async def resume(self):
        self.state["status"] = "idle"

    def subscribe(self):
        queue = asyncio.Queue(maxsize=20)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.subscribers.discard(queue)


@pytest.fixture
def engine():
    return FakeEngine()


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine=engine, start_runtime=False), base_url=ORIGIN) as value:
        yield value


def test_health_identifies_runtime_without_starting_it(client, engine):
    result = client.get("/api/health").json()
    assert result["identity"] == "ai-am"
    assert result["version"] == "2.0.0"
    assert result["backend"] == "demo"
    assert result["runtime_running"] is False
    assert not engine.started


def test_submit_preserves_text_and_returns_durable_event_shape(client):
    result = client.post("/api/input", json={"text": "  Remember this correction.  "})
    assert result.status_code == 202
    assert result.json()["content"] == "Remember this correction."
    assert result.json()["status"] == "pending"


@pytest.mark.parametrize("payload", [
    {"text": 123}, {"text": True}, {"text": None}, {"text": []},
    {"text": "  "}, {"text": "x" * 8001}, {},
    {"text": "hello", "execute": True},
])
def test_invalid_input_never_enters_queue(client, engine, payload):
    assert client.post("/api/input", json=payload).status_code == 422
    assert len(engine.store.events) == 1


def test_cross_origin_mutation_is_rejected(client, engine):
    result = client.post("/api/input", json={"text": "injected"},
                         headers={"Origin": "https://example.com"})
    assert result.status_code == 403
    assert len(engine.store.events) == 1


@pytest.mark.parametrize("origin", [
    "null", "http://127.0.0.1:9999", "http://localhost:8002",
    "http://127.0.0.1:8002.evil.example", "http://user@127.0.0.1:8002",
])
def test_origin_must_match_exact_local_authority(client, origin):
    assert client.post("/api/input", json={"text": "hello"},
                       headers={"Origin": origin}).status_code == 403


def test_same_origin_browser_and_no_origin_cli_are_allowed(client):
    assert client.post("/api/input", json={"text": "browser"},
                       headers={"Origin": ORIGIN}).status_code == 202
    assert client.post("/api/input", json={"text": "local CLI"}).status_code == 202


def test_untrusted_host_and_fetch_site_are_rejected(client):
    assert client.get("/api/state", headers={"Host": "evil.example:8002"}).status_code == 403
    assert client.get("/api/state", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_oversized_body_is_rejected_before_json_parse(client, engine):
    result = client.post("/api/input", content=b"x" * (MAX_BODY_BYTES + 1),
                         headers={"Content-Type": "application/json"})
    assert result.status_code == 413
    assert len(engine.store.events) == 1


def test_chunked_body_is_bounded_without_content_length(client):
    chunks = (b"x" * 16384 for _ in range(5))
    result = client.post("/api/input", content=chunks,
                         headers={"Content-Type": "application/json"})
    assert result.status_code == 413


def test_state_and_evidence_reads(client):
    assert client.get("/api/state").json()["self_model"]["name"] == "AI-AM"
    assert client.get("/api/events").json()["events"][0]["id"] == "e1"
    assert client.get("/api/events/e1").json()["content"] == "Remember the blue door."
    assert client.get("/api/events/missing").status_code == 404
    memory = client.get("/api/memories/m1").json()
    assert memory["source_event_ids"] == ["e1"]
    assert client.get("/api/memories/missing").status_code == 404
    assert len(client.get("/api/memories", params={"query": "blue"}).json()["memories"]) == 1
    assert client.get("/api/memories", params={"query": "orange"}).json()["memories"] == []


def test_query_limits_are_validated(client):
    assert client.get("/api/events?limit=0").status_code == 422
    assert client.get("/api/events?limit=201").status_code == 422
    assert client.get("/api/memories?limit=101").status_code == 422


def test_model_changes_require_installed_model_and_known_role(client, engine):
    assert client.post("/api/model/select", json={"role": "manas", "model": "missing"}).status_code == 400
    assert client.post("/api/model/select", json={"role": "witness", "model": "local:reason"}).status_code == 422
    assert client.post("/api/model/select", json={"role": "buddhi", "model": "local:reason"}).status_code == 200
    assert engine.state["roles"]["buddhi"]["model"] == "local:reason"
    assert engine.state["roles"]["manas"]["model"] == "local:small"
    assert client.post("/api/model/select", json={"role": "all", "model": "local:reason"}).status_code == 200
    assert all(value["model"] == "local:reason" for value in engine.state["roles"].values())


def test_inventory_outage_returns_actionable_error(client, engine):
    async def unavailable():
        raise OSError("connection refused")
    engine.backend.list_models = unavailable
    result = client.get("/api/models").json()
    assert result["models"] == []
    assert "local model service" in result["error"]


def test_controls_reflection_and_consolidation(client, engine):
    assert client.post("/api/control", json={"action": "pause"}).json()["status"] == "paused"
    assert client.post("/api/control", json={"action": "resume"}).json()["status"] == "idle"
    assert client.post("/api/background", json={"enabled": True}).status_code == 200
    assert engine.state["background_enabled"] is True
    assert client.post("/api/background", json={"enabled": "true"}).status_code == 422
    reflection = client.post("/api/reflect", json={"topic": "Review yesterday's commitments."})
    assert reflection.status_code == 202
    assert reflection.json()["kind"] == "reflection"
    assert client.post("/api/reflect", json={"topic": "x" * 2001}).status_code == 422
    assert client.post("/api/consolidate", json={}).json()["deduplicated"] == 0


def test_retry_preserves_existing_event(client, engine):
    assert client.post("/api/retry", json={"event_id": "missing"}).status_code == 404
    assert client.post("/api/retry", json={"event_id": "e1"}).status_code == 400
    engine.store.events[0]["status"] = "failed"
    result = client.post("/api/retry", json={"event_id": "e1"})
    assert result.status_code == 202
    assert result.json()["id"] == "e1"
    assert len(engine.store.events) == 1


@pytest.mark.parametrize("error_type", [asyncio.QueueFull, OverflowError])
def test_queue_backpressure_is_reported(client, engine, error_type):
    async def full(text):
        raise error_type()
    engine.submit = full
    assert client.post("/api/input", json={"text": "hello"}).status_code == 429


def test_unknown_engine_error_does_not_expose_trace_or_secret(client, engine):
    async def broken(text):
        raise OSError("private-file-path-and-secret")
    engine.submit = broken
    response = client.post("/api/input", json={"text": "hello"})
    assert response.status_code == 503
    assert "private-file-path" not in response.text


def test_stop_calls_only_owned_runtime(client, engine):
    assert client.post("/api/stop", json={}).json()["identity"] == "ai-am"
    assert engine.stop_called
    assert engine.state["status"] == "stopped"


def test_websocket_restores_snapshot_and_cleans_subscription(client, engine):
    with client.websocket_connect(WS_ORIGIN + "/ws", headers={"Origin": ORIGIN}) as websocket:
        message = websocket.receive_json()
        assert message["type"] == "state"
        assert message["data"]["self_model"]["name"] == "AI-AM"
    assert len(engine.subscribers) == 0


def test_websocket_rejects_foreign_origin(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(WS_ORIGIN + "/ws", headers={"Origin": "https://evil.example"}):
            pass
    assert exc.value.code == 1008


def test_websocket_cannot_bypass_input_validation(client, engine):
    with client.websocket_connect(WS_ORIGIN + "/ws", headers={"Origin": ORIGIN}) as websocket:
        websocket.receive_json()
        websocket.send_json({"prompt": "unvalidated input"})
        with pytest.raises(WebSocketDisconnect) as exc:
            websocket.receive_json()
        assert exc.value.code == 1008
    assert len(engine.store.events) == 1


def test_runtime_task_is_owned_by_lifespan():
    engine = FakeEngine()
    with TestClient(create_app(engine=engine), base_url=ORIGIN) as client:
        assert client.get("/api/health").json()["runtime_running"] is True
    assert engine.started
    assert engine.stop_called


def test_security_headers_and_static_page(client):
    response = client.get("/api/state")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"
    page = client.get("/")
    assert page.status_code == 200
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert client.get("/ui/app.js").status_code == 200


def test_startup_waits_for_engine_readiness():
    class PreparingEngine(FakeEngine):
        async def run_forever(self):
            await asyncio.sleep(0.02)
            self.started = True
            self.ready.set()
            await self.stopped.wait()
    engine = PreparingEngine()
    with TestClient(create_app(engine=engine), base_url=ORIGIN) as client:
        assert engine.started
        assert engine.ready.is_set()
        assert client.get("/api/health").json()["runtime_running"] is True


@pytest.mark.parametrize("announce_failure", [False, True])
def test_startup_failure_prevents_serving_requests(announce_failure):
    class FailedEngine(FakeEngine):
        async def run_forever(self):
            error = RuntimeError("Another runtime owns this memory.")
            if announce_failure:
                self.startup_error = error
                self.ready.set()
            raise error
    engine = FailedEngine()
    with pytest.raises(RuntimeError, match="owns this memory"):
        with TestClient(create_app(engine=engine), base_url=ORIGIN):
            pytest.fail("A failed runtime must not start serving requests.")
    assert engine.stop_called


def test_worker_exit_before_readiness_prevents_startup():
    class ExitedEngine(FakeEngine):
        async def run_forever(self):
            return
    with pytest.raises(RuntimeError, match="before it became ready"):
        with TestClient(create_app(engine=ExitedEngine()), base_url=ORIGIN):
            pytest.fail("An exited runtime must not start serving requests.")


@pytest.mark.parametrize("path,payload", [
    ("/api/input", {"text": "Do not lose this request."}),
    ("/api/retry", {"event_id": "e1"}),
    ("/api/reflect", {"topic": "A new reflection"}),
    ("/api/consolidate", {}),
    ("/api/control", {"action": "pause"}),
    ("/api/background", {"enabled": True}),
    ("/api/model/select", {"role": "manas", "model": "local:small"}),
])
def test_dead_worker_rejects_mutations_but_preserves_read_access(path, payload):
    class FailingEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.crash = asyncio.Event()
        async def run_forever(self):
            self.started = True
            self.ready.set()
            await self.crash.wait()
            self.state["status"] = "stopped"
            raise RuntimeError("The worker encountered an error.")
    engine = FailingEngine()
    app = create_app(engine=engine)
    with TestClient(app, base_url=ORIGIN) as client:
        async def finish_worker():
            engine.crash.set()
            await asyncio.gather(app.state.runtime_task, return_exceptions=True)
        client.portal.call(finish_worker)
        assert client.post(path, json=payload).status_code == 503
        assert client.get("/api/events/e1").status_code == 200
        assert client.get("/api/health").json()["runtime_running"] is False
        assert client.post("/api/stop", json={}).status_code == 200
    assert len(engine.store.events) == 1


def test_stop_immediately_blocks_new_work_even_without_worker(client, engine):
    assert client.post("/api/stop", json={}).status_code == 200
    assert client.post("/api/control", json={"action": "pause"}).status_code == 503
    assert client.post("/api/input", json={"text": "Too late."}).status_code == 503
    assert engine.state["status"] == "stopped"
    assert len(engine.store.events) == 1

def test_websocket_repeated_disconnect_releases_all_subscriptions(client, engine):
    for _ in range(25):
        with client.websocket_connect(WS_ORIGIN + "/ws", headers={"Origin": ORIGIN}) as websocket:
            assert websocket.receive_json()["type"] == "state"
        assert not engine.subscribers

def test_websocket_immediate_disconnect_releases_subscription(client, engine):
    for _ in range(10):
        with client.websocket_connect(WS_ORIGIN + "/ws", headers={"Origin": ORIGIN}):
            pass
        assert not engine.subscribers
