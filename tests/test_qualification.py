"""Focused offline tests for the D-0019 agent-quality qualification gate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from blackwell_lab.cloud import cli, lifecycle
from blackwell_lab.cloud.cli import main
from blackwell_lab.cloud.mvl import (
    FROZEN_MODEL_ARTIFACT,
    FROZEN_MODEL_ARTIFACT_HASH,
    FROZEN_MODEL_REVISION,
)
from blackwell_lab.cloud.qualification import (
    C1_TEMPERATURE,
    C2_TEMPERATURE,
    CATALOG_WORKLOAD_VERSION,
    DEVELOPMENT_TASKS,
    DEVELOPMENT_TEMPLATE_IDS,
    FORBIDDEN_IDENTITY_MARKERS,
    FREEZE_TASKS,
    HOLDOUT_MUST_NOT_REVISE_WORDING,
    HOLDOUT_TASKS,
    HOLDOUT_TEMPLATE_IDS,
    MAX_INTERACTIVE_E2E_P95_MS,
    MAX_INTERACTIVE_TTFT_P95_MS,
    QUALIFICATION_APPROVAL_TEMPLATE,
    QUALIFICATION_ARTIFACT_FAMILY,
    QUALIFICATION_WORKLOAD_VERSION,
    SPLIT_SEED,
    QualificationError,
    approval_phrase,
    candidate_identity_digest,
    candidate_temperature,
    compute_qualification_metrics,
    evaluate_production_like_target,
    evaluate_stage_thresholds,
    evaluate_study_entry_gate,
    freeze_template_split,
    frozen_candidate_fields,
    refuse_mvl_identities,
    require_approval,
    sanitized_receipt,
    serialize_candidate,
    stage_spec,
    validate_authorized_qualification_config,
    validate_stage_request,
)
from blackwell_lab.workload.agent import system_prompt, task_prompt
from blackwell_lab.workload.evaluator import EVALUATOR_VERSION, QUALITY_THRESHOLD
from blackwell_lab.workload.native_tools import TOOL_DESCRIPTIONS, openai_tool_definitions
from blackwell_lab.workload.scenarios import WORKLOAD_VERSION, catalog
from blackwell_lab.workload.tools import SimulatedToolbox
from blackwell_lab.workload.validation import ConfigError

COMMIT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
RUN_TAG = "p3-qual-20260918a"
FROZEN_ACCEPTED_ANSWERS_SHA256 = "2edb7134040af2e8e9e9fec4c068bbc0dfaf6bfa55cbd4284b8bf6045c7aea2a"
LOG_DEPENDENT_SCENARIOS = (
    "elevated-latency-001",
    "pod-failures-001",
    "memory-pressure-001",
    "failed-deployment-001",
    "dns-failures-001",
    "rate-limiting-001",
)


@dataclass
class _FakeRecord:
    run_id: str
    outcomes: list
    written_files: tuple = ()
    result: dict | None = None
    manifest: dict | None = None
    measured_observations: dict | None = None
    warmup_observations: dict | None = None


def _accepted_answers_payload() -> bytes:
    payload = {
        scenario_id: {
            "accepted_diagnoses": list(scenario.accepted_diagnoses),
            "accepted_remediations": list(scenario.accepted_remediations),
            "evidence_predicate_ids": [
                predicate.predicate_id for predicate in scenario.evidence_predicates
            ],
        }
        for scenario_id, scenario in catalog().items()
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _outcome(
    *,
    template_id: str = "dns-failures-001",
    success: bool = True,
    error_category: str | None = None,
    e2e_ms: float = 1_000.0,
    ttft_ms: float = 200.0,
) -> dict:
    return {
        "template_id": template_id,
        "success": success,
        "evaluation": {"success": success, "evaluator_version": EVALUATOR_VERSION},
        "error_category": error_category,
        "e2e_ms": e2e_ms,
        "ttft_ms": ttft_ms,
        "turns": [{"ttft_ms": ttft_ms}],
    }


def _metrics(outcomes, *, provenance_ok=True, verification_ok=True):
    return compute_qualification_metrics(
        outcomes, provenance_ok=provenance_ok, verification_ok=verification_ok
    )


def qualification_config_dict(candidate_id="C1", stage="development"):
    spec = stage_spec(stage)
    temperature = candidate_temperature(candidate_id)
    return {
        "workflow": "qualify-agent",
        "candidate_id": candidate_id,
        "workload_version": QUALIFICATION_WORKLOAD_VERSION,
        "endpoint": {"base_url": "http://127.0.0.1:8000/v1", "model": "m"},
        "cloud": {
            "instance_type": "g3-gpu-rtxpro6000-blackwell-1",
            "region": "us-sea",
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
            "temperature": temperature,
            "top_p": 0.95,
            "max_tokens": 1024,
            "seed": 20260906,
            "reasoning_mode": True,
        },
    }


def write_qual_config(tmp_path: Path, **overrides) -> Path:
    config = qualification_config_dict()
    config.update(overrides)
    path = tmp_path / "qualify.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def config_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def qual_argv(
    config_path: Path,
    *,
    run_label="qual-a",
    candidate="C1",
    stage="development",
    approve=None,
    run_tag=RUN_TAG,
):
    phrase = approve or approval_phrase(run_tag, run_label, candidate, config_sha256(config_path))
    return [
        "qualify-agent",
        "--run-tag",
        run_tag,
        "--run-label",
        run_label,
        "--candidate",
        candidate,
        "--stage",
        stage,
        "--config",
        str(config_path),
        "--approve",
        phrase,
    ]


def ready_ledger(**overrides):
    ledger = {
        "run_tag": RUN_TAG,
        "reconciled": True,
        "reconciliation": {"provider_checked": True},
        "resources": [
            {
                "address": "linode_instance.gpu_baseline",
                "type": "linode_instance",
                "provider_id": "42",
                "region": "us-sea",
            },
            {
                "address": "linode_firewall.gpu_baseline",
                "type": "linode_firewall",
                "provider_id": "555",
            },
        ],
    }
    ledger.update(overrides)
    return ledger


def _observed():
    from blackwell_lab.cloud.provenance import ObservedProvenance

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
            "region": "us-sea",
            "tags": ["blackwell-lab", f"run:{RUN_TAG}"],
        },
        host_facts={
            "storage_description": "plan NVMe",
            "network_description": "plan default networking",
        },
        gpu_facts={"gpu_model": "RTX PRO 6000 Blackwell", "driver_version": "580"},
        container_cuda_runtime_version="13.0",
    )


def _stage_outcomes(stage: str, *, success: bool = True) -> list[dict]:
    spec = stage_spec(stage)
    outcomes = []
    templates = spec["template_ids"]
    for index in range(spec["tasks"]):
        outcomes.append(_outcome(template_id=templates[index % len(templates)], success=success))
    return outcomes


def _prepare_qualify(tmp_path, monkeypatch, *, stage="development", success=True):
    from blackwell_lab.cloud import provenance, realbench

    monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
    monkeypatch.setattr(cli, "_git_head", lambda: COMMIT)
    monkeypatch.setattr(cli, "_tree_clean", lambda: True)
    external = tmp_path / "external"
    monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
    paths = lifecycle.lifecycle_paths(external, RUN_TAG)
    lifecycle.write_private_json(paths.ledger_path, ready_ledger())
    calls: list[object] = []
    destroy_calls: list[int] = []

    def fake_verify(**kwargs):
        return _observed()

    def fake_run(spec, *_args, **_kwargs):
        calls.append(spec)
        return [
            _FakeRecord(
                run_id="qual",
                outcomes=_stage_outcomes(stage, success=success),
                written_files=("qual.result.json",),
            )
        ]

    monkeypatch.setattr(provenance, "verify_live_provenance", fake_verify)
    monkeypatch.setattr(realbench, "run_real_cell", fake_run)
    monkeypatch.setattr(
        lifecycle,
        "plan_destroy",
        lambda *args, **kwargs: destroy_calls.append(1) or (_ for _ in ()).throw(AssertionError()),
    )
    config = write_qual_config(
        tmp_path,
        candidate_id="C1",
        warmup_passes=stage_spec(stage)["warmup_passes"],
        tasks_per_repetition=stage_spec(stage)["tasks"],
    )
    return config, calls, destroy_calls, external


class TestToolContract:
    def test_remediation_ids_are_absent_from_the_task_prompt(self):
        from blackwell_lab.workload.sampling import generate_task_instances

        generic = system_prompt(next(iter(catalog().values())))
        for scenario in catalog().values():
            instances = generate_task_instances([scenario.scenario_id], 1, 20260906)
            prompt = task_prompt(scenario, instances[0])
            combined = f"{generic}\n{prompt}"
            for remediation_id in scenario.accepted_remediations:
                assert remediation_id not in prompt
                assert remediation_id not in generic
            for remediation_id in scenario.distractor_remediations:
                assert remediation_id not in prompt
                assert remediation_id not in generic
            assert "Candidate diagnosis ids" in combined

    def test_retrieve_runbook_documents_and_accepts_a_service_key(self):
        description = TOOL_DESCRIPTIONS["retrieve_runbook"]
        assert "service or system" in description
        assert "remediation_ids" in description
        assert "found=false" in description
        schema = next(
            entry["function"]
            for entry in openai_tool_definitions()
            if entry["function"]["name"] == "retrieve_runbook"
        )
        assert schema["description"] == description
        assert schema["parameters"]["required"] == ["key"]
        scenario = catalog()["elevated-latency-001"]
        from fakes import FakeClock

        toolbox = SimulatedToolbox(scenario, clock=FakeClock())
        hit = toolbox.execute("retrieve_runbook", {"key": "zephyr-cart"})
        assert hit.payload["found"] is True
        assert "rollback-config-release-cfg-2041" in hit.payload["runbook"]["remediation_ids"]
        miss = toolbox.execute("retrieve_runbook", {"key": "not-a-published-runbook"})
        assert miss.payload["found"] is False

    def test_recommend_remediation_requires_runbook_sourced_id(self):
        description = TOOL_DESCRIPTIONS["recommend_remediation"]
        assert "retrieve_runbook" in description
        assert "remediation_ids" in description
        scenario = catalog()["elevated-latency-001"]
        from fakes import FakeClock

        toolbox = SimulatedToolbox(scenario, clock=FakeClock())
        runbook = toolbox.execute("retrieve_runbook", {"key": scenario.affected_service})
        remediation_id = runbook.payload["runbook"]["remediation_ids"][0]
        result = toolbox.execute(
            "recommend_remediation",
            {
                "diagnosis_id": scenario.accepted_diagnoses[0],
                "rationale": "evidence from the runbook",
                "remediation_id": remediation_id,
            },
        )
        assert result.payload["remediation_id"] == remediation_id

    def test_log_dependent_scenarios_use_search_logs(self):
        assert "log-dependent" in TOOL_DESCRIPTIONS["search_logs"]
        assert "search_logs" in system_prompt(next(iter(catalog().values())))
        for scenario_id in LOG_DEPENDENT_SCENARIOS:
            scenario = catalog()[scenario_id]
            tools = [step["tool"] for step in scenario.reference_tool_sequence]
            assert "search_logs" in tools, scenario_id


class TestCandidates:
    def test_c1_retains_temperature_and_top_p(self):
        fields = frozen_candidate_fields("C1")
        assert fields["candidate_id"] == "C1"
        assert fields["workload_version"] == "2.4.0"
        assert fields["generation"]["temperature"] == C1_TEMPERATURE == 1.0
        assert fields["generation"]["top_p"] == 0.95
        assert fields["generation"]["seed"] == 20260906
        assert fields["generation"]["reasoning_mode"] is True
        assert fields["generation"]["max_tokens"] == 1024
        assert fields["serving"]["engine_version"] == "0.27.1"

    def test_c2_changes_temperature_only(self):
        c1 = json.loads(serialize_candidate("C1"))
        c2 = json.loads(serialize_candidate("C2"))
        assert c1["generation"]["temperature"] == 1.0
        assert c2["generation"]["temperature"] == C2_TEMPERATURE == 0.2
        c1_rest = dict(c1)
        c2_rest = dict(c2)
        assert c1_rest.pop("candidate_id") == "C1"
        assert c2_rest.pop("candidate_id") == "C2"
        g1 = dict(c1_rest.pop("generation"))
        g2 = dict(c2_rest.pop("generation"))
        assert c1_rest == c2_rest
        assert g1.pop("temperature") == 1.0
        assert g2.pop("temperature") == 0.2
        assert g1 == g2
        assert candidate_identity_digest("C1") != candidate_identity_digest("C2")

    def test_no_third_candidate(self):
        with pytest.raises(ConfigError, match="C1 or C2"):
            frozen_candidate_fields("C3")


class TestFrozenSplitAndCounts:
    def test_deterministic_split_is_stable_and_disjoint(self):
        first = freeze_template_split()
        second = freeze_template_split()
        assert first == second
        development, holdout = first
        assert development == DEVELOPMENT_TEMPLATE_IDS
        assert holdout == HOLDOUT_TEMPLATE_IDS
        assert development == (
            "dns-failures-001",
            "memory-pressure-001",
            "failed-deployment-001",
            "unhealthy-upstream-001",
            "capacity-exhaustion-001",
            "gpu-saturation-001",
        )
        assert holdout == (
            "rate-limiting-001",
            "pod-failures-001",
            "elevated-latency-001",
            "storage-latency-001",
        )
        assert set(development).isdisjoint(holdout)
        assert set(development).union(holdout) == set(catalog())
        assert SPLIT_SEED == "blackwell-lab-agent-qualification-v1"
        validate_stage_request("development", template_ids=development, tasks=20)
        validate_stage_request("holdout", template_ids=holdout, tasks=20)
        validate_stage_request("freeze", template_ids=tuple(catalog()), tasks=200)

    def test_stage_task_counts_are_exact(self):
        assert stage_spec("development")["tasks"] == DEVELOPMENT_TASKS == 20
        assert stage_spec("holdout")["tasks"] == HOLDOUT_TASKS == 20
        assert stage_spec("freeze")["tasks"] == FREEZE_TASKS == 200
        assert len(stage_spec("development")["template_ids"]) == 6
        assert len(stage_spec("holdout")["template_ids"]) == 4
        assert len(stage_spec("freeze")["template_ids"]) == 10
        assert stage_spec("freeze")["warmup_passes"] == 1
        assert stage_spec("development")["warmup_passes"] == 0
        assert HOLDOUT_MUST_NOT_REVISE_WORDING.startswith("Holdout results must never")


class TestGates:
    def test_study_entry_aggregate_boundary(self):
        passing = [_outcome(success=True)] * 70 + [_outcome(success=False)] * 30
        failing = [_outcome(success=True)] * 69 + [_outcome(success=False)] * 31
        assert evaluate_study_entry_gate(_metrics(passing))["passed"] is True
        assert evaluate_study_entry_gate(_metrics(failing))["passed"] is False

    def test_study_entry_per_scenario_boundary(self):
        passing = (
            [_outcome(template_id="a", success=True)] * 2
            + [_outcome(template_id="a", success=False)] * 3
            + [_outcome(template_id="b", success=True)] * 8
        )
        failing = (
            [_outcome(template_id="a", success=True)]
            + [_outcome(template_id="a", success=False)] * 4
            + [_outcome(template_id="b", success=True)] * 14
        )
        assert evaluate_study_entry_gate(_metrics(passing))["passed"] is True
        assert evaluate_study_entry_gate(_metrics(failing))["passed"] is False

    def test_valid_native_tool_call_rate_boundary(self):
        passing = [_outcome()] * 99 + [
            _outcome(success=False, error_category="malformed_tool_call")
        ]
        failing = [_outcome()] * 98 + [
            _outcome(success=False, error_category="malformed_tool_call")
        ] * 2
        assert evaluate_study_entry_gate(_metrics(passing))["passed"] is True
        assert evaluate_study_entry_gate(_metrics(failing))["passed"] is False

    def test_invalid_tool_name_rate_must_be_zero(self):
        passing = [_outcome()] * 100
        failing = [_outcome()] * 99 + [_outcome(success=False, error_category="invalid_tool_name")]
        assert evaluate_study_entry_gate(_metrics(passing))["passed"] is True
        assert evaluate_study_entry_gate(_metrics(failing))["passed"] is False

    def test_invalid_argument_rate_boundary(self):
        passing = [_outcome()] * 99 + [
            _outcome(success=False, error_category="invalid_tool_arguments")
        ]
        failing = [_outcome()] * 98 + [
            _outcome(success=False, error_category="invalid_tool_arguments")
        ] * 2
        assert evaluate_study_entry_gate(_metrics(passing))["passed"] is True
        assert evaluate_study_entry_gate(_metrics(failing))["passed"] is False

    def test_request_inference_error_rate_boundary(self):
        passing = [_outcome()] * 99 + [_outcome(success=False, error_category="endpoint_error")]
        failing = [_outcome()] * 98 + [_outcome(success=False, error_category="endpoint_error")] * 2
        assert evaluate_study_entry_gate(_metrics(passing))["passed"] is True
        assert evaluate_study_entry_gate(_metrics(failing))["passed"] is False

    def test_timeouts_must_be_zero(self):
        passing = [_outcome()] * 20
        failing = [_outcome()] * 19 + [_outcome(success=False, error_category="task_timeout")]
        assert evaluate_study_entry_gate(_metrics(passing))["passed"] is True
        assert evaluate_study_entry_gate(_metrics(failing))["passed"] is False

    def test_interactive_latency_boundaries(self):
        def series(p95_value: float, count: int = 200) -> list[float]:
            return [1.0] * 189 + [p95_value] * 11

        passing = [
            _outcome(e2e_ms=e2e, ttft_ms=ttft)
            for e2e, ttft in zip(
                series(MAX_INTERACTIVE_E2E_P95_MS),
                series(MAX_INTERACTIVE_TTFT_P95_MS),
                strict=True,
            )
        ]
        failing_ttft = [
            _outcome(e2e_ms=e2e, ttft_ms=ttft)
            for e2e, ttft in zip(
                series(MAX_INTERACTIVE_E2E_P95_MS),
                series(MAX_INTERACTIVE_TTFT_P95_MS + 0.001),
                strict=True,
            )
        ]
        failing_e2e = [
            _outcome(e2e_ms=e2e, ttft_ms=ttft)
            for e2e, ttft in zip(
                series(MAX_INTERACTIVE_E2E_P95_MS + 0.001),
                series(MAX_INTERACTIVE_TTFT_P95_MS),
                strict=True,
            )
        ]
        assert evaluate_study_entry_gate(_metrics(passing))["passed"] is True
        assert evaluate_study_entry_gate(_metrics(failing_ttft))["passed"] is False
        assert evaluate_study_entry_gate(_metrics(failing_e2e))["passed"] is False

    def test_provenance_and_verification_are_required(self):
        outcomes = [_outcome()] * 20
        assert evaluate_study_entry_gate(_metrics(outcomes, provenance_ok=False))["passed"] is False
        assert (
            evaluate_study_entry_gate(_metrics(outcomes, verification_ok=False))["passed"] is False
        )

    def test_production_like_boundaries_do_not_invalidate_measurement(self):
        passing = [_outcome(success=True)] * 90 + [_outcome(success=False)] * 10
        failing = [_outcome(success=True)] * 89 + [_outcome(success=False)] * 11
        passed = evaluate_production_like_target(_metrics(passing))
        failed = evaluate_production_like_target(_metrics(failing))
        assert passed["passed"] is True
        assert failed["passed"] is False
        assert failed["invalidates_measurement"] is False

    def test_development_and_holdout_floors(self):
        dev_pass = [_outcome(success=True)] * 8 + [_outcome(success=False)] * 12
        dev_fail = [_outcome(success=True)] * 7 + [_outcome(success=False)] * 13
        hold_pass = [_outcome(success=True)] * 10 + [_outcome(success=False)] * 10
        hold_fail = [_outcome(success=True)] * 9 + [_outcome(success=False)] * 11
        assert evaluate_stage_thresholds("development", _metrics(dev_pass))["continue"] is True
        assert evaluate_stage_thresholds("development", _metrics(dev_fail))["stopped"] is True
        hold = evaluate_stage_thresholds("holdout", _metrics(hold_pass))
        assert hold["continue"] is True
        assert HOLDOUT_MUST_NOT_REVISE_WORDING in (hold["note"] or "")
        assert evaluate_stage_thresholds("holdout", _metrics(hold_fail))["stopped"] is True


class TestEvaluatorUnchanged:
    def test_evaluator_version_and_accepted_answers_are_unchanged(self):
        assert EVALUATOR_VERSION == "3.1.0"
        assert QUALITY_THRESHOLD == 1.0
        assert WORKLOAD_VERSION == CATALOG_WORKLOAD_VERSION == "2.3.0"
        assert QUALIFICATION_WORKLOAD_VERSION == "2.4.0"
        digest = hashlib.sha256(_accepted_answers_payload()).hexdigest()
        assert digest == FROZEN_ACCEPTED_ANSWERS_SHA256


class TestApprovalAndIdentities:
    def test_approval_phrase_and_config_digest_are_enforced(self):
        expected = approval_phrase(RUN_TAG, "qual-a", "C1", "abc123")
        assert expected == QUALIFICATION_APPROVAL_TEMPLATE.format(
            run_tag=RUN_TAG,
            run_label="qual-a",
            candidate_id="C1",
            config_sha256="abc123",
        )
        require_approval(
            expected,
            run_tag=RUN_TAG,
            run_label="qual-a",
            candidate_id="C1",
            config_sha256="abc123",
        )
        with pytest.raises(QualificationError, match="exact owner approval phrase"):
            require_approval(
                expected.replace("abc123", "fff"),
                run_tag=RUN_TAG,
                run_label="qual-a",
                candidate_id="C1",
                config_sha256="abc123",
            )

    def test_mvl_f_identities_and_paths_are_refused(self):
        config = qualification_config_dict()
        refuse_mvl_identities(
            run_tag=RUN_TAG,
            run_label="qual-a",
            config=config,
            artifact_family=QUALIFICATION_ARTIFACT_FAMILY,
        )
        for marker, kwargs in (
            ("mvl-f", {"run_tag": "p3-mvl-f", "run_label": "qual-a", "config": config}),
            ("mvl", {"run_tag": RUN_TAG, "run_label": "mvl-a", "config": config}),
            ("p3-mvl", {"run_tag": "p3-mvl-20260915c", "run_label": "qual-a", "config": config}),
            (
                "workflow",
                {
                    "run_tag": RUN_TAG,
                    "run_label": "qual-a",
                    "config": {**config, "workflow": "mvl-baseline"},
                },
            ),
        ):
            del marker
            with pytest.raises(QualificationError, match="MVL-F"):
                refuse_mvl_identities(artifact_family=QUALIFICATION_ARTIFACT_FAMILY, **kwargs)
        with pytest.raises(QualificationError, match=r"MVL-F|qualification-runs"):
            refuse_mvl_identities(
                run_tag=RUN_TAG,
                run_label="qual-a",
                config={"workflow": "qualify-agent"},
                artifact_family="real-runs",
            )
        assert "mvl-f" in FORBIDDEN_IDENTITY_MARKERS


class TestReceiptPrivacy:
    def test_receipt_omits_secrets_paths_reasoning_and_provider_payloads(self):
        receipt = sanitized_receipt(
            run_label="qual-a-c1-development",
            candidate_id="C1",
            stage="development",
            config_sha256="abc",
            identity_digest="def",
            gates={"continue": True, "stopped": False},
            files=("qual.result.json",),
            stopped=False,
        )
        blob = json.dumps(receipt)
        assert receipt["artifact_family"] == "qualification-runs"
        assert receipt["not_mvl"] is True
        assert receipt["not_comparative"] is True
        for forbidden in (
            "LINODE_TOKEN",
            "AKIA",
            "reasoning",
            "/home/",
            "/opt/models/",
            "127.0.0.1",
            "provider_id",
        ):
            assert forbidden not in blob


class TestQualifyCommand:
    def test_development_screen_persists_qualification_artifacts(
        self, tmp_path, monkeypatch, capsys
    ):
        config, calls, destroy_calls, external = _prepare_qualify(tmp_path, monkeypatch)
        assert main(qual_argv(config)) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["workflow"] == "qualify-agent"
        assert report["candidate_id"] == "C1"
        assert report["stage"] == "development"
        assert report["stopped"] is False
        assert calls[0].artifact_family == QUALIFICATION_ARTIFACT_FAMILY
        assert calls[0].template_ids == DEVELOPMENT_TEMPLATE_IDS
        assert calls[0].tasks_per_repetition == 20
        assert calls[0].generation.temperature == 1.0
        assert calls[0].generation.top_p == 0.95
        assert calls[0].workload_version == "2.4.0"
        assert destroy_calls == []
        assert (external / "qualification-runs").is_dir()
        assert not (external / "real-runs").exists() or not any((external / "real-runs").rglob("*"))

    def test_holdout_and_freeze_counts_and_c2_temperature(self, tmp_path, monkeypatch):
        from blackwell_lab.cloud import provenance, realbench

        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        monkeypatch.setattr(cli, "_git_head", lambda: COMMIT)
        monkeypatch.setattr(cli, "_tree_clean", lambda: True)
        external = tmp_path / "external"
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        paths = lifecycle.lifecycle_paths(external, RUN_TAG)
        lifecycle.write_private_json(paths.ledger_path, ready_ledger())
        calls: list[object] = []

        def fake_run(spec, *_args, **_kwargs):
            calls.append(spec)
            stage = "holdout" if spec.tasks_per_repetition == 20 else "freeze"
            return [_FakeRecord(run_id="q", outcomes=_stage_outcomes(stage))]

        monkeypatch.setattr(provenance, "verify_live_provenance", lambda **kwargs: _observed())
        monkeypatch.setattr(realbench, "run_real_cell", fake_run)
        holdout = write_qual_config(
            tmp_path,
            candidate_id="C2",
            warmup_passes=0,
            tasks_per_repetition=20,
            generation={
                "temperature": 0.2,
                "top_p": 0.95,
                "max_tokens": 1024,
                "seed": 20260906,
                "reasoning_mode": True,
            },
        )
        assert main(qual_argv(holdout, candidate="C2", stage="holdout")) == 0
        assert calls[0].generation.temperature == 0.2
        assert calls[0].template_ids == HOLDOUT_TEMPLATE_IDS
        freeze = write_qual_config(
            tmp_path / "freeze" if False else tmp_path,
            candidate_id="C1",
            warmup_passes=1,
            tasks_per_repetition=200,
        )
        freeze.write_text(
            json.dumps({**qualification_config_dict("C1", "freeze")}), encoding="utf-8"
        )
        assert main(qual_argv(freeze, stage="freeze")) == 0
        freeze_spec = calls[-1]
        assert freeze_spec.tasks_per_repetition == 200
        assert freeze_spec.warmup_passes == 1
        assert freeze_spec.template_ids == tuple(catalog())
        assert freeze_spec.repetitions == 1

    def test_wrong_approval_and_mvl_label_are_refused(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        config = write_qual_config(tmp_path)
        assert main(qual_argv(config, approve="I approve some other digest")) == 1
        assert "exact owner approval phrase" in capsys.readouterr().err
        assert main(qual_argv(config, run_label="mvl-f")) == 1
        assert "MVL-F" in capsys.readouterr().err

    def test_missing_results_dir_fails_closed(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        monkeypatch.setattr(cli, "_git_head", lambda: COMMIT)
        monkeypatch.setattr(cli, "_tree_clean", lambda: True)
        monkeypatch.delenv("LAB_RESULTS_DIR", raising=False)
        config = write_qual_config(tmp_path)
        assert main(qual_argv(config)) == 3
        assert "LAB_RESULTS_DIR" in capsys.readouterr().err

    def test_stage_stop_writes_receipt_and_makes_no_teardown(self, tmp_path, monkeypatch, capsys):
        config, calls, destroy_calls, _external = _prepare_qualify(
            tmp_path, monkeypatch, success=False
        )
        assert main(qual_argv(config)) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["stopped"] is True
        assert calls[0].tasks_per_repetition == 20
        assert destroy_calls == []

    def test_config_rejects_wrong_stage_size(self):
        config = qualification_config_dict("C1", "development")
        config["tasks_per_repetition"] = 21
        with pytest.raises(ConfigError, match="must equal 20"):
            validate_authorized_qualification_config(config, candidate_id="C1", stage="development")
