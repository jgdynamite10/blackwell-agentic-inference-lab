"""``workflow-controller-v3`` for workloads 2.8.0 and 2.8.1 (decision D-0034).

Inherits the generic workflow checks (investigation, usable log, retrieved
runbook, published diagnosis, returned remediation, and the optional
evidence-refs shape). Adds exact coverage of the selected hypothesis.
It does not inherit the v2 lexical relevance gate. It never selects a
diagnosis and never reads the evaluator.
"""

from __future__ import annotations

from typing import Any

from blackwell_lab.workload.evidence import ELIGIBLE
from blackwell_lab.workload.relevance_v3 import (
    SUPPORT_CONTRADICTION,
    SUPPORT_NO_HYPOTHESIS,
    SUPPORT_PARTIAL_CITATION,
    SUPPORT_SUPPORTED,
    SUPPORT_UNCOVERED,
    pattern_support,
)
from blackwell_lab.workload.tools import EVIDENCE_REFS_ARGUMENT
from blackwell_lab.workload.workflow import (
    REJECT_EVIDENCE_REFS_REQUIRED,
    REJECT_HYPOTHESIS_SUPPORT,
    REJECT_HYPOTHESIS_WITNESS_REF,
    REJECT_MALFORMED_TERMINAL,
    WorkflowController,
    WorkflowVerdict,
    _is_clean_string,
)

CONTROLLER_WORKFLOW_V3 = "workflow-controller-v3"


class StructuredEvidenceWorkflowController(WorkflowController):
    """Per-task v3 controller. One instance per task execution."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.controller_id = CONTROLLER_WORKFLOW_V3
        self._catalog: list[dict[str, Any]] | None = None
        self._witnesses: list[tuple[str, str, list[dict[str, Any]]]] = []

    def record(self, tool: str, payload: object):
        observation = super().record(tool, payload)
        if not isinstance(payload, dict):
            return observation
        if tool == "retrieve_runbook" and payload.get("found") is True:
            catalog = payload.get("diagnosis_hypotheses")
            if isinstance(catalog, list):
                self._catalog = [item for item in catalog if isinstance(item, dict)]
        if observation.eligibility == ELIGIBLE and tool in {"search_logs", "get_service_health"}:
            witness_type = "log" if tool == "search_logs" else "health"
            findings = _findings(payload, tool)
            self._witnesses.append((observation.observation_id, witness_type, findings))
        return observation

    def _judge(self, arguments: object) -> WorkflowVerdict:
        verdict = super()._judge(arguments)
        if not isinstance(arguments, dict):
            return verdict
        failures = list(verdict.failure_categories)
        diagnosis = arguments.get("diagnosis_id")
        if not _is_clean_string(diagnosis):
            return WorkflowVerdict(accepted=False, failure_categories=tuple(failures))
        cited: set[str] | None = None
        if (
            self.requires_evidence_refs
            and REJECT_EVIDENCE_REFS_REQUIRED not in failures
            and REJECT_MALFORMED_TERMINAL not in failures
        ):
            refs = arguments.get(EVIDENCE_REFS_ARGUMENT)
            cited = (
                {ref for ref in refs if isinstance(ref, str)} if isinstance(refs, list) else set()
            )
        outcome = pattern_support(
            catalog=self._catalog,
            diagnosis_id=diagnosis,
            witnesses=self._witnesses,
            cited_ids=cited,
        )
        if outcome != SUPPORT_SUPPORTED:
            if outcome == SUPPORT_PARTIAL_CITATION:
                failures.append(REJECT_HYPOTHESIS_WITNESS_REF)
            elif outcome in {
                SUPPORT_UNCOVERED,
                SUPPORT_CONTRADICTION,
                SUPPORT_NO_HYPOTHESIS,
            }:
                failures.append(REJECT_HYPOTHESIS_SUPPORT)
            else:
                failures.append(REJECT_HYPOTHESIS_SUPPORT)
        if failures:
            return WorkflowVerdict(accepted=False, failure_categories=tuple(failures))
        return verdict


def _findings(payload: dict[str, Any], tool: str) -> list[dict[str, Any]]:
    if tool == "get_service_health":
        raw = payload.get("finding")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]
    lines = payload.get("lines")
    if not isinstance(lines, list):
        return []
    collected: list[dict[str, Any]] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        raw = line.get("finding")
        if isinstance(raw, list):
            collected.extend(item for item in raw if isinstance(item, dict))
    return collected
