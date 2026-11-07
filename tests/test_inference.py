import asyncio
import json
import httpx
import pytest
from pydantic import ValidationError
from core.contracts import ROLE_SCHEMAS, ManasResult, MemoryProposal
from core.inference import DemoBackend, InferenceError, OllamaBackend

def settings():
    return {
        "api_url": "http://127.0.0.1:11434/api/chat",
        "request_timeout": 2,
        "num_ctx": 8192,
        "max_output_tokens": 600,
        "roles": {role: {"model": "qwen3:8b", "temperature": 0.2} for role in ROLE_SCHEMAS},
    }

def model_transport(handler):
    def local_inventory(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        return handler(request)
    return httpx.MockTransport(local_inventory)

def test_native_api_schema_and_context_are_sent():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "message": {"content": '{"observations":["A message arrived."],"salience":0.7}'},
            "eval_count": 12,
        })
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler)) as client:
            result, tokens = await OllamaBackend(settings(), client).infer("manas", {"current_event": {"content": "Hello"}})
            assert result["observations"] == ["A message arrived."]
            assert tokens == 12
    asyncio.run(run())
    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/api/chat"
    assert body["options"] == {"temperature": 0.2, "num_ctx": 8192, "num_predict": 600}
    assert body["format"]["additionalProperties"] is False
    assert body["stream"] is False
    assert json.dumps(body["format"], separators=(",", ":")) in body["messages"][0]["content"]

def test_bad_schema_has_only_one_repair_and_counts_both_calls():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        content = '{"salience":7}' if len(calls) == 1 else '{"salience":0.5}'
        return httpx.Response(200, json={"message": {"content": content}, "eval_count": 10})
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler)) as client:
            result, tokens = await OllamaBackend(settings(), client).infer("manas", {})
            assert result["salience"] == 0.5
            assert tokens == 20
    asyncio.run(run())
    assert len(calls) == 2
    assert len(calls[1]["messages"]) == 4

def test_invalid_output_is_never_reported_as_success():
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json={"message": {"content": "not JSON"}})
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler)) as client:
            with pytest.raises(InferenceError, match="one repair attempt"):
                await OllamaBackend(settings(), client).infer("buddhi", {})
    asyncio.run(run())
    assert count == 2

def test_http_failure_is_not_retried_as_schema_repair():
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(404, json={"error": "model missing"})
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler)) as client:
            with pytest.raises(InferenceError, match="HTTP 404"):
                await OllamaBackend(settings(), client).infer("manas", {})
    asyncio.run(run())
    assert count == 1

def test_redirect_is_rejected_without_following():
    urls = []
    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://remote.example/api/chat"})
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler), follow_redirects=True) as client:
            with pytest.raises(InferenceError, match="HTTP 307"):
                await OllamaBackend(settings(), client).infer("manas", {})
    asyncio.run(run())
    assert urls == ["http://127.0.0.1:11434/api/chat"]

def test_response_size_is_bounded():
    def handler(request):
        return httpx.Response(200, json={"message": {"content": "x" * 140000}})
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler)) as client:
            with pytest.raises(InferenceError, match="size limit"):
                await OllamaBackend(settings(), client).infer("manas", {})
    asyncio.run(run())

def test_model_inventory_filters_cloud_and_bad_names():
    def handler(request):
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [
            {"name": "qwen3:8b"}, {"name": "qwen3:8b"},
            {"name": "gemma4:cloud"},
            {"name": "remote-alias:latest", "remote_host": "https://ollama.com"},
            {"name": "bad name"}, {"name": "gemma4:e4b"},
        ]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await OllamaBackend(settings(), client).list_models() == ["gemma4:e4b", "qwen3:8b"]
    asyncio.run(run())

def test_cloud_tag_cannot_be_dispatched():
    config = settings()
    config["roles"]["buddhi"]["model"] = "gemma4:cloud"
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: pytest.fail("No request expected"))) as client:
            with pytest.raises(ValueError, match="Cloud"):
                await OllamaBackend(config, client).infer("buddhi", {})
    asyncio.run(run())

