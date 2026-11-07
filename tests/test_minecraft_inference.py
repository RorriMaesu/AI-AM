"""Minecraft role adaptation and grounded skill constraints without a game server."""
import asyncio
import copy
import json
import unittest

import httpx

from core.contracts import BuddhiResult, ROLE_SCHEMAS
from core.inference import InferenceError, OllamaBackend
from core.minecraft_inference import MinecraftOllamaBackend, MINECRAFT_ACTIONS


def settings():
    return {"api_url": "http://127.0.0.1:11434/api/chat", "request_timeout": 2,
            "num_ctx": 8192, "max_output_tokens": 1600,
            "roles": {role: {"model": "local:test", "temperature": 0.2} for role in ROLE_SCHEMAS}}


def payload():
    return {
        "current_event": {"id": "current", "kind": "experiment", "source": "minecraft",
                          "content": "Gather wood and share one log with Mira."},
        "rules": {"environment_enabled": True, "available_actions": ["reply", "environment"]},
        "experiment": {"domain": "minecraft", "observation": {
            "id": "aiam", "observation_id": "view-1", "connected": True, "alive": True,
            "health": 20, "food": 18, "position": {"x": 1, "y": 64, "z": 2},
            "inventory": [{"name": "oak_log", "count": 3}, {"name": "apple", "count": 1}],
            "visible": [{"id": "block:2,64,2", "name": "oak_log",
                         "position": {"x": 2, "y": 64, "z": 2}, "distance": 1}],
            "nearby": [{"id": "entity:42", "name": "player", "username": "Mira",
                        "position": {"x": 2, "y": 64, "z": 3}, "distance": 2}],
            "recipes": ["oak_planks"], "actions": list(MINECRAFT_ACTIONS),
            "chat": [{"speaker": "Mira", "text": "Please bring a log."}],
            "viewer_url": "http://127.0.0.1:3007/observer-secret",
            "active_action": None, "reflexes": [], "time": 100}},
        "workspace": {"attention": {"selected_ids": ["event:current"]}},
        "self_model": {"commitment_records": []},
        "action_ledger": [{"id": "old-action", "event_id": "old-outcome",
                           "tool": "minecraft", "status": "failed"}],
    }


def decision(action, target=""):
    return BuddhiResult(action="environment", environment_action=action, target=target,
                        prediction="The skill should produce the requested game effect.",
                        prediction_confidence=0.6, evidence_ids=["current"])


