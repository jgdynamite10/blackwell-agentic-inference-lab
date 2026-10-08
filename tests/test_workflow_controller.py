"""workflow-controller-v1 (workloads 2.6.0 / 2.6.1; decision D-0031).

Covers every state transition, zero-match correction, missing direct
evidence, missing or incorrect runbook, remediation outside the retrieved
runbook, premature terminal attempts, correction after rejection, budget
exhaustion with no accepted terminal, answer-key isolation, one native tool
call per turn, the unchanged retry policy, and the explicit W1/W2 treatment
difference. Everything runs offline through the real agent loop.
"""

from __future__ import annotations

import inspect
import json

import pytest
from test_evidence_grounding import (
    ScriptedClient,
    changes,
    health,
    metric,
    rejections,
    run,
    runbook,
    search,
    seen_ids,
    terminal,
)

from blackwell_lab.workload import workflow as workflow_module
from blackwell_lab.workload.agent import (
    DEFAULT_MAX_TURNS,
    ERROR_TAXONOMY,
    RETRIES,
    SYSTEM_PROMPT_V241,
    system_prompt,
)
from blackwell_lab.workload.evidence import (
    CONTROLLER_WORKFLOW_V1,
    WORKFLOW_WORKLOAD_CONTROLLERS,
    WORKFLOW_WORKLOAD_TREATMENTS,
    all_workload_controllers,
    build_controller,
    controller_for_workload,
    workload_treatments,
)
from blackwell_lab.workload.model_client import GenerationSettings
from blackwell_lab.workload.native_tools import TOOL_DESCRIPTIONS_BY_VERSION
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.tools import TERMINAL_TOOL, TOOL_SPECS, TOOL_SPECS_V250, tool_specs
from blackwell_lab.workload.workflow import (
    GUIDANCE,
    REJECT_DIAGNOSIS_NOT_PUBLISHED,
    REJECT_EVIDENCE_REFS_REQUIRED,
    REJECT_INVESTIGATION_REQUIRED,
    REJECT_LOG_EVIDENCE_REQUIRED,
    REJECT_MALFORMED_TERMINAL,
    REJECT_REMEDIATION_NOT_IN_RUNBOOK,
    REJECT_RUNBOOK_REQUIRED,
    REJECTION_CATEGORIES,
    REMAINING_TURN_WARNING_AT,
    RUNBOOK_NOT_FOUND_GUIDANCE,
    STATE_BUDGET_EXHAUSTED,
    STATE_EVIDENCE_COLLECTED,
    STATE_INVESTIGATING,
    STATE_READY_FOR_TERMINAL,
    STATE_RUNBOOK_RETRIEVED,
    STATE_TERMINAL_ACCEPTED,
    STATES,
    UNUSABLE_GUIDANCE,
    WORKFLOW_FIELD,
    WORKFLOW_REQUIREMENTS_UNMET,
    WorkflowController,
)

V260 = GenerationSettings(workload_version="2.6.0")
V261 = GenerationSettings(workload_version="2.6.1")
SCENARIO = catalog()["elevated-latency-001"]
USABLE_QUERY = "audit-log"
ZERO_MATCH_QUERY = "this-token-matches-nothing"


def _state_path(execution) -> list[str]:
    return [t["to"] for t in execution.workflow_control["transitions"]]


def _cite_eligible(messages):
    ids = []
    for message in messages:
        if message.role != "tool":
            continue
        payload = json.loads(message.content)
        if isinstance(payload, dict) and payload.get(WORKFLOW_FIELD, {}).get("usable_evidence"):
            if "observation_id" in payload and payload.get("tool", "") != "retrieve_runbook":
                ids.append(payload["observation_id"])
    return terminal(ids)


# --- bindings ----------------------------------------------------------------------


