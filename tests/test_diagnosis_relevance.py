"""workflow-controller-v2 (workloads 2.7.0 / 2.7.1; decision D-0033).

Proves diagnosis-relevant evidence, unusable no-match results, repeated
zero-match rejection, fail-closed malformed terminals, frozen W1/W2
behavior and identities, and cross-generation control isolation. The
relevance module is not allowed to import evaluator or holdout answers.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest
from test_evidence_grounding import run, runbook, search

from blackwell_lab.cloud.canary import (
    CANARY_MEASURED_SEED,
    CANARY_QUALITY_FLOOR,
    CANARY_TASKS,
)
from blackwell_lab.cloud.matched_control import (
    W1_W2_PAIR,
    W3_W4_PAIR,
    pair_for_control,
    pair_for_treatment,
    require_development_control_section,
)
from blackwell_lab.cloud.mvl import FROZEN_REGION
from blackwell_lab.cloud.qualification import (
    DEVELOPMENT_QUALITY_FLOOR,
    DEVELOPMENT_TASKS,
    HOLDOUT_TEMPLATE_IDS,
    candidate_identity_digest,
    control_candidate_for,
)
from blackwell_lab.workload import relevance
from blackwell_lab.workload.agent import DEFAULT_MAX_TURNS, RETRIES
from blackwell_lab.workload.evidence import ELIGIBLE, build_controller
from blackwell_lab.workload.model_client import GenerationSettings
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.tools import TERMINAL_TOOL
from blackwell_lab.workload.validation import ConfigError
from blackwell_lab.workload.workflow import (
    REJECT_DIAGNOSIS_RELEVANT_EVIDENCE,
    REJECT_EVIDENCE_REFS_REQUIRED,
    REJECT_LOG_EVIDENCE_REQUIRED,
    REJECT_MALFORMED_TERMINAL,
    REJECT_RELEVANT_EVIDENCE_REF,
    REJECT_REPEATED_ZERO_MATCH,
    REPEATED_ZERO_MATCH_GUIDANCE,
    WORKFLOW_FIELD,
    WORKFLOW_REQUIREMENTS_UNMET,
    DiagnosisRelevantWorkflowController,
    WorkflowController,
)

PARENT_FILE_SHA256 = {
    "src/blackwell_lab/workload/evaluator.py": (
        "6d1033d9fa1c69f0ef7923f61271592d9d543ab75313762292f2eb4c2ebcd1bf"
    ),
    "src/blackwell_lab/workload/scenarios.py": (
        "b0591c8b96c6120f5276570d39d75cd72dbec478989832bd1c02951080d12d6d"
    ),
    "src/blackwell_lab/cloud/canary.py": (
        "cd71e2b8eda0b2d2360a83530ba63bd705b557179583c245d1b42e1474c75223"
    ),
}
FROZEN_W1 = "2b7c5745084f4459af66e58fa5701d5c513108a9536385acbed00722c02e68cf"
FROZEN_W2 = "06d673972a696efcddc9ce00d6c9f6a15a032e0c516d4dc8b54f01dbba0917a2"
W3_DIGEST = "bdb9947bf5c0934833d077536ae85740857c36bfd08d88f7fee8b7b54f0f857e"
W4_DIGEST = "66df267f60f062f867678331fc466305cde5432bc9cba861ebe90e8cef108414"
V260 = GenerationSettings(workload_version="2.6.0")
V270 = GenerationSettings(workload_version="2.7.0")
V271 = GenerationSettings(workload_version="2.7.1")
DNS = "dns-failures-001"


def _log(query: str, message: str, *, total: int = 1) -> dict:
    lines = [{"message": message, "level": "ERROR"}] if total else []
    return {"query": query, "total_matches": total, "lines": lines}


def _runbook(steps: list[str], diagnoses: list[str], remediations: list[str]) -> dict:
    return {
        "key": "svc",
        "found": True,
        "runbook": {"title": "published runbook", "steps": steps, "remediation_ids": remediations},
        "diagnosis_candidates": diagnoses,
    }


def _terminal(diagnosis: str, remediation: str, refs: list[str] | None = None) -> dict:
    arguments: dict = {
        "diagnosis_id": diagnosis,
        "rationale": "recorded observation",
        "remediation_id": remediation,
    }
    if refs is not None:
        arguments["evidence_refs"] = refs
    return arguments


def _prepare(controller: WorkflowController, log_payload: dict, steps: list[str], diagnosis: str):
    observation = controller.record("search_logs", log_payload)
    controller.record(
        "retrieve_runbook",
        _runbook(steps, [diagnosis], ["remediation-a"]),
    )
    return observation


class TestRelevanceContract:
    def test_irrelevant_log_cannot_authenticate_a_terminal(self):
        controller = DiagnosisRelevantWorkflowController(context="ctx")
        _prepare(
            controller,
            _log("cache", "cache eviction disabled"),
            ["Check resolver SERVFAIL counters before any restart."],
            "published-diagnosis-a",
        )
        verdict = controller.validate_terminal(_terminal("published-diagnosis-a", "remediation-a"))
        assert verdict.accepted is False
        assert REJECT_DIAGNOSIS_RELEVANT_EVIDENCE in verdict.failure_categories
        assert "SERVFAIL" not in verdict.tool_payload()["guidance"]
        assert "published-diagnosis-a" not in verdict.tool_payload()["guidance"]
        assert "remediation-a" not in verdict.tool_payload()["guidance"]

    @pytest.mark.parametrize(
        ("diagnosis", "steps", "query", "message"),
        [
            (
                "resolver-servfail",
                ["Follow the published steps."],
                "SERVFAIL",
                "lookup returned SERVFAIL from the resolver",
            ),
            (
                "unhealthy-upstream-signer",
                ["Follow the published steps."],
                "upstream",
                "upstream dependency timed out",
            ),
            (
                "unbounded-cache-growth",
                ["Follow the published steps."],
                "cache",
                "cache size grew without a bound",
            ),
            (
                "readiness-probe-misconfigured",
                ["Follow the published steps."],
                "readiness",
                "readiness probe failing on the new replica",
            ),
        ],
    )
    def test_generic_categories_can_satisfy_relevance(self, diagnosis, steps, query, message):
        controller = DiagnosisRelevantWorkflowController(context="ctx")
        _prepare(controller, _log(query, message), steps, diagnosis)
        verdict = controller.validate_terminal(_terminal(diagnosis, "remediation-a"))
        assert verdict.accepted is True
        assert verdict.failure_categories == ()

    def test_zero_match_and_not_found_stay_unusable(self):
        controller = DiagnosisRelevantWorkflowController(context="ctx")
        zero = controller.record("search_logs", _log("SERVFAIL", "", total=0))
        missing = controller.record(
            "retrieve_runbook",
            {"key": "missing", "found": False, "available": ["svc"]},
        )
        assert zero.eligibility != ELIGIBLE
        assert missing.eligibility != ELIGIBLE
        assert controller._log_categories == {}
        controller.record(
            "retrieve_runbook",
            _runbook(
                ["Check resolver SERVFAIL counters before any restart."],
                ["published-diagnosis-a"],
                ["remediation-a"],
            ),
        )
        verdict = controller.validate_terminal(_terminal("published-diagnosis-a", "remediation-a"))
        assert verdict.accepted is False
        assert REJECT_LOG_EVIDENCE_REQUIRED in verdict.failure_categories
        assert REJECT_DIAGNOSIS_RELEVANT_EVIDENCE in verdict.failure_categories

    def test_repeated_equivalent_zero_match_is_a_typed_rejection(self):
        controller = DiagnosisRelevantWorkflowController(context="ctx")
        controller.note_turn(0, DEFAULT_MAX_TURNS)
        first = controller.annotate(
            _log("ServFail", "", total=0),
            controller.record("search_logs", _log("ServFail", "", total=0)),
        )
        controller.note_turn(DEFAULT_MAX_TURNS - 4, DEFAULT_MAX_TURNS)
        second = controller.annotate(
            _log("  servfail  ", "", total=0),
            controller.record("search_logs", _log("  servfail  ", total=0, message="")),
        )
        assert first[WORKFLOW_FIELD].get("failure_category") is None
        block = second[WORKFLOW_FIELD]
        assert block["failure_category"] == REJECT_REPEATED_ZERO_MATCH
        assert block["guidance"] == REPEATED_ZERO_MATCH_GUIDANCE
        assert "change the query" in block["guidance"].casefold()
        assert "source" in block["guidance"].casefold()
        assert "hypothesis" in block["guidance"].casefold()
        assert block["turns_remaining"] == 3
        assert "turns remain" in block["warning"]
        other = controller.annotate(
            _log("cache", "", total=0),
            controller.record("search_logs", _log("cache", "", total=0)),
        )
        assert other[WORKFLOW_FIELD].get("failure_category") is None

    def test_malformed_terminals_fail_closed_and_stay_bounded(self):
        controller = DiagnosisRelevantWorkflowController(context="ctx")
        first = controller.validate_terminal({"diagnosis_id": "", "rationale": "", "extra": 1})
        second = controller.validate_terminal("not-a-mapping")
        assert first.accepted is False and second.accepted is False
        assert REJECT_MALFORMED_TERMINAL in first.failure_categories
        assert second.failure_categories == (REJECT_MALFORMED_TERMINAL,)
        assert controller.terminal_attempts == 2
        assert controller.state != "terminal_accepted"
        assert controller.exhaustion_error_category == WORKFLOW_REQUIREMENTS_UNMET

    def test_treatment_refs_must_cite_a_relevant_log(self):
        controller = DiagnosisRelevantWorkflowController(
            context="ctx", treatments=("evidence-refs",)
        )
        log_obs = controller.record(
            "search_logs",
            _log("SERVFAIL", "lookup returned SERVFAIL from the resolver"),
        )
        health = controller.record(
            "get_service_health",
            {"services": {"svc": {"api": "degraded"}}},
        )
        change = controller.record("check_recent_changes", {"changes": [{"change_id": "c1"}]})
        controller.record(
            "retrieve_runbook",
            _runbook(
                ["Check resolver SERVFAIL counters before any restart."],
                ["resolver-servfail", "badger-queue-outage"],
                ["remediation-a"],
            ),
        )
        assert health.eligible is True
        assert change.eligible is False
        cited_health = controller.validate_terminal(
            _terminal("resolver-servfail", "remediation-a", [health.observation_id])
        )
        assert cited_health.accepted is False
        assert REJECT_RELEVANT_EVIDENCE_REF in cited_health.failure_categories
        cited_change = controller.validate_terminal(
            _terminal("resolver-servfail", "remediation-a", [change.observation_id])
        )
        assert REJECT_EVIDENCE_REFS_REQUIRED in cited_change.failure_categories
        cited_log = controller.validate_terminal(
            _terminal("resolver-servfail", "remediation-a", [log_obs.observation_id])
        )
        assert cited_log.accepted is True
        distractor = controller.validate_terminal(
            _terminal("badger-queue-outage", "remediation-a", [log_obs.observation_id])
        )
        assert distractor.accepted is False
        assert REJECT_DIAGNOSIS_RELEVANT_EVIDENCE in distractor.failure_categories
        assert REJECT_RELEVANT_EVIDENCE_REF in distractor.failure_categories

    def test_short_technical_tokens_and_acronyms_match_returned_lines(self):
        cases = (
            ("edge-mtu-clamp", "mtu", "interface mtu dropped on the path"),
            ("worker-oom-kill", "oom", "worker oom kill on the replica"),
            ("expired-tls-cert", "tls", "handshake used an expired tls certificate"),
            ("ingress-h2-stall", "h2", "ingress h2 stream stalled"),
            ("stale-ipv4-route", "ipv4", "route advertised the wrong ipv4 prefix"),
        )
        for diagnosis, token, message in cases:
            payload = _log(token, message)
            kept = relevance.diagnosis_tokens(diagnosis)
            lines = relevance.log_message_tokens(payload)
            assert token in kept
            assert token in lines
            assert relevance.evidence_relevant(
                diagnosis_categories=relevance.categories_in_text(diagnosis),
                diagnosis_id_tokens=kept,
                log_categories=relevance.classify_usable_log(payload),
                log_tokens=lines,
            )
        payload = _log("mtu", "interface mtu dropped on the path")
        assert relevance.diagnosis_tokens("edge-mtu-clamp") & relevance.log_message_tokens(
            payload
        ) == frozenset({"mtu"})
        controller = DiagnosisRelevantWorkflowController(context="ctx")
        _prepare(
            controller,
            payload,
            ["Follow the published steps."],
            "edge-mtu-clamp",
        )
        verdict = controller.validate_terminal(_terminal("edge-mtu-clamp", "remediation-a"))
        assert verdict.accepted is True

    def test_stopwords_numbers_and_unrelated_acronyms_do_not_match(self):
        assert "failure" not in relevance.diagnosis_tokens("config-failure")
        assert "release" not in relevance.diagnosis_tokens("bad-release-window")
        assert "missing" not in relevance.diagnosis_tokens("header-missing-span")
        assert "404" not in relevance.diagnosis_tokens("ticket-404-only")
        failure = _log("failure", "process failure during reload")
        assert not relevance.evidence_relevant(
            diagnosis_categories=relevance.categories_in_text("config-failure"),
            diagnosis_id_tokens=relevance.diagnosis_tokens("config-failure"),
            log_categories=relevance.classify_usable_log(failure),
            log_tokens=relevance.log_message_tokens(failure),
        )
        numeric = _log("404", "status 404 returned to the caller")
        assert not (
            relevance.diagnosis_tokens("ticket-404-only") & relevance.log_message_tokens(numeric)
        )
        injected = _log("mtu", "disk latency exceeded budget")
        assert "mtu" not in relevance.log_message_tokens(injected)
        assert not relevance.evidence_relevant(
            diagnosis_categories=frozenset(),
            diagnosis_id_tokens=relevance.diagnosis_tokens("edge-mtu-clamp"),
            log_categories=relevance.classify_usable_log(injected),
            log_tokens=relevance.log_message_tokens(injected),
        )
        other = _log("rpc", "rpc deadline exceeded on the client")
        assert "rpc" in relevance.log_message_tokens(other)
        assert "rpc" not in relevance.diagnosis_tokens("edge-mtu-clamp")
        assert not (
            relevance.diagnosis_tokens("edge-mtu-clamp") & relevance.log_message_tokens(other)
        )
        controller = DiagnosisRelevantWorkflowController(context="ctx")
        _prepare(
            controller,
            failure,
            ["Check resolver SERVFAIL counters before any restart."],
            "config-failure",
        )
        verdict = controller.validate_terminal(_terminal("config-failure", "remediation-a"))
        assert verdict.accepted is False
        assert REJECT_DIAGNOSIS_RELEVANT_EVIDENCE in verdict.failure_categories


CORRECT_DIAGNOSIS = "resolver-servfail"
DISTRACTOR_DIAGNOSES = ("badger-queue-outage", "network-firewall-block")
TOPICAL_STEPS = ["Check resolver SERVFAIL counters before any restart."]


class TestSelectedDiagnosisOnly:
    def test_w3_and_w4_accept_the_matching_diagnosis_and_reject_distractors(self):
        log_payload = _log("SERVFAIL", "lookup returned SERVFAIL from the resolver")
        for version in ("2.7.0", "2.7.1"):
            controller = build_controller(version, context=f"ctx-{version}")
            assert isinstance(controller, DiagnosisRelevantWorkflowController)
            log_obs = controller.record("search_logs", log_payload)
            controller.record(
                "retrieve_runbook",
                _runbook(
                    TOPICAL_STEPS,
                    [CORRECT_DIAGNOSIS, *DISTRACTOR_DIAGNOSES],
                    ["remediation-a"],
                ),
            )
            refs = [log_obs.observation_id] if controller.requires_evidence_refs else None
            accepted = controller.validate_terminal(
                _terminal(CORRECT_DIAGNOSIS, "remediation-a", refs)
            )
            assert accepted.accepted is True
            assert accepted.failure_categories == ()
            for distractor in DISTRACTOR_DIAGNOSES:
                rejected = controller.validate_terminal(
                    _terminal(distractor, "remediation-a", refs)
                )
                assert rejected.accepted is False
                assert REJECT_DIAGNOSIS_RELEVANT_EVIDENCE in rejected.failure_categories
                if controller.requires_evidence_refs:
                    assert REJECT_RELEVANT_EVIDENCE_REF in rejected.failure_categories
            if controller.requires_evidence_refs:
                uncited = controller.validate_terminal(
                    _terminal(CORRECT_DIAGNOSIS, "remediation-a")
                )
                assert uncited.accepted is False
                assert REJECT_EVIDENCE_REFS_REQUIRED in uncited.failure_categories
                assert REJECT_DIAGNOSIS_RELEVANT_EVIDENCE not in uncited.failure_categories


class TestLegacyPairUnchanged:
    def test_w1_still_accepts_a_diagnosis_irrelevant_log(self):
        controller = WorkflowController(context="ctx")
        _prepare(
            controller,
            _log("cache", "cache eviction disabled"),
            ["Check resolver SERVFAIL counters before any restart."],
            "published-diagnosis-a",
        )
        verdict = controller.validate_terminal(_terminal("published-diagnosis-a", "remediation-a"))
        assert verdict.accepted is True
        assert type(build_controller("2.6.0", context="ctx")) is WorkflowController
        assert type(build_controller("2.6.1", context="ctx")) is WorkflowController
        successor = build_controller("2.7.0", context="ctx")
        treatment = build_controller("2.7.1", context="ctx")
        assert isinstance(successor, DiagnosisRelevantWorkflowController)
        assert isinstance(treatment, DiagnosisRelevantWorkflowController)
        assert successor.controller_id == "workflow-controller-v2"
        assert treatment.requires_evidence_refs is True
        assert successor.requires_evidence_refs is False

    def test_w1_and_w2_identity_digests_are_unchanged(self):
        assert candidate_identity_digest("W1") == FROZEN_W1
        assert candidate_identity_digest("W2") == FROZEN_W2
        assert candidate_identity_digest("W3") == W3_DIGEST
        assert candidate_identity_digest("W4") == W4_DIGEST
        assert len({FROZEN_W1, FROZEN_W2, W3_DIGEST, W4_DIGEST}) == 4

    def test_controls_cannot_authenticate_across_generations(self):
        assert control_candidate_for("W2") == "W1"
        assert control_candidate_for("W4") == "W3"
        assert control_candidate_for("W2") != "W3"
        assert control_candidate_for("W4") != "W1"
        assert pair_for_treatment("W2") is W1_W2_PAIR
        assert pair_for_treatment("W4") is W3_W4_PAIR
        assert pair_for_control("W1") is W1_W2_PAIR
        assert pair_for_control("W3") is W3_W4_PAIR
        assert W1_W2_PAIR.kind != W3_W4_PAIR.kind
        assert W1_W2_PAIR.label_key == "w1_run_label"
        assert W3_W4_PAIR.label_key == "w3_run_label"

        def section(prefix: str) -> dict:
            return {
                "schema_version": "1.0.0",
                "run_tag": "w1-dev-20261008b",
                "canonical_commit": "a" * 40,
                "region": "ca-central",
                f"{prefix}_run_label": "qual-ctrl",
                "config_sha256": "ab" * 32,
                "result_sha256": "cd" * 32,
                "control_record_sha256": "ef" * 32,
                "resource_identity_sha256": "12" * 32,
                "ledger_sha256": "34" * 32,
            }

        w4 = {"development_control": section("w1")}
        w2 = {"development_control": section("w3")}
        with pytest.raises(ConfigError, match="unexpected or missing fields"):
            require_development_control_section(w4, candidate_id="W4", stage="development")
        with pytest.raises(ConfigError, match="unexpected or missing fields"):
            require_development_control_section(w2, candidate_id="W2", stage="development")


class TestIsolationAndFrozenSchedules:
    def test_relevance_module_does_not_import_holdout_or_evaluator(self):
        source = Path("src/blackwell_lab/workload/relevance.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        assert imported == ["__future__", "re"]
        for name in imported:
            assert "evaluator" not in name
            assert "holdout" not in name
            assert "scenario" not in name
        for template_id in HOLDOUT_TEMPLATE_IDS:
            assert template_id not in source
        for banned in (
            "accepted_diagnoses",
            "evidence_predicates",
            "predicate_id",
            "servfail-evidence",
            "upstream-health-evidence",
            "cache-log-evidence",
            "readiness-log-evidence",
        ):
            assert banned not in source
        assert relevance.categories_in_text("SERVFAIL") == frozenset({"servfail"})

    def test_evaluator_and_frozen_schedules_match_the_parent_commit(self):
        for relative, digest in PARENT_FILE_SHA256.items():
            current = hashlib.sha256(Path(relative).read_bytes()).hexdigest()
            assert current == digest, relative
        assert CANARY_TASKS == 10
        assert CANARY_MEASURED_SEED == 20261007
        assert CANARY_QUALITY_FLOOR == 0.40
        assert DEVELOPMENT_TASKS == 20
        assert DEVELOPMENT_QUALITY_FLOOR == 0.40
        assert FROZEN_REGION == "ca-central"
        assert RETRIES == 0
        assert DEFAULT_MAX_TURNS == 12


def _dns_terminal(refs: list[str] | None = None) -> dict:
    scenario = catalog()[DNS]
    arguments = {
        "diagnosis_id": scenario.accepted_diagnoses[0],
        "rationale": "recorded observation",
        "remediation_id": scenario.accepted_remediations[0],
    }
    if refs is not None:
        arguments["evidence_refs"] = refs
    return {"tool": TERMINAL_TOOL, "arguments": arguments}


class TestSuccessorLoop:
    def test_historical_controller_still_accepts_an_unrelated_log(self):
        execution, _ = run(
            [search("request"), runbook("dns-resolver"), _dns_terminal()],
            scenario_id=DNS,
            settings=V260,
        )
        assert execution.status == "completed"

    def test_successor_rejects_an_unrelated_log_until_the_budget_ends(self):
        steps = [search("request"), runbook("dns-resolver")] + [_dns_terminal()] * 10
        execution, _ = run(steps, scenario_id=DNS, settings=V270)
        assert execution.status == "error"
        assert execution.error_category == WORKFLOW_REQUIREMENTS_UNMET
        assert execution.diagnosis_id is None

    def test_successor_accepts_relevant_servfail_evidence(self):
        execution, _ = run(
            [search("SERVFAIL"), runbook("dns-resolver"), _dns_terminal()],
            scenario_id=DNS,
            settings=V270,
        )
        assert execution.status == "completed"

    def test_treatment_requires_the_relevant_log_reference(self):
        def cite_log(messages):
            import json

            from blackwell_lab.workload.evidence import OBSERVATION_ID_FIELD

            for message in messages:
                if message.role != "tool":
                    continue
                payload = json.loads(message.content)
                if payload.get("query") == "SERVFAIL" and payload.get(WORKFLOW_FIELD, {}).get(
                    "usable_evidence"
                ):
                    return _dns_terminal([payload[OBSERVATION_ID_FIELD]])
            raise AssertionError("relevant log was not returned")

        execution, _ = run(
            [search("SERVFAIL"), runbook("dns-resolver"), cite_log],
            scenario_id=DNS,
            settings=V271,
        )
        assert execution.status == "completed"
        assert execution.workflow_control["controller"] == "workflow-controller-v2"

    def test_malformed_terminal_stays_inside_the_turn_budget(self):
        bad = {
            "tool": TERMINAL_TOOL,
            "arguments": {"diagnosis_id": "", "rationale": "", "remediation_id": ""},
        }
        execution, _ = run([bad] * DEFAULT_MAX_TURNS, scenario_id=DNS, settings=V270)
        assert execution.status == "error"
        assert execution.error_category == WORKFLOW_REQUIREMENTS_UNMET
        assert execution.workflow_control["rejection_categories"][REJECT_MALFORMED_TERMINAL] == 12
