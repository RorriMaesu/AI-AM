"""Local-only inference with bounded transport and schema validation."""
import copy
import json
from urllib.parse import urlsplit, urlunsplit
import httpx
from pydantic import ValidationError
from core.config import validate_api_url, validate_model_name
from core.contracts import ROLE_PROMPTS, ROLE_SCHEMAS

MAX_RESPONSE_BYTES = 128 * 1024
MAX_PAYLOAD_CHARS = 100_000


def _context_payload(payload, num_ctx):
    """Bound redundant context while preserving the current event and source IDs."""
    prepared = copy.deepcopy(payload)
    workspace = prepared.get("workspace")
    if isinstance(workspace, dict):
        workspace.pop("memories", None)  # Already supplied as top-level evidence.
    budget = min(MAX_PAYLOAD_CHARS, int(num_ctx) * 3)
    serialized = json.dumps(prepared, ensure_ascii=False, allow_nan=False)
    if len(serialized) <= budget:
        return serialized
    history = prepared.get("history")
    if isinstance(history, list):
        prepared["history"] = history[-4:]

    def compact(value, key=""):
        if key == "id" or key.endswith("_id") or key.endswith("_ids"):
            return value
        if isinstance(value, str):
            return value if len(value) <= 1000 else value[:960] + " [earlier proposal abridged]"
        if isinstance(value, list):
            return [compact(item) for item in value[:8]]
        if isinstance(value, dict):
            return {name: compact(item, name) for name, item in value.items()}
        return value

    if isinstance(workspace, dict):
        prepared["workspace"] = compact(workspace)
    serialized = json.dumps(prepared, ensure_ascii=False, allow_nan=False)
    if len(serialized) > budget:
        raise InferenceError("Cognitive context budget exceeded. Shorten the input or increase num_ctx in settings.")
    return serialized

class InferenceError(RuntimeError):
    """A model call failed; callers must preserve the pending interaction."""

class OllamaBackend:
    def __init__(self, config, client=None):
        self.config = config
        self.api_url = validate_api_url(config["api_url"])
        parts = urlsplit(self.api_url)
        self.base_url = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        self._owned_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.get("request_timeout", 60)),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def _json_request(self, method, url, payload=None):
        try:
            async with self.client.stream(
                method, url, json=payload,
                timeout=self.config.get("request_timeout", 60),
                follow_redirects=False,
            ) as response:
                if response.status_code >= 300:
                    raise InferenceError(f"Local Ollama returned HTTP {response.status_code}.")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise InferenceError("Local Ollama response exceeded the size limit.")
                    raw.extend(chunk)
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise InferenceError("Local Ollama returned invalid response JSON.") from exc
                if not isinstance(data, dict):
                    raise InferenceError("Local Ollama returned an unexpected response shape.")
                return data
        except httpx.HTTPError as exc:
            raise InferenceError("Cannot reach local Ollama. Check that Ollama is running and the model is installed.") from exc

    async def infer(self, role, payload):
        if role not in ROLE_SCHEMAS:
            raise ValueError("Unknown cognitive role.")
        schema = ROLE_SCHEMAS[role]
        model = validate_model_name(self.config["roles"][role]["model"])
        serialized = _context_payload(payload, self.config.get("num_ctx", 8192))
        output_schema = schema.model_json_schema()
        # The format parameter constrains decoding. The prompt also needs to
        # communicate the fields and their meaning to the model.
        system_prompt = ROLE_PROMPTS[role] + "\nOutput JSON schema:\n" + json.dumps(output_schema, separators=(",", ":"))
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": serialized},
        ]
        token_count = 0
        for attempt in range(2):
            # A conservative character estimate, including schema and repair
            # context, with space reserved for output and chat framing.
            budget = min(MAX_PAYLOAD_CHARS, max(0, (
                self.config.get("num_ctx", 8192) - self.config.get("max_output_tokens", 600) - 256
            ) * 3))
            if sum(len(message["content"]) for message in messages) > budget:
                raise InferenceError("Complete cognitive context budget exceeded, including schema or repair. Increase num_ctx or shorten the input.")
            # A harmless-looking local alias can point to an Ollama cloud model.
            # Check server metadata before every generation, not just UI selection.
            installed = await self.list_models()
            if model not in installed and model + ":latest" not in installed:
                raise InferenceError(f"The model '{model}' is not installed as a local model. Select an installed local tag.")
            request = {
                "model": model,
                "messages": messages,
                "stream": False,
                "format": output_schema,
                "options": {
                    "temperature": self.config["roles"][role]["temperature"],
                    "num_ctx": self.config.get("num_ctx", 8192),
                    "num_predict": self.config.get("max_output_tokens", 600),
                },
                "keep_alive": "5m",
            }
            data = await self._json_request("POST", self.api_url, request)
            message = data.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or len(content) > 32000:
                raise InferenceError("Local Ollama returned missing or oversized message content.")
            usage = data.get("eval_count")
            token_count += usage if type(usage) is int and usage >= 0 else len(content.split())
            try:
                parsed = json.loads(content)
                result = schema.model_validate(parsed)
                return result.model_dump(), token_count
            except (json.JSONDecodeError, ValidationError, TypeError):
                if attempt:
                    raise InferenceError(f"{role.title()} returned invalid structured output after one repair attempt.")
                # The previous output remains assistant data; the only new instruction is a schema repair.
                messages = messages + [
                    {"role": "assistant", "content": content[:8192]},
                    {"role": "user", "content": (
                        "Your last output did not satisfy the supplied JSON schema. "
                        "Return one complete JSON object using exactly the schema fields and types. "
                        "Do not use markdown fences, unknown fields, or invented evidence IDs."
                    )},
                ]
        raise InferenceError("Unreachable inference state.")

    async def list_models(self):
        data = await self._json_request("GET", self.base_url + "/api/tags")
        models = []
        raw_models = data.get("models", [])
        if not isinstance(raw_models, list):
            raise InferenceError("Local Ollama returned an invalid model inventory.")
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            # Recent Ollama releases expose cloud-backed tags through these fields.
            if item.get("remote_host") or item.get("remote_model"):
                continue
            try:
                models.append(validate_model_name(item.get("name")))
            except ValueError:
                continue
        return sorted(set(models))

    async def aclose(self):
        if self._owned_client:
            await self.client.aclose()


