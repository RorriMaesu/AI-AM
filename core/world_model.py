"""Observation-only persistent beliefs and a small learned action forward model.

This is online transition counting, not neural training or a consciousness score.
No environment snapshot, hidden control mapping, or complete map is accepted.
"""
from __future__ import annotations

from copy import deepcopy

ACTIONS = ("look", "turn_left", "turn_right", "move_forward", "inspect",
           "pick_up", "drop", "wait")
DIRECTIONS = ("north", "east", "south", "west")
VECTORS = ((0, -1), (1, 0), (0, 1), (-1, 0))
HISTORY_LIMIT = 32
_EFFECTS = ("0,0,0", "1,0,0", "0,0,-1", "0,0,1")


def _position(value, width, height):
    if (not isinstance(value, list) or len(value) != 2
            or any(type(i) is not int for i in value)
            or not (0 <= value[0] < width and 0 <= value[1] < height)):
        raise ValueError("Invalid observed position")


def _validate_observation(observation):
    expected = {"version", "tick", "width", "height", "pose", "energy", "inventory",
                "visible_cells", "objects", "actions", "steps_remaining",
                "visibility", "simulated"}
    if (not isinstance(observation, dict) or set(observation) != expected
            or observation["version"] != 1 or observation["simulated"] is not True):
        raise ValueError("Expected a public simulated observation, not hidden state")
    for key, high in (("tick", 1000000), ("width", 128), ("height", 128),
                      ("energy", 100), ("steps_remaining", 256)):
        if type(observation[key]) is not int or not 0 <= observation[key] <= high:
            raise ValueError("Invalid observation number")
    width, height = observation["width"], observation["height"]
    pose = observation["pose"]
    if (not isinstance(pose, dict) or set(pose) != {"position", "heading"}
            or pose["heading"] not in DIRECTIONS):
        raise ValueError("Invalid observed pose")
    _position(pose["position"], width, height)
    if observation["actions"] != list(ACTIONS):
        raise ValueError("Unknown observation action surface")
    if observation["visibility"] != {"range": 4, "field_of_view_degrees": 90}:
        raise ValueError("Unknown visibility contract")
    cells = observation["visible_cells"]
    if not isinstance(cells, list) or len(cells) > 1024:
        raise ValueError("Invalid visible cells")
    visible = {}
    for cell in cells:
        if not isinstance(cell, dict) or set(cell) != {"position", "kind"}:
            raise ValueError("Invalid visible cell")
        _position(cell["position"], width, height)
        if cell["kind"] not in ("floor", "wall") or tuple(cell["position"]) in visible:
            raise ValueError("Invalid or duplicate visible cell")
        visible[tuple(cell["position"])] = cell["kind"]
    if visible.get(tuple(pose["position"])) != "floor":
        raise ValueError("Own position must be observed as floor")
    inventory = observation["inventory"]
    if (not isinstance(inventory, list) or len(inventory) > 1
            or any(not isinstance(i, str) or not i or len(i) > 32 for i in inventory)):
        raise ValueError("Invalid observed inventory")
    objects = observation["objects"]
    if not isinstance(objects, list) or len(objects) > 16:
        raise ValueError("Invalid observed objects")
    ids = set()
    for item in objects:
        if (not isinstance(item, dict) or set(item) != {"id", "name", "portable", "position"}
                or not isinstance(item["id"], str) or not 1 <= len(item["id"]) <= 32
                or not isinstance(item["name"], str) or len(item["name"]) > 64
                or type(item["portable"]) is not bool):
            raise ValueError("Invalid observed object")
        _position(item["position"], width, height)
        if (visible.get(tuple(item["position"])) != "floor" or item["id"] in ids
                or item["id"] in inventory):
            raise ValueError("Object is not supported by visible evidence")
        ids.add(item["id"])


def new_world_model(observation=None):
    model = {"version": 1, "last_observation": None, "beliefs": {},
             "transitions": {}, "history": [], "discrepancies": [],
             "calibration": {"samples": 0, "brier_sum": 0.0, "mean_brier": None,
                             "last_brier": None, "correct_predictions": 0}}
    return update_world_model(model, observation) if observation is not None else model


