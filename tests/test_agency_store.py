"""Action ownership, crash recovery, and atomic simulated effects."""
import sqlite3

import pytest

from database.db_manager import MindStore


@pytest.fixture
def store():
    value = MindStore(":memory:")
    yield value
    value.close()


def proposal(store, name="calculation", actor="self"):
    event = store.enqueue("Please calculate 17 * 23.")
    return store.propose_action(
        name, event["id"], "calculator", {"expression": "17*23"}, actor,
        prediction={"expected": 391},
    )


def attempted(store, name="calculation"):
    action = proposal(store, name)
    store.transition_action(name, "authorized")
    store.transition_action(name, "attempted")
    return action


def test_proposal_records_intent_and_cannot_skip_authorization(store):
    action = proposal(store)
    assert action["status"] == "proposed"
    assert action["actor"] == "self"
    assert action["source_event_ids"] == [action["episode_id"]]
    assert action["outcome_event_id"] is None
    assert len(store.list_events()) == 1
    with pytest.raises(ValueError, match="transition"):
        store.transition_action(action["id"], "attempted")
    with pytest.raises(ValueError, match="transition"):
        store.commit_action_outcome(action["id"], "succeeded", 391)
    assert store.get_action(action["id"]) == action


def test_proposal_is_idempotent_and_identity_cannot_be_reused(store):
    action = proposal(store)
    assert store.propose_action(
        action["id"], action["episode_id"], "calculator", {"expression": "17*23"},
        prediction={"expected": 391},
    ) == action
    with pytest.raises(ValueError, match="different proposal"):
        store.propose_action(
            action["id"], action["episode_id"], "calculator", {"expression": "1+1"}
        )
    assert len(store.list_actions()) == 1
    assert len(store.get_action(action["id"])["transitions"]) == 1


def test_calculator_outcome_records_actual_source_and_immutable_lifecycle(store):
    action = attempted(store)
    outcome = store.commit_action_outcome(action["id"], "succeeded", 391)
    assert outcome["event"]["content"] == "17*23 = 391"
    assert outcome["event"]["source"] == "calculator"
    assert outcome["event"]["metadata"]["result"] == 391
    assert outcome["event"]["metadata"]["actor"] == "self"
    assert outcome["event"]["episode_id"] == action["episode_id"]
    assert outcome["action"]["outcome_event_id"] == outcome["event"]["id"]
    assert [t["status"] for t in outcome["action"]["transitions"]] == [
        "proposed", "authorized", "attempted", "succeeded"
    ]
    with store._connection(write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE mind_action_transitions SET status='failed'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM mind_action_transitions")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE mind_actions SET actor='user'")
    assert store.get_action(action["id"]) == outcome["action"]


def test_terminal_retry_returns_exact_original_outcome_and_no_new_effects(store):
    action = attempted(store)
    first = store.commit_action_outcome(
        action["id"], "succeeded", 391, {"position": 1}, expected_revision=0
    )
    store.save_experiment({"position": 2}, expected_revision=1)
    again = store.commit_action_outcome(
        action["id"], "failed", {"invented": True}, {"position": 99},
        expected_revision=0, error="Late error",
    )
    assert again == first
    assert store.load_experiment()["snapshot"] == {"position": 2}
    assert store.load_experiment()["revision"] == 2
    assert len([e for e in store.list_events() if e["kind"] == "tool"]) == 1
    with pytest.raises(ValueError, match="transition"):
        store.transition_action(action["id"], "attempted")


def test_experiment_state_is_separate_revision_checked_and_copied(store):
    assert store.load_experiment() == {"revision": 0, "snapshot": None, "updated_at": None}
    store.save_state({"conversation": "untouched"})
    snapshot = {"agent": {"position": [1, 2]}, "seed": 42}
    first = store.save_experiment(snapshot, expected_revision=0)
    snapshot["agent"]["position"][0] = 99
    assert first["snapshot"]["agent"]["position"] == [1, 2]
    assert store.load_experiment() == first
    with pytest.raises(ValueError, match="revision conflict"):
        store.save_experiment({"seed": 7}, expected_revision=0)
    assert store.load_experiment() == first
    assert store.load_state() == {"conversation": "untouched"}


