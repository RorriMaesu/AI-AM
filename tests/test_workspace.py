"""Attention is tested as a causal scheduling mechanism, not a consciousness score."""
import copy
import json
import pytest

from core.workspace import broadcast, focus_context, initialize_workspace, update_attention


def event(identifier="e1", text="Find the red key.", kind="user"):
    return {"id": identifier, "content": text, "kind": kind}


def memory(identifier="m1", text="The red key is beside the lamp.", **extra):
    return {"id": identifier, "content": text, "kind": "user_statement",
            "source_event_ids": ["source-" + identifier], **extra}


def attend(workspace, evt=None, memories=(), salience=.5, capacity=6):
    return update_attention(workspace, evt or event(), {"salience": salience}, list(memories), capacity)


def test_state_survives_json_restore_but_transient_actions_do_not():
    one = attend(initialize_workspace(), memories=[memory()])
    one["decision"] = {"action": "calculate"}
    one["tool_results"] = [{"result": 391}]
    saved = json.loads(json.dumps(one))
    two = initialize_workspace(saved)
    assert {x["id"] for x in focus_context(one)} == {x["id"] for x in focus_context(two)}
    assert two["attention"]["tick"] == 2
    assert two["decision"] == {} and two["tool_results"] == []
    assert one["tool_results"] == [{"result": 391}]


def test_helpers_do_not_mutate_input_or_caller_evidence():
    workspace, evt, record = initialize_workspace(), event(), memory()
    before = copy.deepcopy((workspace, evt, record))
    updated = attend(workspace, evt, [record])
    published = broadcast(updated, "chitta", {"selected_memory_ids": ["m1"], "contradictions": []})
    assert (workspace, evt, record) == before
    assert updated["attention"]["broadcast_version"] == 0
    assert published["attention"]["broadcast_version"] == 1


def test_salience_changes_which_event_gets_focus():
    low = attend(initialize_workspace(capacity=1), event("first", "An orange triangle."), salience=.1, capacity=1)
    high = attend(initialize_workspace(low, capacity=1), event("second", "An amber circle."), salience=1, capacity=1)
    assert focus_context(high)[0]["id"] == "event:second"
    assert any(x["id"] == "event:first" and x["reason"] == "below_focus_capacity"
               for x in high["attention"]["missed"])


def test_active_goal_retains_relevant_memory_across_distractor():
    workspace = initialize_workspace(capacity=1)
    workspace["active_goals"] = [{"description": "Find the red key", "status": "active"}]
    workspace = attend(workspace, event("e1", "Begin the task."), [memory()], salience=.1, capacity=1)
    assert focus_context(workspace)[0]["memory_id"] == "m1"
    next_episode = initialize_workspace(workspace, capacity=1)
    distractor = event("e2", "The weather is overcast.")
    workspace = attend(next_episode, distractor, salience=.5, capacity=1)
    assert focus_context(workspace)[0]["memory_id"] == "m1"
    # The two conditions differ only in active goals, not inputs or salience.
    ablated = copy.deepcopy(next_episode)
    ablated["active_goals"] = []
    ablated = attend(ablated, distractor, salience=.5, capacity=1)
    assert focus_context(ablated)[0]["id"] == "event:e2"


def test_completed_goal_no_longer_adds_priority():
    workspace = initialize_workspace()
    workspace["active_goals"] = [{"description": "Find the red key", "status": "completed"}]
    workspace = attend(workspace, memories=[memory()])
    candidate = next(x for x in workspace["attention"]["items"] if x.get("memory_id") == "m1")
    assert candidate["score_components"]["goal_relevance"] == 0


def test_repeated_identical_content_is_not_permanently_novel():
    workspace = attend(initialize_workspace(), event("one", "A violet cube."), salience=.5)
    workspace = attend(initialize_workspace(workspace), event("two", "A violet cube."), salience=.5)
    repeat = next(x for x in workspace["attention"]["items"] if x["id"] == "event:two")
    assert repeat["novelty"] == 0


def test_age_decays_and_expires_without_a_wall_clock():
    workspace = attend(initialize_workspace(), event("old", "The old task."), salience=1)
    initial = focus_context(workspace)[0]["score"]
    workspace = initialize_workspace(workspace)
    assert focus_context(workspace)[0]["score"] < initial
    for _ in range(6):
        workspace = initialize_workspace(workspace)
    assert focus_context(workspace) == []
    assert workspace["attention"]["missed"][0]["reason"] == "expired"


