"""Task-success evaluator: positive, partial-credit, incorrect, malformed,
and timeout cases; determinism; versioning."""

from __future__ import annotations

import re

import pytest

from blackwell_lab.workload.agent import run_task
from blackwell_lab.workload.evaluator import (
    EVALUATOR_VERSION,
    EVIDENCE_WEIGHT,
    PROPOSED_QUALITY_THRESHOLD,
    REMEDIATION_WEIGHT,
    ROOT_CAUSE_WEIGHT,
    evaluate,
)
from blackwell_lab.workload.model_client import DeterministicMockClient, GenerationSettings
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.tools import SimulatedToolbox

SETTINGS = GenerationSettings()
SCENARIO = catalog()["memory-pressure-001"]


def executed(behavior: str = "correct", *, timeout_s: float = 30.0, **client_kwargs):
    client = DeterministicMockClient(behavior=behavior, **client_kwargs)
    return run_task(SCENARIO, client, SimulatedToolbox(SCENARIO), SETTINGS, timeout_s=timeout_s)


class TestScoringDesign:
    def test_weights_sum_to_one(self):
        assert ROOT_CAUSE_WEIGHT + EVIDENCE_WEIGHT + REMEDIATION_WEIGHT == pytest.approx(1.0)

    def test_evaluator_is_versioned(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", EVALUATOR_VERSION)
        assert evaluate(SCENARIO, executed()).evaluator_version == EVALUATOR_VERSION

    def test_proposed_threshold_is_declared(self):
        assert 0.0 < PROPOSED_QUALITY_THRESHOLD < 1.0


class TestPositive:
    def test_correct_run_scores_full_marks(self):
        evaluation = evaluate(SCENARIO, executed())
        assert evaluation.score == pytest.approx(1.0)
        assert evaluation.success is True
        assert evaluation.root_cause_component == pytest.approx(ROOT_CAUSE_WEIGHT)
        assert evaluation.evidence_component == pytest.approx(EVIDENCE_WEIGHT)
        assert evaluation.remediation_component == pytest.approx(REMEDIATION_WEIGHT)


class TestPartialCredit:
    def test_partial_evidence_earns_partial_evidence_credit(self):
        evaluation = evaluate(SCENARIO, executed("partial_evidence"))
        assert 0.0 < evaluation.evidence_component < EVIDENCE_WEIGHT
        # Root cause and remediation are still correct in this behavior.
        assert evaluation.root_cause_component == pytest.approx(ROOT_CAUSE_WEIGHT)
        assert evaluation.remediation_component == pytest.approx(REMEDIATION_WEIGHT)
        assert evaluation.score < 1.0

    def test_partial_keyword_match_earns_fractional_root_cause_credit(self):
        execution = executed()
        # Keep only one of the scenario's three keywords in the stated cause.
        execution.root_cause = "A cache regression of some kind."
        evaluation = evaluate(SCENARIO, execution)
        expected_fraction = 1 / len(SCENARIO.root_cause_keywords)
        assert evaluation.root_cause_component == pytest.approx(
            ROOT_CAUSE_WEIGHT * expected_fraction, abs=1e-6
        )


class TestIncorrect:
    def test_wrong_root_cause_scores_zero_diagnosis(self):
        evaluation = evaluate(SCENARIO, executed("wrong_root_cause"))
        assert evaluation.root_cause_component == 0.0
        assert evaluation.success is False

    def test_distractor_remediation_scores_zero_remediation(self):
        evaluation = evaluate(SCENARIO, executed("wrong_remediation"))
        assert evaluation.remediation_component == 0.0
        assert evaluation.success is False
        assert "not accepted" in evaluation.reason


class TestFailureModes:
    @pytest.mark.parametrize(
        "behavior", ["malformed_tool_call", "unknown_tool", "bad_arguments", "endpoint_error"]
    )
    def test_malformed_and_error_tasks_score_zero(self, behavior):
        evaluation = evaluate(SCENARIO, executed(behavior))
        assert evaluation.score == 0.0
        assert evaluation.success is False
        assert "did not complete" in evaluation.reason

    def test_timeout_scores_zero(self):
        execution = executed("slow", timeout_s=0.05, response_delay_s=0.2)
        evaluation = evaluate(SCENARIO, execution)
        assert execution.status == "timeout"
        assert evaluation.score == 0.0
        assert evaluation.success is False


class TestDeterminismAndThreshold:
    def test_identical_inputs_produce_identical_scores(self):
        execution = executed()
        assert evaluate(SCENARIO, execution) == evaluate(SCENARIO, execution)

    def test_threshold_is_applied(self):
        execution = executed()
        strict = evaluate(SCENARIO, execution, quality_threshold=1.0)
        lax = evaluate(SCENARIO, execution, quality_threshold=0.5)
        assert strict.success is True  # full-score run passes even at 1.0
        assert lax.success is True
        partial = evaluate(SCENARIO, executed("wrong_remediation"), quality_threshold=0.5)
        assert partial.score >= 0.5 or partial.success is False
