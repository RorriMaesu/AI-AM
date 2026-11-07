import asyncio
import json
import httpx
import pytest
from pydantic import ValidationError
from core.contracts import ROLE_SCHEMAS, ManasResult, MemoryProposal, BuddhiResult
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

@pytest.mark.parametrize("output", [
    {"action": "reply", "reply": "   "},
    {"action": "calculate", "query": "17*23"},
    {"action": "retrieve"},
    {"action": "reconsider", "query": "  "},
])
def test_decision_requires_payload_for_selected_action(output):
    with pytest.raises(ValidationError):
        BuddhiResult.model_validate(output)

def test_calculator_missing_expression_is_repaired_before_dispatch():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        content = {"action": "calculate", "query": "17*23"} if len(calls) == 1 else {"action": "calculate", "expression": "17*23"}
        return httpx.Response(200, json={"message": {"content": json.dumps(content)}})
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler)) as client:
            result, _ = await OllamaBackend(settings(), client).infer("buddhi", {})
            assert result["expression"] == "17*23"
    asyncio.run(run())
    assert len(calls) == 2
    assert "requires the arithmetic in expression" in calls[1]["messages"][-1]["content"]

def test_output_limit_failure_has_actionable_error():
    def handler(request):
        return httpx.Response(200, json={"message": {"content": '{"action":'}, "done_reason": "length"})
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler)) as client:
            with pytest.raises(InferenceError, match="exhausted max_output_tokens"):
                await OllamaBackend(settings(), client).infer("buddhi", {})
    asyncio.run(run())

def test_loading_timeout_is_not_reported_as_unreachable_server():
    def handler(request):
        raise httpx.ReadTimeout("still loading", request=request)
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler)) as client:
            with pytest.raises(InferenceError, match="loading or generating"):
                await OllamaBackend(settings(), client).infer("manas", {})
    asyncio.run(run())

def test_chitta_repairs_invented_id_using_episode_enum():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        chosen = "invented" if len(calls) == 1 else "memory-actual"
        return httpx.Response(200, json={"message": {"content": json.dumps({"selected_memory_ids": [chosen]})}})
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler)) as client:
            result, _ = await OllamaBackend(settings(), client).infer("chitta", {
                "memories": [{"id": "memory-actual", "source_event_ids": ["source-actual"]}]})
            assert result["selected_memory_ids"] == ["memory-actual"]
    asyncio.run(run())
    assert len(calls) == 2
    assert calls[0]["format"]["properties"]["selected_memory_ids"]["items"]["enum"] == ["memory-actual"]
    assert "retrieved memory IDs" in calls[1]["messages"][-1]["content"]

def test_no_retrieved_memory_requires_empty_selection():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": '{"selected_memory_ids":[]}'}})
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler)) as client:
            await OllamaBackend(settings(), client).infer("chitta", {"memories": []})
    asyncio.run(run())
    assert calls[0]["format"]["properties"]["selected_memory_ids"]["maxItems"] == 0

def test_buddhi_schema_distinguishes_memory_ids_and_event_sources():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": '{"reply":"Known.","evidence_ids":["m1"]}'}})
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler)) as client:
            await OllamaBackend(settings(), client).infer("buddhi", {
                "current_event": {"id": "current"}, "history": [{"id": "past"}],
                "memories": [{"id": "m1", "source_event_ids": ["source"]}],
                "workspace": {"tool_results": [{"event_id": "tool"}]}})
    asyncio.run(run())
    schema = calls[0]["format"]
    assert set(schema["properties"]["evidence_ids"]["items"]["enum"]) == {"m1","current","past","source","tool"}
    proposal = schema["$defs"]["MemoryProposal"]["properties"]
    assert set(proposal["source_event_ids"]["items"]["enum"]) == {"current","past","source","tool"}
    assert any(choice.get("enum") == ["m1"] for choice in proposal["supersedes"]["anyOf"])

def test_unrepaired_fabricated_evidence_is_not_accepted():
    def handler(request):
        return httpx.Response(200, json={"message": {"content": '{"reply":"Claim.","evidence_ids":["fake"]}'}})
    async def run():
        async with httpx.AsyncClient(transport=model_transport(handler)) as client:
            with pytest.raises(InferenceError, match="one repair attempt"):
                await OllamaBackend(settings(), client).infer("buddhi", {"current_event": {"id": "real"}})
    asyncio.run(run())
