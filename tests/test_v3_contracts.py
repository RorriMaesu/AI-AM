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
