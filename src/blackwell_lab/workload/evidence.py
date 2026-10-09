"""Evidence-grounding controller ``evidence-grounding-v1`` (workload 2.5.0).

The controller sits between the agent loop and the simulated toolbox for
one task execution. It does three things and nothing else:

1. **Assigns opaque, task-local observation IDs** to every non-terminal
   tool result. An ID is a salted SHA-256 prefix of the task's private
   execution context plus the observation sequence number, so it reveals
   neither the tool, the order, nor any scenario content. The context is
   unique per task execution (per repetition, per concurrency slot), so an
   ID from another task, another repetition, another worker, or an earlier
   execution of the same instance never resolves here.
2. **Tags structural eligibility.** Direct evidence is a successful,
   nonempty diagnostic observation: a ``search_logs`` result with at least
   one returned line, a usable ``get_service_health`` response, or a found
   ``query_metrics`` series with data points. Zero-match searches,
   not-found lookups, unknown services, malformed payloads, runbooks, and
   change records are **ineligible** (runbooks and change records may guide
   the agent but are never direct evidence by themselves).
3. **Validates the terminal recommendation's provenance.** A
   ``recommend_remediation`` call is accepted only when ``evidence_refs``
   is a nonempty list whose every entry resolves to an *earlier, eligible*
   observation of this task. Anything else is rejected with the single
   generic failure category :data:`DIRECT_EVIDENCE_REQUIRED`; the rejection
   never says which reference failed or why.

The controller judges **provenance and structure only**. It does not know
the scenario, the accepted answers, the evaluator predicates, or whether
the cited observation semantically supports the diagnosis; irrelevant but
structurally eligible evidence passes the controller and is left to the
unchanged evaluator (``evaluator 3.1.0``). The controller holds no prompts,
completions, reasoning, or credentials: its state is the ordered list of
(observation id, tool, eligibility) tuples for one task.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from blackwell_lab.workload.tools import TERMINAL_TOOL

#: Controller identity recorded in manifests, candidate identity, receipts.
CONTROLLER_EVIDENCE_GROUNDING_V1 = "evidence-grounding-v1"

#: Workload contract -> bound controller. ``None`` means the plain agent
#: loop. Unknown workload versions never reach this table: they fail in
#: :func:`blackwell_lab.workload.native_tools.require_workload_version`.
WORKLOAD_CONTROLLERS: dict[str, str | None] = {
    "2.3.0": None,
    "2.4.0": None,
    "2.4.1": None,
    "2.5.0": CONTROLLER_EVIDENCE_GROUNDING_V1,
}

#: Workflow-controlled contracts (decision D-0031). Kept in a separate,
#: closed table so the historical binding above stays byte-identical.
#: Both members bind the same generic controller; 2.6.1 adds the single
#: ``evidence-refs`` treatment (see :mod:`blackwell_lab.workload.workflow`).
CONTROLLER_WORKFLOW_V1 = "workflow-controller-v1"
WORKFLOW_WORKLOAD_CONTROLLERS: dict[str, str] = {
    "2.6.0": CONTROLLER_WORKFLOW_V1,
    "2.6.1": CONTROLLER_WORKFLOW_V1,
}
WORKFLOW_WORKLOAD_TREATMENTS: dict[str, tuple[str, ...]] = {
    "2.6.0": (),
    "2.6.1": ("evidence-refs",),
}
#: Successor pair (decision D-0033). A separate table so the 2.6.0 / 2.6.1
#: binding above stays byte-identical. Both members bind
#: ``workflow-controller-v2``; 2.7.1 adds the same ``evidence-refs`` treatment.
CONTROLLER_WORKFLOW_V2 = "workflow-controller-v2"
SUCCESSOR_WORKLOAD_CONTROLLERS: dict[str, str] = {
    "2.7.0": CONTROLLER_WORKFLOW_V2,
    "2.7.1": CONTROLLER_WORKFLOW_V2,
}
SUCCESSOR_WORKLOAD_TREATMENTS: dict[str, tuple[str, ...]] = {
    "2.7.0": (),
    "2.7.1": ("evidence-refs",),
}
#: Structured-evidence pair (decision D-0034). A separate table so the
#: 2.6 and 2.7 bindings stay byte-identical. Both members bind
#: ``workflow-controller-v3``; 2.8.1 adds the same ``evidence-refs`` treatment.
CONTROLLER_WORKFLOW_V3 = "workflow-controller-v3"
STRUCTURED_WORKLOAD_CONTROLLERS: dict[str, str] = {
    "2.8.0": CONTROLLER_WORKFLOW_V3,
    "2.8.1": CONTROLLER_WORKFLOW_V3,
}
STRUCTURED_WORKLOAD_TREATMENTS: dict[str, tuple[str, ...]] = {
    "2.8.0": (),
    "2.8.1": ("evidence-refs",),
}


def all_workload_controllers() -> dict[str, str | None]:
    """Every executed contract -> bound controller (historical plus workflow)."""
    return {
        **WORKLOAD_CONTROLLERS,
        **WORKFLOW_WORKLOAD_CONTROLLERS,
        **SUCCESSOR_WORKLOAD_CONTROLLERS,
        **STRUCTURED_WORKLOAD_CONTROLLERS,
    }


def workload_treatments(workload_version: str | None) -> tuple[str, ...]:
    """Explicit experimental treatments bound to a contract (empty for most)."""
    from blackwell_lab.workload.native_tools import require_workload_version

    resolved = require_workload_version(workload_version)
    if resolved in STRUCTURED_WORKLOAD_TREATMENTS:
        return STRUCTURED_WORKLOAD_TREATMENTS[resolved]
    if resolved in SUCCESSOR_WORKLOAD_TREATMENTS:
        return SUCCESSOR_WORKLOAD_TREATMENTS[resolved]
    return WORKFLOW_WORKLOAD_TREATMENTS.get(resolved, ())


#: Field injected into every non-terminal tool result returned to the agent.
OBSERVATION_ID_FIELD = "observation_id"
#: Terminal-tool argument carrying the cited observation IDs.
EVIDENCE_REFS_ARGUMENT = "evidence_refs"
#: The only failure category the controller ever returns.
DIRECT_EVIDENCE_REQUIRED = "direct_evidence_required"

OBSERVATION_ID_PREFIX = "obs-"
_OBSERVATION_ID_HEX = 24

#: Tools whose successful, nonempty results are direct diagnostic evidence.
DIRECT_EVIDENCE_TOOLS = frozenset({"search_logs", "get_service_health", "query_metrics"})
#: Tools that may guide the agent but are never direct evidence alone.
GUIDANCE_TOOLS = frozenset({"retrieve_runbook", "check_recent_changes"})

#: Health status tokens the toolbox uses for an unknown or unusable service.
_UNUSABLE_HEALTH_STATUSES = frozenset({"unknown-service", "unknown", ""})

#: Structural eligibility reason codes (never content, never scenario data).
ELIGIBLE = "eligible"
INELIGIBLE_ZERO_MATCH_SEARCH = "zero_match_search"
INELIGIBLE_NOT_FOUND = "not_found"
INELIGIBLE_UNUSABLE_STATUS = "unusable_status"
INELIGIBLE_MALFORMED = "malformed_result"
INELIGIBLE_GUIDANCE_ONLY = "guidance_only"
INELIGIBLE_UNKNOWN_TOOL = "unknown_tool"


def controller_for_workload(workload_version: str | None) -> str | None:
    """Controller bound to an executed workload contract (fails closed)."""
    from blackwell_lab.workload.native_tools import require_workload_version

    return all_workload_controllers()[require_workload_version(workload_version)]


def require_controller_binding(workload_version: str | None, controller: str | None) -> str | None:
    """Reject any workload/controller combination other than the bound one.

    Called before a model client is constructed or used. Returns the
    resolved controller (``None`` for controller-free workloads).
    """
    from blackwell_lab.workload.validation import ConfigError

    expected = controller_for_workload(workload_version)
    if controller != expected:
        raise ConfigError(
            "workload/controller binding mismatch: workload "
            f"{workload_version or 'default'} is bound to controller "
            f"{expected or 'none'}, not {controller or 'none'}"
        )
    return expected


def observation_id(context: str, sequence: int) -> str:
    """Deterministic opaque ID for observation ``sequence`` of ``context``.

    Same context and sequence -> same ID; any other context -> an unrelated
    ID. The hex prefix carries no tool, order, or scenario information.
    """
    if not isinstance(context, str) or not context:
        raise ValueError("observation context must be a non-empty string")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("observation sequence must be a non-negative integer")
    digest = hashlib.sha256(f"{context}\x1f{sequence}".encode()).hexdigest()
    return f"{OBSERVATION_ID_PREFIX}{digest[:_OBSERVATION_ID_HEX]}"


def _usable_health(payload: dict[str, Any]) -> str:
    services = payload.get("services")
    if not isinstance(services, dict) or not services:
        return INELIGIBLE_MALFORMED
    saw_usable = False
    saw_unusable = False
    for components in services.values():
        if not isinstance(components, dict) or not components:
            saw_unusable = True
            continue
        statuses = list(components.values())
        if any(not isinstance(status, str) for status in statuses):
            return INELIGIBLE_MALFORMED
        if any(status.strip().casefold() in _UNUSABLE_HEALTH_STATUSES for status in statuses):
            saw_unusable = True
            continue
        saw_usable = True
    if saw_usable:
        return ELIGIBLE
    return INELIGIBLE_UNUSABLE_STATUS if saw_unusable else INELIGIBLE_MALFORMED


def _usable_search(payload: dict[str, Any]) -> str:
    total = payload.get("total_matches")
    lines = payload.get("lines")
    if isinstance(total, bool) or not isinstance(total, int) or not isinstance(lines, list):
        return INELIGIBLE_MALFORMED
    if total <= 0:
        return INELIGIBLE_ZERO_MATCH_SEARCH
    if not lines:
        return INELIGIBLE_MALFORMED
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("message"), str):
            return INELIGIBLE_MALFORMED
    return ELIGIBLE


def _usable_metric(payload: dict[str, Any]) -> str:
    found = payload.get("found")
    if found is False:
        return INELIGIBLE_NOT_FOUND
    if found is not True:
        return INELIGIBLE_MALFORMED
    metric = payload.get("metric")
    points = payload.get("points")
    if not isinstance(metric, str) or not metric.strip() or not isinstance(points, list):
        return INELIGIBLE_MALFORMED
    if not points:
        return INELIGIBLE_MALFORMED
    for point in points:
        value = point.get("value") if isinstance(point, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return INELIGIBLE_MALFORMED
    return ELIGIBLE


def classify_observation(tool: str, payload: object) -> str:
    """Structural eligibility of one tool result (pure, scenario-blind).

    Returns :data:`ELIGIBLE` or one of the ``INELIGIBLE_*`` reason codes.
    The function inspects only the structured response fields the tool
    contract defines; it never compares content against scenario data.
    """
    if tool in GUIDANCE_TOOLS:
        return INELIGIBLE_GUIDANCE_ONLY
    if tool not in DIRECT_EVIDENCE_TOOLS:
        return INELIGIBLE_UNKNOWN_TOOL
    if not isinstance(payload, dict):
        return INELIGIBLE_MALFORMED
    if tool == "search_logs":
        return _usable_search(payload)
    if tool == "get_service_health":
        return _usable_health(payload)
    return _usable_metric(payload)


@dataclass(frozen=True)
class Observation:
    """One recorded tool result: opaque ID, tool, structural eligibility."""

    observation_id: str
    sequence: int
    tool: str
    eligibility: str

    @property
    def eligible(self) -> bool:
        return self.eligibility == ELIGIBLE


@dataclass(frozen=True)
class TerminalVerdict:
    """The controller's decision on one terminal recommendation attempt."""

    accepted: bool
    cited: int
    failure_category: str | None = None

    def tool_payload(self) -> dict[str, Any]:
        """The generic payload returned to the agent on rejection."""
        return {"accepted": False, "failure_category": self.failure_category}


