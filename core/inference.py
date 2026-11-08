"""Local-only inference with bounded transport and schema validation."""
import copy
import json
from urllib.parse import urlsplit, urlunsplit
import httpx
from pydantic import ValidationError
from core.config import validate_api_url, validate_model_name
from core.contracts import ENVIRONMENT_ACTIONS, ROLE_PROMPTS, ROLE_SCHEMAS

MAX_RESPONSE_BYTES = 128 * 1024
MAX_PAYLOAD_CHARS = 100_000


def _environment_allowed(payload):
    rules = payload.get("rules") or {}
    current = payload.get("current_event") or {}
    return (current.get("kind") == "experiment"
            and rules.get("environment_enabled") is True
            and "environment" in rules.get("available_actions", []))


def _observed_targets(payload):
    observation = (payload.get("experiment") or {}).get("observation") or {}
    visible = {item["id"] for item in observation.get("objects", [])
               if isinstance(item, dict) and isinstance(item.get("id"), str)}
    inventory = {item for item in observation.get("inventory", []) if isinstance(item, str)}
    return visible, inventory


def _attention_ids(payload):
    attention = (payload.get("workspace") or {}).get("attention") or {}
    return {value for value in attention.get("selected_ids", []) if isinstance(value, str)}


def _commitment_ids(payload):
    records = (payload.get("self_model") or {}).get("commitment_records") or []
    return {record["id"] for record in records
            if isinstance(record, dict) and isinstance(record.get("id"), str)}


def _action_evidence(payload):
    evidence, events = set(), set()
    for record in payload.get("action_ledger") or []:
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("id"), str):
            evidence.add(record["id"])
        for key in ("event_id", "result_event_id", "tool_event_id"):
            if isinstance(record.get(key), str):
                events.add(record[key])
        events.update(value for value in record.get("source_event_ids", [])
                      if isinstance(value, str))
    rules = payload.get("rules") or {}
    evidence.update(value for value in rules.get("evidence_ids", []) if isinstance(value, str))
    events.update(value for value in rules.get("event_ids", []) if isinstance(value, str))
    return evidence, events



