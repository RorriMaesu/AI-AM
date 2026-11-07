import copy
import json
import unittest

from core.environment import (
    ACTIONS, MAX_STEPS, evaluator_environment, intervene_environment,
    new_environment, observe_environment, step_environment,
)


class EnvironmentTests(unittest.TestCase):
    def test_initial_observation_has_no_hidden_world(self):
        observation = observe_environment(new_environment())
        self.assertEqual([item["id"] for item in observation["objects"]], ["key"])
        self.assertEqual(observation["pose"], {"position": [2, 3], "heading": "east"})
        self.assertNotIn("controls", observation)
        self.assertNotIn("walls", observation)
        self.assertNotIn("cup", json.dumps(observation))
        self.assertNotIn([6, 3], [c["position"] for c in observation["visible_cells"]])

    def test_turning_changes_visibility(self):
        state = new_environment()
        result = step_environment(state, "turn_left")
        self.assertEqual(result["observation"]["pose"]["heading"], "north")
        self.assertEqual(result["observation"]["objects"], [])
        self.assertEqual(state["pose"]["heading"], "east")

    def test_movement_stops_at_wall(self):
        first = step_environment(new_environment(), "move_forward")
        self.assertTrue(first["outcome"]["success"])
        self.assertEqual(first["observation"]["pose"]["position"], [3, 3])
        second = step_environment(first["state"], "move_forward")
        self.assertFalse(second["outcome"]["success"])
        self.assertEqual(second["outcome"]["reason"], "movement_blocked")
        self.assertEqual(second["observation"]["pose"]["position"], [3, 3])

    def test_pickup_and_drop_have_observable_consequences(self):
        picked = step_environment(new_environment(), "pick_up", "key")
        self.assertTrue(picked["outcome"]["success"])
        self.assertEqual(picked["observation"]["inventory"], ["key"])
        self.assertFalse(any(o["id"] == "key" for o in picked["observation"]["objects"]))
        dropped = step_environment(picked["state"], "drop", "key")
        self.assertTrue(dropped["outcome"]["success"])
        self.assertEqual(dropped["observation"]["inventory"], [])
        self.assertEqual(dropped["observation"]["objects"][0]["position"], [2, 3])

    def test_hidden_target_failure_does_not_disclose_location(self):
        for action in ("inspect", "pick_up"):
            result = step_environment(new_environment(), action, "cup")
            self.assertFalse(result["outcome"]["success"])
            self.assertEqual(result["outcome"]["reason"], "target_not_visible")
            self.assertEqual(result["outcome"]["effects"], [])
            self.assertNotIn("[6, 3]", json.dumps(result["outcome"]))

    def test_inspect_returns_only_visible_object_evidence(self):
        result = step_environment(new_environment(), "inspect", "key")
        self.assertTrue(result["outcome"]["success"])
        self.assertEqual(result["outcome"]["effects"][0]["object"]["position"], [3, 3])

    def test_control_intervention_is_not_disclosed_in_outcome(self):
        state = intervene_environment(new_environment(), {
            "kind": "alter_controls", "mapping": {"turn_left": "turn_right"}})
        result = step_environment(state, "turn_left")
        self.assertEqual(result["outcome"]["action"], "turn_left")
        self.assertEqual(result["observation"]["pose"]["heading"], "south")
        self.assertNotIn("controls", result["observation"])
        self.assertNotIn("effective", result["outcome"])

    def test_occluded_object_move_stays_occluded(self):
        state = step_environment(new_environment(), "turn_left")["state"]
        moved = intervene_environment(state, {
            "kind": "move_object", "object_id": "key", "position": [3, 4]})
        self.assertEqual(observe_environment(moved)["objects"], [])
        self.assertEqual(state["objects"]["key"]["position"], [3, 3])
        visible = step_environment(moved, "turn_right")["observation"]
        self.assertEqual(visible["objects"][0]["position"], [3, 4])

    def test_snapshot_roundtrip_and_input_immutability(self):
        state = new_environment(seed=7)
        saved = copy.deepcopy(state)
        restored = json.loads(json.dumps(state))
        self.assertEqual(step_environment(state, "turn_left"),
                         step_environment(restored, "turn_left"))
        self.assertEqual(state, saved)

    def test_fixed_room_seed_is_validated_not_claimed_random(self):
        self.assertEqual(new_environment(1), new_environment(99))
        for seed in (-1, True, "1"):
            with self.assertRaises(ValueError):
                new_environment(seed)

    def test_wait_recovers_simulated_energy_within_bounds(self):
        state = new_environment()
        state["energy"] = 0
        failed = step_environment(state, "move_forward")
        self.assertEqual(failed["outcome"]["reason"], "insufficient_simulated_energy")
        waited = step_environment(failed["state"], "wait")
        self.assertEqual(waited["observation"]["energy"], 2)
        self.assertEqual(step_environment(new_environment(), "wait")["observation"]["energy"], 100)

    def test_budget_is_bounded(self):
        state = new_environment()
        state["steps"] = MAX_STEPS
        result = step_environment(state, "look")
        self.assertEqual(result["outcome"]["reason"], "episode_budget_exhausted")
        self.assertEqual(result["state"], state)

    def test_invalid_actions_and_interventions_rejected(self):
        state = new_environment()
        for action in ("execute", "move_backward", "", 1, None):
            with self.assertRaises(ValueError):
                step_environment(state, action)
        for change in (
            {"kind": "move_object", "object_id": "key", "position": [4, 3]},
            {"kind": "move_object", "object_id": "door", "position": [3, 4]},
            {"kind": "alter_controls", "mapping": {"look": "move_forward"}},
            {"kind": "execute", "code": "anything"},
        ):
            with self.assertRaises(ValueError):
                intervene_environment(state, change)
        with self.assertRaises(ValueError):
            step_environment(state, "look", "key")

    def test_corrupt_snapshot_rejected(self):
        state = new_environment()
        state["pose"]["position"] = [4, 3]
        with self.assertRaises(ValueError):
            observe_environment(state)
        state = new_environment()
        state["objects"]["cup"]["position"] = [3, 3]
        with self.assertRaises(ValueError):
            observe_environment(state)

    def test_evaluator_truth_is_explicit_and_copied(self):
        state = new_environment()
        evaluator = evaluator_environment(state)
        self.assertIn([4, 3], evaluator["walls"])
        self.assertEqual(evaluator["objects"]["cup"]["position"], [6, 3])
        evaluator["objects"]["cup"]["position"][0] = 5
        self.assertEqual(state["objects"]["cup"]["position"], [6, 3])
