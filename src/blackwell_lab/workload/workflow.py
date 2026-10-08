"""Generic workflow controller ``workflow-controller-v1`` (workloads 2.6.0 / 2.6.1).

Private development evidence (decision D-0031) showed that the minimal
benchmark agent accepts a terminal recommendation as soon as the model
submits one, so a model that diagnoses correctly but skips or fails to
correct its evidence acquisition still "completes" and then fails the
evaluator. This controller sits between the agent loop and the simulated
toolbox for **one task execution** and enforces the documented workflow
**structurally**:

1. investigation (at least one non-terminal observation) before a terminal
   recommendation is accepted;
2. tracking of successful health, metric, and log observations (the same
   structural eligibility rules as ``evidence-grounding-v1``);
3. zero-match searches and ``found: false`` lookups are **unusable** and
   return generic corrective guidance that names no expected query,
   answer, or scenario content;
4. usable direct incident evidence — including at least one usable
   ``search_logs`` result — before terminal submission;
5. retrieval of a published runbook (``found: true``) before terminal
   submission;
6. the submitted ``remediation_id`` must have been **returned** by a
   retrieved runbook, and the submitted ``diagnosis_id`` must be one of the
   published candidates a retrieved runbook returned;
7. rejection of premature or structurally invalid terminal attempts with a
   generic payload, leaving the model free to correct itself inside the
   **unchanged** turn budget (a rejected attempt consumes a turn like any
   other tool call);
8. a generic remaining-turn warning early enough to submit;
9. a clear terminal failure (``workflow_requirements_unmet``) when the
   budget is exhausted after at least one rejected terminal attempt.

Optional treatment ``evidence-refs`` (workload 2.6.1 only) additionally
requires the ``evidence_refs`` citation that ``evidence-grounding-v1``
introduced: every reference must resolve to an earlier eligible observation
of this task. It is the **only** difference between the W1 and W2 members
of the candidate pair.

What the controller never does
------------------------------
It receives no scenario, no accepted diagnosis or remediation, no evaluator
predicate, no sealed material, and no prompt or completion text. Its inputs
are the tool name, the structured payload the tool returned **to the
model**, the terminal arguments the model submitted, and the turn budget.
It never selects a diagnosis or a remediation, never rewrites arguments,
never adds a tool call, and never changes the retry policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from blackwell_lab.workload.evidence import (
    ELIGIBLE,
    INELIGIBLE_NOT_FOUND,
    INELIGIBLE_UNUSABLE_STATUS,
    INELIGIBLE_ZERO_MATCH_SEARCH,
    OBSERVATION_ID_FIELD,
    Observation,
    classify_observation,
    observation_id,
)
from blackwell_lab.workload.tools import EVIDENCE_REFS_ARGUMENT, TERMINAL_TOOL

#: Controller identity recorded in manifests, candidate identity, receipts.
CONTROLLER_WORKFLOW_V1 = "workflow-controller-v1"

#: The single optional treatment: require the ``evidence_refs`` citation.
TREATMENT_EVIDENCE_REFS = "evidence-refs"
KNOWN_TREATMENTS = frozenset({TREATMENT_EVIDENCE_REFS})

#: Field injected into every tool result returned to the agent.
WORKFLOW_FIELD = "workflow"

#: Execution-error category when the budget is exhausted after at least one
#: rejected terminal attempt (otherwise ``no_terminal_recommendation``).
WORKFLOW_REQUIREMENTS_UNMET = "workflow_requirements_unmet"

#: Remaining-turn warning threshold: warn when this many turns (or fewer)
#: remain after the current one. With the 12-turn budget the first warning
#: arrives with the result of turn 9.
REMAINING_TURN_WARNING_AT = 3

# -- states -------------------------------------------------------------------
STATE_INVESTIGATING = "investigating"
STATE_RUNBOOK_RETRIEVED = "runbook_retrieved"
STATE_EVIDENCE_COLLECTED = "evidence_collected"
STATE_READY_FOR_TERMINAL = "ready_for_terminal"
STATE_TERMINAL_ACCEPTED = "terminal_accepted"
STATE_BUDGET_EXHAUSTED = "budget_exhausted"
STATES = (
    STATE_INVESTIGATING,
    STATE_RUNBOOK_RETRIEVED,
    STATE_EVIDENCE_COLLECTED,
    STATE_READY_FOR_TERMINAL,
    STATE_TERMINAL_ACCEPTED,
    STATE_BUDGET_EXHAUSTED,
)

# -- rejection categories (generic; never content) -----------------------------
REJECT_INVESTIGATION_REQUIRED = "investigation_required"
REJECT_LOG_EVIDENCE_REQUIRED = "log_evidence_required"
REJECT_RUNBOOK_REQUIRED = "runbook_required"
REJECT_REMEDIATION_NOT_IN_RUNBOOK = "remediation_not_in_runbook"
REJECT_DIAGNOSIS_NOT_PUBLISHED = "diagnosis_not_published"
REJECT_EVIDENCE_REFS_REQUIRED = "evidence_refs_required"
REJECT_MALFORMED_TERMINAL = "malformed_terminal"
REJECTION_CATEGORIES = (
    REJECT_INVESTIGATION_REQUIRED,
    REJECT_LOG_EVIDENCE_REQUIRED,
    REJECT_RUNBOOK_REQUIRED,
    REJECT_REMEDIATION_NOT_IN_RUNBOOK,
    REJECT_DIAGNOSIS_NOT_PUBLISHED,
    REJECT_EVIDENCE_REFS_REQUIRED,
    REJECT_MALFORMED_TERMINAL,
)

#: Generic corrective guidance. Every sentence is scenario-independent: it
#: names no service, identifier, query, log line, or accepted answer.
GUIDANCE: dict[str, str] = {
    REJECT_INVESTIGATION_REQUIRED: (
        "No investigation has been recorded. Inspect health, metrics, logs, "
        "and recent changes before submitting a recommendation."
    ),
    REJECT_LOG_EVIDENCE_REQUIRED: (
        "No usable log evidence has been recorded for this incident. "
        "Zero-match searches are not evidence. Call search_logs with a "
        "specific identifier, service name, or diagnostic term drawn from "
        "the health, metric, or change data already observed, then resubmit."
    ),
    REJECT_RUNBOOK_REQUIRED: (
        "No published runbook has been retrieved (found=true). Call "
        "retrieve_runbook with the service or system key inferred from the "
        "evidence, then choose a remediation_id from its remediation_ids."
    ),
    REJECT_REMEDIATION_NOT_IN_RUNBOOK: (
        "The submitted remediation_id was not returned by any runbook "
        "retrieved in this task. Choose an exact ID from "
        "runbook.remediation_ids of a retrieved runbook (found=true)."
    ),
    REJECT_DIAGNOSIS_NOT_PUBLISHED: (
        "The submitted diagnosis_id is not one of the published diagnosis "
        "candidates. Choose an exact ID from the candidate list."
    ),
    REJECT_EVIDENCE_REFS_REQUIRED: (
        "evidence_refs must be a non-empty list of observation_id values "
        "returned earlier in this task by usable health, metric, or log "
        "results. Zero-match and not-found results cannot be cited."
    ),
    REJECT_MALFORMED_TERMINAL: (
        "The recommendation is structurally invalid. diagnosis_id, "
        "rationale, and remediation_id must be non-empty strings."
    ),
}

#: Generic guidance attached to unusable non-terminal results.
UNUSABLE_GUIDANCE: dict[str, str] = {
    INELIGIBLE_ZERO_MATCH_SEARCH: (
        "This search returned zero matches, so it is not usable evidence. "
        "search_logs is literal substring matching: retry with a shorter, "
        "more specific token (an identifier, service name, or diagnostic "
        "term) that appears in data already observed."
    ),
    INELIGIBLE_NOT_FOUND: (
        "This lookup did not find the requested item, so it is not usable "
        "evidence. Use an exact name from the available list or from data "
        "already observed."
    ),
    INELIGIBLE_UNUSABLE_STATUS: (
        "This health lookup did not identify a known service, so it is not "
        "usable evidence. Use an exact service name from data already "
        "observed, or omit the service argument to list every service."
    ),
}
RUNBOOK_NOT_FOUND_GUIDANCE = (
    "The key did not identify a published runbook. The key must be a "
    "service or system name observed in health, log, or change data; a "
    "diagnosis ID is not a runbook key. Use an exact key from the available "
    "list."
)


def _is_clean_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class WorkflowVerdict:
    """The controller's decision on one terminal recommendation attempt."""

    accepted: bool
    failure_categories: tuple[str, ...] = ()
    cited: int = 0

    @property
    def failure_category(self) -> str | None:
        return self.failure_categories[0] if self.failure_categories else None

    def tool_payload(self) -> dict[str, Any]:
        """The generic payload returned to the agent on rejection."""
        return {
            "accepted": False,
            "failure_category": self.failure_category,
            "failure_categories": list(self.failure_categories),
            "guidance": " ".join(GUIDANCE[c] for c in self.failure_categories),
        }