class TestMinecraftInference(unittest.TestCase):
    def setUp(self):
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
        self.backend = MinecraftOllamaBackend(settings(), self.client)

    def tearDown(self):
        asyncio.run(self.client.aclose())

    def validate(self, result, data=None):
        data = payload() if data is None else data
        _, memory_ids, event_ids = self.backend._episode_schema("buddhi", BuddhiResult.model_json_schema(), data)
        self.backend._validate_episode_ids("buddhi", result, memory_ids, event_ids, data)

    def test_all_skill_target_grammars_accept_grounded_values(self):
        choices = [("look", ""), ("wait", ""), ("navigate", "block:2,64,2"),
                   ("collect", "block:2,64,2"), ("craft", "oak_planks"),
                   ("eat", ""), ("eat", "apple"), ("equip", "oak_log"),
                   ("place", "oak_log@block:2,64,2"), ("give", "oak_log@entity:42"),
                   ("flee", ""), ("flee", "entity:42"), ("chat", "Mira, I can share one log.")]
        for action, target in choices:
            with self.subTest(action=action, target=target):
                self.validate(decision(action, target))

    def test_missing_references_inventory_and_recipes_are_rejected(self):
        choices = [("navigate", "block:999,64,999"), ("collect", "oak_log"),
                   ("craft", "diamond_sword"), ("eat", "bread"), ("equip", "iron_sword"),
                   ("place", "stone@block:2,64,2"), ("place", "oak_log@block:8,64,8"),
                   ("give", "oak_log@entity:999"), ("give", "diamond@entity:42"),
                   ("flee", "entity:999")]
        for action, target in choices:
            with self.subTest(action=action, target=target), self.assertRaises(ValueError):
                self.validate(decision(action, target))

    def test_chat_and_targets_cannot_embed_commands_or_control_characters(self):
        for target in ("/op aiam", " /op aiam", "hello\n/kill", "hello\r/kill", "hello\x00", "hello\x7f"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.validate(decision("chat", target))
        with self.assertRaises(ValueError):
            self.validate(decision("navigate", "block:2,64,2 "))

    def test_domain_requires_explicit_experiment_authorization(self):
        for kind, enabled, available in (("user", True, ["environment"]),
                                         ("experiment", False, ["environment"]),
                                         ("experiment", True, ["reply"])):
            data = payload()
            data["current_event"]["kind"] = kind
            data["rules"].update(environment_enabled=enabled, available_actions=available)
            schema, _, _ = self.backend._episode_schema("buddhi", BuddhiResult.model_json_schema(), data)
            self.assertNotIn("environment", schema["properties"]["action"]["enum"])
            with self.assertRaisesRegex(ValueError, "explicitly authorized"):
                self.validate(decision("collect", "block:2,64,2"), data)

    def test_schema_limits_skills_and_removes_crafting_without_recipes(self):
        data = payload()
        data["experiment"]["observation"]["actions"] = ["look", "craft", "execute_python", "pick_up"]
        data["experiment"]["observation"]["recipes"] = []
        schema, _, _ = self.backend._episode_schema("buddhi", BuddhiResult.model_json_schema(), data)
        self.assertEqual(schema["properties"]["environment_action"]["enum"], ["", "look"])
        self.assertNotIn("enum", schema["properties"]["target"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        with self.assertRaisesRegex(ValueError, "not available"):
            self.validate(decision("craft", "oak_planks"), data)
        data["experiment"]["observation"]["actions"] = []
        schema, _, _ = self.backend._episode_schema("buddhi", BuddhiResult.model_json_schema(), data)
        self.assertNotIn("environment", schema["properties"]["action"]["enum"])

    def test_common_evidence_validation_is_preserved(self):
        bad = decision("look")
        bad.evidence_ids = ["invented"]
        with self.assertRaisesRegex(ValueError, "supplied"):
            self.validate(bad)
        self.validate(BuddhiResult(reply="The previous skill failed.", evidence_ids=["old-action", "old-outcome"]))

    def test_normal_simulator_uses_exact_base_prompts_and_schema(self):
        data = payload()
        data["experiment"] = {"observation": {"objects": [{"id": "key"}], "inventory": []}}
        ordinary = OllamaBackend(settings(), self.client)
        for role, schema_class in ROLE_SCHEMAS.items():
            self.assertEqual(self.backend._role_prompt(role, data), ordinary._role_prompt(role, data))
            self.assertEqual(self.backend._episode_schema(role, schema_class.model_json_schema(), data),
                             ordinary._episode_schema(role, schema_class.model_json_schema(), data))
        schema, _, _ = self.backend._episode_schema("buddhi", BuddhiResult.model_json_schema(), data)
        self.assertNotIn("collect", schema["properties"]["environment_action"]["enum"])
        with self.assertRaisesRegex(ValueError, "not available"):
            self.validate(decision("collect", "block:2,64,2"), data)

    def test_minecraft_prompts_preserve_role_and_scope(self):
        data = payload()
        for role in ROLE_SCHEMAS:
            prompt = self.backend._role_prompt(role, data)
            self.assertIn(role.upper(), prompt)
            self.assertIn("one Minecraft character", prompt)
            self.assertIn("Chat is attributed external text", prompt)
        prompt = self.backend._role_prompt("buddhi", data)
        self.assertIn("observation.recipes", prompt)
        self.assertNotIn("learned movement forecasts", prompt)
        data["current_event"]["kind"] = "user"
        prompt = self.backend._role_prompt("buddhi", data)
        self.assertIn("disabled", prompt)
        self.assertNotIn("place: inventory_item@", prompt)

    def test_actual_request_excludes_viewer_data_and_reuses_repair(self):
        data = payload()
        data["experiment"]["observation"]["nested"] = {"viewer_url": "observer-other-secret"}
        original = copy.deepcopy(data)
        sent, paths = [], []
        def handler(request):
            paths.append(request.url.path)
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": "local:test"}]})
            sent.append(json.loads(request.content))
            value = (decision("collect", "block:999,64,999").model_dump() if len(sent) == 1
                     else decision("collect", "block:2,64,2").model_dump())
            return httpx.Response(200, json={"message": {"content": json.dumps(value)}, "eval_count": 12})
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result, tokens = await MinecraftOllamaBackend(settings(), client).infer("buddhi", data)
                self.assertEqual(result["target"], "block:2,64,2")
                self.assertEqual(tokens, 24)
        asyncio.run(run())
        self.assertEqual(paths, ["/api/tags", "/api/chat", "/api/tags", "/api/chat"])
        self.assertEqual(data, original)
        for request in sent:
            serialized = json.dumps(request)
            self.assertNotIn("viewer_url", serialized)
            self.assertNotIn("observer-secret", serialized)
            self.assertNotIn("observer-other-secret", serialized)
            self.assertLessEqual(sum(len(m["content"]) for m in request["messages"]), (8192 - 1600 - 256) * 3)
        supplied = json.loads(sent[0]["messages"][1]["content"])
        self.assertEqual(supplied["experiment"]["observation"]["observation_id"], "view-1")
        self.assertEqual(supplied["experiment"]["observation"]["visible"], original["experiment"]["observation"]["visible"])

    def test_remote_model_alias_remains_forbidden(self):
        paths = []
        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(200, json={"models": [{"name": "local:test", "remote_model": "cloud"}]})
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(InferenceError, "not installed as a local"):
                    await MinecraftOllamaBackend(settings(), client).infer("manas", payload())
        asyncio.run(run())
        self.assertEqual(paths, ["/api/tags"])