def test_contracts_reject_extra_fields_coercion_and_unbounded_lists():
    with pytest.raises(ValidationError):
        ManasResult.model_validate({"salience": "0.5"})
    with pytest.raises(ValidationError):
        ManasResult.model_validate({"invented_identity": "human"})
    with pytest.raises(ValidationError):
        ManasResult.model_validate({"observations": ["x"] * 13})
    with pytest.raises(ValidationError):
        MemoryProposal.model_validate({"content": " ", "kind": "observation", "source_event_ids": ["e1"]})

def test_demo_is_explicit_and_preserves_source_ids():
    async def run():
        backend = DemoBackend(settings())
        payload = {"current_event": {"id": "event-1", "kind": "user", "content": "I prefer short replies."}, "memories": []}
        for role in ROLE_SCHEMAS:
            result, tokens = await backend.infer(role, payload)
            ROLE_SCHEMAS[role].model_validate(result)
            assert tokens == 0
        result, _ = await backend.infer("buddhi", payload)
        assert "Demonstration mode" in result["reply"]
        assert result["memory_updates"][0]["source_event_ids"] == ["event-1"]
        assert result["memory_updates"][0]["kind"] == "user_statement"
        assert result["evidence_ids"] == ["event-1"]
        assert await backend.list_models() == ["demo"]
    asyncio.run(run())


def test_oversized_current_event_is_never_silently_truncated_or_sent():
    config = settings()
    config["num_ctx"] = 1024
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: pytest.fail("No request expected"))) as client:
            with pytest.raises(InferenceError, match="context budget"):
                await OllamaBackend(config, client).infer("manas", {"current_event": {"content": "x" * 8000}})
    asyncio.run(run())

def test_duplicate_workspace_memories_are_removed_without_mutating_source():
    sent = []
    payload = {
        "current_event": {"id": "e1", "content": "Hello"},
        "memories": [{"id": "m1", "content": "A memory.", "source_event_ids": ["e0"]}],
        "workspace": {"memories": [{"id": "m1", "content": "A memory.", "source_event_ids": ["e0"]}]},
    }
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "{}"}})
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler)) as client:
            await OllamaBackend(settings(), client).infer("chitta", payload)
    asyncio.run(run())
    actual = json.loads(sent[0]["messages"][1]["content"])
    assert actual["memories"][0]["source_event_ids"] == ["e0"]
    assert "memories" not in actual["workspace"]
    assert "memories" in payload["workspace"]

def test_cloud_backed_alias_is_rejected_before_generation():
    requests = []
    def handler(request):
        requests.append(request.url.path)
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [
            {"name": "qwen3:8b", "remote_host": "https://ollama.com", "remote_model": "remote"}
        ]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(InferenceError, match="not installed as a local"):
                await OllamaBackend(settings(), client).infer("manas", {})
    asyncio.run(run())
    assert requests == ["/api/tags"]

def test_unqualified_local_tag_resolves_to_latest_inventory():
    config = settings()
    config["roles"]["manas"]["model"] = "local-model"
    requests = []
    def handler(request):
        requests.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "local-model:latest"}]})
        return httpx.Response(200, json={"message": {"content": "{}"}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await OllamaBackend(config, client).infer("manas", {})
    asyncio.run(run())
    assert requests == ["/api/tags", "/api/chat"]

def test_schema_repair_cannot_overfill_complete_context():
    config = settings()
    config["num_ctx"] = 4096
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"message": {"content": "x" * 10000}})
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler)) as client:
            with pytest.raises(InferenceError, match="Complete cognitive context budget"):
                await OllamaBackend(config, client).infer("manas", {})
    asyncio.run(run())
    assert calls == ["/api/chat"]

def test_remote_alias_change_is_checked_between_calls():
    inventory_count = 0
    generations = 0
    def handler(request):
        nonlocal inventory_count, generations
        if request.url.path == "/api/tags":
            inventory_count += 1
            model = {"name": "qwen3:8b"}
            if inventory_count > 1:
                model["remote_model"] = "remote"
            return httpx.Response(200, json={"models": [model]})
        generations += 1
        return httpx.Response(200, json={"message": {"content": "{}"}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = OllamaBackend(settings(), client)
            await backend.infer("manas", {})
            with pytest.raises(InferenceError, match="not installed as a local"):
                await backend.infer("chitta", {})
    asyncio.run(run())
    assert inventory_count == 2
    assert generations == 1
