"""Task-success evaluator v3: mandatory gates, S_min = 1.0, adversarial
failure modes, alternative evidence paths, determinism, versioning."""

from __future__ import annotations

import re

import pytest
from fakes import FakeClock

from blackwell_lab.workload.agent import ToolTrace, run_task
from blackwell_lab.workload.evaluator import (
    EVALUATOR_VERSION,
    QUALITY_THRESHOLD,
    S_MIN,
    evaluate,
    predicate_satisfied,
)
from blackwell_lab.workload.model_client import DeterministicMockClient, GenerationSettings
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.tools import SimulatedToolbox

SETTINGS = GenerationSettings()
SCENARIO = catalog()["memory-pressure-001"]
ELEVATED = catalog()["elevated-latency-001"]


def executed(behavior: str = "correct", *, timeout_s: float = 30.0, scenario=SCENARIO):
    clock = FakeClock()
    client = DeterministicMockClient(behavior=behavior)
    toolbox = SimulatedToolbox(scenario, clock=clock)
    return run_task(scenario, client, toolbox, SETTINGS, timeout_s=timeout_s, clock=clock)


def failed_gate_ids(evaluation) -> set[str]:
    return {g.gate_id for g in evaluation.gates if not g.passed}


def run_tools(scenario, calls: list[dict]) -> list[ToolTrace]:
    """Executes tool calls against the real toolbox and records their traces."""
    toolbox = SimulatedToolbox(scenario, clock=FakeClock())
    trace = []
    for call in calls:
        result = toolbox.execute(call["tool"], call["arguments"])
        trace.append(
            ToolTrace(
                tool=result.tool,
                arguments=dict(call["arguments"]),
                result=result.payload,
                simulated_latency_ms=result.simulated_latency_ms,
            )
        )
    return trace


def predicate_by_id(scenario, predicate_id: str):
    return next(p for p in scenario.evidence_predicates if p.predicate_id == predicate_id)


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


class TestTypedResultConstraints:
    """Evidence is judged only against typed structured response fields —
    never a serialization of the whole response — so echoed arguments,
    ``available`` listings, unknown resources, and ``found: false`` responses
    can never satisfy evidence (owner blocker 2)."""

    def test_expected_text_injected_into_the_query_does_not_pass(self):
        """search_logs 'audit cfg-2041' matches zero log lines; the echoed
        query field is the only place cfg-2041 appears, and it never counts."""
        [trace] = run_tools(
            ELEVATED, [{"tool": "search_logs", "arguments": {"query": "audit cfg-2041"}}]
        )
        assert trace.result["total_matches"] == 0  # the reproduction is real
        predicate = predicate_by_id(ELEVATED, "log-evidence-cfg-2041")
        assert predicate_satisfied(predicate, [trace]) is False

    def test_zero_search_matches_never_pass_even_with_a_matching_query(self):
        [trace] = run_tools(
            ELEVATED,
            [{"tool": "search_logs", "arguments": {"query": "audit that never happened"}}],
        )
        assert trace.result["total_matches"] == 0
        predicate = predicate_by_id(ELEVATED, "log-evidence-cfg-2041")
        assert predicate_satisfied(predicate, [trace]) is False

    def test_found_false_runbook_does_not_pass(self):
        """retrieve_runbook with the remediation id smuggled into the key
        returns found=false; the echoed key and the `available` listing never
        satisfy evidence."""
        key = "zephyr-cart rollback-config-release-cfg-2041"
        [trace] = run_tools(ELEVATED, [{"tool": "retrieve_runbook", "arguments": {"key": key}}])
        assert trace.result["found"] is False
        predicate = predicate_by_id(ELEVATED, "change-correlation-cfg-2041")
        assert predicate_satisfied(predicate, [trace]) is False

    def test_metric_name_only_in_available_listing_does_not_pass(self):
        """query_metrics for an unknown metric echoes the request and lists
        real metric names under `available`; neither satisfies evidence."""
        gpu = catalog()["gpu-saturation-001"]
        [trace] = run_tools(
            gpu,
            [{"tool": "query_metrics", "arguments": {"metric": "batch_queue_depth_hourly"}}],
        )
        assert trace.result["found"] is False
        assert "batch_queue_depth" in trace.result["available"]
        predicate = predicate_by_id(gpu, "contention-signal")
        assert predicate_satisfied(predicate, [trace]) is False

    def test_owner_reproduction_elevated_latency_must_fail_both_gates(self):
        """The exact owner reproduction: a completed elevated-latency task
        whose only evidence is the zero-match search and the found=false
        runbook must fail BOTH evidence gates and score 0.0."""
        execution = executed(scenario=ELEVATED)
        assert execution.status == "completed"
        execution.tool_trace = run_tools(
            ELEVATED,
            [
                {"tool": "search_logs", "arguments": {"query": "audit cfg-2041"}},
                {
                    "tool": "retrieve_runbook",
                    "arguments": {"key": "zephyr-cart rollback-config-release-cfg-2041"},
                },
            ],
        )
        evaluation = evaluate(ELEVATED, execution)
        assert evaluation.success is False
        assert evaluation.score == 0.0
        assert {
            "evidence:log-evidence-cfg-2041",
            "evidence:change-correlation-cfg-2041",
        } <= failed_gate_ids(evaluation)

    def test_legitimate_reference_and_alternative_paths_still_pass(self):
        for scenario in catalog().values():
            for sequence in (scenario.reference_tool_sequence, scenario.alternative_tool_sequence):
                trace = run_tools(scenario, list(sequence))
                for predicate in scenario.evidence_predicates:
                    assert predicate_satisfied(predicate, trace) is True, (
                        scenario.scenario_id,
                        predicate.predicate_id,
                    )


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
