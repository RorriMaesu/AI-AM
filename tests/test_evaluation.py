"""Guard the meaning of the component comparisons, not just their output shape."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def evaluator():
    path = Path(__file__).with_name("evaluate_mind.py")
    spec = importlib.util.spec_from_file_location("aiam_component_evaluation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hidden_information_is_unavailable_until_reobserved(evaluator):
    result = evaluator.evaluate_belief_retention()
    assert all(result["checks"].values())
    assert result["retained_belief_while_hidden"]["last_observed_position"] == [3, 3]
    assert result["baseline_belief_while_hidden"] == {}
    assert result["observed_location_after_return"] == [3, 4]


def test_actual_changed_controls_drive_learning_gain(evaluator):
    result = evaluator.evaluate_control_learning()
    assert all(result["checks"].values())
    assert result["adaptive_late_accuracy"] > result["baseline_late_accuracy"]
    assert result["adaptive_late_mean_brier"] < result["baseline_late_mean_brier"]
    assert all(trial["actual_effect"] == "0,0,1" for trial in result["trials"])


def test_attention_ablation_preserves_capacity_and_changes_evidence(evaluator):
    result = evaluator.evaluate_attention()
    assert all(result["checks"].values())
    assert len(result["responsive_focus"]) == len(result["ablated_focus"]) == 1
    assert result["responsive_focus"][0]["memory_id"] == "m1"


@pytest.mark.parametrize("steps", [True, 0, 5, 21, 6.5])
def test_learning_evaluation_has_a_finite_valid_budget(evaluator, steps):
    with pytest.raises(ValueError):
        evaluator.evaluate_control_learning(steps)


def test_reports_are_repeatable_and_do_not_claim_model_validation(evaluator):
    first = evaluator.evaluate()
    second = evaluator.evaluate()
    assert first == second
    assert first["component_checks_passed"] is True
    assert first["model_inference"] is False
    assert first["runtime_integration_test"] is False
    assert len(first["cases"]) == 3


def test_cli_prints_machine_readable_report(evaluator, capsys):
    assert evaluator.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["component_checks_passed"] is True
    assert result["state"] == "RAM only"