@dataclass
class EvidenceGroundingController:
    """Per-task evidence store and terminal validator (``evidence-grounding-v1``).

    One instance exists per task execution and is never shared between
    tasks, repetitions, or scheduler workers. ``context`` is the private
    execution context from which observation IDs are derived.
    """

    context: str
    controller_id: str = CONTROLLER_EVIDENCE_GROUNDING_V1
    _observations: list[Observation] = field(default_factory=list, repr=False)
    _by_id: dict[str, Observation] = field(default_factory=dict, repr=False)
    _terminal_attempts: int = 0
    _rejected_attempts: int = 0
    _accepted_refs: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.context, str) or not self.context:
            raise ValueError("evidence controller context must be a non-empty string")

    # -- observations -----------------------------------------------------

    def record(self, tool: str, payload: object) -> Observation:
        """Register one non-terminal tool result and return its observation."""
        if tool == TERMINAL_TOOL:
            raise ValueError("the terminal tool result is never an observation")
        sequence = len(self._observations)
        observation = Observation(
            observation_id=observation_id(self.context, sequence),
            sequence=sequence,
            tool=tool,
            eligibility=classify_observation(tool, payload),
        )
        self._observations.append(observation)
        self._by_id[observation.observation_id] = observation
        return observation

    def annotate(self, payload: dict[str, Any], observation: Observation) -> dict[str, Any]:
        """The tool payload returned to the agent, carrying only the ID."""
        return {**payload, OBSERVATION_ID_FIELD: observation.observation_id}

    @property
    def observations(self) -> tuple[Observation, ...]:
        return tuple(self._observations)

    # -- terminal validation ----------------------------------------------

    def validate_terminal(self, arguments: dict[str, Any]) -> TerminalVerdict:
        """Accept only references to earlier eligible observations of this task.

        Rejections carry the generic category only. The controller never
        reports which reference failed, nor why, to the agent.
        """
        self._terminal_attempts += 1
        refs = arguments.get(EVIDENCE_REFS_ARGUMENT) if isinstance(arguments, dict) else None
        verdict = self._judge(refs)
        if verdict.accepted:
            self._accepted_refs = verdict.cited
        else:
            self._rejected_attempts += 1
        return verdict

    def _judge(self, refs: object) -> TerminalVerdict:
        rejected = TerminalVerdict(
            accepted=False, cited=0, failure_category=DIRECT_EVIDENCE_REQUIRED
        )
        if not isinstance(refs, list) or not refs:
            return rejected
        resolved: set[str] = set()
        for ref in refs:
            if not isinstance(ref, str):
                return rejected
            observation = self._by_id.get(ref)
            if observation is None or not observation.eligible:
                return rejected
            resolved.add(ref)
        return TerminalVerdict(accepted=True, cited=len(resolved))

    # -- sanitized accounting ----------------------------------------------

    @property
    def terminal_attempts(self) -> int:
        return self._terminal_attempts

    @property
    def rejected_terminal_attempts(self) -> int:
        return self._rejected_attempts

    def summary(self) -> dict[str, Any]:
        """Counts only: no IDs, no payloads, no prompts, no reasoning."""
        return {
            "controller": self.controller_id,
            "observations": len(self._observations),
            "eligible_observations": sum(1 for o in self._observations if o.eligible),
            "terminal_attempts": self._terminal_attempts,
            "rejected_terminal_attempts": self._rejected_attempts,
            "accepted_evidence_refs": self._accepted_refs,
        }