class TestBindings:
    def test_workloads_260_and_261_bind_the_workflow_controller(self):
        assert WORKFLOW_WORKLOAD_CONTROLLERS == {
            "2.6.0": CONTROLLER_WORKFLOW_V1,
            "2.6.1": CONTROLLER_WORKFLOW_V1,
        }
        assert WORKFLOW_WORKLOAD_TREATMENTS == {"2.6.0": (), "2.6.1": ("evidence-refs",)}
        assert controller_for_workload("2.6.0") == CONTROLLER_WORKFLOW_V1
        assert controller_for_workload("2.6.1") == CONTROLLER_WORKFLOW_V1
        assert workload_treatments("2.6.0") == ()
        assert workload_treatments("2.6.1") == ("evidence-refs",)
        assert all_workload_controllers()["2.5.0"] == "evidence-grounding-v1"
        # Historical bindings are untouched.
        for version in ("2.3.0", "2.4.0", "2.4.1"):
            assert controller_for_workload(version) is None

    def test_pair_shares_prompt_and_tool_text_and_differs_only_in_evidence_refs(self):
        assert system_prompt(SCENARIO, "2.6.0") == system_prompt(SCENARIO, "2.6.1")
        assert system_prompt(SCENARIO, "2.6.0") == SYSTEM_PROMPT_V241
        assert TOOL_DESCRIPTIONS_BY_VERSION["2.6.0"] is TOOL_DESCRIPTIONS_BY_VERSION["2.6.1"]
        assert tool_specs("2.6.0") is TOOL_SPECS
        assert tool_specs("2.6.1") is TOOL_SPECS_V250
        w1 = build_controller("2.6.0", context="ctx")
        w2 = build_controller("2.6.1", context="ctx")
        assert isinstance(w1, WorkflowController) and isinstance(w2, WorkflowController)
        assert w1.treatments == () and w2.treatments == ("evidence-refs",)
        assert not w1.requires_evidence_refs and w2.requires_evidence_refs

    def test_controller_constructor_rejects_unknown_treatments_and_bad_budgets(self):
        with pytest.raises(ValueError):
            WorkflowController(context="ctx", treatments=("mystery",))
        with pytest.raises(ValueError):
            WorkflowController(context="")
        with pytest.raises(ValueError):
            WorkflowController(context="ctx", warning_at=0)

    def test_error_taxonomy_documents_the_new_category(self):
        assert WORKFLOW_REQUIREMENTS_UNMET in ERROR_TAXONOMY
        assert RETRIES == 0 and DEFAULT_MAX_TURNS == 12


# --- state machine -----------------------------------------------------------------


class TestStateMachine:
    def test_states_and_rejection_categories_are_closed_sets(self):
        assert STATES == (
            STATE_INVESTIGATING,
            STATE_RUNBOOK_RETRIEVED,
            STATE_EVIDENCE_COLLECTED,
            STATE_READY_FOR_TERMINAL,
            STATE_TERMINAL_ACCEPTED,
            STATE_BUDGET_EXHAUSTED,
        )
        assert set(GUIDANCE) == set(REJECTION_CATEGORIES)

    def test_runbook_first_path_walks_every_forward_state(self):
        execution, _ = run(
            [runbook(), search(USABLE_QUERY), terminal(omit_refs=True)], settings=V260
        )
        assert execution.status == "completed"
        assert _state_path(execution) == [
            STATE_RUNBOOK_RETRIEVED,
            STATE_READY_FOR_TERMINAL,
            STATE_TERMINAL_ACCEPTED,
        ]

    def test_evidence_first_path_walks_every_forward_state(self):
        execution, _ = run(
            [search(USABLE_QUERY), runbook(), terminal(omit_refs=True)], settings=V260
        )
        assert _state_path(execution) == [
            STATE_EVIDENCE_COLLECTED,
            STATE_READY_FOR_TERMINAL,
            STATE_TERMINAL_ACCEPTED,
        ]
        assert execution.workflow_control["final_state"] == STATE_TERMINAL_ACCEPTED

    def test_investigating_to_budget_exhausted_without_any_terminal(self):
        execution, _ = run([health()] * 12, settings=V260)
        assert execution.status == "error"
        assert execution.error_category == "no_terminal_recommendation"
        assert _state_path(execution) == [STATE_BUDGET_EXHAUSTED]
        assert execution.workflow_control["terminal_attempts"] == 0

    def test_ready_for_terminal_to_budget_exhausted_after_rejections(self):
        steps = [runbook(), search(USABLE_QUERY)] + [
            {
                "tool": TERMINAL_TOOL,
                "arguments": {
                    "diagnosis_id": SCENARIO.accepted_diagnoses[0],
                    "rationale": "r",
                    "remediation_id": "not-a-published-remediation",
                },
            }
        ] * 10
        execution, _ = run(steps, settings=V260)
        assert execution.status == "error"
        assert execution.error_category == WORKFLOW_REQUIREMENTS_UNMET
        assert _state_path(execution) == [
            STATE_RUNBOOK_RETRIEVED,
            STATE_READY_FOR_TERMINAL,
            STATE_BUDGET_EXHAUSTED,
        ]
        assert execution.workflow_control["rejected_terminal_attempts"] == 10
        assert execution.workflow_control["rejection_categories"] == {
            REJECT_REMEDIATION_NOT_IN_RUNBOOK: 10
        }

    def test_unit_level_transitions_are_recorded_with_causes(self):
        controller = WorkflowController(context="ctx")
        assert controller.state == STATE_INVESTIGATING
        controller.record("get_service_health", {"services": {"a": {"status": "degraded"}}})
        assert controller.state == STATE_INVESTIGATING
        controller.record(
            "search_logs", {"total_matches": 1, "lines": [{"message": "one matching line"}]}
        )
        assert controller.state == STATE_EVIDENCE_COLLECTED
        controller.record(
            "retrieve_runbook",
            {
                "found": True,
                "runbook": {"remediation_ids": ["fix-it"]},
                "diagnosis_candidates": ["cause-a"],
            },
        )
        assert controller.state == STATE_READY_FOR_TERMINAL
        verdict = controller.validate_terminal(
            {"diagnosis_id": "cause-a", "rationale": "r", "remediation_id": "fix-it"}
        )
        assert verdict.accepted
        assert controller.state == STATE_TERMINAL_ACCEPTED
        controller.mark_exhausted()  # no effect after acceptance
        assert controller.state == STATE_TERMINAL_ACCEPTED
        assert [t[2] for t in controller.transitions] == [
            "observed search_logs",
            "observed retrieve_runbook",
            "terminal accepted",
        ]


