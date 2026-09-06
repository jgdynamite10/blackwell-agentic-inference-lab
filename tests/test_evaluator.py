"""Task-success evaluator v3: mandatory gates, S_min = 1.0, adversarial
failure modes, alternative evidence paths, determinism, versioning."""

from __future__ import annotations

import re

import pytest
from fakes import FakeClock

from blackwell_lab.workload.agent import run_task
from blackwell_lab.workload.evaluator import (
    EVALUATOR_VERSION,
    QUALITY_THRESHOLD,
    S_MIN,
    evaluate,
)
from blackwell_lab.workload.model_client import DeterministicMockClient, GenerationSettings
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.tools import SimulatedToolbox

SETTINGS = GenerationSettings()
SCENARIO = catalog()["memory-pressure-001"]


def executed(behavior: str = "correct", *, timeout_s: float = 30.0, scenario=SCENARIO):
    clock = FakeClock()
    client = DeterministicMockClient(behavior=behavior)
    toolbox = SimulatedToolbox(scenario, clock=clock)
    return run_task(scenario, client, toolbox, SETTINGS, timeout_s=timeout_s, clock=clock)


def failed_gate_ids(evaluation) -> set[str]:
    return {g.gate_id for g in evaluation.gates if not g.passed}


class TestScoringDesign:
    def test_owner_approved_threshold_is_all_gates(self):
        """Decision D-0010: deterministic success requires every mandatory
        gate, so S_min is exactly 1.0."""
        assert QUALITY_THRESHOLD == 1.0
        assert S_MIN == 1.0

    def test_evaluator_is_versioned(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", EVALUATOR_VERSION)
        assert evaluate(SCENARIO, executed()).evaluator_version == EVALUATOR_VERSION

    def test_score_is_binary(self):
        assert evaluate(SCENARIO, executed()).score == 1.0
        assert evaluate(SCENARIO, executed("wrong_remediation")).score == 0.0

    def test_gates_cover_completion_diagnosis_remediation_and_every_predicate(self):
        evaluation = evaluate(SCENARIO, executed())
        gate_ids = [g.gate_id for g in evaluation.gates]
        assert gate_ids[0] == "task_completed"
        assert "diagnosis" in gate_ids
        assert "remediation" in gate_ids
        for predicate in SCENARIO.evidence_predicates:
            assert f"evidence:{predicate.predicate_id}" in gate_ids

    def test_components_are_diagnostics_not_success_definitions(self):
        """A run failing one gate scores 0.0 overall even though component
        diagnostics remain positive — components never define success."""
        evaluation = evaluate(SCENARIO, executed("wrong_remediation"))
        assert evaluation.diagnosis_component == 1.0
        assert evaluation.evidence_component == 1.0
        assert evaluation.remediation_component == 0.0
        assert evaluation.score == 0.0
        assert evaluation.success is False


class TestPositive:
    def test_correct_run_passes_every_gate(self):
        evaluation = evaluate(SCENARIO, executed())
        assert evaluation.success is True
        assert evaluation.score == 1.0
        assert all(g.passed for g in evaluation.gates)
        assert evaluation.reason == "all mandatory gates passed"

    def test_correct_run_passes_for_every_scenario(self):
        for scenario in catalog().values():
            evaluation = evaluate(scenario, executed(scenario=scenario))
            assert evaluation.success is True, (scenario.scenario_id, evaluation.reason)

    def test_alternative_evidence_path_also_passes(self):
        """Permitted alternative evidence paths are first-class: the
        alternative sequence satisfies every predicate for every scenario."""
        for scenario in catalog().values():
            evaluation = evaluate(scenario, executed("alternative_path", scenario=scenario))
            assert evaluation.success is True, (scenario.scenario_id, evaluation.reason)


class TestAdversarial:
    def test_distractor_diagnosis_fails_the_diagnosis_gate(self):
        evaluation = evaluate(SCENARIO, executed("wrong_diagnosis"))
        assert evaluation.success is False
        assert "diagnosis" in failed_gate_ids(evaluation)

    def test_negated_keyword_rationale_cannot_pass(self):
        """A rationale stuffed with the (negated) ground-truth wording must
        not rescue a wrong diagnosis id — diagnosis is an exact id gate."""
        evaluation = evaluate(SCENARIO, executed("keyword_rationale_wrong_diagnosis"))
        assert evaluation.success is False
        assert evaluation.score == 0.0
        assert "diagnosis" in failed_gate_ids(evaluation)

    def test_rationale_text_is_not_a_gate(self):
        """Mutating the rationale (even to a negation) neither fails nor
        passes a task by itself: gates are structural."""
        execution = executed()
        execution.rationale = "It is definitely not what the evidence suggests."
        evaluation = evaluate(SCENARIO, execution)
        assert evaluation.success is True

    def test_irrelevant_queries_cannot_satisfy_evidence(self):
        """The right tools asked the wrong questions: every evidence gate
        must fail because predicates constrain arguments and results."""
        evaluation = evaluate(SCENARIO, executed("irrelevant_queries"))
        assert evaluation.success is False
        evidence_gates = {g.gate_id for g in evaluation.gates if g.gate_id.startswith("evidence:")}
        assert evidence_gates <= failed_gate_ids(evaluation)
        assert evaluation.evidence_component == 0.0

    def test_repeated_tool_names_do_not_accumulate_evidence(self):
        """Calling one evidencing tool repeatedly satisfies at most the
        predicates that call actually satisfies — never the others."""
        evaluation = evaluate(SCENARIO, executed("repeated_tools"))
        assert evaluation.success is False
        assert any(g.gate_id.startswith("evidence:") for g in evaluation.gates if not g.passed)

    def test_missing_evidence_fails(self):
        evaluation = evaluate(SCENARIO, executed("missing_evidence"))
        assert evaluation.success is False
        assert evaluation.score == 0.0
        assert any(g.gate_id.startswith("evidence:") for g in evaluation.gates if not g.passed)

    def test_distractor_remediation_fails_the_remediation_gate(self):
        evaluation = evaluate(SCENARIO, executed("wrong_remediation"))
        assert evaluation.success is False
        assert "remediation" in failed_gate_ids(evaluation)
        assert "remediation" in evaluation.reason


class TestFailureModes:
    @pytest.mark.parametrize(
        "behavior",
        ["malformed_tool_call", "unknown_tool", "bad_arguments", "endpoint_error", "runtime_crash"],
    )
    def test_execution_errors_score_zero(self, behavior):
        evaluation = evaluate(SCENARIO, executed(behavior))
        assert evaluation.score == 0.0
        assert evaluation.success is False
        assert "task_completed" in failed_gate_ids(evaluation)

    def test_timeout_scores_zero(self):
        clock = FakeClock()
        client = DeterministicMockClient(behavior="slow", response_delay_s=0.2)
        execution = run_task(
            SCENARIO,
            client,
            SimulatedToolbox(SCENARIO, clock=clock),
            SETTINGS,
            timeout_s=0.05,
            clock=clock,
        )
        evaluation = evaluate(SCENARIO, execution)
        assert execution.status == "timeout"
        assert evaluation.score == 0.0
        assert evaluation.success is False


class TestDeterminism:
    def test_identical_inputs_produce_identical_evaluations(self):
        execution = executed()
        assert evaluate(SCENARIO, execution) == evaluate(SCENARIO, execution)