def build_controller(workload_version: str | None, context: str) -> object | None:
    """The controller instance for one task, or ``None`` for plain workloads.

    Returns an :class:`EvidenceGroundingController` for workload 2.5.0, a
    :class:`blackwell_lab.workload.workflow.WorkflowController` for workloads
    2.6.0 / 2.6.1, and a
    :class:`blackwell_lab.workload.workflow.DiagnosisRelevantWorkflowController`
    for workloads 2.7.0 / 2.7.1, and a
    :class:`blackwell_lab.workload.workflow_v3.StructuredEvidenceWorkflowController`
    for workloads 2.8.0 / 2.8.1.
    """
    controller = controller_for_workload(workload_version)
    if controller is None:
        return None
    if controller == CONTROLLER_EVIDENCE_GROUNDING_V1:
        return EvidenceGroundingController(context=context)
    if controller == CONTROLLER_WORKFLOW_V1:
        from blackwell_lab.workload.workflow import WorkflowController

        return WorkflowController(context=context, treatments=workload_treatments(workload_version))
    if controller == CONTROLLER_WORKFLOW_V2:
        from blackwell_lab.workload.workflow import DiagnosisRelevantWorkflowController

        return DiagnosisRelevantWorkflowController(
            context=context, treatments=workload_treatments(workload_version)
        )
    if controller == CONTROLLER_WORKFLOW_V3:
        from blackwell_lab.workload.public_metadata import (
            evidence_contract,
            require_evidence_contract,
        )
        from blackwell_lab.workload.workflow_v3 import StructuredEvidenceWorkflowController

        require_evidence_contract(evidence_contract())
        return StructuredEvidenceWorkflowController(
            context=context, treatments=workload_treatments(workload_version)
        )
    raise ValueError(f"no implementation registered for controller {controller!r}")