# --- rejection categories and correction -------------------------------------------


class TestRejectionsAndCorrection:
    def test_terminal_before_any_investigation_is_rejected_generically(self):
        execution, _ = run(
            [terminal(omit_refs=True), runbook(), search(USABLE_QUERY)], settings=V260
        )
        first = execution.tool_trace[0].result
        assert first["accepted"] is False
        assert REJECT_INVESTIGATION_REQUIRED in first["failure_categories"]
        assert REJECT_LOG_EVIDENCE_REQUIRED in first["failure_categories"]
        assert REJECT_RUNBOOK_REQUIRED in first["failure_categories"]
        assert first["workflow"]["turns_remaining"] == 11
        for secret in (
            SCENARIO.accepted_diagnoses[0],
            SCENARIO.accepted_remediations[0],
            USABLE_QUERY,
            SCENARIO.affected_service,
        ):
            assert secret not in json.dumps(first)

    def test_zero_match_search_is_unusable_and_the_corrected_search_is_accepted(self):
        execution, _ = run(
            [
                runbook(),
                search(ZERO_MATCH_QUERY),
                terminal(omit_refs=True),
                search(USABLE_QUERY),
                terminal(omit_refs=True),
            ],
            settings=V260,
        )
        zero = execution.tool_trace[1].result
        assert zero["total_matches"] == 0
        assert zero[WORKFLOW_FIELD]["usable_evidence"] is False
        assert zero[WORKFLOW_FIELD]["guidance"] == UNUSABLE_GUIDANCE["zero_match_search"]
        rejected = execution.tool_trace[2].result
        assert rejected["failure_categories"] == [REJECT_LOG_EVIDENCE_REQUIRED]
        usable = execution.tool_trace[3].result
        assert usable[WORKFLOW_FIELD]["usable_evidence"] is True
        assert execution.status == "completed"
        summary = execution.workflow_control
        assert summary["zero_match_searches"] == 1
        assert summary["usable_log_observations"] == 1
        assert summary["terminal_attempts"] == 2
        assert summary["rejected_terminal_attempts"] == 1

    def test_metrics_and_health_alone_do_not_satisfy_the_log_evidence_requirement(self):
        execution, _ = run(
            [health(), metric("latency_p99_ms"), changes(), runbook(), terminal(omit_refs=True)],
            settings=V260,
            max_turns=5,
        )
        assert execution.status == "error"
        assert execution.error_category == WORKFLOW_REQUIREMENTS_UNMET
        rejected = rejections(execution)
        assert len(rejected) == 1
        assert rejected[0]["failure_categories"] == [REJECT_LOG_EVIDENCE_REQUIRED]
        assert execution.workflow_control["eligible_observations"] == 2

    def test_missing_runbook_is_rejected_and_not_found_key_gets_generic_guidance(self):
        execution, _ = run(
            [
                search(USABLE_QUERY),
                runbook(SCENARIO.accepted_diagnoses[0]),  # a diagnosis ID is not a key
                terminal(omit_refs=True),
                runbook(),
                terminal(omit_refs=True),
            ],
            settings=V260,
        )
        not_found = execution.tool_trace[1].result
        assert not_found["found"] is False
        assert not_found[WORKFLOW_FIELD]["usable_evidence"] is False
        assert not_found[WORKFLOW_FIELD]["guidance"] == RUNBOOK_NOT_FOUND_GUIDANCE
        assert execution.tool_trace[2].result["failure_categories"] == [REJECT_RUNBOOK_REQUIRED]
        assert execution.status == "completed"
        assert execution.workflow_control["not_found_lookups"] == 1
        assert execution.workflow_control["runbooks_found"] == 1

    def test_remediation_not_returned_by_a_retrieved_runbook_is_rejected(self):
        bad = {
            "tool": TERMINAL_TOOL,
            "arguments": {
                "diagnosis_id": SCENARIO.accepted_diagnoses[0],
                "rationale": "r",
                "remediation_id": "invented-remediation",
            },
        }
        execution, _ = run(
            [runbook(), search(USABLE_QUERY), bad, terminal(omit_refs=True)], settings=V260
        )
        assert execution.tool_trace[2].result["failure_categories"] == [
            REJECT_REMEDIATION_NOT_IN_RUNBOOK
        ]
        assert execution.status == "completed"

    def test_distractor_remediation_from_the_runbook_is_structurally_accepted(self):
        """The controller enforces workflow, never correctness: a published
        distractor passes the controller and fails the evaluator."""
        from blackwell_lab.workload.evaluator import evaluate

        distractor = {
            "tool": TERMINAL_TOOL,
            "arguments": {
                "diagnosis_id": SCENARIO.accepted_diagnoses[0],
                "rationale": "r",
                "remediation_id": SCENARIO.distractor_remediations[0],
            },
        }
        execution, _ = run([runbook(), search(USABLE_QUERY), distractor], settings=V260)
        assert execution.status == "completed"
        assert execution.remediation_id == SCENARIO.distractor_remediations[0]
        assert evaluate(SCENARIO, execution).success is False

    def test_unpublished_diagnosis_is_rejected_when_candidates_were_returned(self):
        wrong = {
            "tool": TERMINAL_TOOL,
            "arguments": {
                "diagnosis_id": "invented-diagnosis",
                "rationale": "r",
                "remediation_id": SCENARIO.accepted_remediations[0],
            },
        }
        execution, _ = run(
            [runbook(), search(USABLE_QUERY), wrong, terminal(omit_refs=True)], settings=V260
        )
        assert execution.tool_trace[2].result["failure_categories"] == [
            REJECT_DIAGNOSIS_NOT_PUBLISHED
        ]
        assert execution.status == "completed"

    def test_malformed_terminal_is_rejected_at_unit_level_and_the_loop_retry_policy_is_unchanged(
        self,
    ):
        controller = WorkflowController(context="ctx")
        verdict = controller.validate_terminal(
            {"diagnosis_id": "", "rationale": "", "remediation_id": ""}
        )
        assert not verdict.accepted
        assert REJECT_MALFORMED_TERMINAL in verdict.failure_categories
        assert "guidance" in verdict.tool_payload()
        # Through the agent loop, schema-invalid arguments still hit the
        # existing RETRIES=0 taxonomy before the controller: the policy is
        # preserved, not relaxed.
        malformed = {
            "tool": TERMINAL_TOOL,
            "arguments": {"diagnosis_id": "", "rationale": "", "remediation_id": ""},
        }
        execution, _ = run(
            [runbook(), search(USABLE_QUERY), malformed, terminal(omit_refs=True)], settings=V260
        )
        assert execution.status == "error"
        assert execution.error_category == "invalid_tool_arguments"
        assert len(execution.tool_trace) == 2

    def test_rejected_attempts_consume_turns_and_the_budget_is_unchanged(self):
        execution, client = run(
            [terminal(omit_refs=True)] * 12, settings=V260, max_turns=DEFAULT_MAX_TURNS
        )
        assert client.calls == DEFAULT_MAX_TURNS
        assert len(execution.tool_trace) == DEFAULT_MAX_TURNS
        assert execution.status == "error"
        assert execution.error_category == WORKFLOW_REQUIREMENTS_UNMET
        assert execution.workflow_control["rejected_terminal_attempts"] == DEFAULT_MAX_TURNS

    def test_remaining_turn_warnings_arrive_early_enough_to_submit(self):
        execution, _ = run([health()] * 12, settings=V260)
        warned = [
            t.result[WORKFLOW_FIELD]
            for t in execution.tool_trace
            if "warning" in t.result.get(WORKFLOW_FIELD, {})
        ]
        remaining = [block["turns_remaining"] for block in warned]
        assert remaining == list(range(REMAINING_TURN_WARNING_AT, -1, -1))
        assert "recommend_remediation" in warned[0]["warning"]
        assert execution.workflow_control["remaining_turn_warnings"] == len(warned)
        # The warning arrives with turn 9's result: three turns remain.
        assert execution.tool_trace[8].result[WORKFLOW_FIELD]["turns_remaining"] == 3


