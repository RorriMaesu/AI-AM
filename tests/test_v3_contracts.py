"""Contracts and grounded inference for the active-mind experiment."""
import asyncio
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import ValidationError
from core import config
from core.contracts import AhamkaraResult, BuddhiResult, CommitmentProposal, ROLE_SCHEMAS
from core.inference import (DemoBackend, InferenceError, OllamaBackend, _context_payload,
                            _episode_schema, _validate_episode_ids)


def settings():
    return {"api_url": "http://127.0.0.1:11434/api/chat", "request_timeout": 2,
            "num_ctx": 8192, "max_output_tokens": 600,
            "roles": {role: {"model": "local:test", "temperature": 0.2} for role in ROLE_SCHEMAS}}


def payload():
    return {
        "current_event": {"id": "now", "kind": "experiment", "content": "Pick up the key."},
        "rules": {"environment_enabled": True, "available_actions": ["reply", "environment"]},
        "experiment": {"goal": "Pick up the key.", "observation": {
            "pose": {"position": [2, 3], "heading": "east"},
            "objects": [{"id": "key", "position": [3, 3], "portable": True}],
            "inventory": [], "actions": ["look", "pick_up", "turn_left", "move_forward"]}},
        "workspace": {"attention": {"selected_ids": ["event:now"], "items": [
            {"id": "event:now", "source_ids": ["now"], "text": "Inspect the key."}]}},
        "self_model": {"commitment_records": [{"id": "promise-1", "description": "Return the key."}]},
        "action_ledger": [{"id": "action-1", "actor": "self", "tool": "calculator",
                           "status": "succeeded", "tool_event_id": "tool-1", "episode_id": "prior"}],
    }


def validate(value, data=None, role="buddhi"):
    data = payload() if data is None else data
    result = ROLE_SCHEMAS[role].model_validate(value)
    _, memory_ids, event_ids = _episode_schema(role, ROLE_SCHEMAS[role].model_json_schema(), data)
    _validate_episode_ids(role, result, memory_ids, event_ids, data)
    return result


