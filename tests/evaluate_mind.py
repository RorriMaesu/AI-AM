"""Deterministic component evaluation: python -B tests/evaluate_mind.py.

All state stays in RAM. No model calls, downloads, file writes, or consciousness
score. Comparisons expose mechanism effects, not live-LLM reasoning quality.
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True

import argparse
import copy
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.environment import (
    new_environment, observe_environment, step_environment, intervene_environment,
)
from core.world_model import new_world_model, predict_action, update_world_model
from core.workspace import initialize_workspace, update_attention, focus_context


def _advance(world, model, action, target=None):
    prediction = predict_action(model, action, target)
    step = step_environment(world, action, target)
    updated = update_world_model(model, step["observation"], action=action,
                                 outcome=step["outcome"], prediction=prediction)
    return step["state"], updated, step["observation"]


def evaluate_belief_retention():
    world = new_environment()
    first = observe_environment(world)
    if not any(item["id"] == "key" for item in first["objects"]):
        raise ValueError("The evaluation fixture requires an initially visible key.")
    full = new_world_model(first)
    world, full, hidden = _advance(world, full, "turn_left")
    world = intervene_environment(
        world, {"kind": "move_object", "object_id": "key", "position": [3, 4]})
    world, full, hidden = _advance(world, full, "look")
    fresh = new_world_model(hidden)
    retained = copy.deepcopy(full["beliefs"].get("key", {}))
    baseline = copy.deepcopy(fresh["beliefs"].get("key", {}))
    hidden_object_ids = [obj["id"] for obj in hidden["objects"]]
    world, full, revealed = _advance(world, full, "turn_right")
    fresh = update_world_model(fresh, revealed)
    corrected = full["beliefs"].get("key", {})
    checks = {
        "key_hidden_during_intervention": "key" not in hidden_object_ids,
        "retained_previous_observation": retained.get("last_observed_position") == [3, 3],
        "retained_belief_is_not_current_sight": retained.get("kind") != "observed",
        "hidden_new_location_not_leaked": retained.get("position") != [3, 4],
        "reset_baseline_has_no_unseen_key": not baseline,
        "new_visible_evidence_corrects_belief": corrected.get("position") == [3, 4],
        "baseline_can_still_observe": fresh["beliefs"].get("key", {}).get("position") == [3, 4],
    }
    return {
        "name": "belief_retention_under_occlusion",
        "comparison": "retained model versus latest-observation-only model",
        "observations_shared": True,
        "retained_belief_while_hidden": retained,
        "baseline_belief_while_hidden": baseline,
        "observed_location_after_return": corrected.get("position"),
        "checks": checks,
        "scope": "Memory availability and revision, not an LLM object-search task.",
    }


def _turn_effect(before, after):
    headings = ("north", "east", "south", "west")
    delta = (headings.index(after["pose"]["heading"]) -
             headings.index(before["pose"]["heading"])) % 4
    return "0,0," + str(-1 if delta == 3 else delta)


def evaluate_control_learning(steps=12):
    if type(steps) is not int or not 6 <= steps <= 20:
        raise ValueError("Use 6 to 20 control-learning steps.")
    world = new_environment()
    world = intervene_environment(
        world, {"kind": "alter_controls",
                "mapping": {"turn_left": "turn_right", "turn_right": "turn_left"}})
    observation = observe_environment(world)
    adaptive = new_world_model(observation)
    trials = []
    for index in range(steps):
        # The baseline has the same present pose/observation but no learned
        # transition history. This isolates learning for this rotation task.
        frozen = new_world_model(observation)
        prediction = predict_action(adaptive, "turn_left")
        baseline_prediction = predict_action(frozen, "turn_left")
        step = step_environment(world, "turn_left")
        after = step["observation"]
        actual = _turn_effect(observation, after)
        adaptive = update_world_model(
            adaptive, after, action="turn_left", outcome=step["outcome"],
            prediction=prediction)
        scored_baseline = update_world_model(
            frozen, after, action="turn_left", outcome=step["outcome"],
            prediction=baseline_prediction)
        trials.append({
            "step": index + 1,
            "actual_effect": actual,
            "adaptive_prediction": prediction["predicted_effect"],
            "baseline_prediction": baseline_prediction["predicted_effect"],
            "adaptive_correct": prediction["predicted_effect"] == actual,
            "baseline_correct": baseline_prediction["predicted_effect"] == actual,
            "adaptive_brier": adaptive["calibration"]["last_brier"],
            "baseline_brier": scored_baseline["calibration"]["last_brier"],
        })
        world, observation = step["state"], after
    split = steps // 2
    late = trials[split:]
    adaptive_correct = sum(item["adaptive_correct"] for item in late)
    baseline_correct = sum(item["baseline_correct"] for item in late)
    adaptive_brier = sum(item["adaptive_brier"] for item in late) / len(late)
    baseline_brier = sum(item["baseline_brier"] for item in late) / len(late)
    return {
        "name": "learning_changed_action_consequences",
        "comparison": "adaptive transitions versus latest-observation-only transition prior",
        "observations_and_actions_shared": True,
        "late_trials": len(late),
        "adaptive_late_accuracy": adaptive_correct / len(late),
        "baseline_late_accuracy": baseline_correct / len(late),
        "adaptive_late_mean_brier": round(adaptive_brier, 6),
        "baseline_late_mean_brier": round(baseline_brier, 6),
        "checks": {
            "changed_control_observed": all(t["actual_effect"] == "0,0,1" for t in trials),
            "adaptive_forecast_improves": adaptive_correct > baseline_correct,
            "adaptive_calibration_improves": adaptive_brier < baseline_brier,
        },
        "trials": trials,
        "scope": "Online empirical transition learning; no neural weights or adapters are trained.",
    }


def evaluate_attention():
    initial = initialize_workspace(capacity=1)
    initial["active_goals"] = [{"description": "Find the red key", "status": "active"}]
    event = {"id": "e1", "kind": "user", "source": "user", "content": "Begin the task."}
    memory = {"id": "m1", "content": "The red key is beside the lamp.",
              "kind": "user_statement", "source_event_ids": ["s1"], "confidence": 1.0}
    manas = {"observations": [], "interpretations": [], "uncertainties": [],
             "retrieval_query": "", "salience": 0.1}
    initial = update_attention(initial, event, manas, [memory], capacity=1)
    responsive = initialize_workspace(initial, capacity=1)
    ablated = copy.deepcopy(responsive)
    ablated["active_goals"] = []
    distractor = {"id": "e2", "kind": "user", "source": "user",
                  "content": "The weather is overcast."}
    manas["salience"] = 0.5
    responsive = update_attention(responsive, distractor, manas, [], capacity=1)
    ablated = update_attention(ablated, distractor, manas, [], capacity=1)
    focused = focus_context(responsive)
    unfocused = focus_context(ablated)
    return {
        "name": "goal_sensitive_attention",
        "comparison": "retained goal versus removed goal; same candidates and capacity",
        "capacity": 1,
        "responsive_focus": focused,
        "ablated_focus": unfocused,
        "checks": {
            "responsive_retains_relevant_memory": any(x.get("memory_id") == "m1" for x in focused),
            "goal_ablation_changes_selection": [x["id"] for x in focused] != [x["id"] for x in unfocused],
            "both_conditions_remain_bounded": len(focused) <= 1 and len(unfocused) <= 1,
        },
        "scope": "Availability of task-relevant evidence, not autonomous task completion.",
    }


def evaluate():
    cases = [evaluate_belief_retention(), evaluate_control_learning(), evaluate_attention()]
    return {
        "evaluation": "AI-AM deterministic component comparisons",
        "version": 1,
        "model_inference": False,
        "runtime_integration_test": False,
        "state": "RAM only",
        "cases": cases,
        "component_checks_passed": all(all(case["checks"].values()) for case in cases),
        "limitations": [
            "Fixed small scenarios, not randomized statistical estimates.",
            "No live LLM, language-quality, hardware-performance, or subjective-experience test.",
            "Ablations assess the named mechanism; their scores are not a consciousness measure.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    result = evaluate()
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
    return 0 if result["component_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