# --- the W2 treatment ------------------------------------------------------------


class TestEvidenceRefsTreatment:
    def test_w1_ignores_evidence_refs_and_w2_requires_them(self):
        w1, _ = run([runbook(), search(USABLE_QUERY), terminal(omit_refs=True)], settings=V260)
        assert w1.status == "completed"
        assert w1.workflow_control["accepted_evidence_refs"] is None
        w2_missing, _ = run(
            [runbook(), search(USABLE_QUERY), terminal(omit_refs=True)], settings=V261, max_turns=3
        )
        assert w2_missing.status == "error"
        assert w2_missing.error_category == WORKFLOW_REQUIREMENTS_UNMET
        assert rejections(w2_missing)[0]["failure_categories"] == [REJECT_EVIDENCE_REFS_REQUIRED]

    def test_w2_accepts_a_citation_of_eligible_observations_only(self):
        def cite_second(messages):
            return terminal([seen_ids(messages)[1]])

        execution, _ = run([runbook(), search(USABLE_QUERY), cite_second], settings=V261)
        assert execution.status == "completed"
        assert execution.workflow_control["accepted_evidence_refs"] == 1
        assert execution.workflow_control["treatments"] == ["evidence-refs"]

    def test_w2_rejects_citations_of_zero_match_results_then_accepts_a_correction(self):
        def cite_zero_match(messages):
            return terminal([seen_ids(messages)[1]])

        def cite_usable(messages):
            return terminal([seen_ids(messages)[2]])

        execution, _ = run(
            [
                runbook(),
                search(ZERO_MATCH_QUERY),
                cite_zero_match,
                search(USABLE_QUERY),
                cite_usable,
            ],
            settings=V261,
        )
        assert execution.status == "completed"
        categories = rejections(execution)[0]["failure_categories"]
        assert REJECT_LOG_EVIDENCE_REQUIRED in categories
        assert REJECT_EVIDENCE_REFS_REQUIRED in categories