class TestActiveMindContracts(unittest.TestCase):
    def test_environment_requires_prediction_and_payload(self):
        valid = {"action": "environment", "environment_action": "pick_up", "target": "key",
                 "prediction": "The key will be in inventory.", "prediction_confidence": 0.8}
        self.assertEqual(validate(valid).target, "key")
        for change in ({"prediction": ""}, {"environment_action": ""}, {"target": ""},
                       {"environment_action": "execute_python"}, {"prediction_confidence": 2},
                       {"environment_action": "move_forward", "target": "key"}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                BuddhiResult.model_validate({**valid, **change})
        with self.assertRaises(ValidationError):
            BuddhiResult.model_validate({"reply": "Done", "prediction": "A secret action."})

    def test_environment_is_gated_by_event_and_runtime_permission(self):
        decision = {"action": "environment", "environment_action": "look", "prediction": "I will receive a view."}
        for kind, enabled, actions in (("user", True, ["environment"]),
                                       ("experiment", False, ["environment"]),
                                       ("experiment", True, ["reply"])):
            data = payload()
            data["current_event"]["kind"] = kind
            data["rules"].update(environment_enabled=enabled, available_actions=actions)
            schema, _, _ = _episode_schema("buddhi", BuddhiResult.model_json_schema(), data)
            self.assertNotIn("environment", schema["properties"]["action"]["enum"])
            with self.assertRaisesRegex(ValueError, "explicitly authorized"):
                validate(decision, data)

    def test_current_view_and_inventory_constrain_targets(self):
        decision = {"action": "environment", "environment_action": "inspect", "target": "hidden-cup",
                    "prediction": "The cup can be inspected."}
        with self.assertRaisesRegex(ValueError, "currently observed"):
            validate(decision)
        data = payload()
        data["experiment"]["observation"]["actions"].append("drop")
        with self.assertRaisesRegex(ValueError, "inventory"):
            validate({**decision, "environment_action": "drop", "target": "key"}, data)
        data["experiment"]["observation"]["inventory"] = ["key"]
        validate({**decision, "environment_action": "drop", "target": "key"}, data)

    def test_episode_schema_has_only_allowed_target_and_attention_ids(self):
        data = payload()
        schema, _, _ = _episode_schema("buddhi", BuddhiResult.model_json_schema(), data)
        self.assertEqual(set(schema["properties"]["required"]) if "required" in schema["properties"] else
                         set(schema["required"]), set(schema["properties"]))
        self.assertEqual(schema["properties"]["target"]["enum"], ["", "key"])
        self.assertIn("environment", schema["properties"]["action"]["enum"])
        self.assertNotIn("drop", schema["properties"]["environment_action"]["enum"])
        schema, _, _ = _episode_schema("ahamkara", AhamkaraResult.model_json_schema(), data)
        self.assertEqual(schema["properties"]["attended_ids"]["items"]["enum"], ["event:now"])
        validate({"attended_ids": ["event:now"]}, data, "ahamkara")
        with self.assertRaisesRegex(ValueError, "selected items"):
            validate({"attended_ids": ["invented"]}, data, "ahamkara")

    def test_action_ids_can_support_reply_but_not_become_memory_sources(self):
        validate({"reply": "I used the calculator.", "evidence_ids": ["action-1", "tool-1"]})
        with self.assertRaisesRegex(ValueError, "source events"):
            validate({"reply": "Remembered.", "memory_updates": [{
                "content": "A claim.", "kind": "inference", "source_event_ids": ["action-1"]}]})
        validate({"reply": "Remembered.", "memory_updates": [{
            "content": "A claim.", "kind": "inference", "source_event_ids": ["tool-1"]}]})

    def test_commitments_have_explicit_lifecycle_ids_and_sources(self):
        change = {"operation": "complete", "id": "promise-1", "source_event_ids": ["tool-1"]}
        validate({"reply": "Completed.", "commitment_updates": [change]})
        for bad in ({**change, "id": "unknown"}, {**change, "source_event_ids": ["invented"]}):
            with self.assertRaises(ValueError):
                validate({"reply": "No.", "commitment_updates": [bad]})
        for bad in ({"operation": "create", "id": "invented", "description": "Promise", "source_event_ids": ["now"]},
                    {"operation": "update", "id": None, "description": "Promise", "source_event_ids": ["now"]},
                    {"operation": "create", "description": "", "source_event_ids": ["now"]},
                    {"operation": "create", "description": "Promise", "source_event_ids": []}):
            with self.assertRaises(ValidationError):
                CommitmentProposal.model_validate(bad)

    def test_new_commitment_schema_only_allows_create_when_none_exist(self):
        data = payload()
        data["self_model"]["commitment_records"] = []
        schema, _, _ = _episode_schema("buddhi", BuddhiResult.model_json_schema(), data)
        props = schema["$defs"]["CommitmentProposal"]["properties"]
        self.assertEqual(props["operation"]["enum"], ["create"])
        self.assertEqual(props["id"]["type"], "null")

    def test_evaluator_state_is_rejected_before_transport(self):
        for key in ("ground_truth", "evaluator", "world_state", "private_state", "world", "evaluator_only", "controls", "walls"):
            data = payload()
            data["experiment"][key] = {"hidden_cup": [6, 1]}
            with self.assertRaisesRegex(InferenceError, "Evaluator"):
                _context_payload(data, 8192)

    def test_context_includes_experiment_and_ledger_without_mutating_input(self):
        data = payload()
        original = copy.deepcopy(data)
        supplied = json.loads(_context_payload(data, 8192))
        self.assertEqual(supplied["experiment"], data["experiment"])
        self.assertEqual(supplied["action_ledger"], data["action_ledger"])
        self.assertEqual(data, original)
        data["action_ledger"] = [{"id": str(i)} for i in range(30)]
        supplied = json.loads(_context_payload(data, 8192))
        self.assertEqual(len(supplied["action_ledger"]), 12)
        self.assertEqual(supplied["action_ledger_omitted"], 18)
        self.assertEqual(supplied["action_ledger"][0]["id"], "0")
        self.assertEqual(supplied["action_ledger"][-1]["id"], "11")
        data["experiment"]["oversized"] = "x" * 100_000
        with self.assertRaisesRegex(InferenceError, "context budget"):
            _context_payload(data, 8192)

    def test_demo_uses_only_visible_objects_then_reports_inventory(self):
        async def run():
            backend = DemoBackend(settings())
            data = payload()
            result, tokens = await backend.infer("buddhi", data)
            self.assertEqual((result["action"], result["environment_action"], result["target"]),
                             ("environment", "pick_up", "key"))
            self.assertTrue(result["prediction"])
            self.assertEqual(tokens, 0)
            data["experiment"]["observation"]["objects"] = []
            data["experiment"]["observation"]["inventory"] = ["key"]
            result, _ = await backend.infer("buddhi", data)
            self.assertEqual(result["action"], "reply")
            self.assertIn("Demonstration mode", result["reply"])
            self.assertIn("inventory contains key", result["reply"])
            data["experiment"]["observation"]["inventory"] = []
            result, _ = await backend.infer("buddhi", data)
            self.assertEqual(result["environment_action"], "turn_left")
            self.assertEqual(result["target"], "")
        asyncio.run(run())

    def test_real_backend_repairs_unauthorized_environment_attempt(self):
        sent = []
        def handler(request):
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": "local:test"}]})
            sent.append(json.loads(request.content))
            result = ({"action": "environment", "environment_action": "look", "prediction": "A view."}
                      if len(sent) == 1 else {"reply": "No experiment was authorized."})
            return httpx.Response(200, json={"message": {"content": json.dumps(result)}})
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result, _ = await OllamaBackend(settings(), client).infer("buddhi", {
                    "current_event": {"id": "chat", "kind": "user", "content": "Say hi."}})
                self.assertEqual(result["action"], "reply")
        asyncio.run(run())
        self.assertEqual(len(sent), 2)
        self.assertNotIn("environment", sent[0]["format"]["properties"]["action"]["enum"])


    def test_focused_memory_ids_never_become_source_event_ids(self):
        data = payload()
        data["memories"] = [{"id": "remembered-1", "content": "A memory.",
                             "source_event_ids": ["source-1"]}]
        data["focus"] = [{"id": "memory:remembered-1", "memory_id": "remembered-1",
                          "source_ids": ["remembered-1", "source-1", "omitted-source"],
                          "text": "A memory cue."}]
        schema, memory_ids, event_ids = _episode_schema("buddhi", BuddhiResult.model_json_schema(), data)
        self.assertIn("remembered-1", memory_ids)
        self.assertIn("source-1", event_ids)
        self.assertNotIn("remembered-1", event_ids)
        self.assertNotIn("omitted-source", event_ids)
        with self.assertRaisesRegex(ValueError, "source events"):
            validate({"reply": "A cue is not a source event.", "memory_updates": [{
                "content": "A claim.", "kind": "inference",
                "source_event_ids": ["remembered-1"]}]}, data)

    def test_two_simulator_actions_then_chat_fit_complete_context(self):
        data = payload()
        data["current_event"] = {"id": "third-event", "kind": "user",
                                 "content": "What did you actually do, and what did you only predict?"}
        data["rules"] = {"environment_enabled": False,
                         "available_actions": ["reply", "retrieve", "reconsider", "calculate"],
                         "event_ids": ["omitted-event"], "evidence_ids": ["omitted-action"]}
        observation = data["experiment"]["observation"]
        observation.update(tick=2, inventory=["key"], objects=[], energy=98,
                           visible_cells=[{"position": [x, y], "kind": "floor"}
                                          for x in range(3, 7) for y in range(1, 6)])
        data["experiment"]["recent_transitions"] = [
            {"tick": i, "observation": observation,
             "forecast": {"basis": "A prediction, not an observation. " * 90}}
            for i in range(4)]
        data["experiment"]["action_predictions"] = [
            {"action": action, "predicted_pose": {"position": [4, 3], "heading": "east"},
             "confidence": 0.6, "samples": 1, "basis": "Past transition."}
            for action in ("move_forward", "turn_left", "turn_right")]
        data["experiment"]["beliefs"] = [{"id": str(i), "claim": "A tentative map belief. " * 8}
                                           for i in range(12)]
        data["action_ledger"] = [
            {"id": "action-pickup", "event_id": "outcome-pickup", "actor": "self",
             "tool": "simulator", "status": "succeeded",
             "parameters": {"action": "pick_up", "target": "key"},
             "prediction": {"statement": "The key will be in inventory.", "reported_confidence": 0.8},
             "result": {"ok": True, "reason": "Picked up key.", "observation": observation}},
            {"id": "action-move", "event_id": "outcome-move", "actor": "self",
             "tool": "simulator", "status": "succeeded",
             "parameters": {"action": "move_forward"},
             "prediction": {"statement": "I will move east.", "reported_confidence": 0.7},
             "result": {"ok": True, "reason": "Moved forward.", "observation": observation}}]
        data["history"] = [{"id": "history-" + str(i), "kind": "user", "content": "Prior dialogue. " * 60}
                           for i in range(10)]
        data["memories"] = [{"id": "memory-" + str(i), "kind": "observation",
                             "content": "Previously recorded evidence. " * 45,
                             "source_event_ids": ["memory-source-" + str(i)]} for i in range(8)]
        assessment = "This is a generated assessment whose wording can be abridged. " * 22
        data["workspace"]["manas"] = {"observations": [assessment] * 4,
                                      "interpretations": [assessment] * 4,
                                      "uncertainties": [assessment] * 4,
                                      "retrieval_query": "What actions actually succeeded?"}
        data["workspace"].update(copy.deepcopy(data["workspace"]["manas"]))
        data["workspace"]["chitta"] = {"selected_memory_ids": ["memory-7"],
                                       "associations": [assessment] * 4,
                                       "contradictions": [], "missing_context": []}
        data["workspace"]["self_assessment"] = {"agency": assessment,
                                                "capability_limits": [assessment] * 3}
        data["focus"] = [{"id": "memory:memory-" + str(i), "memory_id": "memory-" + str(i),
                          "text": assessment[:240], "source_ids": ["memory-" + str(i),
                                                                    "memory-source-" + str(i)],
                          "score_components": {"recency": 0.8, "relevance": 0.6}} for i in range(6)]
        original = copy.deepcopy(data)
        sent = []
        def handler(request):
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": "local:test"}]})
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"message": {"content": json.dumps({
                "reply": "The recorded actions moved forward and picked up the key.",
                "evidence_ids": ["action-pickup", "action-move", "outcome-pickup", "outcome-move"]})}})
        async def run():
            cfg = settings()
            cfg["max_output_tokens"] = 1600
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result, _ = await OllamaBackend(cfg, client).infer("buddhi", data)
                self.assertEqual(result["action"], "reply")
        asyncio.run(run())
        self.assertEqual(len(sent), 1)
        self.assertLessEqual(sum(len(m["content"]) for m in sent[0]["messages"]), (8192 - 1600 - 256) * 3)
        supplied = json.loads(sent[0]["messages"][1]["content"])
        self.assertEqual(supplied["current_event"], original["current_event"])
        self.assertEqual(supplied["experiment"]["observation"], original["experiment"]["observation"])
        self.assertEqual([item["id"] for item in supplied["action_ledger"][:2]],
                         ["action-pickup", "action-move"])
        self.assertEqual([item["result"]["reason"] for item in supplied["action_ledger"][:2]],
                         ["Picked up key.", "Moved forward."])
        self.assertNotIn("recent_transitions", supplied["experiment"])
        self.assertNotIn("observations", supplied["workspace"])
        selected = next(item for item in supplied["memories"] if item["id"] == "memory-7")
        self.assertEqual(selected["content"], original["memories"][7]["content"])
        event_enum = sent[0]["format"]["$defs"]["MemoryProposal"]["properties"]["source_event_ids"]["items"]["enum"]
        self.assertNotIn("memory-7", event_enum)
        self.assertNotIn("omitted-event", event_enum)
        self.assertNotIn("omitted-action", sent[0]["format"]["properties"]["evidence_ids"]["items"]["enum"])
        self.assertEqual(data, original)

    def test_config_defaults_are_bounded_and_do_not_create_state(self):
        with patch.object(config, "ROOT", Path("T:/AIAM_UNWRITTEN_CONFIG_TEST")):
            value = config.load_config()
        self.assertEqual(value["workspace_capacity"], 6)
        self.assertTrue(value["selective_recurrence"])
        self.assertTrue(value["experiment_enabled"])
        self.assertEqual((value["experiment_max_steps"], value["experiment_budget"]), (10, 30))
        for name, bad, low, high in (("workspace_capacity", 0, 2, 16),
                                     ("experiment_max_steps", True, 1, 100),
                                     ("experiment_budget", 1001, 1, 1000)):
            with self.assertRaises(config.ConfigurationError):
                config._number({name: bad}, name, 1, low, high, integer=True)
        with self.assertRaises(config.ConfigurationError):
            config._bool({"experiment_enabled": "true"}, "experiment_enabled", True)