def _copy_model(model):
    if (not isinstance(model, dict) or model.get("version") != 1
            or set(model) != {"version", "last_observation", "beliefs", "transitions",
                             "history", "discrepancies", "calibration"}
            or not isinstance(model["beliefs"], dict)
            or not isinstance(model["transitions"], dict)
            or len(model["history"]) > HISTORY_LIMIT
            or len(model["discrepancies"]) > HISTORY_LIMIT):
        raise ValueError("Invalid world model")
    if model["last_observation"] is not None:
        _validate_observation(model["last_observation"])
    return deepcopy(model)


def _relative_effect(before, after):
    heading = DIRECTIONS.index(before["heading"])
    forward = VECTORS[heading]
    right = (-forward[1], forward[0])
    dx = after["position"][0] - before["position"][0]
    dy = after["position"][1] - before["position"][1]
    turn = (DIRECTIONS.index(after["heading"]) - heading) % 4
    if turn == 3:
        turn = -1
    return f"{dx * forward[0] + dy * forward[1]},{dx * right[0] + dy * right[1]},{turn}"


def _predicted_pose(before, effect):
    ahead, lateral, turn = map(int, effect.split(","))
    heading = DIRECTIONS.index(before["heading"])
    fx, fy = VECTORS[heading]
    rx, ry = -fy, fx
    return {"position": [before["position"][0] + ahead * fx + lateral * rx,
                         before["position"][1] + ahead * fy + lateral * ry],
            "heading": DIRECTIONS[(heading + turn) % 4]}


def predict_action(model, action, target=None):
    """Predict before acting from recent learned transitions and declared weak priors."""
    _copy_model(model)
    if action not in ACTIONS or not isinstance(action, str):
        raise ValueError("Unknown simulated action")
    if target is not None and (not isinstance(target, str) or len(target) > 32):
        raise ValueError("Invalid target")
    before = model["last_observation"]
    if before is None:
        raise ValueError("Observe before predicting")
    preferred = {"move_forward": "1,0,0", "turn_left": "0,0,-1",
                 "turn_right": "0,0,1"}.get(action, "0,0,0")
    prior = {effect: (3 if effect == preferred else 1) for effect in _EFFECTS}
    if action not in ("move_forward", "turn_left", "turn_right"):
        prior = {"0,0,0": 1}
    counts = model["transitions"].get(action, {})
    if (not isinstance(counts, dict) or any(type(n) is not int or not 0 <= n <= HISTORY_LIMIT
                                           for n in counts.values())):
        raise ValueError("Invalid learned transition counts")
    for effect, count in counts.items():
        if not isinstance(effect, str) or effect not in _EFFECTS:
            raise ValueError("Invalid learned effect")
        prior[effect] = prior.get(effect, 0) + count
    total = sum(prior.values())
    distribution = {effect: count / total for effect, count in prior.items()}
    best = max(distribution, key=lambda effect: (distribution[effect], effect == preferred))
    return {"action": action, "target": target, "before_tick": before["tick"],
            "before_pose": deepcopy(before["pose"]), "distribution": distribution,
            "predicted_effect": best, "predicted_pose": _predicted_pose(before["pose"], best),
            "confidence": distribution[best], "samples": sum(counts.values()),
            "basis": "recent_observed_transitions_with_prior" if counts else "untrained_prior"}


def _ownership(outcome):
    actor = outcome.get("actor") if isinstance(outcome, dict) else None
    return "self" if actor == "self" else ("other" if actor in ("user", "other") else "unknown")


