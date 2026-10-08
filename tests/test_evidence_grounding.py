"""Adversarial offline tests for the evidence-grounding controller (P2).

Workload 2.5.0 binds controller ``evidence-grounding-v1``. These tests prove
the controller contract (opaque task-local IDs, isolation, structural
eligibility, the single generic rejection category, no semantic judgment),
the frozen turn budget, the P2 candidate/CLI/manifest binding, and that
C1, C2, P1, evaluator 3.1.0, scenarios, thresholds, and the 2.4.1 prompt are
unchanged. Everything runs offline against scripted clients and the virtual
clock: no network, no credentials, no inference.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Sequence

import pytest
from fakes import FakeClock

from blackwell_lab.cloud import cli, lifecycle
from blackwell_lab.cloud.cli import main
from blackwell_lab.cloud.qualification import (
    AUTHORIZED_CANDIDATES,
    C2_TEMPERATURE,
    CANDIDATE_CONTROLLERS,
    CANDIDATE_P2,
    CANDIDATE_WORKLOAD_VERSIONS,
    DEVELOPMENT_QUALITY_FLOOR,
    DEVELOPMENT_TASKS,
    FREEZE_TASKS,
    HOLDOUT_QUALITY_FLOOR,
    HOLDOUT_TASKS,
    P2_CONTROLLER,
    P2_TEMPERATURE,
    P2_WORKLOAD_VERSION,
    PRODUCTION_LIKE_MIN_AGGREGATE,
    PRODUCTION_LIKE_MIN_SCENARIO,
    STUDY_ENTRY_MIN_AGGREGATE,
    STUDY_ENTRY_MIN_SCENARIO,
    approval_phrase,
    candidate_controller,
    candidate_identity_digest,
    candidate_temperature,
    candidate_workload_version,
    experimental_behavior_fields,
    experimental_configuration_digest,
    frozen_candidate_fields,
    sanitized_receipt,
    serialize_candidate,
    stage_spec,
    validate_authorized_qualification_config,
)
from blackwell_lab.cloud.realbench import RealRunSpec, run_real_cell
from blackwell_lab.workload import agent as agent_module
from blackwell_lab.workload.agent import (
    DEFAULT_MAX_TURNS,
    ERROR_TAXONOMY,
    SYSTEM_PROMPT_V230,
    SYSTEM_PROMPT_V240,
    SYSTEM_PROMPT_V241,
    TaskExecution,
    run_task,
    system_prompt,
)
from blackwell_lab.workload.clock import SYSTEM_CLOCK
from blackwell_lab.workload.evaluator import EVALUATOR_VERSION, QUALITY_THRESHOLD, evaluate
from blackwell_lab.workload.evidence import (
    CONTROLLER_EVIDENCE_GROUNDING_V1,
    DIRECT_EVIDENCE_REQUIRED,
    DIRECT_EVIDENCE_TOOLS,
    ELIGIBLE,
    EVIDENCE_REFS_ARGUMENT,
    GUIDANCE_TOOLS,
    INELIGIBLE_GUIDANCE_ONLY,
    INELIGIBLE_NOT_FOUND,
    INELIGIBLE_UNUSABLE_STATUS,
    INELIGIBLE_ZERO_MATCH_SEARCH,
    OBSERVATION_ID_FIELD,
    WORKLOAD_CONTROLLERS,
    EvidenceGroundingController,
    build_controller,
    classify_observation,
    controller_for_workload,
    observation_id,
    require_controller_binding,
)
from blackwell_lab.workload.model_client import (
    DeterministicMockClient,
    GenerationSettings,
    Message,
    ModelClient,
    NativeToolCall,
    StreamEvent,
    _eligible_refs,
)
from blackwell_lab.workload.native_tools import (
    TOOL_DESCRIPTIONS_V240,
    ToolCallAssembler,
    openai_tool_definitions,
    tool_descriptions,
)
from blackwell_lab.workload.openai_client import OpenAICompatibleClient
from blackwell_lab.workload.runner import PROFILES, _observation, _run_pass
from blackwell_lab.workload.sampling import generate_task_instances
from blackwell_lab.workload.scenarios import WORKLOAD_VERSION, catalog, catalog_digest
from blackwell_lab.workload.tools import (
    TERMINAL_TOOL,
    TOOL_SPECS,
    TOOL_SPECS_V250,
    InvalidToolArgumentsError,
    SimulatedToolbox,
    tool_specs,
    validate_tool_call,
)
from blackwell_lab.workload.validation import ConfigError

# Current D-0032 ca-central identity contract, recomputed from serialize_candidate.
# The D-0030 us-ord digests, the D-0029 us-sea digests, and the D-0027
# us-iad-2 digests stay historical in the decision log.
FROZEN_C1_IDENTITY_SHA256 = "22d92d3351ed92b7b46ba0e1c0756c6ecf8c47abe0ae2b6070b9b821e482f27e"
FROZEN_C2_IDENTITY_SHA256 = "062bc2063a967638a5943fd760126703a66c34f1d9ec07a4f99e245c538f421b"
FROZEN_P1_IDENTITY_SHA256 = "be54086f77c59f334b5b77bbfbd04d917c295d875a1542c79aa9cb69d0da3bc3"
FROZEN_ACCEPTED_ANSWERS_SHA256 = "2edb7134040af2e8e9e9fec4c068bbc0dfaf6bfa55cbd4284b8bf6045c7aea2a"
FROZEN_SYSTEM_PROMPT_V240_SHA256 = (
    "8c8c84b85f8970485380fc5de299ef1f902f8007007185d8375385d65f44ae08"
)
FROZEN_SYSTEM_PROMPT_V241_SHA256 = (
    "37b3a4fb615dc21c8d39a5301dc4318870fea3498fbed196c50bfcbe67de1bd3"
)
FROZEN_SYSTEM_PROMPT_V241 = (
    "You are a Cloud Operations Agent working a synthetic incident. "
    "Diagnose the incident using only the provided tools. "
    "Call exactly one tool per turn. "
    "Required workflow: "
    "(1) inspect relevant metrics, changes, logs, and other evidence; "
    "(2) select the exact diagnosis ID from the published diagnosis candidates; "
    "(3) infer the affected service or system from the evidence; "
    "(4) call retrieve_runbook using that service/system key; "
    "(5) select an exact remediation ID returned in runbook.remediation_ids; "
    "(6) call recommend_remediation with that exact ID and an evidence-based rationale. "
    "search_logs uses literal substring matching, not semantic search. "
    "Search queries should use exact identifiers, service names, "
    "configuration IDs, job IDs, or diagnostic terms supported by "
    "information already available to the agent. "
    "A zero-match search must be retried with a different specific "
    "token before making a terminal recommendation. "
    "Gather direct supporting evidence for the diagnosis before "
    "submitting the terminal recommendation. "
    "Seeing a plausible change record or runbook remediation is not "
    "a substitute for collecting the required incident evidence. "
    "Submit exactly one recommendation via recommend_remediation."
)

SCENARIO_ID = "elevated-latency-001"
SERVICE = "zephyr-cart"
UNRELATED_HEALTHY_SERVICE = "heron-auth"
V250 = GenerationSettings(workload_version="2.5.0")
COMMIT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
RUN_TAG = "p3-qual-20260918a"


# --- scripted client and helpers ---------------------------------------------


class ScriptedClient(ModelClient):
    """Replays scripted native tool calls; a step may be a callable of the
    conversation so a test can cite observation IDs it has seen."""

    name = "scripted"
    version = "0.0.0"

    def __init__(self, steps: Sequence[object]) -> None:
        self.steps = list(steps)
        self.calls = 0
        self.seen: list[tuple[list[Message], GenerationSettings]] = []

    def stream_turn(self, messages, settings, *, deadline=None, clock=SYSTEM_CLOCK):
        self.calls += 1
        self.seen.append((list(messages), settings))
        index = sum(1 for message in messages if message.role == "tool")
        step = self.steps[min(index, len(self.steps) - 1)]
        if callable(step):
            step = step(messages)
        yield StreamEvent(kind="native_tool_call_delta")
        yield StreamEvent(
            kind="native_tool_call",
            tool_call=NativeToolCall(
                call_id=f"call-{index}",
                name=step["tool"],
                arguments=dict(step["arguments"]),
            ),
        )


def health(service: str = SERVICE) -> dict:
    return {"tool": "get_service_health", "arguments": {"service": service}}


def search(query: str) -> dict:
    return {"tool": "search_logs", "arguments": {"query": query}}


def metric(name: str) -> dict:
    return {"tool": "query_metrics", "arguments": {"metric": name}}


def runbook(key: str = SERVICE) -> dict:
    return {"tool": "retrieve_runbook", "arguments": {"key": key}}


def changes() -> dict:
    return {"tool": "check_recent_changes", "arguments": {}}


def terminal(refs: object = None, *, scenario=None, omit_refs: bool = False) -> dict:
    scenario = scenario or catalog()[SCENARIO_ID]
    arguments: dict = {
        "diagnosis_id": scenario.accepted_diagnoses[0],
        "rationale": "evidence-based rationale",
        "remediation_id": scenario.accepted_remediations[0],
    }
    if not omit_refs:
        arguments[EVIDENCE_REFS_ARGUMENT] = refs
    return {"tool": TERMINAL_TOOL, "arguments": arguments}


def seen_ids(messages: Sequence[Message]) -> list[str]:
    """Every observation_id returned to the agent so far, in order."""
    ids: list[str] = []
    for message in messages:
        if message.role != "tool":
            continue
        payload = json.loads(message.content)
        if isinstance(payload, dict) and OBSERVATION_ID_FIELD in payload:
            ids.append(payload[OBSERVATION_ID_FIELD])
    return ids


def cite_all(messages: Sequence[Message]) -> dict:
    """Cite every observation seen, eligible or not (a naive agent)."""
    return terminal(seen_ids(messages))


def cite_eligible(messages: Sequence[Message]) -> dict:
    """Cite only the structurally eligible observations seen so far."""
    return terminal(_eligible_refs(messages))


def cite(indexes: Sequence[int]):
    def _step(messages: Sequence[Message]) -> dict:
        ids = seen_ids(messages)
        return terminal([ids[i] for i in indexes])

    return _step


def run(
    steps: Sequence[object],
    *,
    scenario_id: str = SCENARIO_ID,
    settings: GenerationSettings = V250,
    max_turns: int = DEFAULT_MAX_TURNS,
    toolbox: SimulatedToolbox | None = None,
    evidence_context: str | None = None,
    client: ScriptedClient | None = None,
) -> tuple[TaskExecution, ScriptedClient]:
    scenario = catalog()[scenario_id]
    client = client or ScriptedClient(steps)
    toolbox = toolbox or SimulatedToolbox(scenario, clock=FakeClock())
    execution = run_task(
        scenario,
        client,
        toolbox,
        settings,
        timeout_s=120.0,
        max_turns=max_turns,
        clock=FakeClock(),
        evidence_context=evidence_context,
    )
    return execution, client


def trace_ids(execution: TaskExecution) -> list[str]:
    return [
        trace.result[OBSERVATION_ID_FIELD]
        for trace in execution.tool_trace
        if OBSERVATION_ID_FIELD in trace.result
    ]


def rejections(execution: TaskExecution) -> list[dict]:
    return [
        trace.result
        for trace in execution.tool_trace
        if trace.tool == TERMINAL_TOOL and trace.result.get("accepted") is False
    ]


REJECTION_PAYLOAD = {"accepted": False, "failure_category": DIRECT_EVIDENCE_REQUIRED}


def _catalog_answer_tokens() -> set[str]:
    """Scenario-specific strings that must not appear in generic text."""
    tokens: set[str] = set()

    def add(value: object) -> None:
        if isinstance(value, str) and value.strip():
            tokens.add(value)

    for scenario in catalog().values():
        add(scenario.scenario_id)
        add(scenario.title)
        add(scenario.incident_class)
        add(scenario.affected_service)
        add(scenario.description)
        add(scenario.root_cause_id)
        add(scenario.root_cause_summary)
        for item in (
            *scenario.accepted_diagnoses,
            *scenario.distractor_diagnoses,
            *scenario.accepted_remediations,
            *scenario.distractor_remediations,
        ):
            add(item)
        for line in scenario.logs:
            add(line.get("message"))
        for change in scenario.recent_changes:
            add(change.get("change_id"))
            add(change.get("summary"))
            add(change.get("kind"))
            add(change.get("service"))
        for key, runbook_doc in scenario.runbooks.items():
            add(key)
            add(runbook_doc.get("title"))
            for step in runbook_doc.get("steps") or ():
                add(step)
            for remediation_id in runbook_doc.get("remediation_ids") or ():
                add(remediation_id)
        for name in scenario.metrics:
            add(name)
        for service, components in scenario.health.items():
            add(service)
            for component, status in components.items():
                add(component)
                add(status)
        for predicate in scenario.evidence_predicates:
            add(predicate.predicate_id)
            add(predicate.description)
            for alternative in predicate.alternatives:
                for _argument, needle in alternative.argument_contains:
                    add(needle)
                for attr in ("text", "change_id", "remediation_id", "metric", "component"):
                    if hasattr(alternative.result, attr):
                        add(getattr(alternative.result, attr))
        for sequence in (scenario.reference_tool_sequence, scenario.alternative_tool_sequence):
            for step in sequence:
                for value in (step.get("arguments") or {}).values():
                    add(value)
    return tokens


def _accepted_answers_payload() -> bytes:
    payload = {
        scenario_id: {
            "accepted_diagnoses": list(scenario.accepted_diagnoses),
            "accepted_remediations": list(scenario.accepted_remediations),
            "evidence_predicate_ids": [p.predicate_id for p in scenario.evidence_predicates],
        }
        for scenario_id, scenario in catalog().items()
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


# --- valid evidence and the generic rejection ---------------------------------


class TestValidAndMissingEvidence:
    def test_valid_task_local_evidence_is_accepted(self):
        execution, client = run([health(), search("audit"), runbook(), cite_eligible])
        assert execution.status == "completed"
        assert execution.error_category is None
        assert execution.diagnosis_id == catalog()[SCENARIO_ID].accepted_diagnoses[0]
        ids = trace_ids(execution)
        assert len(ids) == 3 and len(set(ids)) == 3
        assert all(i.startswith("obs-") for i in ids)
        # Only the two direct observations are cited (runbook is guidance);
        # the controller accepted every cited reference.
        terminal_trace = execution.tool_trace[-1]
        assert terminal_trace.tool == TERMINAL_TOOL
        assert terminal_trace.result["acknowledged"] is True
        assert terminal_trace.arguments[EVIDENCE_REFS_ARGUMENT] == ids[:2]
        assert terminal_trace.result[EVIDENCE_REFS_ARGUMENT] == ids[:2]
        assert execution.evidence_grounding == {
            "controller": CONTROLLER_EVIDENCE_GROUNDING_V1,
            "observations": 3,
            "eligible_observations": 2,
            "terminal_attempts": 1,
            "rejected_terminal_attempts": 0,
            "accepted_evidence_refs": 2,
        }
        assert client.calls == len(execution.turns) == 4
        assert evaluate(catalog()[SCENARIO_ID], execution).gates[0].passed is True

    def test_citing_an_ineligible_guidance_observation_poisons_the_list(self):
        # The naive agent cites the runbook too; one ineligible reference
        # rejects the whole terminal attempt.
        execution, _ = run([health(), search("audit"), runbook(), cite_all], max_turns=4)
        assert execution.status == "error"
        assert execution.error_category == DIRECT_EVIDENCE_REQUIRED
        assert execution.evidence_grounding["eligible_observations"] == 2
        assert execution.evidence_grounding["rejected_terminal_attempts"] == 1

    def test_citing_only_eligible_subset_is_accepted(self):
        execution, _ = run([runbook(), health(), search("audit"), cite([1])])
        assert execution.status == "completed"
        assert execution.evidence_grounding["accepted_evidence_refs"] == 1

    def test_missing_evidence_refs_is_rejected_with_generic_category_only(self):
        steps = [health(), search("audit"), terminal(omit_refs=True)]
        execution, client = run(steps, max_turns=5)
        assert execution.status == "error"
        assert execution.error_category == DIRECT_EVIDENCE_REQUIRED
        rejected = rejections(execution)
        assert rejected and all(r == REJECTION_PAYLOAD for r in rejected)
        # The agent received exactly the generic payload as the tool message.
        last_tool = [m for m in client.seen[-1][0] if m.role == "tool"][-1]
        assert json.loads(last_tool.content) == REJECTION_PAYLOAD
        assert client.calls == len(execution.turns) == 5
        assert execution.evidence_grounding["rejected_terminal_attempts"] == 3
        assert execution.evidence_grounding["accepted_evidence_refs"] is None

    @pytest.mark.parametrize("refs", [[], None, "obs-not-a-list", 7, {"obs": 1}, [1], [None]])
    def test_empty_or_non_string_refs_are_rejected(self, refs):
        controller = EvidenceGroundingController(context="ctx")
        controller.record("search_logs", {"total_matches": 1, "lines": [{"message": "x"}]})
        verdict = controller.validate_terminal({EVIDENCE_REFS_ARGUMENT: refs})
        assert verdict.accepted is False
        assert verdict.failure_category == DIRECT_EVIDENCE_REQUIRED
        assert verdict.tool_payload() == REJECTION_PAYLOAD

    def test_rejection_then_grounded_resubmission_completes(self):
        steps = [runbook(), cite_all, search("cfg-2041"), cite_eligible]
        execution, _ = run(steps)
        assert execution.status == "completed"
        assert [t.tool for t in execution.tool_trace] == [
            "retrieve_runbook",
            TERMINAL_TOOL,
            "search_logs",
            TERMINAL_TOOL,
        ]
        assert rejections(execution) == [REJECTION_PAYLOAD]
        assert execution.evidence_grounding["terminal_attempts"] == 2
        assert execution.evidence_grounding["rejected_terminal_attempts"] == 1
        assert evaluate(catalog()[SCENARIO_ID], execution).success is True


# --- fabricated, stale, foreign, and future references ------------------------


class TestReferenceProvenance:
    def test_fabricated_references_are_rejected(self):
        for fake in ("obs-000000000000000000000000", "obs-1", "1", "search_logs", ""):
            execution, _ = run([health(), search("audit"), terminal([fake])], max_turns=3)
            assert execution.status == "error"
            assert execution.error_category == DIRECT_EVIDENCE_REQUIRED
        # One fabricated reference poisons an otherwise valid list.
        execution, _ = run(
            [health(), search("audit"), lambda m: terminal([*seen_ids(m), "obs-deadbeef"])],
            max_turns=3,
        )
        assert execution.error_category == DIRECT_EVIDENCE_REQUIRED

    def test_stale_references_from_a_prior_execution_are_rejected(self):
        first, _ = run([health(), search("audit"), cite_all])
        assert first.status == "completed"
        stale = trace_ids(first)
        second, _ = run([health(), search("audit"), terminal(stale)], max_turns=3)
        assert trace_ids(second) != stale
        assert set(trace_ids(second)).isdisjoint(stale)
        assert second.status == "error"
        assert second.error_category == DIRECT_EVIDENCE_REQUIRED

    def test_cross_task_references_are_rejected(self):
        other, _ = run(
            [health("quokka-payments"), search("startup")],
            scenario_id="pod-failures-001",
            max_turns=2,
        )
        foreign = trace_ids(other)
        assert len(foreign) == 2
        execution, _ = run([health(), search("audit"), terminal(foreign)], max_turns=3)
        assert set(trace_ids(execution)).isdisjoint(foreign)
        assert execution.error_category == DIRECT_EVIDENCE_REQUIRED

    def test_cross_repetition_references_are_rejected(self):
        scenario = catalog()[SCENARIO_ID]
        instances = generate_task_instances([SCENARIO_ID], 1, 20260907)
        per_repetition: list[list[str]] = []
        for _repetition in range(2):
            execution = run_task(
                scenario,
                ScriptedClient([health(), search("audit"), cite_all]),
                SimulatedToolbox(scenario, clock=FakeClock()),
                V250,
                timeout_s=60.0,
                clock=FakeClock(),
                instance=instances[0],
            )
            assert execution.status == "completed"
            per_repetition.append(trace_ids(execution))
        assert set(per_repetition[0]).isdisjoint(per_repetition[1])
        replay = run_task(
            scenario,
            ScriptedClient([health(), search("audit"), terminal(per_repetition[0])]),
            SimulatedToolbox(scenario, clock=FakeClock()),
            V250,
            timeout_s=60.0,
            max_turns=3,
            clock=FakeClock(),
            instance=instances[0],
        )
        assert replay.error_category == DIRECT_EVIDENCE_REQUIRED

    def test_references_to_observations_created_after_the_attempt_are_rejected(self):
        future = observation_id("ctx", 1)
        steps = [health(), terminal([future]), search("audit"), terminal([future])]
        execution, _ = run(steps, evidence_context="ctx")
        tools = [t.tool for t in execution.tool_trace]
        assert tools == ["get_service_health", TERMINAL_TOOL, "search_logs", TERMINAL_TOOL]
        assert execution.tool_trace[1].result == REJECTION_PAYLOAD
        assert execution.tool_trace[2].result[OBSERVATION_ID_FIELD] == future
        assert execution.tool_trace[3].result["acknowledged"] is True
        assert execution.status == "completed"

    def test_terminal_results_never_become_observations(self):
        controller = EvidenceGroundingController(context="ctx")
        with pytest.raises(ValueError):
            controller.record(TERMINAL_TOOL, {"acknowledged": True})
        execution, _ = run([health(), terminal(omit_refs=True), cite_all])
        assert len(trace_ids(execution)) == 1
        assert execution.status == "completed"


# --- concurrency isolation ------------------------------------------------------


class _LeakingClient(ModelClient):
    """Cites only observation IDs that OTHER concurrent tasks produced."""

    name = "leaking"
    version = "0.0.0"

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.all_ids: set[str] = set()
        self.calls = 0
        self.foreign_citations = 0

    def stream_turn(self, messages, settings, *, deadline=None, clock=SYSTEM_CLOCK):
        with self.lock:
            self.calls += 1
        own = seen_ids(messages)
        with self.lock:
            self.all_ids.update(own)
            foreign = sorted(self.all_ids - set(own))
            if foreign and len(own) >= 2:
                self.foreign_citations += 1
        index = len(own)
        scenario_id = next(
            line.split(":", 1)[1].strip()
            for m in messages
            if m.role == "user"
            for line in m.content.splitlines()
            if line.startswith("scenario_id:")
        )
        scenario = catalog()[scenario_id]
        if index < 2:
            step = scenario.reference_tool_sequence[index]
        else:
            step = terminal(foreign, scenario=scenario)
        yield StreamEvent(kind="native_tool_call_delta")
        yield StreamEvent(
            kind="native_tool_call",
            tool_call=NativeToolCall(
                call_id=f"leak-{scenario_id}-{index}",
                name=step["tool"],
                arguments=dict(step["arguments"]),
            ),
        )


class _Gated(ModelClient):
    """Blocks each task's first turn on a barrier so `parties` tasks are
    genuinely in flight at once (the virtual clock would otherwise serialize)."""

    name = "gated"
    version = "0.0.0"

    def __init__(self, inner: ModelClient, parties: int) -> None:
        self.inner = inner
        self._barrier = threading.Barrier(parties, timeout=30)

    def stream_turn(self, messages, settings, *, deadline=None, clock=SYSTEM_CLOCK):
        if not any(m.role == "tool" for m in messages):
            self._barrier.wait()
        yield from self.inner.stream_turn(messages, settings, deadline=deadline, clock=clock)


def _pass(client: ModelClient, *, concurrency: int = 8, tasks: int = 16, gated: bool = False):
    instances = generate_task_instances(list(catalog()), tasks, 20260906)
    return _run_pass(
        instances=instances,
        scenarios_by_id=catalog(),
        client=_Gated(client, concurrency) if gated else client,
        profile=PROFILES["interactive"],
        concurrency=concurrency,
        settings=V250,
        timeout_ms=120_000.0,
        max_turns=DEFAULT_MAX_TURNS,
        clock=FakeClock(),
    )


class TestConcurrencyIsolation:
    def test_concurrent_tasks_never_share_ids_and_own_citations_pass(self):
        data = _pass(DeterministicMockClient("correct"), gated=True)
        assert data.achieved_max_concurrency == 8
        all_ids: list[str] = []
        for outcome in data.outcomes:
            assert outcome.execution.status == "completed", outcome.execution.error_category
            assert outcome.evaluation.success is True
            ids = trace_ids(outcome.execution)
            assert ids and len(set(ids)) == len(ids)
            all_ids.extend(ids)
        assert len(set(all_ids)) == len(all_ids)

    def test_cross_concurrency_leakage_is_rejected(self):
        client = _LeakingClient()
        data = _pass(client, gated=True)
        assert len(data.outcomes) == 16
        assert data.achieved_max_concurrency == 8
        # Foreign IDs were genuinely cited (not merely empty lists) and every
        # one of them was a real observation of some other concurrent task.
        assert client.foreign_citations > 0
        all_ids = {i for o in data.outcomes for i in trace_ids(o.execution)}
        assert client.all_ids == all_ids
        assert len(all_ids) == sum(len(trace_ids(o.execution)) for o in data.outcomes)
        for outcome in data.outcomes:
            execution = outcome.execution
            assert execution.status == "error"
            assert execution.error_category == DIRECT_EVIDENCE_REQUIRED
            assert all(r == REJECTION_PAYLOAD for r in rejections(execution))
            assert execution.evidence_grounding["accepted_evidence_refs"] is None
        # Full accounting survived: every turn and tool call is recorded.
        assert sum(len(o.execution.turns) for o in data.outcomes) == client.calls
        assert all(len(o.execution.tool_trace) == DEFAULT_MAX_TURNS for o in data.outcomes)


# --- structural eligibility -----------------------------------------------------


class TestStructuralEligibility:
    def test_zero_match_search_is_ineligible(self):
        toolbox = SimulatedToolbox(catalog()[SCENARIO_ID], clock=FakeClock())
        payload = toolbox.execute("search_logs", {"query": "unicorn sightings"}).payload
        assert payload["total_matches"] == 0
        assert classify_observation("search_logs", payload) == INELIGIBLE_ZERO_MATCH_SEARCH
        execution, _ = run([search("unicorn sightings"), cite_all], max_turns=3)
        assert execution.error_category == DIRECT_EVIDENCE_REQUIRED
        assert execution.evidence_grounding["eligible_observations"] == 0

    def test_tool_and_endpoint_failures_are_ineligible(self):
        toolbox = SimulatedToolbox(catalog()[SCENARIO_ID], clock=FakeClock())
        not_found = toolbox.execute("query_metrics", {"metric": "no_such_metric"}).payload
        assert not_found["found"] is False
        assert classify_observation("query_metrics", not_found) == INELIGIBLE_NOT_FOUND
        missing_runbook = toolbox.execute("retrieve_runbook", {"key": "nonexistent"}).payload
        assert classify_observation("retrieve_runbook", missing_runbook) == INELIGIBLE_GUIDANCE_ONLY
        execution, _ = run(
            [metric("no_such_metric"), runbook("nonexistent"), cite_all], max_turns=3
        )
        assert execution.error_category == DIRECT_EVIDENCE_REQUIRED

    def test_unknown_service_health_is_ineligible(self):
        toolbox = SimulatedToolbox(catalog()[SCENARIO_ID], clock=FakeClock())
        payload = toolbox.execute("get_service_health", {"service": "ghost-service"}).payload
        assert payload["services"]["ghost-service"] == {"status": "unknown-service"}
        assert classify_observation("get_service_health", payload) == INELIGIBLE_UNUSABLE_STATUS
        execution, _ = run([health("ghost-service"), cite_all], max_turns=3)
        assert execution.error_category == DIRECT_EVIDENCE_REQUIRED
        for unusable in ({"services": {"a": {"api": "unknown"}}}, {"services": {"a": {}}}):
            assert classify_observation("get_service_health", unusable) != ELIGIBLE

    @pytest.mark.parametrize(
        ("tool", "payload"),
        [
            ("search_logs", {"query": "x", "total_matches": "3", "lines": [{"message": "m"}]}),
            ("search_logs", {"query": "x", "total_matches": 3, "lines": "oops"}),
            ("search_logs", {"query": "x", "total_matches": 3, "lines": []}),
            ("search_logs", {"query": "x", "total_matches": 1, "lines": [{"host": "h"}]}),
            ("search_logs", {"query": "x", "total_matches": True, "lines": [{"message": "m"}]}),
            ("search_logs", []),
            ("search_logs", "not an object"),
            ("get_service_health", {"services": []}),
            ("get_service_health", {"services": {}}),
            ("get_service_health", {"services": {"svc": {"api": 7}}}),
            ("get_service_health", {}),
            ("query_metrics", {"found": True, "metric": "m", "points": []}),
            ("query_metrics", {"found": True, "metric": "m", "points": [{"value": "x"}]}),
            ("query_metrics", {"found": True, "metric": "", "points": [{"value": 1}]}),
            ("query_metrics", {"found": "yes", "metric": "m", "points": [{"value": 1}]}),
            ("query_metrics", {"metric": "m", "points": [{"value": 1}]}),
            ("no_such_tool", {"total_matches": 1, "lines": [{"message": "m"}]}),
        ],
    )
    def test_malformed_results_are_ineligible(self, tool, payload):
        assert classify_observation(tool, payload) != ELIGIBLE
        controller = EvidenceGroundingController(context="ctx")
        observation = controller.record(tool, payload)
        assert observation.eligible is False
        verdict = controller.validate_terminal(
            {EVIDENCE_REFS_ARGUMENT: [observation.observation_id]}
        )
        assert verdict.accepted is False

    def test_malformed_tool_result_in_the_loop_is_rejected(self):
        class MalformedToolbox(SimulatedToolbox):
            def _tool_search_logs(self, query, limit=None):
                return {"query": query, "total_matches": 2, "lines": "corrupted"}

        toolbox = MalformedToolbox(catalog()[SCENARIO_ID], clock=FakeClock())
        execution, _ = run([search("audit"), cite_all], toolbox=toolbox, max_turns=3)
        assert execution.error_category == DIRECT_EVIDENCE_REQUIRED
        assert execution.evidence_grounding == {
            "controller": CONTROLLER_EVIDENCE_GROUNDING_V1,
            "observations": 1,
            "eligible_observations": 0,
            "terminal_attempts": 2,
            "rejected_terminal_attempts": 2,
            "accepted_evidence_refs": None,
        }

    def test_runbook_only_recommendation_is_rejected(self):
        execution, _ = run([runbook(), cite_all], max_turns=3)
        assert execution.error_category == DIRECT_EVIDENCE_REQUIRED
        assert execution.tool_trace[0].result["found"] is True
        assert execution.evidence_grounding["eligible_observations"] == 0

    def test_change_feed_only_recommendation_is_rejected(self):
        execution, _ = run([changes(), cite_all], max_turns=3)
        assert execution.error_category == DIRECT_EVIDENCE_REQUIRED
        assert execution.tool_trace[0].result["changes"]
        assert execution.evidence_grounding["eligible_observations"] == 0

    def test_runbook_and_change_feed_together_are_still_insufficient(self):
        execution, _ = run([runbook(), changes(), cite_all], max_turns=3)
        assert execution.error_category == DIRECT_EVIDENCE_REQUIRED

    def test_eligible_shapes_are_exactly_the_direct_tools(self):
        toolbox = SimulatedToolbox(catalog()[SCENARIO_ID], clock=FakeClock())
        assert DIRECT_EVIDENCE_TOOLS == {"search_logs", "get_service_health", "query_metrics"}
        assert GUIDANCE_TOOLS == {"retrieve_runbook", "check_recent_changes"}
        assert DIRECT_EVIDENCE_TOOLS | GUIDANCE_TOOLS | {TERMINAL_TOOL} == set(TOOL_SPECS)
        for tool, arguments in (
            ("search_logs", {"query": "audit"}),
            ("get_service_health", {"service": SERVICE}),
            ("get_service_health", {}),
            ("query_metrics", {"metric": "latency_p99_ms"}),
        ):
            assert classify_observation(tool, toolbox.execute(tool, arguments).payload) == ELIGIBLE
        for tool, arguments in (
            ("retrieve_runbook", {"key": SERVICE}),
            ("check_recent_changes", {}),
        ):
            assert classify_observation(tool, toolbox.execute(tool, arguments).payload) == (
                INELIGIBLE_GUIDANCE_ONLY
            )

    def test_classifier_is_scenario_blind(self):
        # A log line that matches nothing in any scenario is still eligible:
        # the controller judges shape, never content.
        payload = {"query": "q", "total_matches": 1, "lines": [{"message": "zzz unrelated"}]}
        assert classify_observation("search_logs", payload) == ELIGIBLE
        assert (
            classify_observation("search_logs", {**payload, "lines": [{"message": ""}]}) == ELIGIBLE
        )


# --- no semantic judgment: controller passes, evaluator fails -------------------


class TestSemanticSeparation:
    def test_irrelevant_but_structurally_valid_evidence_passes_controller_not_evaluator(self):
        scenario = catalog()[SCENARIO_ID]
        steps = [health(UNRELATED_HEALTHY_SERVICE), runbook(), cite([0])]
        execution, _ = run(steps)
        assert execution.status == "completed"
        assert execution.error_category is None
        assert execution.evidence_grounding["accepted_evidence_refs"] == 1
        assert execution.tool_trace[0].result["services"] == {
            UNRELATED_HEALTHY_SERVICE: {"api": "healthy"}
        }
        evaluation = evaluate(scenario, execution)
        assert evaluation.evaluator_version == EVALUATOR_VERSION == "3.1.0"
        assert evaluation.success is False
        assert evaluation.score == 0.0
        gates = {gate.gate_id: gate.passed for gate in evaluation.gates}
        assert gates["task_completed"] is True
        assert gates["diagnosis"] is True
        assert gates["remediation"] is True
        assert gates["evidence:log-evidence-cfg-2041"] is False
        assert gates["evidence:change-correlation-cfg-2041"] is True

    def test_wrong_answer_with_valid_evidence_passes_controller_not_evaluator(self):
        scenario = catalog()[SCENARIO_ID]

        def wrong(messages):
            step = cite_eligible(messages)
            step["arguments"]["diagnosis_id"] = scenario.distractor_diagnoses[0]
            return step

        execution, _ = run([health(), search("audit"), runbook(), wrong])
        assert execution.status == "completed"
        evaluation = evaluate(scenario, execution)
        assert evaluation.success is False
        assert dict((g.gate_id, g.passed) for g in evaluation.gates)["diagnosis"] is False


# --- opaque, deterministic IDs ------------------------------------------------------


class TestOpaqueIds:
    def test_ids_are_deterministic_per_context_and_opaque(self):
        first = [observation_id("ctx-a", i) for i in range(5)]
        again = [observation_id("ctx-a", i) for i in range(5)]
        other = [observation_id("ctx-b", i) for i in range(5)]
        assert first == again
        assert len(set(first)) == 5
        assert set(first).isdisjoint(other)
        for value in first:
            assert value.startswith("obs-")
            assert len(value) == len("obs-") + 24
            assert all(ch in "0123456789abcdef" for ch in value[4:])
        # No tool name, context, or sequence number is embedded in the text:
        # the same sequence under another context yields unrelated digests.
        for index, value in enumerate(first):
            for tool in TOOL_SPECS:
                assert tool not in value
            assert "ctx-a" not in value
            assert value[4:] != other[index][4:]

    def test_controller_ids_follow_the_same_derivation(self):
        one = EvidenceGroundingController(context="ctx")
        two = EvidenceGroundingController(context="ctx")
        payload = {"total_matches": 1, "lines": [{"message": "m"}]}
        assert one.record("search_logs", payload).observation_id == observation_id("ctx", 0)
        assert two.record(
            "get_service_health", {"services": {"a": {"b": "ok"}}}
        ).observation_id == (observation_id("ctx", 0))
        assert one.record("search_logs", payload).observation_id == observation_id("ctx", 1)

    def test_run_task_defaults_to_a_fresh_private_context(self):
        a, _ = run([health(), search("audit")], max_turns=2)
        b, _ = run([health(), search("audit")], max_turns=2)
        assert trace_ids(a) != trace_ids(b)
        c, _ = run([health(), search("audit")], evidence_context="fixed", max_turns=2)
        d, _ = run([health(), search("audit")], evidence_context="fixed", max_turns=2)
        assert (
            trace_ids(c) == trace_ids(d) == [observation_id("fixed", 0), observation_id("fixed", 1)]
        )

    @pytest.mark.parametrize("bad", ["", None, 3])
    def test_contexts_and_sequences_are_validated(self, bad):
        with pytest.raises(ValueError):
            observation_id(bad, 0)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            EvidenceGroundingController(context=bad)  # type: ignore[arg-type]
        for sequence in (-1, True, "0"):
            with pytest.raises(ValueError):
                observation_id("ctx", sequence)  # type: ignore[arg-type]

    def test_identifier_renaming_invariance(self):
        """Verdicts depend on which observation is cited, never on ID text."""
        results = [
            ("search_logs", {"total_matches": 1, "lines": [{"message": "m"}]}),
            ("retrieve_runbook", {"found": True, "runbook": {}}),
            ("search_logs", {"total_matches": 0, "lines": []}),
            ("get_service_health", {"services": {"s": {"api": "ok"}}}),
        ]
        citations = [[0], [1], [2], [3], [0, 3], [0, 1], [], [0, 2]]
        verdicts: list[list[bool]] = []
        for context in ("alpha", "beta", "gamma"):
            controller = EvidenceGroundingController(context=context)
            ids = [controller.record(tool, payload).observation_id for tool, payload in results]
            verdicts.append(
                [
                    controller.validate_terminal(
                        {EVIDENCE_REFS_ARGUMENT: [ids[i] for i in c]}
                    ).accepted
                    for c in citations
                ]
            )
        assert verdicts[0] == verdicts[1] == verdicts[2]
        assert verdicts[0] == [True, False, False, True, True, False, False, False]
        # Renaming an ID (case, prefix, suffix, whitespace) breaks resolution.
        controller = EvidenceGroundingController(context="delta")
        valid = controller.record(*results[0]).observation_id
        for renamed in (valid.upper(), valid[4:], "OBS-" + valid[4:], valid + "0", f" {valid}"):
            assert renamed != valid
            assert (
                controller.validate_terminal({EVIDENCE_REFS_ARGUMENT: [renamed]}).accepted is False
            )
        assert controller.validate_terminal({EVIDENCE_REFS_ARGUMENT: [valid]}).accepted is True


# --- leakage, persistence, and turn budget ---------------------------------------


class TestLeakageAndPersistence:
    def test_model_visible_text_for_250_adds_nothing(self):
        """2.5.0 adds NO prose: the model sees the 2.4.1 prompt and the 2.4.0
        tool descriptions byte-for-byte. The only new model-visible element
        is the evidence_refs entry in the terminal JSON argument schema."""
        scenario = next(iter(catalog().values()))
        assert system_prompt(scenario, "2.5.0") == system_prompt(scenario, "2.4.1")
        assert tool_descriptions("2.5.0") == tool_descriptions("2.4.1") == TOOL_DESCRIPTIONS_V240
        new = {d["function"]["name"]: d for d in openai_tool_definitions("2.5.0")}
        old = {d["function"]["name"]: d for d in openai_tool_definitions("2.4.1")}
        for name in new:
            assert new[name]["function"]["description"] == old[name]["function"]["description"]
        # The schema-level addition carries no scenario or evaluator token.
        schema_blob = json.dumps(new[TERMINAL_TOOL]["function"]["parameters"], sort_keys=True)
        folded = schema_blob.casefold()
        leaked = sorted(t for t in _catalog_answer_tokens() if t.casefold() in folded)
        assert leaked == [], leaked
        for forbidden in ("predicate", "total_matches", "accepted_", "healthy", "degraded"):
            assert forbidden not in folded

    def test_rejection_reveals_nothing(self):
        controller = EvidenceGroundingController(context="ctx")
        ok = controller.record("search_logs", {"total_matches": 1, "lines": [{"message": "m"}]})
        bad = controller.record("search_logs", {"total_matches": 0, "lines": []})
        for refs in ([bad.observation_id], ["obs-fake"], [ok.observation_id, "obs-fake"], []):
            verdict = controller.validate_terminal({EVIDENCE_REFS_ARGUMENT: refs})
            assert verdict.tool_payload() == REJECTION_PAYLOAD
        blob = json.dumps(controller.summary())
        assert ok.observation_id not in blob and bad.observation_id not in blob
        assert "ctx" not in blob

    def test_no_raw_prompt_completion_or_reasoning_is_persisted(self):
        from dataclasses import dataclass

        execution, client = run([health(), search("audit"), runbook(), cite_all])
        system_text = client.seen[0][0][0].content
        assert system_text == SYSTEM_PROMPT_V241

        @dataclass(frozen=True)
        class _Outcome:
            task_index: int
            instance: object
            execution: TaskExecution
            evaluation: object
            submitted_offset_ms: float

        observation = _observation(
            _Outcome(0, None, execution, evaluate(catalog()[SCENARIO_ID], execution), 0.0)
        )
        blob = json.dumps(observation)
        assert "You are a Cloud Operations Agent" not in blob
        assert "observation_id that is valid only within this task" not in blob
        assert "reasoning" not in blob
        assert "evidence_grounding" in observation
        assert set(observation["evidence_grounding"]) == {
            "controller",
            "observations",
            "eligible_observations",
            "terminal_attempts",
            "rejected_terminal_attempts",
            "accepted_evidence_refs",
        }
        assert not hasattr(execution, "messages")
        from blackwell_lab.schemas import validate_task_observations

        validate_task_observations(
            {
                "schema_version": "1.1.0",
                "run_id": "run-00000001",
                "repetition_index": 1,
                "phase": "measured",
                "observations": [
                    {
                        **observation,
                        "template_id": SCENARIO_ID,
                        "instance_id": "x#1",
                        "instance_seed": 1,
                        "submitted_at_utc": "2026-10-02T00:00:00+00:00",
                    }
                ],
            }
        )

    def test_frozen_turn_budget_is_enforced_without_hidden_calls(self):
        assert DEFAULT_MAX_TURNS == 12
        execution, client = run([health(), terminal(omit_refs=True)])
        assert execution.status == "error"
        assert execution.error_category == DIRECT_EVIDENCE_REQUIRED
        assert client.calls == 12
        assert len(execution.turns) == 12
        assert len(execution.tool_trace) == 12
        assert execution.evidence_grounding["terminal_attempts"] == 11
        assert execution.evidence_grounding["rejected_terminal_attempts"] == 11
        # Every tool call, including rejected attempts, consumed its latency.
        assert execution.tool_latencies_ms == [5.0] + [5.0] * 11
        # Grounded on the final turn still completes inside the budget.
        steps = [health(), *([terminal(omit_refs=True)] * 10), cite([0])]
        final, client = run(steps)
        assert final.status == "completed"
        assert client.calls == 12
        # A task that never attempts the terminal tool keeps the old category.
        never, _ = run([health()])
        assert never.error_category == "no_terminal_recommendation"
        assert never.evidence_grounding["terminal_attempts"] == 0

    def test_timeout_still_wins_over_acceptance(self):
        scenario = catalog()[SCENARIO_ID]
        clock = FakeClock()
        execution = run_task(
            scenario,
            ScriptedClient([health(), search("audit"), cite_all]),
            SimulatedToolbox(scenario, clock=clock),
            V250,
            timeout_s=0.040,  # health 5 ms + search 30 ms + terminal 5 ms reaches the deadline
            clock=clock,
        )
        assert execution.status == "timeout"
        assert execution.error_category == "task_timeout"


# --- contract binding: tools, assembler, versions ---------------------------------


class TestContractBinding:
    def test_workload_controller_table_is_closed(self):
        assert WORKLOAD_CONTROLLERS == {
            "2.3.0": None,
            "2.4.0": None,
            "2.4.1": None,
            "2.5.0": CONTROLLER_EVIDENCE_GROUNDING_V1,
        }
        assert controller_for_workload("2.5.0") == "evidence-grounding-v1"
        assert controller_for_workload("2.4.1") is None
        assert controller_for_workload(None) is None
        with pytest.raises(ConfigError, match="unknown workload version"):
            controller_for_workload("9.9.9")
        assert build_controller("2.4.1", "ctx") is None
        assert isinstance(build_controller("2.5.0", "ctx"), EvidenceGroundingController)

    @pytest.mark.parametrize(
        ("version", "controller"),
        [
            ("2.5.0", None),
            ("2.4.1", "evidence-grounding-v1"),
            ("2.4.0", "evidence-grounding-v1"),
            ("2.3.0", "evidence-grounding-v1"),
            ("2.5.0", "evidence-grounding-v2"),
            ("9.9.9", "evidence-grounding-v1"),
            ("9.9.9", None),
        ],
    )
    def test_binding_mismatches_fail_closed(self, version, controller):
        with pytest.raises(ConfigError):
            require_controller_binding(version, controller)

    def test_binding_matches_resolve(self):
        assert require_controller_binding("2.5.0", "evidence-grounding-v1") == (
            "evidence-grounding-v1"
        )
        for version in ("2.3.0", "2.4.0", "2.4.1", None):
            assert require_controller_binding(version, None) is None

    def test_2_5_0_terminal_schema_adds_evidence_refs_only(self):
        assert tool_specs("2.5.0") is TOOL_SPECS_V250
        for version in ("2.3.0", "2.4.0", "2.4.1"):
            assert tool_specs(version) is TOOL_SPECS
        for name in TOOL_SPECS:
            if name != TERMINAL_TOOL:
                assert TOOL_SPECS_V250[name] is TOOL_SPECS[name]
        assert TOOL_SPECS_V250[TERMINAL_TOOL]["required"] == TOOL_SPECS[TERMINAL_TOOL]["required"]
        assert TOOL_SPECS_V250[TERMINAL_TOOL]["controller"] == {EVIDENCE_REFS_ARGUMENT: list}
        assert "controller" not in TOOL_SPECS[TERMINAL_TOOL]
        old = {d["function"]["name"]: d for d in openai_tool_definitions("2.4.1")}
        new = {d["function"]["name"]: d for d in openai_tool_definitions("2.5.0")}
        for name in old:
            if name != TERMINAL_TOOL:
                assert old[name] == new[name]
        parameters = new[TERMINAL_TOOL]["function"]["parameters"]
        assert parameters["properties"][EVIDENCE_REFS_ARGUMENT] == {
            "type": "array",
            "items": {"type": "string"},
        }
        assert parameters["required"] == [
            "diagnosis_id",
            "evidence_refs",
            "rationale",
            "remediation_id",
        ]
        assert (
            EVIDENCE_REFS_ARGUMENT not in old[TERMINAL_TOOL]["function"]["parameters"]["properties"]
        )
        # Description prose is identical; the ONLY difference in the whole
        # native-tool definition set is the evidence_refs schema entry.
        assert (
            new[TERMINAL_TOOL]["function"]["description"]
            == (old[TERMINAL_TOOL]["function"]["description"])
        )
        assert (
            new[TERMINAL_TOOL]["function"]["description"] == (TOOL_DESCRIPTIONS_V240[TERMINAL_TOOL])
        )
        old_parameters = old[TERMINAL_TOOL]["function"]["parameters"]
        stripped = {
            **parameters,
            "properties": {
                k: v for k, v in parameters["properties"].items() if k != EVIDENCE_REFS_ARGUMENT
            },
            "required": [r for r in parameters["required"] if r != EVIDENCE_REFS_ARGUMENT],
        }
        assert stripped == old_parameters
        assert tool_descriptions("2.5.0") is TOOL_DESCRIPTIONS_V240
        assert tool_descriptions("2.4.1") is TOOL_DESCRIPTIONS_V240

    def test_validation_is_version_bound(self):
        arguments = {
            "diagnosis_id": "d",
            "rationale": "r",
            "remediation_id": "m",
            EVIDENCE_REFS_ARGUMENT: ["obs-x"],
        }
        validate_tool_call(TERMINAL_TOOL, arguments, "2.5.0")
        with pytest.raises(InvalidToolArgumentsError, match="unexpected argument"):
            validate_tool_call(TERMINAL_TOOL, arguments, "2.4.1")
        with pytest.raises(InvalidToolArgumentsError, match="unexpected argument"):
            validate_tool_call(TERMINAL_TOOL, arguments)
        # Absent refs are a schema-valid call (the controller adjudicates).
        validate_tool_call(
            TERMINAL_TOOL, {k: v for k, v in arguments.items() if k != "evidence_refs"}, "2.5.0"
        )
        with pytest.raises(InvalidToolArgumentsError, match="invalid type"):
            validate_tool_call(
                TERMINAL_TOOL, {**arguments, EVIDENCE_REFS_ARGUMENT: "obs-x"}, "2.5.0"
            )
        with pytest.raises(ConfigError, match="unknown workload version"):
            validate_tool_call(TERMINAL_TOOL, arguments, "9.9.9")

    def test_assembler_and_wire_body_follow_the_version(self):
        fragments = [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-1",
                        "function": {
                            "name": TERMINAL_TOOL,
                            "arguments": json.dumps(
                                {
                                    "diagnosis_id": "d",
                                    "rationale": "r",
                                    "remediation_id": "m",
                                    "evidence_refs": ["obs-x"],
                                }
                            ),
                        },
                    }
                ]
            }
        ]
        assembler = ToolCallAssembler(workload_version="2.5.0")
        for fragment in fragments:
            assembler.consume_delta(fragment)
        assembled = assembler.finalize()
        assert isinstance(assembled, NativeToolCall)
        assert assembled.arguments["evidence_refs"] == ["obs-x"]
        legacy = ToolCallAssembler(workload_version="2.4.1")
        for fragment in fragments:
            legacy.consume_delta(fragment)
        error = legacy.finalize()
        assert not isinstance(error, NativeToolCall)
        assert error.category == "invalid_arguments"
        default = ToolCallAssembler()
        for fragment in fragments:
            default.consume_delta(fragment)
        assert default.finalize().category == "invalid_arguments"
        client = OpenAICompatibleClient("http://127.0.0.1:8000/v1", "nemotron")
        body = client._request_body(
            [Message("system", "s")], GenerationSettings(workload_version="2.5.0")
        )
        assert body["tools"] == openai_tool_definitions("2.5.0")
        assert body["tool_choice"] == "auto"
        assert body["parallel_tool_calls"] is False

    def test_workload_250_executes_the_241_prompt_byte_for_byte(self):
        for scenario in catalog().values():
            executed = system_prompt(scenario, "2.5.0")
            assert executed == SYSTEM_PROMPT_V241 == FROZEN_SYSTEM_PROMPT_V241
            assert executed is SYSTEM_PROMPT_V241
            assert hashlib.sha256(executed.encode()).hexdigest() == (
                FROZEN_SYSTEM_PROMPT_V241_SHA256
            )
        assert agent_module.SYSTEM_PROMPTS_BY_VERSION["2.5.0"] is SYSTEM_PROMPT_V241
        assert not hasattr(agent_module, "SYSTEM_PROMPT_V250")
        assert not hasattr(agent_module, "_V250_GROUNDING_INSTRUCTION")
        for token in ("evidence_refs", "observation_id", "direct_evidence_required"):
            assert token not in SYSTEM_PROMPT_V241
        with pytest.raises(ConfigError, match="unknown workload version"):
            system_prompt(next(iter(catalog().values())), "2.5.1")

    def test_real_p2_request_sends_system_prompt_v241(self):
        """The exact wire body of a workload-2.5.0 request: the system message
        is SYSTEM_PROMPT_V241 and the tools differ from 2.4.1 only by the
        evidence_refs schema entry."""
        scenario = catalog()[SCENARIO_ID]
        client = OpenAICompatibleClient("http://127.0.0.1:8000/v1", "nemotron")
        messages = [
            Message("system", system_prompt(scenario, "2.5.0")),
            Message("user", "incident"),
        ]
        body = client._request_body(messages, GenerationSettings(workload_version="2.5.0"))
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][0]["content"] == SYSTEM_PROMPT_V241
        assert hashlib.sha256(body["messages"][0]["content"].encode()).hexdigest() == (
            FROZEN_SYSTEM_PROMPT_V241_SHA256
        )
        legacy = client._request_body(
            [Message("system", system_prompt(scenario, "2.4.1")), Message("user", "incident")],
            GenerationSettings(workload_version="2.4.1"),
        )
        assert legacy["messages"] == body["messages"]
        assert legacy["tools"] != body["tools"]
        by_name = {d["function"]["name"]: d for d in body["tools"]}
        legacy_by_name = {d["function"]["name"]: d for d in legacy["tools"]}
        for name in by_name:
            assert (
                by_name[name]["function"]["description"]
                == (legacy_by_name[name]["function"]["description"])
            )
            if name != TERMINAL_TOOL:
                assert by_name[name] == legacy_by_name[name]
        assert (
            EVIDENCE_REFS_ARGUMENT
            in (by_name[TERMINAL_TOOL]["function"]["parameters"]["properties"])
        )
        assert (
            EVIDENCE_REFS_ARGUMENT
            not in (legacy_by_name[TERMINAL_TOOL]["function"]["parameters"]["properties"])
        )
        # A real P2 run builds its system message through run_task.
        recording = ScriptedClient([health(), search("audit"), cite_eligible])
        execution = run_task(
            scenario,
            recording,
            SimulatedToolbox(scenario, clock=FakeClock()),
            V250,
            timeout_s=30.0,
            clock=FakeClock(),
        )
        assert execution.status == "completed"
        for messages_seen, _settings in recording.seen:
            assert messages_seen[0].role == "system"
            assert messages_seen[0].content == SYSTEM_PROMPT_V241


# --- backward compatibility: C1, C2, P1, prompts, evaluator ------------------------


class TestBackwardCompatibility:
    def test_system_prompt_v241_is_byte_identical(self):
        assert SYSTEM_PROMPT_V241 == FROZEN_SYSTEM_PROMPT_V241
        assert hashlib.sha256(SYSTEM_PROMPT_V241.encode()).hexdigest() == (
            FROZEN_SYSTEM_PROMPT_V241_SHA256
        )
        assert hashlib.sha256(SYSTEM_PROMPT_V240.encode()).hexdigest() == (
            FROZEN_SYSTEM_PROMPT_V240_SHA256
        )
        for scenario in catalog().values():
            assert system_prompt(scenario, "2.4.1") == FROZEN_SYSTEM_PROMPT_V241
            assert system_prompt(scenario, "2.4.0") == SYSTEM_PROMPT_V240
            assert system_prompt(scenario, "2.3.0") == SYSTEM_PROMPT_V230

    def test_c1_c2_p1_identity_digests_match_the_region_lock(self):
        assert candidate_identity_digest("C1") == FROZEN_C1_IDENTITY_SHA256
        assert candidate_identity_digest("C2") == FROZEN_C2_IDENTITY_SHA256
        assert candidate_identity_digest("P1") == FROZEN_P1_IDENTITY_SHA256
        for candidate in ("C1", "C2", "P1"):
            fields = json.loads(serialize_candidate(candidate))
            assert "controller" not in fields
            assert candidate_controller(candidate) is None
            assert CANDIDATE_CONTROLLERS[candidate] is None
        assert candidate_workload_version("C1") == candidate_workload_version("C2") == "2.4.0"
        assert candidate_workload_version("P1") == "2.4.1"
        assert candidate_temperature("C1") == 1.0
        assert candidate_temperature("C2") == candidate_temperature("P1") == 0.2
        assert (
            sanitized_receipt(
                run_label="qual-a-c1-development",
                candidate_id="C1",
                stage="development",
                config_sha256="abc",
                identity_digest="def",
                gates={"continue": True, "stopped": False},
                files=(),
                stopped=False,
            )["controller"]
            is None
        )

    def test_legacy_workloads_run_without_the_controller(self):
        for version in ("2.3.0", "2.4.0", "2.4.1"):
            scenario = catalog()[SCENARIO_ID]
            client = ScriptedClient(
                [health(), search("audit"), runbook(), terminal(omit_refs=True)]
            )
            execution = run_task(
                scenario,
                client,
                SimulatedToolbox(scenario, clock=FakeClock()),
                GenerationSettings(workload_version=version),
                timeout_s=60.0,
                clock=FakeClock(),
            )
            assert execution.status == "completed"
            assert execution.evidence_grounding is None
            assert trace_ids(execution) == []
            assert all(OBSERVATION_ID_FIELD not in t.result for t in execution.tool_trace)
            assert EVIDENCE_REFS_ARGUMENT not in execution.tool_trace[-1].arguments
            assert evaluate(scenario, execution).success is True
            # The mock client never adds evidence_refs on legacy contracts.
            mock = DeterministicMockClient()
            mock_execution = run_task(
                scenario,
                mock,
                SimulatedToolbox(scenario, clock=FakeClock()),
                GenerationSettings(workload_version=version),
                timeout_s=60.0,
                clock=FakeClock(),
            )
            assert mock_execution.status == "completed"
            assert EVIDENCE_REFS_ARGUMENT not in mock_execution.tool_trace[-1].arguments
            # A legacy contract still refuses the 2.5.0-only argument.
            strict = run_task(
                scenario,
                ScriptedClient([terminal(["obs-x"])]),
                SimulatedToolbox(scenario, clock=FakeClock()),
                GenerationSettings(workload_version=version),
                timeout_s=60.0,
                clock=FakeClock(),
            )
            assert strict.error_category == "invalid_tool_arguments"

    def test_model_engine_evaluator_thresholds_and_scenarios_are_unchanged(self):
        from blackwell_lab.cloud.mvl import (
            FROZEN_ENGINE,
            FROZEN_ENGINE_VERSION,
            FROZEN_MODEL_ARTIFACT,
            FROZEN_PRECISION,
        )

        assert EVALUATOR_VERSION == "3.1.0"
        assert QUALITY_THRESHOLD == 1.0
        assert WORKLOAD_VERSION == "2.3.0"
        assert FROZEN_ENGINE == "vllm"
        assert FROZEN_ENGINE_VERSION == "0.27.1"
        assert FROZEN_PRECISION == "bf16"
        assert "Nemotron" in FROZEN_MODEL_ARTIFACT and "BF16" in FROZEN_MODEL_ARTIFACT
        assert hashlib.sha256(_accepted_answers_payload()).hexdigest() == (
            FROZEN_ACCEPTED_ANSWERS_SHA256
        )
        assert catalog_digest().startswith("sha256:")
        assert DEVELOPMENT_TASKS == HOLDOUT_TASKS == 20 and FREEZE_TASKS == 200
        assert DEVELOPMENT_QUALITY_FLOOR == 0.40 and HOLDOUT_QUALITY_FLOOR == 0.50
        assert STUDY_ENTRY_MIN_AGGREGATE == 0.70 and STUDY_ENTRY_MIN_SCENARIO == 0.40
        assert PRODUCTION_LIKE_MIN_AGGREGATE == 0.90 and PRODUCTION_LIKE_MIN_SCENARIO == 0.80
        p2 = frozen_candidate_fields("P2")
        c2 = frozen_candidate_fields("C2")
        assert p2["model"] == c2["model"]
        assert p2["serving"] == c2["serving"]
        assert p2["generation"] == c2["generation"]
        assert p2["generation"]["temperature"] == 0.2
        assert p2["generation"]["top_p"] == 0.95
        assert p2["generation"]["seed"] == 20260906
        assert p2["generation"]["max_tokens"] == 1024
        assert p2["serving"]["tool_call_transport"] == "openai-native-tools"

    def test_error_taxonomy_documents_the_new_category_only(self):
        assert "direct_evidence_required" in ERROR_TAXONOMY
        assert (
            ERROR_TAXONOMY.index("direct_evidence_required")
            == ERROR_TAXONOMY.index("no_terminal_recommendation") + 1
        )
        assert "direct_evidence_required" in (agent_module.__doc__ or "")


# --- P2 candidate identity, configuration, CLI, manifest ---------------------------


def qualification_config_dict(candidate_id="P2", stage="development", sealed_set=None):
    from sealed_fixtures import placeholder_binding

    from blackwell_lab.cloud.mvl import (
        FROZEN_MODEL_ARTIFACT,
        FROZEN_MODEL_ARTIFACT_HASH,
        FROZEN_MODEL_REVISION,
    )
    from blackwell_lab.cloud.sealed_binding import requires_sealed_set

    spec = stage_spec(stage)
    # P2 development/holdout must bind a sealed custody stage (D-0024). A
    # shape-valid artificial binding satisfies config validation; CLI tests
    # that execute pass a binding derived from a synthetic custody fixture.
    sealed_section = (
        {"sealed_set": sealed_set or placeholder_binding(stage)}
        if requires_sealed_set(candidate_id, stage)
        else {}
    )
    config = {
        **sealed_section,
        "workflow": "qualify-agent",
        "candidate_id": candidate_id,
        "workload_version": candidate_workload_version(candidate_id),
        "controller": candidate_controller(candidate_id),
        "endpoint": {"base_url": "http://127.0.0.1:8000/v1", "model": "m"},
        "cloud": {
            "instance_type": "g3-gpu-rtxpro6000-blackwell-1",
            "region": "ca-central",
            "list_price_usd_per_hour": 3.0,
            "price_source_date": "2026-09-18",
        },
        "model": {
            "artifact": FROZEN_MODEL_ARTIFACT,
            "revision": FROZEN_MODEL_REVISION,
            "artifact_hash": FROZEN_MODEL_ARTIFACT_HASH,
            "precision": "bf16",
        },
        "serving": {
            "engine": "vllm",
            "engine_version": "0.27.1",
            "container_digest": (
                "docker.io/vllm/vllm-openai:v0.27.1@"
                "sha256:c2f3b1b964e47809b722b5e75b61b1e7b39a50f70388cf2bf2418f16a9f31da2"
            ),
        },
        "host": {"storage_description": "plan NVMe", "network_description": "plan default"},
        "expected_gpu_model": "RTX PRO 6000 Blackwell",
        "comparison_mode": "provider-native",
        "model_verification": {
            "artifact_dir": "/opt/models/nemotron",
            "digest_manifest": "/opt/models/nemotron.sha256",
        },
        "canonical_commit": COMMIT,
        "cells": [{"profile": "interactive", "concurrency": 1}],
        "warmup_passes": spec["warmup_passes"],
        "repetitions": 1,
        "tasks_per_repetition": spec["tasks"],
        "generation": {
            "temperature": candidate_temperature(candidate_id),
            "top_p": 0.95,
            "max_tokens": 1024,
            "seed": 20260906,
            "reasoning_mode": True,
        },
    }
    if candidate_id == "P2C" and stage == "development":
        # Shape-only binding. CLI tests that execute replace it with a
        # control record authenticated against the session ledger.
        config["development_control"] = {
            "schema_version": "1.0.0",
            "run_tag": RUN_TAG,
            "p1_run_label": "qual-p1",
            "canonical_commit": COMMIT,
            "region": "ca-central",
            "config_sha256": "0" * 64,
            "result_sha256": "0" * 64,
            "control_record_sha256": "0" * 64,
            "resource_identity_sha256": "0" * 64,
            "ledger_sha256": "0" * 64,
        }
    return config


def _config_sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _qual_argv(config_path, *, candidate="P2", stage="development", approve=None, custody_dir=None):
    phrase = approve or approval_phrase(RUN_TAG, "qual-a", candidate, _config_sha256(config_path))
    argv = [
        "qualify-agent",
        "--run-tag",
        RUN_TAG,
        "--run-label",
        "qual-a",
        "--candidate",
        candidate,
        "--stage",
        stage,
        "--config",
        str(config_path),
        "--approve",
        phrase,
    ]
    if custody_dir is not None:
        argv += ["--custody-dir", str(custody_dir)]
    return argv


def _ready_ledger():
    return {
        "run_tag": RUN_TAG,
        "reconciled": True,
        "reconciliation": {"provider_checked": True},
        "resources": [
            {
                "address": "linode_instance.gpu_baseline",
                "type": "linode_instance",
                "provider_id": "42",
                "region": "ca-central",
                "label": f"bwlab-{RUN_TAG}",
                "tags": ["blackwell-lab", f"run:{RUN_TAG}", "ttl-hours:6", "phase:3"],
            },
            {
                "address": "linode_firewall.gpu_baseline",
                "type": "linode_firewall",
                "provider_id": "555",
                "region": "",
                "label": f"bwlab-fw-{RUN_TAG}",
                "tags": ["blackwell-lab", f"run:{RUN_TAG}", "ttl-hours:6", "phase:3"],
            },
        ],
    }


def _observed(*, full_host: bool = False):
    from blackwell_lab.cloud.mvl import FROZEN_MODEL_ARTIFACT_HASH
    from blackwell_lab.cloud.provenance import ObservedProvenance

    if full_host:
        # A complete gpu-mode host block so the real run_real_cell can
        # assemble a schema-valid manifest offline.
        return ObservedProvenance(
            container_digest=(
                "docker.io/vllm/vllm-openai:v0.27.1@"
                "sha256:c2f3b1b964e47809b722b5e75b61b1e7b39a50f70388cf2bf2418f16a9f31da2"
            ),
            model_artifact_hash=FROZEN_MODEL_ARTIFACT_HASH,
            engine_version="0.27.1",
            instance={
                "provider_id": "42",
                "instance_type": "g3-gpu-rtxpro6000-blackwell-1",
                "region": "ca-central",
                "tags": ["blackwell-lab", f"run:{RUN_TAG}"],
            },
            host_facts=dict(_HOST),
            gpu_facts={},
            container_cuda_runtime_version="13.0",
        )
    return ObservedProvenance(
        container_digest=(
            "docker.io/vllm/vllm-openai:v0.27.1@"
            "sha256:c2f3b1b964e47809b722b5e75b61b1e7b39a50f70388cf2bf2418f16a9f31da2"
        ),
        model_artifact_hash=FROZEN_MODEL_ARTIFACT_HASH,
        engine_version="0.27.1",
        instance={
            "provider_id": "42",
            "instance_type": "g3-gpu-rtxpro6000-blackwell-1",
            "region": "ca-central",
            "tags": ["blackwell-lab", f"run:{RUN_TAG}"],
        },
        host_facts={
            "storage_description": "plan NVMe",
            "network_description": "plan default networking",
        },
        gpu_facts={"gpu_model": "RTX PRO 6000 Blackwell", "driver_version": "580"},
        container_cuda_runtime_version="13.0",
    )


def _install_offline_endpoint(monkeypatch):
    """Swap the production client and GPU sampler for offline stand-ins.

    The CLI still constructs ``OpenAICompatibleClient`` (a subclass), but
    every turn is answered by the deterministic mock with usage counts, and
    telemetry comes from the fake sampler, so the real ``run_real_cell``
    executes end to end with no network.
    """
    from blackwell_lab.cloud import telemetry
    from blackwell_lab.workload import openai_client

    class OfflineClient(OpenAICompatibleClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._mock = _UsageMockClient()

        def stream_turn(self, messages, settings, *, deadline=None, clock=SYSTEM_CLOCK):
            yield from self._mock.stream_turn(messages, settings, deadline=deadline, clock=clock)

    monkeypatch.setattr(openai_client, "OpenAICompatibleClient", OfflineClient)
    monkeypatch.setattr(telemetry, "GpuSamplerThread", _FakeSampler)
    return OfflineClient


class _FakeRecord:
    def __init__(self, outcomes):
        self.run_id = "qual"
        self.outcomes = outcomes
        self.written_files = ("qual.result.json",)
        self.result = None
        self.manifest = None
        self.measured_observations = None
        self.warmup_observations = None


def _stage_outcomes(stage: str) -> list[dict]:
    spec = stage_spec(stage)
    templates = spec["template_ids"]
    return [
        {
            "template_id": templates[index % len(templates)],
            "success": True,
            "evaluation": {"success": True, "evaluator_version": EVALUATOR_VERSION},
            "error_category": None,
            "e2e_ms": 1_000.0,
            "ttft_ms": 200.0,
            "turns": [{"ttft_ms": 200.0}],
            "tool_trace": [{"tool": TERMINAL_TOOL}],
        }
        for index in range(spec["tasks"])
    ]


class TestCandidateP2:
    def test_p2_identity_binds_workload_250_and_the_controller(self):
        assert CANDIDATE_P2 == "P2" and CANDIDATE_P2 in AUTHORIZED_CANDIDATES
        assert AUTHORIZED_CANDIDATES == ("C1", "C2", "P1", "P2", "P2C")
        assert CANDIDATE_WORKLOAD_VERSIONS["P2"] == P2_WORKLOAD_VERSION == "2.5.0"
        assert P2_CONTROLLER == CANDIDATE_CONTROLLERS["P2"] == "evidence-grounding-v1"
        assert candidate_controller("P2") == "evidence-grounding-v1"
        assert P2_TEMPERATURE == C2_TEMPERATURE == candidate_temperature("P2") == 0.2
        fields = json.loads(serialize_candidate("P2"))
        assert fields["candidate_id"] == "P2"
        assert fields["workload_version"] == "2.5.0"
        assert fields["controller"] == "evidence-grounding-v1"
        p2 = experimental_behavior_fields("P2")
        assert p2["system_prompt"] == SYSTEM_PROMPT_V241
        assert p2["system_prompt"] == experimental_behavior_fields("P1")["system_prompt"]
        digests = {candidate_identity_digest(c) for c in AUTHORIZED_CANDIDATES}
        assert len(digests) == 5
        assert candidate_identity_digest("P2") != experimental_configuration_digest("P2")
        assert experimental_configuration_digest("P2") != experimental_configuration_digest("P1")

    def test_p2_differs_from_p1_only_in_identity_version_and_controller(self):
        """evidence-grounding-v1 is the single treatment: the system prompt,
        tool text, generation, model, and serving fields are byte-identical."""
        from test_qualification import _differing_paths

        p1 = experimental_behavior_fields("P1")
        p2 = experimental_behavior_fields("P2")
        assert _differing_paths(p1, p2) == ["candidate_id", "controller", "workload_version"]
        assert p1["system_prompt"] == p2["system_prompt"] == SYSTEM_PROMPT_V241
        assert "controller" not in p1
        assert p2["controller"] == "evidence-grounding-v1"

    def test_p2_validates_for_every_stage_and_rejects_mismatches(self):
        for stage in ("development", "holdout", "freeze"):
            config = qualification_config_dict("P2", stage)
            validate_authorized_qualification_config(config, candidate_id="P2", stage=stage)
            assert config["tasks_per_repetition"] == stage_spec(stage)["tasks"]
            without_controller_key = {k: v for k, v in config.items() if k != "controller"}
            validate_authorized_qualification_config(
                without_controller_key, candidate_id="P2", stage=stage
            )
        wrong_version = qualification_config_dict("P2")
        wrong_version["workload_version"] = "2.4.1"
        with pytest.raises(ConfigError, match=r"2\.5\.0"):
            validate_authorized_qualification_config(
                wrong_version, candidate_id="P2", stage="development"
            )
        wrong_controller = qualification_config_dict("P2")
        wrong_controller["controller"] = None
        with pytest.raises(ConfigError, match="controller"):
            validate_authorized_qualification_config(
                wrong_controller, candidate_id="P2", stage="development"
            )
        wrong_controller["controller"] = "evidence-grounding-v2"
        with pytest.raises(ConfigError, match="controller"):
            validate_authorized_qualification_config(
                wrong_controller, candidate_id="P2", stage="development"
            )
        p1_with_controller = qualification_config_dict("P1")
        p1_with_controller["controller"] = "evidence-grounding-v1"
        with pytest.raises(ConfigError, match="controller"):
            validate_authorized_qualification_config(
                p1_with_controller, candidate_id="P1", stage="development"
            )
        c2_on_250 = qualification_config_dict("C2")
        c2_on_250["workload_version"] = "2.5.0"
        with pytest.raises(ConfigError, match=r"2\.4\.0"):
            validate_authorized_qualification_config(
                c2_on_250, candidate_id="C2", stage="development"
            )
        unknown = qualification_config_dict("P2")
        unknown["workload_version"] = "9.9.9"
        with pytest.raises(ConfigError, match=r"2\.5\.0"):
            validate_authorized_qualification_config(
                unknown, candidate_id="P2", stage="development"
            )
        mismatch = qualification_config_dict("P2")
        with pytest.raises(ConfigError, match="candidate_id must match"):
            validate_authorized_qualification_config(
                mismatch, candidate_id="P1", stage="development"
            )

    def test_candidate_table_drift_fails_closed(self, monkeypatch):
        from blackwell_lab.cloud import qualification

        monkeypatch.setitem(qualification.CANDIDATE_CONTROLLERS, "P2", None)
        with pytest.raises(ConfigError, match="binding mismatch"):
            candidate_controller("P2")
        with pytest.raises(ConfigError, match="binding mismatch"):
            frozen_candidate_fields("P2")
        monkeypatch.setitem(qualification.CANDIDATE_CONTROLLERS, "P1", "evidence-grounding-v1")
        with pytest.raises(ConfigError, match="binding mismatch"):
            candidate_controller("P1")

    def test_qualify_agent_binds_p2_before_inference(self, tmp_path, monkeypatch, capsys):
        from sealed_fixtures import binding_for, write_synthetic_custody

        from blackwell_lab.cloud import provenance, realbench
        from blackwell_lab.cloud.sealed_binding import SealedSetBinding, SealedStageTasks

        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        monkeypatch.setattr(cli, "_git_head", lambda: COMMIT)
        monkeypatch.setattr(cli, "_tree_clean", lambda: True)
        external = tmp_path / "external"
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        paths = lifecycle.lifecycle_paths(external, RUN_TAG)
        lifecycle.write_private_json(paths.ledger_path, _ready_ledger())
        custody_root = tmp_path / "custody"
        manifest = write_synthetic_custody(custody_root, commit=COMMIT)
        binding = binding_for(custody_root, manifest, "development")
        calls: list[object] = []
        clients: list[object] = []
        sealed_seen: list[object] = []
        real_run = realbench.run_real_cell

        def recording_run(spec, client, **kwargs):
            calls.append(spec)
            clients.append(client)
            sealed_seen.append(kwargs.get("sealed_tasks"))
            return real_run(spec, client, **kwargs)

        monkeypatch.setattr(
            provenance, "verify_live_provenance", lambda **kwargs: _observed(full_host=True)
        )
        monkeypatch.setattr(realbench, "run_real_cell", recording_run)
        _install_offline_endpoint(monkeypatch)
        config = qualification_config_dict("P2", "development", sealed_set=binding)
        path = tmp_path / "p2.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        phrase = approval_phrase(RUN_TAG, "qual-a", "P2", _config_sha256(path))
        assert "candidate P2" in phrase
        assert main(_qual_argv(path, approve=phrase, custody_dir=custody_root)) == 0
        assert len(calls) == 1
        spec = calls[0]
        assert isinstance(spec, RealRunSpec)
        assert spec.controller == "evidence-grounding-v1"
        assert spec.workload_version == "2.5.0"
        assert spec.generation.workload_version == "2.5.0"
        assert spec.generation.temperature == 0.2
        assert spec.generation.top_p == 0.95
        assert spec.generation.seed == 20260906
        assert spec.generation.reasoning_mode is True
        assert spec.run_label == "qual-a-p2-development"
        assert spec.tasks_per_repetition == 20
        assert spec.warmup_passes == 0
        assert spec.template_ids is None
        assert spec.sealed_set == SealedSetBinding.from_config(binding, stage="development")
        assert isinstance(sealed_seen[0], SealedStageTasks)
        assert len(sealed_seen[0].tasks) == 20
        assert isinstance(clients[0], OpenAICompatibleClient)
        report = json.loads(capsys.readouterr().out)
        assert report["candidate_id"] == "P2"
        assert report["workload_version"] == "2.5.0"
        assert report["controller"] == "evidence-grounding-v1"
        assert report["candidate_identity_sha256"] == candidate_identity_digest("P2")
        assert report["sealed_set"] == binding
        on_disk = json.loads(
            (external / "qualification-runs" / "qual-a-p2-development-receipt.json").read_text(
                encoding="utf-8"
            )
        )
        assert on_disk["controller"] == "evidence-grounding-v1"
        assert on_disk["sealed_set"] == binding
        blob = json.dumps(on_disk)
        for forbidden in ("127.0.0.1", "/opt/models/", "reasoning", "obs-", str(custody_root)):
            assert forbidden not in blob
        assert "syn-dev-task" not in blob
        # Running the bound settings reproduces the 2.5.0 contract end to end.
        scenario = catalog()[SCENARIO_ID]
        recording = ScriptedClient([health(), search("audit"), cite_all])
        execution = run_task(
            scenario,
            recording,
            SimulatedToolbox(scenario, clock=FakeClock()),
            spec.generation,
            timeout_s=30.0,
            clock=FakeClock(),
        )
        assert recording.seen[0][0][0].content == SYSTEM_PROMPT_V241
        assert execution.status == "completed"
        assert execution.evidence_grounding["controller"] == "evidence-grounding-v1"

    def test_qualify_agent_refuses_p2_mismatches_with_zero_client_calls(
        self, tmp_path, monkeypatch, capsys
    ):
        from blackwell_lab.cloud import provenance, realbench
        from blackwell_lab.workload import openai_client

        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        monkeypatch.setattr(cli, "_git_head", lambda: COMMIT)
        monkeypatch.setattr(cli, "_tree_clean", lambda: True)
        external = tmp_path / "external"
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        paths = lifecycle.lifecycle_paths(external, RUN_TAG)
        lifecycle.write_private_json(paths.ledger_path, _ready_ledger())
        constructed: list[object] = []
        runs: list[object] = []

        class SpyClient(OpenAICompatibleClient):
            def __init__(self, *args, **kwargs):
                constructed.append(self)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(openai_client, "OpenAICompatibleClient", SpyClient)
        monkeypatch.setattr(provenance, "verify_live_provenance", lambda **kwargs: _observed())
        monkeypatch.setattr(realbench, "run_real_cell", lambda *a, **k: runs.append(1))
        for mutate in (
            lambda c: c.__setitem__("workload_version", "2.4.1"),
            lambda c: c.__setitem__("controller", None),
            lambda c: c.__setitem__("controller", "evidence-grounding-v2"),
            lambda c: c.__setitem__("workload_version", "9.9.9"),
            lambda c: c.__setitem__("candidate_id", "P1"),
        ):
            config = qualification_config_dict("P2", "development")
            mutate(config)
            path = tmp_path / "bad.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            assert main(_qual_argv(path)) == 1
            assert "BLOCKED" in capsys.readouterr().err
        assert constructed == []
        assert runs == []
        # The mirror image: P1 may not carry the controller.
        config = qualification_config_dict("P1", "development")
        config["controller"] = "evidence-grounding-v1"
        path = tmp_path / "p1-bad.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        assert main(_qual_argv(path, candidate="P1")) == 1
        assert constructed == [] and runs == []

    def test_parser_accepts_p2(self, capsys):
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "qualify-agent",
                "--run-tag",
                RUN_TAG,
                "--run-label",
                "qual-a",
                "--candidate",
                "P2",
                "--stage",
                "freeze",
                "--config",
                "/absolute/outside/qualify.json",
            ]
        )
        assert args.candidate == "P2" and args.stage == "freeze"
        with pytest.raises(SystemExit) as caught:
            parser.parse_args(["qualify-agent", "--help"])
        assert caught.value.code == 0
        help_text = " ".join(capsys.readouterr().out.split())
        assert "P2 (temperature 0.2, workload 2.5.0, evidence-grounding-v1 controller)" in help_text
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "qualify-agent",
                    "--run-tag",
                    RUN_TAG,
                    "--run-label",
                    "qual-a",
                    "--candidate",
                    "P3",
                    "--stage",
                    "development",
                    "--config",
                    "/absolute/outside/qualify.json",
                ]
            )


# --- real-run manifest and provenance binding -----------------------------------------


class _UsageMockClient(DeterministicMockClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0
        self.seen: list[tuple] = []

    def stream_turn(self, messages, settings, *, deadline=None, clock=SYSTEM_CLOCK):
        self.calls += 1
        self.seen.append((list(messages), settings))
        yield from super().stream_turn(messages, settings, deadline=deadline, clock=clock)
        yield StreamEvent(kind="usage", output_tokens=23)


class _FakeSampler:
    def start(self):
        pass

    def stop(self):
        pass

    def summary(self, *, successful_tasks: int) -> dict:
        from blackwell_lab.cloud.telemetry import GpuSample, summarize_gpu_samples

        return summarize_gpu_samples(
            [GpuSample(100.0, 80.0, 70.0, 400.0, 65.0), GpuSample(101.0, 90.0, 75.0, 420.0, 66.0)],
            window_started_monotonic_s=100.0,
            window_ended_monotonic_s=101.1,
            successful_tasks=successful_tasks,
        )


_HOST = {
    "operating_system": "Ubuntu 24.04 LTS",
    "cpu_model": "Synthetic Test CPU",
    "vcpu_count": 16,
    "system_memory_gib": 176.0,
    "storage_description": "plan NVMe (not measured)",
    "network_description": "plan default networking",
    "gpu_model": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
    "gpu_count": 1,
    "gpu_memory_gb": 96.0,
    "driver_version": "580.65.06",
    "driver_max_cuda_version": "13.0",
}


def _real_spec(**overrides) -> RealRunSpec:
    defaults = dict(
        profile_name="interactive",
        concurrency=1,
        comparison_mode="provider-native",
        instance_type="g9-fake-gpu-plan",
        region="us-fake-1",
        list_price_usd_per_hour=2.5,
        price_source_date="2026-09-06",
        model={
            "artifact": "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16",
            "revision": "0123456789abcdef0123456789abcdef01234567",
            "artifact_hash": "sha256:" + "ab" * 32,
            "precision": "bf16",
        },
        engine="vllm",
        engine_version="0.28.0",
        container_digest="docker.io/vllm/vllm-openai@sha256:" + "cd" * 32,
        repetitions=1,
        warmup_passes=0,
        tasks_per_repetition=10,
        run_label="p2-manifest",
        workload_version="2.5.0",
        controller="evidence-grounding-v1",
        generation=GenerationSettings(
            temperature=0.2,
            top_p=0.95,
            reasoning_mode=True,
            seed=20260906,
            workload_version="2.5.0",
        ),
    )
    defaults.update(overrides)
    return RealRunSpec(**defaults)


@pytest.fixture
def real_results_dir(tmp_path, monkeypatch):
    external = tmp_path / "external-results"
    external.mkdir()
    monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
    return external


class TestRealRunBinding:
    def test_manifest_records_controller_and_observations_validate(self, real_results_dir):
        from blackwell_lab.schemas import validate_run_manifest, validate_task_observations

        client = _UsageMockClient()
        records = run_real_cell(
            _real_spec(), client, host=_HOST, sampler_factory=_FakeSampler, clock=FakeClock()
        )
        manifest = records[0].manifest
        validate_run_manifest(manifest)
        assert manifest["workload"]["version"] == "2.5.0"
        assert manifest["workload"]["controller"] == "evidence-grounding-v1"
        assert manifest["workload"]["evaluator_version"] == "3.1.0"
        assert manifest["generation"]["temperature"] == 0.2
        assert client.seen[0][0][0].content == SYSTEM_PROMPT_V241
        measured = records[0].measured_observations
        validate_task_observations(measured)
        for observation in measured["observations"]:
            assert observation["evidence_grounding"]["controller"] == "evidence-grounding-v1"
            assert observation["status"] == "completed"
            assert observation["evaluation"]["success"] is True
        assert records[0].result["tasks"]["succeeded"] == 10
        blob = json.dumps(manifest)
        assert "You are a Cloud Operations Agent" not in blob

    def test_legacy_manifests_omit_the_controller(self, real_results_dir):
        records = run_real_cell(
            _real_spec(
                workload_version="2.4.1",
                controller=None,
                generation=GenerationSettings(
                    temperature=0.2, top_p=0.95, reasoning_mode=True, workload_version="2.4.1"
                ),
                run_label="p1-manifest",
            ),
            _UsageMockClient(),
            host=_HOST,
            sampler_factory=_FakeSampler,
            clock=FakeClock(),
        )
        assert "controller" not in records[0].manifest["workload"]
        for observation in records[0].measured_observations["observations"]:
            assert "evidence_grounding" not in observation

    @pytest.mark.parametrize(
        "overrides",
        [
            {"controller": None},
            {"controller": "evidence-grounding-v2"},
            {
                "workload_version": "2.4.1",
                "generation": GenerationSettings(
                    temperature=0.2, top_p=0.95, reasoning_mode=True, workload_version="2.4.1"
                ),
            },
        ],
    )
    def test_binding_mismatch_makes_zero_client_calls(self, real_results_dir, overrides):
        client = _UsageMockClient()
        with pytest.raises(ConfigError):
            run_real_cell(
                _real_spec(**overrides),
                client,
                host=_HOST,
                sampler_factory=_FakeSampler,
                clock=FakeClock(),
            )
        assert client.calls == 0
        assert not list(real_results_dir.rglob("*.json"))

    def test_rejected_to_exhaustion_tasks_are_accounted_in_the_taxonomy(self):
        """A mock agent that never gathers eligible evidence exhausts its budget
        and is recorded under the new category with full turn accounting."""
        data = _pass(DeterministicMockClient("irrelevant_queries"), concurrency=1, tasks=4)
        assert len(data.outcomes) == 4
        for outcome in data.outcomes:
            execution = outcome.execution
            assert execution.status == "error"
            assert execution.error_category == DIRECT_EVIDENCE_REQUIRED
            assert len(execution.turns) == len(execution.tool_trace) == DEFAULT_MAX_TURNS
            assert execution.evidence_grounding["rejected_terminal_attempts"] >= 1
            assert execution.evidence_grounding["accepted_evidence_refs"] is None
            assert outcome.evaluation.success is False
            observation = _observation(outcome)
            assert observation["error_category"] == DIRECT_EVIDENCE_REQUIRED
            assert observation["evidence_grounding"] == execution.evidence_grounding