def test_stale_world_revision_rolls_back_entire_action_outcome(store):
    action = attempted(store)
    initial = store.save_experiment({"position": 0}, expected_revision=0)
    with pytest.raises(ValueError, match="revision conflict"):
        store.commit_action_outcome(
            action["id"], "succeeded", {"moved": True}, {"position": 1},
            expected_revision=0,
        )
    assert store.load_experiment() == initial
    assert store.get_action(action["id"])["status"] == "attempted"
    assert len(store.get_action(action["id"])["transitions"]) == 3
    assert len(store.list_events()) == 1
    outcome = store.commit_action_outcome(
        action["id"], "succeeded", {"moved": True}, {"position": 1},
        expected_revision=1,
    )
    assert outcome["experiment"]["revision"] == 2
    assert outcome["event"]["metadata"]["experiment_revision"] == 2


def test_tool_event_failure_rolls_back_world_and_ledger(store, monkeypatch):
    action = attempted(store)
    original = store._insert_event

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Simulated journal failure")

    monkeypatch.setattr(store, "_insert_event", fail_after_insert)
    with pytest.raises(RuntimeError, match="journal failure"):
        store.commit_action_outcome(
            action["id"], "succeeded", 391, {"position": 1}, expected_revision=0
        )
    assert store.load_experiment()["revision"] == 0
    assert store.get_action(action["id"])["status"] == "attempted"
    assert len(store.list_events()) == 1


def test_crash_recovery_marks_uncertain_attempt_and_never_replays_it(store):
    action = attempted(store)
    event = store.claim_next()
    assert event["id"] == action["episode_id"]
    store.save_experiment({"position": 0}, expected_revision=0)
    assert store.recover_pending() == 1
    recovered = store.get_action(action["id"])
    assert recovered["status"] == "outcome_unknown"
    assert recovered["result"] is None
    assert store.get_event(recovered["outcome_event_id"])["metadata"]["status"] == "outcome_unknown"
    assert store.recover_actions() == 0
    late = store.commit_action_outcome(
        action["id"], "succeeded", 391, {"position": 1}, expected_revision=1
    )
    assert late["action"]["status"] == "outcome_unknown"
    assert store.load_experiment()["snapshot"] == {"position": 0}
    assert len([e for e in store.list_events() if e["kind"] == "tool"]) == 1
    with pytest.raises(ValueError, match="transition"):
        store.transition_action(action["id"], "attempted")


def test_recovery_preserves_success_and_unattempted_intents(store):
    completed = attempted(store, "finished")
    store.commit_action_outcome(completed["id"], "succeeded", 391)
    proposed = proposal(store, "intent")
    authorized = proposal(store, "allowed")
    store.transition_action(authorized["id"], "authorized")
    assert store.recover_actions() == 0
    assert store.get_action(completed["id"])["status"] == "succeeded"
    assert store.get_action(proposed["id"])["status"] == "proposed"
    assert store.get_action(authorized["id"])["status"] == "authorized"


@pytest.mark.parametrize("actor", ["self", "user", "environment"])
def test_actor_is_explicit_and_cancellation_is_not_success(store, actor):
    action = proposal(store, actor=actor)
    cancelled = store.transition_action(action["id"], "cancelled", error="Cancelled by user")
    assert cancelled["actor"] == actor
    assert cancelled["status"] == "cancelled"
    event = store.get_event(cancelled["outcome_event_id"])
    assert event["metadata"]["status"] == "cancelled"
    assert event["metadata"]["result"] is None
    assert len(cancelled["transitions"]) == 2


@pytest.mark.parametrize("bad", [[], {"bad": float("nan")}, {"bad": {1, 2}},
                                 {1: "bad key"}, {"long": "x" * 20_000},
                                 {"bad": 2**300}])
