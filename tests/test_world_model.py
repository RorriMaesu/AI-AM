import copy
import json
import unittest

from core.environment import (
    intervene_environment, new_environment, observe_environment, step_environment,
)
from core.world_model import new_world_model, predict_action, update_world_model


def advance(state, model, action, target=None, actor="self"):
    forecast = predict_action(model, action, target)
    result = step_environment(state, action, target)
    result["outcome"]["actor"] = actor
    model = update_world_model(model, result["observation"], action,
                               result["outcome"], forecast)
    return result["state"], model, forecast


class WorldModelTests(unittest.TestCase):
    def setUp(self):
        self.state = new_environment()
        self.model = new_world_model(observe_environment(self.state))

    def test_only_observed_objects_enter_initial_model(self):
        self.assertEqual(set(self.model["beliefs"]), {"key"})
        self.assertEqual(self.model["beliefs"]["key"]["kind"], "observed")
        self.assertNotIn("cup", json.dumps(self.model))
        self.assertNotIn("controls", self.model["last_observation"])

    def test_hidden_movement_does_not_magically_update_belief(self):
        state, model, _ = advance(self.state, self.model, "turn_left")
        state = intervene_environment(state, {
            "kind": "move_object", "object_id": "key", "position": [3, 4]})
        state, model, _ = advance(state, model, "look")
        belief = model["beliefs"]["key"]
        self.assertEqual(belief["kind"], "remembered")
        self.assertEqual(belief["position"], [3, 3])
        self.assertEqual(belief["location_status"], "last_seen")
        self.assertEqual(model["discrepancies"], [])

    def test_reappearance_revises_belief_and_records_uncertain_cause(self):
        state, model, _ = advance(self.state, self.model, "turn_left")
        state = intervene_environment(state, {
            "kind": "move_object", "object_id": "key", "position": [3, 4]})
        state, model, _ = advance(state, model, "look")
        state, model, _ = advance(state, model, "turn_right")
        self.assertEqual(model["beliefs"]["key"]["position"], [3, 4])
        self.assertEqual(model["beliefs"]["key"]["kind"], "observed")
        change = model["discrepancies"][-1]
        self.assertEqual(change["kind"], "location_changed")
        self.assertEqual(change["ownership"], "unknown")
        self.assertEqual(change["previous_position"], [3, 3])

    def test_absence_at_visible_old_location_means_unknown_not_new_location(self):
        state = intervene_environment(self.state, {
            "kind": "move_object", "object_id": "key", "position": [6, 2]})
        state, model, _ = advance(state, self.model, "look")
        belief = model["beliefs"]["key"]
        self.assertIsNone(belief["position"])
        self.assertEqual(belief["last_observed_position"], [3, 3])
        self.assertEqual(belief["kind"], "inferred")
        self.assertEqual(belief["location_status"], "unknown")
        self.assertNotIn("[6, 2]", json.dumps(model))
        self.assertEqual(model["discrepancies"][-1]["kind"], "missing_from_visible_cell")

    def test_prediction_is_created_from_prior_observation(self):
        forecast = predict_action(self.model, "turn_left")
        self.assertEqual(forecast["predicted_effect"], "0,0,-1")
        self.assertEqual(forecast["predicted_pose"]["heading"], "north")
        self.assertEqual(forecast["before_tick"], 0)
        self.assertEqual(forecast["basis"], "untrained_prior")
        self.assertAlmostEqual(sum(forecast["distribution"].values()), 1.0)

    def test_changed_controls_are_learned_from_outcomes(self):
        state = intervene_environment(self.state, {
            "kind": "alter_controls", "mapping": {"turn_left": "turn_right"}})
        model = self.model
        scores = []
        for _ in range(12):
            state, model, forecast = advance(state, model, "turn_left")
            scores.append(model["calibration"]["last_brier"])
        self.assertEqual(model["calibration"]["samples"], 12)
        self.assertEqual(predict_action(model, "turn_left")["predicted_effect"], "0,0,1")
        self.assertEqual(model["transitions"]["turn_left"]["0,0,1"], 12)
        self.assertLess(sum(scores[-6:]), sum(scores[:6]))
        self.assertAlmostEqual(scores[0], 1.0)
        self.assertEqual(predict_action(new_world_model(observe_environment(state)),
                                        "turn_left")["predicted_effect"], "0,0,-1")

    def test_user_actions_train_controls_without_self_credit(self):
        state, model, _ = advance(self.state, self.model, "turn_left", actor="user")
        self.assertEqual(model["history"][-1]["ownership"], "other")
        self.assertEqual(model["transitions"]["turn_left"]["0,0,-1"], 1)
        self.assertEqual(model["calibration"]["samples"], 1)

    def test_pickup_and_drop_attribution(self):
        state, model, _ = advance(self.state, self.model, "pick_up", "key")
        self.assertTrue(model["beliefs"]["key"]["carried"])
        self.assertEqual(model["beliefs"]["key"]["location_status"], "carried")
        state, model, _ = advance(state, model, "drop", "key")
        self.assertFalse(model["beliefs"]["key"]["carried"])
        self.assertEqual(model["beliefs"]["key"]["position"], [2, 3])
        self.assertEqual(model["discrepancies"][-1]["ownership"], "self")

    def test_forecasts_cannot_be_backfilled_or_modified(self):
        forecast = predict_action(self.model, "turn_left")
        result = step_environment(self.state, "turn_left")
        forecast["confidence"] = 1.0
        with self.assertRaises(ValueError):
            update_world_model(self.model, result["observation"], "turn_left",
                               result["outcome"], forecast)
        with self.assertRaises(ValueError):
            update_world_model(self.model, result["observation"], prediction={})

    def test_hidden_snapshot_is_rejected(self):
        with self.assertRaises(ValueError):
            update_world_model(self.model, self.state)
        observation = observe_environment(self.state)
        observation["controls"] = self.state["controls"]
        with self.assertRaises(ValueError):
            update_world_model(self.model, observation)

    def test_object_outside_visible_evidence_is_rejected(self):
        observation = observe_environment(self.state)
        observation["objects"].append({"id": "cup", "name": "blue cup",
                                       "portable": True, "position": [6, 3]})
        with self.assertRaises(ValueError):
            update_world_model(self.model, observation)

    def test_state_and_forecasts_survive_json_roundtrip(self):
        state, model, _ = advance(self.state, self.model, "turn_left")
        restored = json.loads(json.dumps(model))
        self.assertEqual(predict_action(model, "turn_right"),
                         predict_action(restored, "turn_right"))
        a = advance(state, model, "turn_right")[1]
        b = advance(state, restored, "turn_right")[1]
        self.assertEqual(a, b)

    def test_inputs_and_last_observation_are_not_mutated(self):
        original = copy.deepcopy(self.model)
        state, model, _ = advance(self.state, self.model, "turn_left")
        self.assertEqual(self.model, original)
        observation = observe_environment(state)
        updated = update_world_model(model, observation)
        observation["pose"]["heading"] = "south"
        self.assertEqual(updated["last_observation"]["pose"]["heading"], "north")

    def test_history_and_learning_window_are_bounded(self):
        state, model = self.state, self.model
        for _ in range(40):
            state, model, _ = advance(state, model, "turn_left")
        self.assertEqual(len(model["history"]), 32)
        self.assertEqual(sum(model["transitions"]["turn_left"].values()), 32)
        self.assertEqual(model["calibration"]["samples"], 40)

    def test_repeated_observation_does_not_invent_action_learning(self):
        model = update_world_model(self.model, observe_environment(self.state))
        self.assertEqual(model["transitions"], {})
        self.assertEqual(model["calibration"]["samples"], 0)
        self.assertEqual(model["history"][-1]["ownership"], "unknown")

    def test_observations_cannot_go_backwards(self):
        state, model, _ = advance(self.state, self.model, "turn_left")
        with self.assertRaises(ValueError):
            update_world_model(model, observe_environment(self.state))