class DemoBackend:
    """A deterministic demonstration of orchestration, never represented as LLM inference."""
    def __init__(self, config):
        self.config = config

    async def list_models(self):
        return ["demo"]

    async def aclose(self):
        return None

    async def infer(self, role, payload):
        event = payload.get("current_event") or {}
        content = str(event.get("content", ""))[:1600]
        event_id = event.get("id")
        memories = payload.get("memories", [])
        if not isinstance(memories, list):
            memories = []
        relevant = [memory for memory in memories if isinstance(memory, dict)][:6]
        memory_ids = [m["id"] for m in relevant if isinstance(m.get("id"), str)]
        if role == "manas":
            result = {
                "observations": [f"The {event.get('kind', 'user')} event says: {content}"],
                "interpretations": ["This demonstration treats the supplied text as a statement to remember."],
                "uncertainties": ["No language model is connected in demonstration mode."],
                "retrieval_query": content,
                "salience": 0.5,
            }
        elif role == "chitta":
            result = {
                "selected_memory_ids": memory_ids,
                "associations": [f"Retrieved record: {str(m.get('content', ''))[:300]}" for m in relevant],
                "contradictions": [],
                "missing_context": [] if relevant else ["There is no matching earlier record."],
            }
        elif role == "ahamkara":
            result = {
                "self_relevance": "This interaction belongs to AI-AM's demonstration history.",
                "agency": "The incoming statement belongs to its source; the system owns this generated response.",
                "capability_limits": ["Responses are scripted demonstrations, not local LLM reasoning."],
                "commitments": [],
            }
        elif role == "buddhi":
            recalled = "\n".join(f"- {str(m.get('content', ''))[:300]}" for m in relevant)
            reply = "Demonstration mode: the four functions processed your message and preserved its source."
            if recalled:
                reply += "\n\nRelated records:\n" + recalled
            else:
                reply += "\n\nNo related earlier memory was retrieved."
            reply += "\n\nConnect an installed local Ollama model to enable generated conversation."
            proposal = []
            if event_id and content:
                proposal = [{
                    "content": content,
                    "kind": "user_statement" if event.get("kind") == "user" else "reflection",
                    "source_event_ids": [event_id],
                    "confidence": 1.0 if event.get("kind") == "user" else 0.5,
                    "supersedes": None,
                }]
            result = {
                "action": "reply", "reply": reply, "query": "", "expression": "",
                "evidence_ids": ([event_id] if event_id else []) + memory_ids,
                "uncertainty": 1.0,
                "rationale": "A scripted demonstration of source-preserving orchestration.",
                "memory_updates": proposal,
                "goal_updates": [],
            }
        else:
            raise ValueError("Unknown cognitive role.")
        parsed = ROLE_SCHEMAS[role].model_validate(result).model_dump()
        return parsed, 0