@dataclass
class WorkflowController:
    """Per-task generic workflow state machine (``workflow-controller-v1``).

    One instance exists per task execution and is never shared between
    tasks, repetitions, or scheduler workers. ``context`` is the private
    execution context from which opaque observation IDs are derived.
    """

    context: str
    treatments: tuple[str, ...] = ()
    controller_id: str = CONTROLLER_WORKFLOW_V1
    warning_at: int = REMAINING_TURN_WARNING_AT
    _observations: list[Observation] = field(default_factory=list, repr=False)
    _by_id: dict[str, Observation] = field(default_factory=dict, repr=False)
    _usable_log_observations: int = 0
    _runbooks_found: int = 0
    _zero_match_searches: int = 0
    _not_found_lookups: int = 0
    _returned_remediations: set[str] = field(default_factory=set, repr=False)
    _returned_diagnoses: set[str] = field(default_factory=set, repr=False)
    _terminal_attempts: int = 0
    _rejected_attempts: int = 0
    _rejections: dict[str, int] = field(default_factory=dict, repr=False)
    _accepted_refs: int | None = None
    _accepted: bool = False
    _exhausted: bool = False
    _turns_remaining: int | None = None
    _warnings_issued: int = 0
    _transitions: list[tuple[str, str, str]] = field(default_factory=list, repr=False)
    _state: str = STATE_INVESTIGATING

    def __post_init__(self) -> None:
        if not isinstance(self.context, str) or not self.context:
            raise ValueError("workflow controller context must be a non-empty string")
        unknown = sorted(set(self.treatments) - KNOWN_TREATMENTS)
        if unknown:
            raise ValueError(f"unknown workflow treatments: {unknown}")
        self.treatments = tuple(sorted(set(self.treatments)))
        if isinstance(self.warning_at, bool) or not isinstance(self.warning_at, int):
            raise ValueError("warning_at must be an integer")
        if self.warning_at < 1:
            raise ValueError("warning_at must be at least 1")

    # -- identity ---------------------------------------------------------

    @property
    def summary_field(self) -> str:
        return "workflow_control"

    @property
    def requires_evidence_refs(self) -> bool:
        return TREATMENT_EVIDENCE_REFS in self.treatments

    # -- state machine -----------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def transitions(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(self._transitions)

    def _derived_state(self) -> str:
        if self._accepted:
            return STATE_TERMINAL_ACCEPTED
        if self._exhausted:
            return STATE_BUDGET_EXHAUSTED
        if self._usable_log_observations and self._runbooks_found:
            return STATE_READY_FOR_TERMINAL
        if self._usable_log_observations:
            return STATE_EVIDENCE_COLLECTED
        if self._runbooks_found:
            return STATE_RUNBOOK_RETRIEVED
        return STATE_INVESTIGATING

    def _advance(self, cause: str) -> None:
        new_state = self._derived_state()
        if new_state != self._state:
            self._transitions.append((self._state, new_state, cause))
            self._state = new_state

    # -- turn budget -------------------------------------------------------

    def note_turn(self, turn_index: int, max_turns: int) -> None:
        """Record the current turn so results can carry the remaining budget.

        ``turn_index`` is 0-based; the remaining count excludes this turn.
        """
        if isinstance(turn_index, bool) or not isinstance(turn_index, int) or turn_index < 0:
            raise ValueError("turn_index must be a non-negative integer")
        if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
            raise ValueError("max_turns must be a positive integer")
        if turn_index >= max_turns:
            raise ValueError("turn_index must be below max_turns")
        self._turns_remaining = max_turns - turn_index - 1

    def mark_exhausted(self) -> None:
        """The agent loop exhausted ``max_turns`` without an accepted terminal."""
        if not self._accepted:
            self._exhausted = True
            self._advance("turn budget exhausted")

    def _budget_block(self) -> dict[str, Any]:
        block: dict[str, Any] = {}
        if self._turns_remaining is None:
            return block
        block["turns_remaining"] = self._turns_remaining
        if self._turns_remaining <= self.warning_at:
            self._warnings_issued += 1
            if self._turns_remaining == 0:
                block["warning"] = (
                    "No turns remain. The task ends without an accepted recommendation."
                )
            elif self._turns_remaining == 1:
                block["warning"] = (
                    "Only one turn remains after this one. The next call must be "
                    f"{TERMINAL_TOOL}, or the task fails with no recommendation."
                )
            else:
                block["warning"] = (
                    f"{self._turns_remaining} turns remain after this one. Submit "
                    f"{TERMINAL_TOOL} before the budget is exhausted; a task with no "
                    "accepted recommendation fails."
                )
        return block

    # -- observations ------------------------------------------------------

    def record(self, tool: str, payload: object) -> Observation:
        """Register one non-terminal tool result and return its observation."""
        if tool == TERMINAL_TOOL:
            raise ValueError("the terminal tool result is never an observation")
        sequence = len(self._observations)
        eligibility = classify_observation(tool, payload)
        observation = Observation(
            observation_id=observation_id(self.context, sequence),
            sequence=sequence,
            tool=tool,
            eligibility=eligibility,
        )
        self._observations.append(observation)
        self._by_id[observation.observation_id] = observation
        if tool == "search_logs":
            if eligibility == ELIGIBLE:
                self._usable_log_observations += 1
            elif eligibility == INELIGIBLE_ZERO_MATCH_SEARCH:
                self._zero_match_searches += 1
        elif eligibility == INELIGIBLE_NOT_FOUND:
            self._not_found_lookups += 1
        if tool == "retrieve_runbook" and isinstance(payload, dict):
            if payload.get("found") is True and isinstance(payload.get("runbook"), dict):
                self._runbooks_found += 1
                remediations = payload["runbook"].get("remediation_ids")
                if isinstance(remediations, list):
                    self._returned_remediations.update(
                        r for r in remediations if _is_clean_string(r)
                    )
                candidates = payload.get("diagnosis_candidates")
                if isinstance(candidates, list):
                    self._returned_diagnoses.update(c for c in candidates if _is_clean_string(c))
            elif payload.get("found") is False:
                self._not_found_lookups += 1
        self._advance(f"observed {tool}")
        return observation

    def annotate(self, payload: dict[str, Any], observation: Observation) -> dict[str, Any]:
        """The tool payload returned to the agent: ID, usability, guidance, budget.

        ``usable_evidence`` matches structural eligibility. Runbooks and
        change records stay in the payload as remediation guidance and are
        never marked usable: :meth:`_resolve_refs` rejects them as direct
        evidence.
        """
        block: dict[str, Any] = {"usable_evidence": observation.eligible}
        if observation.tool == "retrieve_runbook" and payload.get("found") is not True:
            block["guidance"] = RUNBOOK_NOT_FOUND_GUIDANCE
        elif observation.eligibility in UNUSABLE_GUIDANCE:
            block["guidance"] = UNUSABLE_GUIDANCE[observation.eligibility]
        block.update(self._budget_block())
        return {**payload, OBSERVATION_ID_FIELD: observation.observation_id, WORKFLOW_FIELD: block}

    @property
    def observations(self) -> tuple[Observation, ...]:
        return tuple(self._observations)

    # -- terminal validation ----------------------------------------------

    def validate_terminal(self, arguments: dict[str, Any]) -> WorkflowVerdict:
        """Accept only a structurally complete, evidence-backed recommendation.

        Every unmet requirement is reported as a generic category. The
        controller never says which evidence would have satisfied it.
        """
        self._terminal_attempts += 1
        verdict = self._judge(arguments)
        if verdict.accepted:
            self._accepted = True
            self._accepted_refs = verdict.cited if self.requires_evidence_refs else None
            self._advance("terminal accepted")
        else:
            self._rejected_attempts += 1
            for category in verdict.failure_categories:
                self._rejections[category] = self._rejections.get(category, 0) + 1
            self._advance("terminal rejected")
        return verdict

    def rejection_payload(self, verdict: WorkflowVerdict) -> dict[str, Any]:
        """Rejection payload with the remaining-turn block attached."""
        return {**verdict.tool_payload(), WORKFLOW_FIELD: self._budget_block()}

    def _judge(self, arguments: object) -> WorkflowVerdict:
        failures: list[str] = []
        if not isinstance(arguments, dict):
            return WorkflowVerdict(accepted=False, failure_categories=(REJECT_MALFORMED_TERMINAL,))
        allowed = {"diagnosis_id", "rationale", "remediation_id"}
        if self.requires_evidence_refs:
            allowed.add(EVIDENCE_REFS_ARGUMENT)
        diagnosis = arguments.get("diagnosis_id")
        remediation = arguments.get("remediation_id")
        rationale = arguments.get("rationale")
        if any(key not in allowed for key in arguments) or not all(
            _is_clean_string(v) for v in (diagnosis, remediation, rationale)
        ):
            failures.append(REJECT_MALFORMED_TERMINAL)
        if not self._observations:
            failures.append(REJECT_INVESTIGATION_REQUIRED)
        if self._usable_log_observations < 1:
            failures.append(REJECT_LOG_EVIDENCE_REQUIRED)
        if self._runbooks_found < 1:
            failures.append(REJECT_RUNBOOK_REQUIRED)
        elif _is_clean_string(remediation) and remediation not in self._returned_remediations:
            failures.append(REJECT_REMEDIATION_NOT_IN_RUNBOOK)
        if (
            self._returned_diagnoses
            and _is_clean_string(diagnosis)
            and diagnosis not in self._returned_diagnoses
        ):
            failures.append(REJECT_DIAGNOSIS_NOT_PUBLISHED)
        cited = 0
        if self.requires_evidence_refs:
            refs = arguments.get(EVIDENCE_REFS_ARGUMENT)
            resolved = self._resolve_refs(refs)
            if resolved is None:
                failures.append(REJECT_EVIDENCE_REFS_REQUIRED)
            else:
                cited = resolved
        if failures:
            return WorkflowVerdict(accepted=False, failure_categories=tuple(failures))
        return WorkflowVerdict(accepted=True, cited=cited)

    def _resolve_refs(self, refs: object) -> int | None:
        if not isinstance(refs, list) or not refs:
            return None
        resolved: set[str] = set()
        for ref in refs:
            if not isinstance(ref, str):
                return None
            observation = self._by_id.get(ref)
            if observation is None or not observation.eligible:
                return None
            resolved.add(ref)
        return len(resolved)

    # -- sanitized accounting ----------------------------------------------

    @property
    def terminal_attempts(self) -> int:
        return self._terminal_attempts

    @property
    def rejected_terminal_attempts(self) -> int:
        return self._rejected_attempts

    @property
    def exhaustion_error_category(self) -> str:
        """Error category when the agent loop exhausts its turn budget."""
        return (
            WORKFLOW_REQUIREMENTS_UNMET
            if self._rejected_attempts > 0
            else "no_terminal_recommendation"
        )

    def summary(self) -> dict[str, Any]:
        """Counts and generic state names only: no IDs, payloads, or content."""
        return {
            "controller": self.controller_id,
            "treatments": list(self.treatments),
            "final_state": self._state,
            "transitions": [
                {"from": before, "to": after} for before, after, _cause in self._transitions
            ],
            "observations": len(self._observations),
            "eligible_observations": sum(1 for o in self._observations if o.eligible),
            "usable_log_observations": self._usable_log_observations,
            "zero_match_searches": self._zero_match_searches,
            "not_found_lookups": self._not_found_lookups,
            "runbooks_found": self._runbooks_found,
            "terminal_attempts": self._terminal_attempts,
            "rejected_terminal_attempts": self._rejected_attempts,
            "rejection_categories": dict(sorted(self._rejections.items())),
            "accepted_evidence_refs": self._accepted_refs,
            "remaining_turn_warnings": self._warnings_issued,
        }
