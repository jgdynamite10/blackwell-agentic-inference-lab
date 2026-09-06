"""Versioned, deterministic task-success evaluator (measurement contract §8).

Scoring components (weights sum to 1.0):

- **Root-cause diagnosis (weight 0.5).** The fraction of the scenario's
  ``root_cause_keywords`` that appear, case-insensitively, in the agent's
  stated root cause. Full credit requires every keyword.
- **Evidence / appropriate tool use (weight 0.2).** The fraction of the
  scenario's ``required_evidence`` tools the agent actually consulted before
  recommending.
- **Remediation (weight 0.3).** All-or-nothing: the submitted remediation id
  is in the scenario's accepted set (distractors score zero).

Tasks that ended in an error or timeout score 0.0 and are never successful;
they stay in the denominator of task-success rate (contract §7).

``PROPOSED_QUALITY_THRESHOLD`` (S_min) is a **proposal requiring owner
approval** (decision D-0009): 0.85 requires a fully correct root cause, an
accepted remediation, and at least a quarter of the required evidence.

Scoring is machine-checkable and deterministic: identical inputs always
produce identical scores. Human judgment is not part of scoring.
"""

from __future__ import annotations

from dataclasses import dataclass

from blackwell_lab.workload.agent import TaskExecution
from blackwell_lab.workload.scenarios import Scenario

EVALUATOR_VERSION = "2.0.0"

ROOT_CAUSE_WEIGHT = 0.5
EVIDENCE_WEIGHT = 0.2
REMEDIATION_WEIGHT = 0.3

#: Proposed S_min — NOT owner-approved yet (decision D-0009 review item).
PROPOSED_QUALITY_THRESHOLD = 0.85


@dataclass(frozen=True)
class Evaluation:
    """The deterministic evaluation of one task attempt."""

    scenario_id: str
    evaluator_version: str
    score: float
    root_cause_component: float
    evidence_component: float
    remediation_component: float
    success: bool
    reason: str


def evaluate(
    scenario: Scenario,
    execution: TaskExecution,
    *,
    quality_threshold: float = PROPOSED_QUALITY_THRESHOLD,
) -> Evaluation:
    """Scores one task attempt against its scenario's success criteria."""
    if execution.status != "completed":
        return Evaluation(
            scenario_id=scenario.scenario_id,
            evaluator_version=EVALUATOR_VERSION,
            score=0.0,
            root_cause_component=0.0,
            evidence_component=0.0,
            remediation_component=0.0,
            success=False,
            reason=f"task did not complete: {execution.error_category}",
        )

    stated = (execution.root_cause or "").casefold()
    keywords = scenario.root_cause_keywords
    matched = sum(1 for kw in keywords if kw.casefold() in stated)
    root_cause_fraction = matched / len(keywords) if keywords else 0.0

    required = set(scenario.required_evidence)
    consulted = required.intersection(execution.tools_used)
    evidence_fraction = len(consulted) / len(required) if required else 1.0

    remediation_ok = execution.remediation_id in scenario.accepted_remediations

    root_cause_component = ROOT_CAUSE_WEIGHT * root_cause_fraction
    evidence_component = EVIDENCE_WEIGHT * evidence_fraction
    remediation_component = REMEDIATION_WEIGHT * (1.0 if remediation_ok else 0.0)
    score = round(root_cause_component + evidence_component + remediation_component, 6)

    success = score >= quality_threshold
    reason = (
        f"root_cause {matched}/{len(keywords)} keywords; "
        f"evidence {len(consulted)}/{len(required)} tools; "
        f"remediation {'accepted' if remediation_ok else 'not accepted'}"
    )
    return Evaluation(
        scenario_id=scenario.scenario_id,
        evaluator_version=EVALUATOR_VERSION,
        score=score,
        root_cause_component=round(root_cause_component, 6),
        evidence_component=round(evidence_component, 6),
        remediation_component=round(remediation_component, 6),
        success=success,
        reason=reason,
    )