# --- isolation ---------------------------------------------------------------------


class TestIsolation:
    def test_controller_never_receives_scenario_answers_or_predicates(self):
        public = {
            name
            for name in inspect.signature(WorkflowController).parameters
            if not name.startswith("_")
        }
        assert public == {"context", "treatments", "controller_id", "warning_at"}
        source = inspect.getsource(workflow_module)
        for forbidden in (
            "accepted_diagnoses",
            "accepted_remediations",
            "evidence_predicates",
            "distractor_",
            "root_cause",
            "scenarios",
            "sealed_set",
            "SealedSet",
            "catalog(",
        ):
            assert forbidden not in source, forbidden

    def test_guidance_is_scenario_independent(self):
        corpus = " ".join(
            [*GUIDANCE.values(), *UNUSABLE_GUIDANCE.values(), RUNBOOK_NOT_FOUND_GUIDANCE]
        )
        for scenario in catalog().values():
            for secret in (
                scenario.affected_service,
                *scenario.accepted_diagnoses,
                *scenario.accepted_remediations,
                *scenario.distractor_remediations,
            ):
                assert secret not in corpus

    def test_summary_carries_counts_and_state_names_only(self):
        execution, _ = run(
            [runbook(), search(ZERO_MATCH_QUERY), search(USABLE_QUERY), terminal(omit_refs=True)],
            settings=V260,
        )
        blob = json.dumps(execution.workflow_control)
        for secret in (USABLE_QUERY, ZERO_MATCH_QUERY, SCENARIO.accepted_diagnoses[0]):
            assert secret not in blob
        assert set(execution.workflow_control) == {
            "controller",
            "treatments",
            "final_state",
            "transitions",
            "observations",
            "eligible_observations",
            "usable_log_observations",
            "zero_match_searches",
            "not_found_lookups",
            "runbooks_found",
            "terminal_attempts",
            "rejected_terminal_attempts",
            "rejection_categories",
            "accepted_evidence_refs",
            "remaining_turn_warnings",
        }

    def test_exactly_one_native_tool_call_per_turn_and_no_added_calls(self):
        execution, client = run(
            [runbook(), search(USABLE_QUERY), terminal(omit_refs=True)], settings=V260
        )
        assert client.calls == 3
        assert [t.tool for t in execution.tool_trace] == [
            "retrieve_runbook",
            "search_logs",
            TERMINAL_TOOL,
        ]

    def test_controller_never_selects_or_rewrites_arguments(self):
        execution, _ = run(
            [runbook(), search(USABLE_QUERY), terminal(omit_refs=True)], settings=V260
        )
        assert execution.diagnosis_id == SCENARIO.accepted_diagnoses[0]
        assert execution.remediation_id == SCENARIO.accepted_remediations[0]
        assert execution.tool_trace[1].arguments == {"query": USABLE_QUERY}

    def test_historical_workloads_do_not_get_the_workflow_summary(self):
        execution, _ = run(
            [runbook(), search(USABLE_QUERY), terminal(omit_refs=True)],
            settings=GenerationSettings(workload_version="2.4.1"),
        )
        assert execution.workflow_control is None
        assert WORKFLOW_FIELD not in execution.tool_trace[0].result

    def test_observation_schema_accepts_the_workflow_summary(self):
        from pathlib import Path

        from blackwell_lab.schemas import validate_document
        from blackwell_lab.workload.evaluator import evaluate
        from blackwell_lab.workload.runner import TaskOutcome, _observation_document
        from blackwell_lab.workload.sampling import generate_task_instances

        execution, _ = run(
            [runbook(), search(USABLE_QUERY), terminal(omit_refs=True)], settings=V260
        )
        instance = generate_task_instances([SCENARIO.scenario_id], 1, 7)[0]
        execution.instance_id = instance.instance_id
        execution.instance_seed = instance.instance_seed
        outcome = TaskOutcome(
            task_index=0,
            instance=instance,
            execution=execution,
            evaluation=evaluate(SCENARIO, execution),
            submitted_offset_ms=0.0,
        )
        document = _observation_document(
            run_id="00000000-0000-4000-8000-000000000000",
            repetition_index=1,
            phase="measured",
            outcomes=(outcome,),
        )
        observation = document["observations"][0]
        assert observation["workflow_control"]["final_state"] == STATE_TERMINAL_ACCEPTED
        schema = Path(__file__).resolve().parents[1] / "schemas" / "task-observation.schema.json"
        validate_document(document, schema)

    def test_scripted_client_is_the_only_model_contact(self):
        client = ScriptedClient([runbook(), search(USABLE_QUERY), terminal(omit_refs=True)])
        run([], settings=V260, client=client)
        assert client.calls == 3
