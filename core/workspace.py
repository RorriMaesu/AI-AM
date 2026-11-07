"""Persistent, bounded working attention for AI-AM.

Scores are inspectable scheduling heuristics, never measures of consciousness.
These pure helpers use episode ticks, not wall-clock time. The original event
and evidence store remain authoritative; a focus item is only a context cue.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re

MAX_CAPACITY = 12
MAX_ITEMS = 48
MAX_MISSED = 24
ITEM_TTL = 6
ROLES = ("manas", "chitta", "ahamkara", "buddhi")
_STOP = frozenset("a an the is are was were be been i my me you your it its of to in on at and or for this that these those with from please remember find locate".split())
_CONFLICT = frozenset(("correction", "corrected", "contradiction", "contradictory", "inconsistent", "conflict"))


def _capacity(value):
    if type(value) is not int or not 1 <= value <= MAX_CAPACITY:
        raise ValueError("Attention capacity must be an integer from 1 to 12.")
    return value


def _text(value, limit=600):
    return value[:limit] if isinstance(value, str) else ""


def _tokens(value):
    return set(re.findall(r"[a-z0-9]+", _text(value, 8000).casefold())) - _STOP


def _unit(value, default=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return min(1.0, max(0.0, float(value)))


def _ids(values):
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(v for v in values if isinstance(v, str) and 0 < len(v) <= 128))[:24]


def _overlap(left, right):
    return len(left & right) / max(1, len(right))


def _goal_relevance(text, goals):
    words = _tokens(text)
    active = [g.get("description", "") for g in goals
              if isinstance(g, dict) and g.get("status", "active") == "active"]
    return max((_overlap(words, _tokens(goal)) for goal in active), default=0.0)


def _missed(item, reason):
    return {"id": item["id"], "text": item["text"][:160],
            "source_ids": item["source_ids"][:], "reason": reason,
            "score": item.get("score", 0.0)}


def _select(workspace, extra_missed=()):
    attention = workspace["attention"]
    tick, capacity = attention["tick"], attention["capacity"]
    previous = set(attention.get("selected_ids", []))
    candidates, missed = [], list(extra_missed)
    for item in attention["items"]:
        age = max(0, tick - item["last_tick"])
        if tick > item["expires_tick"]:
            missed.append(_missed(item, "expired"))
            continue
        goal = _goal_relevance(item["text"], workspace.get("active_goals", []))
        item["goal_relevance"] = goal
        reasons = {
            "salience": round(.40 * item["salience"], 4),
            "novelty": round(.20 * item["novelty"], 4),
            "goal_relevance": round(.35 * goal, 4),
            "conflict": round(.55 * item["conflict"], 4),
            "age_decay": round(.80 ** age, 4),
            "continuity": .03 if item["id"] in previous else 0.0,
        }
        item["score"] = round(
            (reasons["salience"] + reasons["novelty"] + reasons["goal_relevance"] +
             reasons["conflict"]) * reasons["age_decay"] + reasons["continuity"], 4)
        item["score_components"] = reasons
        candidates.append(item)
    candidates.sort(key=lambda item: (-item["score"], -item["last_tick"], item["id"]))
    pool_limit = min(MAX_ITEMS, max(24, capacity * 4))
    for item in candidates[pool_limit:]:
        missed.append(_missed(item, "candidate_limit"))
    attention["items"] = candidates[:pool_limit]
    attention["selected_ids"] = [item["id"] for item in candidates[:capacity]]
    for item in candidates[capacity:pool_limit]:
        missed.append(_missed(item, "below_focus_capacity"))
    # One bounded diagnostic for each unselected/expired item.
    by_id = {item["id"]: item for item in missed}
    attention["missed"] = list(by_id.values())[:MAX_MISSED]
    attention["selection_method"] = "deterministic weighted salience, novelty, goal relevance, conflict and age"
    return workspace


def initialize_workspace(previous=None, capacity=6):
    """Begin an episode, retaining attention while clearing transient decisions.

    Set returned workspace['active_goals'] to the current durable goals before
    update_attention. A legacy v2 workspace is accepted and starts fresh focus.
    Inputs are never mutated. Calling once per *episode*, not per role, advances age.
    """
    capacity = _capacity(capacity)
    previous = previous if isinstance(previous, dict) else {}
    prior = previous.get("attention")
    if isinstance(prior, dict) and prior.get("schema_version") == 1:
        attention = copy.deepcopy(prior)
        attention["tick"] += 1
        attention["capacity"] = capacity
    else:
        attention = {"schema_version": 1, "tick": 1, "capacity": capacity,
                     "items": [], "selected_ids": [], "missed": [], "broadcast_version": 0}
    attention["broadcasts"] = []
    attention["current_event_id"] = None
    workspace = {
        "observations": [], "interpretations": [], "memories": [], "uncertainties": [],
        "self_assessment": {}, "decision": {}, "tool_results": [],
        "active_goals": copy.deepcopy(previous.get("active_goals", [])),
        "attention": attention,
    }
    return _select(workspace)


def _upsert(workspace, identifier, text, kind, sources, salience, conflict=0.0, memory_id=None):
    attention = workspace["attention"]
    text = _text(text)
    if not text.strip():
        return
    tick = attention["tick"]
    items = attention["items"]
    item = next((item for item in items if item["id"] == identifier), None)
    if item is not None:
        # Re-reading an unchanged record does not make it novel again.
        novelty = 0.0 if item["text"] == text else 1.0
        item.update(text=text, source_ids=_ids(sources), salience=_unit(salience),
                    conflict=max(item["conflict"], _unit(conflict)),
                    novelty=novelty, last_tick=tick, expires_tick=tick + ITEM_TTL)
    else:
        words = _tokens(text)
        similarity = max((len(words & _tokens(old["text"])) /
                          max(1, len(words | _tokens(old["text"]))) for old in items), default=0.0)
        item = {"id": identifier, "text": text, "kind": kind,
                "source_ids": _ids(sources), "memory_id": memory_id,
                "salience": _unit(salience), "novelty": round(1.0 - similarity, 4),
                "conflict": _unit(conflict), "first_tick": tick, "last_tick": tick,
                "expires_tick": tick + ITEM_TTL}
        items.append(item)


def update_attention(workspace, event, manas, memories, capacity=6):
    """Rank current input and real retrieved records alongside retained context.

    The event must be the claimed event, never a future queued message. Superseded
    memories leave the focus pool when their replacement is provided. Model
    interpretations do not silently become observations or memory records.
    """
    result = copy.deepcopy(workspace)
    result["attention"]["capacity"] = _capacity(capacity)
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id or len(event_id) > 128:
        raise ValueError("Attention input requires a real event ID.")
    text = _text(event.get("content"))
    result["attention"]["current_event_id"] = event_id
    conflict = bool(_tokens(text) & _CONFLICT)
    _upsert(result, "event:" + event_id, text, event.get("kind", "event"),
            [event_id], _unit(manas.get("salience"), .5), float(conflict))
    records = [m for m in memories if isinstance(m, dict)]
    superseded = {m.get("supersedes") for m in records if m.get("supersedes")}
    superseded.update(m.get("id") for m in records if m.get("superseded_by"))
    removed = [item for item in result["attention"]["items"] if item.get("memory_id") in superseded]
    result["attention"]["items"] = [item for item in result["attention"]["items"]
                                   if item.get("memory_id") not in superseded]
    event_words = _tokens(text)
    for memory in records[:MAX_ITEMS]:
        identifier = memory.get("id")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 128 or identifier in superseded:
            continue
        content = _text(memory.get("content"))
        if not content.strip():
            continue
        relevance = _overlap(_tokens(content), event_words)
        _upsert(result, "memory:" + identifier, content, "memory",
                _ids([identifier] + _ids(memory.get("source_event_ids", []))),
                .25 + .35 * relevance, float(bool(memory.get("supersedes"))),
                memory_id=identifier)
        item = next(item for item in result["attention"]["items"] if item["id"] == "memory:" + identifier)
        item["memory_kind"] = _text(memory.get("kind"), 40)
    return _select(result, [_missed(item, "superseded") for item in removed])


def broadcast(workspace, role, result):
    """Publish a bounded faculty contribution and reselect on memory conflicts.

    This records communication, not private reasoning. Chitta's selections affect
    priority; its contradiction assessments remain labelled assessments.
    """
    if role not in ROLES:
        raise ValueError("Unknown cognitive role.")
    updated = copy.deepcopy(workspace)
    attention = updated["attention"]
    # Maintain the existing engine handoff fields as well as bounded broadcast
    # diagnostics. Later faculties must receive the actual earlier proposals.
    contribution = copy.deepcopy(result)
    if role == "manas":
        updated["manas"] = contribution
        for field in ("observations", "interpretations", "uncertainties"):
            updated[field] = copy.deepcopy(result.get(field, []))
    elif role == "chitta":
        updated["chitta"] = contribution
    elif role == "ahamkara":
        updated["self_assessment"] = contribution
    elif role == "buddhi":
        updated["decision"] = contribution
    if role == "chitta":
        selected = set(_ids(result.get("selected_memory_ids", [])))
        for item in attention["items"]:
            if item.get("memory_id") in selected:
                item["salience"] = min(1.0, item["salience"] + .20)
        for conflict in result.get("contradictions", [])[:4]:
            conflict = _text(conflict)
            if not conflict.strip():
                continue
            related = [item for item in attention["items"]
                       if _tokens(conflict) & _tokens(item["text"])]
            for item in related:
                item["conflict"] = 1.0
            sources = _ids([source for item in related for source in item["source_ids"]])
            if attention.get("current_event_id"):
                sources = _ids(sources + [attention["current_event_id"]])
            identifier = "assessment:" + hashlib.sha256(conflict.encode("utf-8")).hexdigest()[:20]
            _upsert(updated, identifier, conflict, "assessment", sources, .7, 1.0)
    fields = {"manas": ("observations", "uncertainties"), "chitta": ("associations", "contradictions"),
              "ahamkara": ("agency",), "buddhi": ("rationale", "action")}[role]
    summary = []
    for field in fields:
        value = result.get(field, "")
        summary.extend(_text(item, 240) for item in (value if isinstance(value, list) else [value])
                       if isinstance(item, str) and item.strip())
    attention["broadcast_version"] += 1
    _select(updated)
    attention["broadcasts"].append({
        "version": attention["broadcast_version"], "tick": attention["tick"], "role": role,
        "summary": summary[:4], "selected_ids": attention["selected_ids"][:],
    })
    attention["broadcasts"] = attention["broadcasts"][-8:]
    return updated


def focus_context(workspace):
    """Return bounded, source-linked attention cues; never replace raw evidence."""
    attention = workspace.get("attention", {})
    selected = set(attention.get("selected_ids", []))
    capacity = _capacity(attention.get("capacity", 6))
    return [{"id": item["id"], "text": item["text"][:240], "kind": item["kind"],
             "memory_id": item.get("memory_id"), "memory_kind": item.get("memory_kind"),
             "source_ids": item["source_ids"][:],
             "score": item["score"], "score_components": copy.deepcopy(item["score_components"])}
            for item in attention.get("items", []) if item["id"] in selected][:capacity]