def update_world_model(model, observation, action=None, outcome=None, prediction=None):
    """Update from public evidence only; never infer a hidden object's new location.

    Predictions are checked against the prior model before scoring to prevent
    retrospective forecasts. Actor attribution comes from the execution record,
    not from an LLM narrative. Both manual and agent-driven control provide data.
    """
    result = _copy_model(model)
    _validate_observation(observation)
    before = result["last_observation"]
    if before is not None and observation["tick"] < before["tick"]:
        raise ValueError("Observations must not move backward in time")
    if action is not None and action not in ACTIONS:
        raise ValueError("Unknown action")
    if prediction is not None:
        if before is None or action is None:
            raise ValueError("A forecast needs a prior observation and action")
        expected = predict_action(result, action, prediction.get("target"))
        if prediction != expected:
            raise ValueError("Forecast must match the model before the action")
    effect, brier = None, None
    actor = outcome.get("actor", "unknown") if isinstance(outcome, dict) else "unknown"
    execution = (before is not None and action is not None and isinstance(outcome, dict)
                 and outcome.get("action") == action and type(outcome.get("success")) is bool
                 and outcome.get("tick") == observation["tick"]
                 and observation["tick"] > before["tick"]
                 and observation["steps_remaining"] == before["steps_remaining"] - 1
                 and actor in ("self", "user"))
    if execution:
        effect = _relative_effect(before["pose"], observation["pose"])
        if effect not in _EFFECTS:
            raise ValueError("Observed transition exceeds one simulated action")
        if prediction is not None:
            distribution = prediction["distribution"]
            brier = sum((prob - float(key == effect)) ** 2
                        for key, prob in distribution.items())
            if effect not in distribution:
                brier += 1.0
            calibration = result["calibration"]
            calibration["samples"] += 1
            calibration["brier_sum"] += brier
            calibration["mean_brier"] = calibration["brier_sum"] / calibration["samples"]
            calibration["last_brier"] = brier
            calibration["correct_predictions"] += int(prediction["predicted_effect"] == effect)
    visible_cells = {tuple(cell["position"]) for cell in observation["visible_cells"]}
    seen = {item["id"]: item for item in observation["objects"]}
    carried = set(observation["inventory"])
    changes = []
    for object_id, belief in result["beliefs"].items():
        if object_id in seen:
            continue
        old_position = belief.get("position")
        if object_id in carried:
            belief.update(kind="observed", position=None, carried=True, location_status="carried",
                          last_seen_tick=observation["tick"])
        elif old_position is not None and tuple(old_position) in visible_cells:
            belief.update(kind="inferred", position=None, carried=False, location_status="unknown")
            changes.append({"tick": observation["tick"], "object_id": object_id,
                            "kind": "missing_from_visible_cell", "previous_position": old_position,
                            "observed_position": None, "ownership": "unknown"})
        elif belief.get("carried"):
            belief.update(kind="inferred", position=None, carried=False, location_status="unknown")
        else:
            belief["kind"] = "remembered"
            if belief["position"] is not None:
                belief["location_status"] = "last_seen"
    for object_id, item in seen.items():
        previous = result["beliefs"].get(object_id)
        if (previous and previous.get("last_observed_position") is not None
                and previous["last_observed_position"] != item["position"]):
            caused = (execution and outcome.get("success") and action == "drop"
                      and outcome.get("target") == object_id)
            changes.append({"tick": observation["tick"], "object_id": object_id,
                            "kind": "location_changed",
                            "previous_position": previous["last_observed_position"],
                            "observed_position": list(item["position"]),
                            "ownership": _ownership(outcome) if caused else "unknown"})
        result["beliefs"][object_id] = {
            "id": object_id, "name": item["name"], "kind": "observed",
            "position": list(item["position"]), "last_observed_position": list(item["position"]),
            "last_seen_tick": observation["tick"], "location_status": "visible", "carried": False}
    for object_id in carried:
        if object_id not in result["beliefs"]:
            result["beliefs"][object_id] = {
                "id": object_id, "name": object_id, "kind": "observed", "position": None,
                "last_observed_position": None, "last_seen_tick": observation["tick"],
                "location_status": "carried", "carried": True}
    result["history"].append({"tick": observation["tick"], "action": action, "actor": actor,
                              "ownership": _ownership(outcome), "effect": effect,
                              "prediction_error": brier})
    result["history"] = result["history"][-HISTORY_LIMIT:]
    counts = {}
    for item in result["history"]:
        if item["effect"] is not None:
            bucket = counts.setdefault(item["action"], {})
            bucket[item["effect"]] = bucket.get(item["effect"], 0) + 1
    result["transitions"] = counts
    result["discrepancies"] = (result["discrepancies"] + changes)[-HISTORY_LIMIT:]
    result["last_observation"] = deepcopy(observation)
    return result
