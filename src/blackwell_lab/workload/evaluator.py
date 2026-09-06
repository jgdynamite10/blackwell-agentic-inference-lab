"""Versioned, deterministic task-success evaluator (measurement contract §8).

Evaluator v3 (decision D-0010) replaces keyword-substring scoring with
**mandatory structured gates**. A task is successful if and only if ALL of
the following hold:

- ``task_completed`` — the task produced a terminal recommendation (no
  execution error, no timeout);
- ``diagnosis`` — the submitted ``diagnosis_id`` exactly matches an accepted
  diagnosis (candidate ids are published in the task prompt and runbook
  fixtures, so the model selects, never guesses a hidden string);
- ``remediation`` — the submitted ``remediation_id`` is in the accepted set
  (distractors fail);
- one gate per scenario **evidence predicate** — every mandatory predicate
  must be satisfied by the recorded tool trace. A predicate is satisfied by
  any one of its alternatives (permitted alternative evidence paths); each
  alternative constrains the tool name, relevant validated arguments, and the
  returned result, so irrelevant queries and repeated tool names cannot
  satisfy evidence.

The overall score is binary: **1.0 when successful, 0.0 otherwise**, and the
owner-approved quality threshold is ``S_MIN = 1.0``. Component fractions
(diagnosis, remediation, evidence-predicate satisfaction) are retained as
**diagnostics only** — they never define success.

Rationale text is recorded for audit but is **not a gate**: keyword-stuffed
or negated rationales can neither pass nor fail a task by themselves.

Scoring is machine-checkable and deterministic: identical inputs always
produce identical evaluations. Human judgment is not part of scoring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from blackwell_lab.workload.agent import TaskExecution, ToolTrace
from blackwell_lab.workload.scenarios import EvidenceAlternative, EvidencePredicate, Scenario

EVALUATOR_VERSION = "3.0.0"

#: Owner-approved quality threshold (decision D-0010): deterministic task
#: success requires all mandatory gates, so S_min is exactly 1.0.
QUALITY_THRESHOLD = 1.0
S_MIN = QUALITY_THRESHOLD


@dataclass(frozen=True)
class GateResult:
    """One mandatory gate's outcome (all gates must pass for success)."""

    gate_id: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class Evaluation:
    """The deterministic evaluation of one task attempt."""

    scenario_id: str
    evaluator_version: str
    success: bool
    score: float  # 1.0 iff success, else 0.0 (S_min = 1.0)
    gates: tuple[GateResult, ...]
    # Diagnostic component fractions (never gates, never define success):
    diagnosis_component: float
    remediation_component: float
    evidence_component: float
    reason: str


def _canonical_result_text(trace: ToolTrace) -> str:
    return json.dumps(trace.result, sort_keys=True, separators=(",", ":")).casefold()


def _alternative_matches(alternative: EvidenceAlternative, trace: ToolTrace) -> bool:
    if trace.tool != alternative.tool:
        return False
    for argument, needle in alternative.argument_contains:
        value = trace.arguments.get(argument)
        if not isinstance(value, str) or needle.casefold() not in value.casefold():
            return False
    if alternative.result_contains is not None:
        if alternative.result_contains.casefold() not in _canonical_result_text(trace):
            return False
    return True


def predicate_satisfied(predicate: EvidencePredicate, tool_trace: list[ToolTrace]) -> bool:
    """True when any alternative matches at least one tool-trace entry."""
    return any(
        _alternative_matches(alternative, trace)
        for alternative in predicate.alternatives
        for trace in tool_trace
    )


def evaluate(scenario: Scenario, execution: TaskExecution) -> Evaluation:
    """Applies the mandatory gates of one task attempt (all must pass)."""
    gates: list[GateResult] = []

    completed = execution.status == "completed"
    gates.append(
        GateResult(
            gate_id="task_completed",
            passed=completed,
            detail=(
                "terminal recommendation submitted"
                if completed
                else f"task did not complete: {execution.error_category}"
            ),
        )
    )

    diagnosis_ok = completed and execution.diagnosis_id in scenario.accepted_diagnoses
    gates.append(
        GateResult(
            gate_id="diagnosis",
            passed=diagnosis_ok,
            detail=(
                "exact accepted diagnosis id"
                if diagnosis_ok
                else f"submitted diagnosis_id {execution.diagnosis_id!r} is not accepted"
            ),
        )
    )

    remediation_ok = completed and execution.remediation_id in scenario.accepted_remediations
    gates.append(
        GateResult(
            gate_id="remediation",
            passed=remediation_ok,
            detail=(
                "accepted remediation id"
                if remediation_ok
                else f"submitted remediation_id {execution.remediation_id!r} is not accepted"
            ),
        )
    )

    satisfied_predicates = 0
    for predicate in scenario.evidence_predicates:
        satisfied = predicate_satisfied(predicate, execution.tool_trace)
        satisfied_predicates += 1 if satisfied else 0
        gates.append(
            GateResult(
                gate_id=f"evidence:{predicate.predicate_id}",
                passed=satisfied,
                detail=(
                    "satisfied by tool trace"
                    if satisfied
                    else f"no tool-trace entry satisfies any alternative: {predicate.description}"
                ),
            )
        )

    success = all(gate.passed for gate in gates)
    predicate_count = len(scenario.evidence_predicates)
    evidence_fraction = satisfied_predicates / predicate_count if predicate_count else 1.0
    failed_gates = [g.gate_id for g in gates if not g.passed]
    reason = "all mandatory gates passed" if success else f"failed gates: {', '.join(failed_gates)}"
    return Evaluation(
        scenario_id=scenario.scenario_id,
        evaluator_version=EVALUATOR_VERSION,
        success=success,
        score=1.0 if success else 0.0,
        gates=tuple(gates),
        diagnosis_component=1.0 if diagnosis_ok else 0.0,
        remediation_component=1.0 if remediation_ok else 0.0,
        evidence_component=round(evidence_fraction, 6),
        reason=reason,
    )
