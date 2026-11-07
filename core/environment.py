"""Small deterministic simulated world with partial observations.

Snapshots contain evaluator-only truth. Never send a snapshot to inference: use
observe_environment(). No filesystem, network, robot, or real-world actions exist.
"""
from __future__ import annotations

from copy import deepcopy

ACTIONS = ("look", "turn_left", "turn_right", "move_forward", "inspect",
           "pick_up", "drop", "wait")
DIRECTIONS = ("north", "east", "south", "west")
VECTORS = ((0, -1), (1, 0), (0, 1), (-1, 0))
WIDTH, HEIGHT, VISION_RANGE, MAX_STEPS = 9, 7, 4, 256
WALLS = {(x, y) for x in range(WIDTH) for y in range(HEIGHT)
         if x in (0, WIDTH - 1) or y in (0, HEIGHT - 1)}
WALLS |= {(4, y) for y in range(1, 5)}
OBJECTS = {
    "key": {"name": "brass key", "portable": True, "position": [3, 3]},
    "cup": {"name": "blue cup", "portable": True, "position": [6, 3]},
    "door": {"name": "closed door", "portable": False, "position": [7, 5]},
}


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Invalid {name}")
    return value


def _position(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("Position must be [x, y]")
    x = _integer(value[0], 0, WIDTH - 1, "x")
    y = _integer(value[1], 0, HEIGHT - 1, "y")
    if (x, y) in WALLS:
        raise ValueError("Position is inside a wall")
    return [x, y]


def _validate(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        raise ValueError("Unsupported environment snapshot")
    if set(snapshot) != {"version", "tick", "steps", "pose", "energy", "inventory",
                         "objects", "controls"}:
        raise ValueError("Unexpected environment snapshot fields")
    _integer(snapshot["tick"], 0, 1000000, "tick")
    _integer(snapshot["steps"], 0, MAX_STEPS, "steps")
    _integer(snapshot["energy"], 0, 100, "energy")
    pose = snapshot["pose"]
    if not isinstance(pose, dict) or set(pose) != {"position", "heading"}:
        raise ValueError("Invalid pose")
    _position(pose["position"])
    if pose["heading"] not in DIRECTIONS:
        raise ValueError("Invalid heading")
    inventory = snapshot["inventory"]
    if (not isinstance(inventory, list) or len(inventory) > 1
            or any(item not in ("key", "cup") for item in inventory)):
        raise ValueError("Invalid inventory")
    objects = snapshot["objects"]
    if not isinstance(objects, dict) or set(objects) != set(OBJECTS):
        raise ValueError("Invalid objects")
    positions = []
    for object_id, item in objects.items():
        if (not isinstance(item, dict) or set(item) != {"name", "portable", "position"}
                or item["name"] != OBJECTS[object_id]["name"]
                or item["portable"] is not OBJECTS[object_id]["portable"]):
            raise ValueError("Invalid object")
        if object_id in inventory:
            if item["position"] is not None:
                raise ValueError("Carried objects must have no world position")
        else:
            positions.append(tuple(_position(item["position"])))
    if len(positions) != len(set(positions)):
        raise ValueError("Objects cannot overlap")
    controls = snapshot["controls"]
    allowed = {"turn_left", "turn_right", "move_forward"}
    if (not isinstance(controls, dict) or set(controls) != allowed
            or any(value not in allowed for value in controls.values())):
        raise ValueError("Invalid control mapping")


def new_environment(seed=0):
    """Return fresh private truth. The validated seed does not randomize this fixed room."""
    _integer(seed, 0, 2147483647, "seed")
    return {"version": 1, "tick": 0, "steps": 0,
            "pose": {"position": [2, 3], "heading": "east"}, "energy": 100,
            "inventory": [], "objects": deepcopy(OBJECTS),
            "controls": {action: action for action in
                         ("turn_left", "turn_right", "move_forward")}}


def _line_clear(start, end):
    """Conservative Bresenham visibility, including corner occlusion."""
    x, y = start
    tx, ty = end
    dx, dy = abs(tx - x), -abs(ty - y)
    sx, sy = 1 if x < tx else -1, 1 if y < ty else -1
    error = dx + dy
    while (x, y) != (tx, ty):
        old_x, old_y = x, y
        twice = 2 * error
        if twice >= dy:
            error += dy
            x += sx
        if twice <= dx:
            error += dx
            y += sy
        if x != old_x and y != old_y:
            if (x, old_y) in WALLS or (old_x, y) in WALLS:
                return False
        if (x, y) in WALLS and (x, y) != (tx, ty):
            return False
    return True


def observe_environment(snapshot):
    """Return only egocentrically visible cells/objects and own body telemetry."""
    _validate(snapshot)
    x, y = snapshot["pose"]["position"]
    forward = VECTORS[DIRECTIONS.index(snapshot["pose"]["heading"])]
    cells = []
    visible = set()
    for cy in range(HEIGHT):
        for cx in range(WIDTH):
            dx, dy = cx - x, cy - y
            depth = dx * forward[0] + dy * forward[1]
            lateral = dx * -forward[1] + dy * forward[0]
            if ((dx, dy) == (0, 0) or
                    (depth >= abs(lateral) and dx * dx + dy * dy <= VISION_RANGE ** 2)):
                if _line_clear((x, y), (cx, cy)):
                    visible.add((cx, cy))
                    cells.append({"position": [cx, cy],
                                  "kind": "wall" if (cx, cy) in WALLS else "floor"})
    objects = [
        {"id": object_id, "name": item["name"], "portable": item["portable"],
         "position": list(item["position"])}
        for object_id, item in snapshot["objects"].items()
        if item["position"] is not None and tuple(item["position"]) in visible
    ]
    return {"version": 1, "tick": snapshot["tick"], "width": WIDTH, "height": HEIGHT,
            "pose": deepcopy(snapshot["pose"]), "energy": snapshot["energy"],
            "inventory": list(snapshot["inventory"]), "visible_cells": cells,
            "objects": objects, "actions": list(ACTIONS),
            "steps_remaining": MAX_STEPS - snapshot["steps"],
            "visibility": {"range": VISION_RANGE, "field_of_view_degrees": 90},
            "simulated": True}


def step_environment(snapshot, action, target=None):
    """Execute one bounded simulated action and return private state + public evidence.

    Unknown actions/targets are rejected. Impossible permitted actions are recorded
    as unsuccessful outcomes. Their failure never reveals an unseen object's location.
    """
    _validate(snapshot)
    if action not in ACTIONS or not isinstance(action, str):
        raise ValueError("Unknown simulated action")
    if target is not None and (not isinstance(target, str) or len(target) > 32):
        raise ValueError("Invalid action target")
    if action not in ("inspect", "pick_up", "drop") and target is not None:
        raise ValueError("This action takes no target")
    state = deepcopy(snapshot)
    before = observe_environment(state)
    outcome = {"action": action, "target": target, "actor": "self",
               "success": False, "reason": "", "tick": state["tick"],
               "effects": [], "simulated": True}
    if state["steps"] >= MAX_STEPS:
        outcome["reason"] = "episode_budget_exhausted"
        return {"state": state, "observation": before, "outcome": outcome}
    cost = 2 if action in ("move_forward", "pick_up") else (0 if action == "wait" else 1)
    state["tick"] += 1
    state["steps"] += 1
    outcome["tick"] = state["tick"]
    if state["energy"] < cost:
        outcome["reason"] = "insufficient_simulated_energy"
        return {"state": state, "observation": observe_environment(state), "outcome": outcome}
    state["energy"] -= cost
    effective = state["controls"].get(action, action)
    heading = DIRECTIONS.index(state["pose"]["heading"])
    position = state["pose"]["position"]
    if effective in ("turn_left", "turn_right"):
        turn = -1 if effective == "turn_left" else 1
        state["pose"]["heading"] = DIRECTIONS[(heading + turn) % 4]
        outcome.update(success=True, reason="orientation_changed")
    elif effective == "move_forward":
        dx, dy = VECTORS[heading]
        destination = [position[0] + dx, position[1] + dy]
        door = state["objects"]["door"]["position"]
        if tuple(destination) in WALLS or destination == door:
            outcome["reason"] = "movement_blocked"
        else:
            state["pose"]["position"] = destination
            outcome.update(success=True, reason="position_changed")
    elif action == "wait":
        state["energy"] = min(100, state["energy"] + 2)
        outcome.update(success=True, reason="waited")
    elif action == "look":
        outcome.update(success=True, reason="observed")
    elif action in ("inspect", "pick_up"):
        item = next((item for item in before["objects"] if item["id"] == target), None)
        if item is None:
            outcome["reason"] = "target_not_visible"
        elif action == "inspect":
            outcome.update(success=True, reason="inspected_visible_object")
            outcome["effects"].append({"kind": "inspection", "object": deepcopy(item)})
        elif sum(abs(a - b) for a, b in zip(position, item["position"])) > 1:
            outcome["reason"] = "target_out_of_reach"
        elif not item["portable"]:
            outcome["reason"] = "target_not_portable"
        elif state["inventory"]:
            outcome["reason"] = "inventory_full"
        else:
            state["inventory"] = [target]
            state["objects"][target]["position"] = None
            outcome.update(success=True, reason="picked_up")
            outcome["effects"].append({"kind": "carried", "object_id": target})
    elif action == "drop":
        if target not in state["inventory"]:
            outcome["reason"] = "target_not_carried"
        elif any(item["position"] == position for item in state["objects"].values()):
            outcome["reason"] = "drop_cell_occupied"
        else:
            state["inventory"] = []
            state["objects"][target]["position"] = list(position)
            outcome.update(success=True, reason="dropped")
            outcome["effects"].append({"kind": "dropped", "object_id": target,
                                       "position": list(position)})
    return {"state": state, "observation": observe_environment(state), "outcome": outcome}


def intervene_environment(snapshot, intervention):
    """Evaluator-only intervention. Its private result must not enter model prompts."""
    _validate(snapshot)
    if not isinstance(intervention, dict):
        raise ValueError("Invalid intervention")
    state = deepcopy(snapshot)
    kind = intervention.get("kind")
    if kind == "move_object" and set(intervention) == {"kind", "object_id", "position"}:
        object_id = intervention["object_id"]
        if object_id not in ("key", "cup") or object_id in state["inventory"]:
            raise ValueError("Only uncarried portable objects can be moved")
        destination = _position(intervention["position"])
        if any(item["position"] == destination for key, item in state["objects"].items()
               if key != object_id):
            raise ValueError("Destination occupied")
        state["objects"][object_id]["position"] = destination
    elif kind == "alter_controls" and set(intervention) == {"kind", "mapping"}:
        mapping = intervention["mapping"]
        allowed = {"turn_left", "turn_right", "move_forward"}
        if (not isinstance(mapping, dict) or not mapping or not set(mapping) <= allowed
                or any(value not in allowed for value in mapping.values())):
            raise ValueError("Invalid control intervention")
        state["controls"].update(mapping)
    else:
        raise ValueError("Unknown intervention")
    state["tick"] += 1
    _validate(state)
    return state


def evaluator_environment(snapshot):
    """Full truth for explicitly labelled evaluator views only."""
    _validate(snapshot)
    return {**deepcopy(snapshot), "width": WIDTH, "height": HEIGHT,
            "walls": [list(position) for position in sorted(WALLS)]}