def _context_payload(payload, num_ctx, max_chars=None, compact_level=0):
    """Fit context without shortening current input or current sensory evidence.

    Model-written proposals may be abridged; source records are kept complete
    or omitted explicitly. Ledger order is newest first, chat history oldest first.
    """
    prepared = copy.deepcopy(payload)
    private_fields = {"ground_truth", "evaluator", "world_state", "private_state",
                      "world", "evaluator_only", "controls", "walls"}
    def contains_private(value):
        if isinstance(value, dict):
            return bool(private_fields.intersection(value)) or any(contains_private(v) for v in value.values())
        if isinstance(value, list):
            return any(contains_private(v) for v in value)
        return False
    if contains_private(prepared.get("experiment")):
        raise InferenceError("Evaluator world state must never enter a cognitive payload.")
    workspace = prepared.get("workspace")
    if isinstance(workspace, dict):
        workspace.pop("memories", None)
        manas = workspace.get("manas") or {}
        for key in ("observations", "interpretations", "uncertainties"):
            if isinstance(manas, dict) and workspace.get(key) == manas.get(key):
                workspace.pop(key, None)
        attention = workspace.get("attention")
        if isinstance(attention, dict):
            selected = set(attention.get("selected_ids") or [])
            if isinstance(attention.get("items"), list):
                attention["items"] = [item for item in attention["items"]
                                      if isinstance(item, dict) and item.get("id") in selected]
            if isinstance(attention.get("missed"), list):
                attention["missed"] = attention["missed"][:4]
            if isinstance(attention.get("broadcasts"), list):
                attention["broadcasts"] = attention["broadcasts"][-4:]
    ledger = prepared.get("action_ledger")
    if isinstance(ledger, list) and len(ledger) > 12:
        prepared["action_ledger"] = ledger[:12]
        prepared["action_ledger_omitted"] = len(ledger) - 12
        rules = prepared.get("rules")
        if isinstance(rules, dict):
            rules.pop("event_ids", None)
            rules.pop("evidence_ids", None)
    budget = min(MAX_PAYLOAD_CHARS, int(num_ctx) * 3)
    if max_chars is not None:
        budget = min(budget, max(0, int(max_chars)))

    def serialized():
        return json.dumps(prepared, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    def proposals(value, limit, count, key=""):
        if key == "id" or key.endswith("_id") or key.endswith("_ids"):
            return value
        if isinstance(value, str):
            return value if len(value) <= limit else value[:limit] + " [proposal abridged]"
        if isinstance(value, list):
            return [proposals(item, limit, count) for item in value[:count]]
        if isinstance(value, dict):
            return {name: proposals(item, limit, count, name) for name, item in value.items()}
        return value

    def shrink(level):
        nonlocal workspace
        text_limit, list_limit = ((400, 4) if level == 1 else (220, 3) if level == 2 else (140, 2))
        workspace = prepared.get("workspace")
        if isinstance(workspace, dict):
            # These fields are generated assessments, not immutable evidence.
            for name in ("manas", "chitta", "self_assessment", "attention_probe"):
                if isinstance(workspace.get(name), dict):
                    workspace[name] = proposals(workspace[name], text_limit, list_limit)
            for name in ("observations", "interpretations", "uncertainties"):
                if name in workspace:
                    workspace[name] = proposals(workspace[name], text_limit, list_limit)
            if isinstance(workspace.get("decision"), dict):
                workspace["decision"] = {key: value for key, value in workspace["decision"].items()
                                         if key in {"action", "query", "expression", "environment_action",
                                                    "target", "prediction", "prediction_confidence", "evidence_ids"}}
            attention = workspace.get("attention")
            if isinstance(attention, dict):
                attention.pop("broadcasts", None)
                attention.pop("items", None)  # Selected contents are supplied once in focus.
                if isinstance(attention.get("missed"), list):
                    attention["missed"] = proposals(attention["missed"], text_limit, list_limit)
        focus = prepared.get("focus")
        if isinstance(focus, list):
            prepared["focus"] = [proposals({k: v for k, v in item.items() if k != "score_components"},
                                           text_limit, list_limit)
                                 if isinstance(item, dict) else item for item in focus]
        experiment = prepared.get("experiment")
        if isinstance(experiment, dict):
            for key in ("history", "recent_transitions"):
                if isinstance(experiment.get(key), list):
                    experiment[key + "_omitted"] = len(experiment[key])
                    experiment.pop(key)
            if not _environment_allowed(prepared):
                experiment.pop("action_predictions", None)
            if isinstance(experiment.get("beliefs"), list):
                keep = 8 if level == 1 else 4
                if len(experiment["beliefs"]) > keep:
                    experiment["beliefs_omitted"] = len(experiment["beliefs"]) - keep
                    experiment["beliefs"] = experiment["beliefs"][:keep]
        ledger = prepared.get("action_ledger")
        if isinstance(ledger, list):
            trimmed = []
            for record in ledger:
                if not isinstance(record, dict):
                    continue
                record = copy.deepcopy(record)
                record.pop("transitions", None)
                record.pop("history", None)
                for key in ("result", "prediction"):
                    if isinstance(record.get(key), dict):
                        record[key] = {name: value for name, value in record[key].items()
                                       if name not in {"observation", "visible_cells", "beliefs", "history"}}
                trimmed.append(record)
            keep = 6 if level == 1 else 4 if level == 2 else 2
            prepared["action_ledger"] = trimmed[:keep]
            if len(trimmed) > keep:
                prepared["action_ledger_omitted"] = prepared.get("action_ledger_omitted", 0) + len(trimmed) - keep
        history = prepared.get("history")
        if isinstance(history, list):
            keep = 6 if level == 1 else 4 if level == 2 else 2
            prepared["history"] = history[-keep:]
        memories = prepared.get("memories")
        if isinstance(memories, list):
            keep = 6 if level == 1 else 4 if level == 2 else 2
            preferred = set((prepared.get("workspace") or {}).get("chitta", {}).get("selected_memory_ids", []))
            ordered = [m for m in memories if isinstance(m, dict) and m.get("id") in preferred]
            ordered.extend(m for m in memories if not isinstance(m, dict) or m.get("id") not in preferred)
            prepared["memories"] = ordered[:keep]
            if len(memories) > keep:
                prepared["memories_omitted"] = len(memories) - keep
            kept_ids = {m["id"] for m in prepared["memories"] if isinstance(m, dict) and "id" in m}
            chitta = (prepared.get("workspace") or {}).get("chitta")
            if isinstance(chitta, dict):
                chitta["selected_memory_ids"] = [i for i in chitta.get("selected_memory_ids", []) if i in kept_ids]
        self_model = prepared.get("self_model")
        if isinstance(self_model, dict) and self_model.get("commitment_records"):
            self_model.pop("commitments", None)  # Legacy prose duplicates the stable records.
        rules = prepared.get("rules")
        if isinstance(rules, dict):
            # Enums are recomputed from records actually present.
            # Avoid repeating and authorizing omitted IDs.
            rules.pop("evidence_ids", None)
            rules.pop("event_ids", None)

    if compact_level:
        shrink(compact_level)
    result = serialized()
    if len(result) <= budget:
        return result
    for level in range(max(1, compact_level + 1), 4):
        shrink(level)
        result = serialized()
        if len(result) <= budget:
            return result
    raise InferenceError("Cognitive context budget exceeded. Shorten the input or increase num_ctx in settings.")


def _episode_schema(role, schema, payload):
    """Constrain record choices to the actual evidence in this handoff."""
    result = copy.deepcopy(schema)
    memories = payload.get("memories") or []
    history = payload.get("history") or []
    workspace = payload.get("workspace") or {}
    memory_ids = {m["id"] for m in memories if isinstance(m, dict) and isinstance(m.get("id"), str)}
    event_ids = {e["id"] for e in history if isinstance(e, dict) and isinstance(e.get("id"), str)}
    action_evidence, action_events = _action_evidence(payload)
    event_ids.update(action_events)
    current = payload.get("current_event") or {}
    if isinstance(current.get("id"), str):
        event_ids.add(current["id"])
    for memory in memories:
        if isinstance(memory, dict):
            event_ids.update(i for i in memory.get("source_event_ids", []) if isinstance(i, str))
    for tool in workspace.get("tool_results", []):
        if isinstance(tool, dict) and isinstance(tool.get("event_id"), str):
            event_ids.add(tool["event_id"])

    def restrict(field, values):
        if values:
            field["items"]["enum"] = sorted(values)
        else:
            field["maxItems"] = 0

    if role == "chitta":
        restrict(result["properties"]["selected_memory_ids"], memory_ids)
    elif role == "ahamkara":
        restrict(result["properties"]["attended_ids"], _attention_ids(payload))
    elif role == "buddhi":
        # Optional JSON properties let some local models omit action operands
        # even after repair. Require a complete decision at decoding time.
        result["required"] = list(result["properties"])
        restrict(result["properties"]["evidence_ids"], memory_ids | event_ids | action_evidence)
        proposal = result["$defs"]["MemoryProposal"]["properties"]
        restrict(proposal["source_event_ids"], event_ids)
        if memory_ids:
            for choice in proposal["supersedes"]["anyOf"]:
                if choice.get("type") == "string":
                    choice["enum"] = sorted(memory_ids)
        else:
            proposal["supersedes"] = {"type": "null", "default": None}
        actions = ["reply", "retrieve", "reconsider", "calculate"]
        if _environment_allowed(payload):
            actions.append("environment")
            visible, inventory = _observed_targets(payload)
            result["properties"]["target"]["enum"] = [""] + sorted(visible | inventory)
            observation = (payload.get("experiment") or {}).get("observation") or {}
            available = observation.get("actions", ENVIRONMENT_ACTIONS)
            result["properties"]["environment_action"]["enum"] = [""] + [
                action for action in ENVIRONMENT_ACTIONS if action in available]
        result["properties"]["action"]["enum"] = actions
        if not _environment_allowed(payload):
            for name in ("environment_action", "target", "prediction"):
                result["properties"][name]["enum"] = [""]
        commitment = result["$defs"]["CommitmentProposal"]["properties"]
        restrict(commitment["source_event_ids"], event_ids)
        existing = _commitment_ids(payload)
        if existing:
            for choice in commitment["id"]["anyOf"]:
                if choice.get("type") == "string":
                    choice["enum"] = sorted(existing)
        else:
            commitment["id"] = {"type": "null", "default": None}
            commitment["operation"]["enum"] = ["create"]
    return result, memory_ids, event_ids


def _validate_episode_ids(role, result, memory_ids, event_ids, payload):
    if role == "chitta" and not set(result.selected_memory_ids).issubset(memory_ids):
        raise ValueError("selected_memory_ids must use only the supplied retrieved memory IDs")
    if role == "ahamkara" and not set(result.attended_ids).issubset(_attention_ids(payload)):
        raise ValueError("attended_ids must name selected items in the supplied attention workspace")
    if role == "buddhi":
        action_evidence, _ = _action_evidence(payload)
        if result.action == "environment" and not _environment_allowed(payload):
            raise ValueError("Environment actions are available only in an explicitly authorized experiment event")
        if result.action == "environment":
            visible, inventory = _observed_targets(payload)
            if result.environment_action in ("inspect", "pick_up") and result.target not in visible:
                raise ValueError("Environment target must be a currently observed object ID; look for it first")
            if result.environment_action == "drop" and result.target not in inventory:
                raise ValueError("Only an object in the supplied inventory can be dropped")
            observation = (payload.get("experiment") or {}).get("observation") or {}
            if result.environment_action not in observation.get("actions", ENVIRONMENT_ACTIONS):
                raise ValueError("Environment action is not available in the current observation")
        if not set(result.evidence_ids).issubset(memory_ids | event_ids | action_evidence):
            raise ValueError("evidence_ids must use only the supplied event or memory IDs")
        for change in result.commitment_updates:
            current = payload.get("current_event") or {}
            if change.operation in ("create", "update") and (
                current.get("kind") != "user" or current.get("id") not in change.source_event_ids
            ):
                raise ValueError("Creating or updating a commitment requires the current user event as evidence")
            if not set(change.source_event_ids).issubset(event_ids):
                raise ValueError("commitment_updates.source_event_ids must cite supplied source events")
            if change.operation != "create" and change.id not in _commitment_ids(payload):
                raise ValueError("commitment_updates must use an existing stable commitment ID")
        for proposal in result.memory_updates:
            if not proposal.source_event_ids or not set(proposal.source_event_ids).issubset(event_ids):
                raise ValueError("memory_updates.source_event_ids must cite supplied source events")
            if proposal.supersedes and proposal.supersedes not in memory_ids:
                raise ValueError("memory_updates.supersedes must identify a retrieved memory")
            if proposal.supersedes:
                current = payload.get("current_event") or {}
                if current.get("kind") != "user" or current.get("id") not in proposal.source_event_ids:
                    raise ValueError("A correction requires the current user event as its source; omit unsupported memory updates.")
                target = next(m for m in payload["memories"] if m["id"] == proposal.supersedes)
                if target.get("kind") in ("user_statement", "observation") and proposal.kind not in ("user_statement", "observation"):
                    raise ValueError("Inferences, reflections and commitments cannot replace raw evidence. Remove that update. Recall alone needs no memory update.")

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
            timeout=httpx.Timeout(config.get("request_timeout", 180)),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def _json_request(self, method, url, payload=None):
        try:
            async with self.client.stream(
                method, url, json=payload,
                timeout=self.config.get("request_timeout", 180),
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
        except httpx.TimeoutException as exc:
            raise InferenceError("Local Ollama exceeded the request time limit while loading or generating. Increase request_timeout if needed; the input remains retryable.") from exc
        except httpx.HTTPError as exc:
            raise InferenceError("Cannot reach local Ollama. Check that Ollama is running and the model is installed.") from exc

    def _role_prompt(self, role, supplied):
        prompt = ROLE_PROMPTS[role]
        if role == "buddhi" and not _environment_allowed(supplied):
            begin = prompt.find("Use action=environment only")
            finish = prompt.find("commitment_updates require", begin)
            if begin >= 0 and finish >= 0:
                prompt = prompt[:begin] + "Environment actions are not authorized in this conversation.\n" + prompt[finish:]
        return prompt

    def _episode_schema(self, role, schema, payload):
        return _episode_schema(role, schema, payload)

    def _validate_episode_ids(self, role, result, memory_ids, event_ids, payload):
        _validate_episode_ids(role, result, memory_ids, event_ids, payload)

    async def infer(self, role, payload):
        if role not in ROLE_SCHEMAS:
            raise ValueError("Unknown cognitive role.")
        schema = ROLE_SCHEMAS[role]
        model = validate_model_name(self.config["roles"][role]["model"])
        total_budget = min(MAX_PAYLOAD_CHARS, max(0, (
            self.config.get("num_ctx", 8192) - self.config.get("max_output_tokens", 1600) - 256
        ) * 3))
        for level in range(4):
            try:
                serialized = _context_payload(payload, self.config.get("num_ctx", 8192), compact_level=level)
            except InferenceError:
                if level == 3:
                    raise
                continue
            supplied = json.loads(serialized)
            output_schema, memory_ids, event_ids = self._episode_schema(role, schema.model_json_schema(), supplied)
            # Refit after each reduction: source enums themselves consume context.
            prompt = self._role_prompt(role, supplied)
            system_prompt = prompt + "\nOutput JSON schema:\n" + json.dumps(output_schema, separators=(",", ":"))
            if len(system_prompt) + len(serialized) <= total_budget:
                break
        else:
            raise InferenceError("Complete cognitive context budget exceeded, including schema. Increase num_ctx or shorten the input.")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": serialized},
        ]
        token_count = 0
        for attempt in range(2):
            # A conservative character estimate, including schema and repair
            # context, with space reserved for output and chat framing.
            budget = min(MAX_PAYLOAD_CHARS, max(0, (
                self.config.get("num_ctx", 8192) - self.config.get("max_output_tokens", 1600) - 256
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
                    "num_predict": self.config.get("max_output_tokens", 1600),
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
                self._validate_episode_ids(role, result, memory_ids, event_ids, supplied)
                return result.model_dump(), token_count
            except (ValueError, TypeError) as exc:
                if attempt:
                    if data.get("done_reason") == "length":
                        raise InferenceError(f"{role.title()} exhausted max_output_tokens before returning valid JSON. Increase that budget for this model.")
                    raise InferenceError(f"{role.title()} returned invalid structured output after one repair attempt.")
                problem = str(exc)[:1200] if not isinstance(exc, json.JSONDecodeError) else "The response was not valid JSON."
                if isinstance(exc, ValidationError):
                    problem = "; ".join(
                        ".".join(str(part) for part in item["loc"]) + ": " + item["msg"]
                        for item in exc.errors(include_input=False, include_url=False)[:4]
                    )[:1200]
                # The previous output remains assistant data; the only new instruction is a schema repair.
                messages = messages + [
                    {"role": "assistant", "content": content[:8192]},
                    {"role": "user", "content": (
                        "Your last output did not satisfy the supplied contract. "
                        + "Validation problem: " + problem + "\n"
                        + "Return one complete JSON object using exactly the schema fields and types. "
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
        experiment = payload.get("experiment") or {}
        observation = experiment.get("observation") or {}
        ledger = payload.get("action_ledger") or []
        if role == "manas":
            result = {
                "observations": [f"The {event.get('kind', 'user')} event says: {content}"],
                "interpretations": ["This demonstration treats the supplied text as a statement to remember."],
                "uncertainties": ["No language model is connected in demonstration mode."],
                "retrieval_query": content,
                "salience": 0.8 if experiment.get("discrepancies") else 0.5,
            }
            if observation:
                result["observations"].append("Simulation observation: " + json.dumps(observation, separators=(",", ":"))[:1000])
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
                "attended_ids": sorted(_attention_ids(payload))[:12],
                "overlooked": ["Objects outside the current view may have changed."] if observation else [],
                "uncertainty_to_check": "Inspect a changed or uncertain visible object." if experiment.get("discrepancies") else "",
            }
            if ledger:
                result["agency"] = "Recorded actions: " + "; ".join(
                    f"{record.get('actor', 'unknown')}: {record.get('tool', 'tool')} {record.get('status', 'unknown')}"
                    for record in ledger[-6:] if isinstance(record, dict))[:1600]
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
            if _environment_allowed(payload):
                visible = observation.get("objects", [])
                inventory = observation.get("inventory", [])
                goal = str(experiment.get("goal") or content).lower()
                target_name = "cup" if "cup" in goal else "key"
                target = next((item for item in visible if item.get("id") == target_name), None)
                tool_results = (payload.get("workspace") or {}).get("tool_results") or []
                result.update(memory_updates=[], commitment_updates=[])
                if target_name in inventory:
                    result["reply"] = f"Demonstration mode: the observed inventory contains {target_name}; the simulated task is complete."
                elif tool_results:
                    result["reply"] = "Demonstration mode: the simulated action returned an observation. The action ledger records its outcome."
                else:
                    pose = (observation.get("pose") or {}).get("position") or []
                    position = (target or {}).get("position") or []
                    reachable = (len(pose) == len(position) == 2
                                 and sum(abs(a-b) for a, b in zip(pose, position)) <= 1)
                    action = "pick_up" if target and reachable else "move_forward" if target else "turn_left"
                    result.update(action="environment", reply="", environment_action=action,
                                  target=target_name if action == "pick_up" else "",
                                  prediction=(f"The inventory will contain {target_name}." if action == "pick_up"
                                              else "The next observation will reflect the attempted simulated movement."),
                                  prediction_confidence=0.8, uncertainty=0.2,
                                  rationale="A scripted action chosen only from the limited observation.")
        else:
            raise ValueError("Unknown cognitive role.")
        parsed = ROLE_SCHEMAS[role].model_validate(result).model_dump()
        return parsed, 0