def test_action_parameters_reject_non_json_or_oversized_input(store, bad):
    event = store.enqueue("A request")
    with pytest.raises(ValueError):
        store.propose_action("bad", event["id"], "calculator", bad)
    assert store.list_actions() == []


def test_sources_and_action_status_are_validated(store):
    event = store.enqueue("A request")
    with pytest.raises(ValueError, match="Unknown source"):
        store.propose_action("bad", event["id"], "calculator", {}, source_event_ids=["fake"])
    with pytest.raises(ValueError, match="actor"):
        store.propose_action("bad", event["id"], "calculator", {}, actor="imagined")
    action = proposal(store)
    for bad in ([], "invented_status"):
        with pytest.raises(ValueError):
            store.transition_action(action["id"], bad)
    with pytest.raises(ValueError):
        store.transition_action(action["id"], "authorized", result=391)
    assert store.get_action("missing") is None
    assert store.list_actions(0) == []


@pytest.mark.parametrize("revision", [True, -1, 1.2, None, "0"])
def test_experiment_cas_requires_valid_integer(store, revision):
    with pytest.raises(ValueError, match="expected_revision"):
        store.save_experiment({}, revision)
    assert store.load_experiment()["revision"] == 0


def test_deep_state_and_invalid_outcomes_cannot_partially_commit(store):
    nested = {}
    for _ in range(20):
        nested = {"child": nested}
    with pytest.raises(ValueError, match="deeply"):
        store.save_experiment(nested, expected_revision=0)
    action = attempted(store)
    with pytest.raises(ValueError):
        store.commit_action_outcome(action["id"], "succeeded", float("inf"))
    with pytest.raises(ValueError, match="Only successful"):
        store.commit_action_outcome(action["id"], "failed", None, {}, expected_revision=0)
    with pytest.raises(ValueError, match="snapshot"):
        store.commit_action_outcome(action["id"], "succeeded", 391, expected_revision=0)
    assert store.get_action(action["id"])["status"] == "attempted"
    assert store.load_experiment()["revision"] == 0


def test_experiment_input_priority_commit_and_retry(store):
    reflection = store.enqueue("Reflect", kind="reflection")
    experiment = store.enqueue("Inspect simulated room", kind="experiment", source="experiment")
    user = store.enqueue("Hello")
    assert store.claim_next()["id"] == user["id"]
    assert store.claim_next()["id"] == experiment["id"]
    store.fail_event(experiment["id"], "Interrupted")
    assert store.retry_event(experiment["id"])["status"] == "pending"
    assert store.claim_next()["id"] == experiment["id"]
    store.commit_cycle(experiment["id"], {"state_version": 1}, "Inspected.", [], [])
    assert store.get_event(experiment["id"])["status"] == "complete"
    assert store.claim_next()["id"] == reflection["id"]


def test_experiment_cancellation_does_not_touch_user_or_reflection(store):
    user = store.enqueue("Hello")
    reflection = store.enqueue("Reflect", kind="reflection")
    experiments = [store.enqueue("Simulate", kind="experiment") for _ in range(2)]
    store.claim_next()
    store.claim_next()
    assert store.cancel_pending_experiments("Experiment paused after restart") == 2
    assert all(store.get_event(e["id"])["status"] == "failed" for e in experiments)
    assert store.get_event(user["id"])["status"] == "processing"
    assert store.get_event(reflection["id"])["status"] == "pending"


def test_action_and_experiment_survive_reopen_without_reinitializing_history(tmp_path):
    path = tmp_path / "agency.sqlite3"
    first = MindStore(path)
    action = attempted(first)
    outcome = first.commit_action_outcome(
        action["id"], "succeeded", 391, {"position": [1, 2]}, expected_revision=0
    )
    first.close()
    second = MindStore(path)
    try:
        assert second.get_action(action["id"]) == outcome["action"]
        assert second.load_experiment() == outcome["experiment"]
        with second._connection() as conn:
            assert conn.execute("SELECT value FROM mind_meta WHERE key='schema_version'").fetchone()[0] == "2"
        assert second.recover_actions() == 0
    finally:
        second.close()