def test_corrected_memory_removes_superseded_focus_preserving_provenance():
    workspace = attend(initialize_workspace(), memories=[memory()])
    replacement = memory("m2", "Correction: the red key is in the drawer.", supersedes="m1")
    workspace = attend(initialize_workspace(workspace), event("e2", "Use the correction."), [replacement])
    assert not any(x.get("memory_id") == "m1" for x in workspace["attention"]["items"])
    item = next(x for x in focus_context(workspace) if x["memory_id"] == "m2")
    assert item["source_ids"] == ["m2", "source-m2"]
    assert any(x["id"] == "memory:m1" and x["reason"] == "superseded"
               for x in workspace["attention"]["missed"])


def test_chitta_conflict_changes_attention_and_is_not_an_observation():
    workspace = attend(initialize_workspace(capacity=1), event("e1", "Think about geometry."),
                       [memory()], salience=1, capacity=1)
    prior = focus_context(workspace)[0]["id"]
    workspace = broadcast(workspace, "chitta", {
        "selected_memory_ids": ["m1"], "contradictions": ["Conflict: the red key has two locations."]})
    focus = focus_context(workspace)
    assert focus[0]["id"] != prior
    assert focus[0]["score_components"]["conflict"] > 0
    assessment = next(x for x in workspace["attention"]["items"] if x["kind"] == "assessment")
    assert "source-m1" in assessment["source_ids"]
    assert "fabricated" not in assessment["source_ids"]
    assert workspace["attention"]["broadcasts"][0]["role"] == "chitta"


def test_candidate_pool_focus_diagnostics_and_broadcasts_are_bounded():
    workspace = initialize_workspace(capacity=3)
    workspace = attend(workspace, memories=[memory(str(i), "Item " + str(i)) for i in range(100)], capacity=3)
    assert len(focus_context(workspace)) == 3
    assert len(workspace["attention"]["items"]) <= 24
    assert len(workspace["attention"]["missed"]) <= 24
    for _ in range(20):
        workspace = broadcast(workspace, "buddhi", {"rationale": "x" * 2000, "action": "reply"})
    assert len(workspace["attention"]["broadcasts"]) == 8
    assert workspace["attention"]["broadcast_version"] == 20
    assert len(workspace["attention"]["broadcasts"][-1]["summary"][0]) == 240


def test_legacy_workspace_and_empty_focus_are_supported():
    workspace = initialize_workspace({"decision": {"reply": "old"}, "observations": ["past"]})
    assert focus_context(workspace) == []
    assert workspace["observations"] == []
    assert workspace["attention"]["tick"] == 1


@pytest.mark.parametrize("capacity", [0, 13, True, 1.5, "6"])
def test_invalid_capacity_is_rejected(capacity):
    with pytest.raises(ValueError):
        initialize_workspace(capacity=capacity)


def test_current_event_must_have_real_identifier():
    with pytest.raises(ValueError):
        attend(initialize_workspace(), {"content": "No ID"})


def test_inference_cannot_invent_focus_evidence():
    workspace = attend(initialize_workspace(), memories=[memory()])
    workspace = broadcast(workspace, "chitta", {
        "selected_memory_ids": ["fabricated"],
        "contradictions": ["An unrelated purported conflict."]})
    assert all("fabricated" not in x["source_ids"] for x in workspace["attention"]["items"])
    assert all(x.get("memory_id") != "fabricated" for x in workspace["attention"]["items"])


def test_broadcast_preserves_complete_faculty_handoffs_without_aliasing():
    workspace = initialize_workspace()
    manas = {"observations": ["an actual event"], "interpretations": ["a possibility"],
             "uncertainties": ["an unknown"], "retrieval_query": "event", "salience": .8}
    chitta = {"selected_memory_ids": [], "associations": ["past context"],
              "contradictions": [], "missing_context": ["missing record"]}
    agency = {"agency": "I ran the calculator.", "commitments": ["Preserve the source."]}
    decision = {"action": "reply", "rationale": "Evidence agrees.", "reply": "A response."}
    workspace = broadcast(workspace, "manas", manas)
    workspace = broadcast(workspace, "chitta", chitta)
    workspace = broadcast(workspace, "ahamkara", agency)
    workspace = broadcast(workspace, "buddhi", decision)
    assert workspace["manas"] == manas
    assert workspace["observations"] == manas["observations"]
    assert workspace["interpretations"] == manas["interpretations"]
    assert workspace["uncertainties"] == manas["uncertainties"]
    assert workspace["chitta"] == chitta
    assert workspace["self_assessment"] == agency
    assert workspace["decision"] == decision
    manas["observations"].append("caller mutation")
    agency["commitments"].clear()
    assert workspace["observations"] == ["an actual event"]
    assert workspace["self_assessment"]["commitments"] == ["Preserve the source."]
