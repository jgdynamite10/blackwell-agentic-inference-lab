"""Focused offline tests for the D-0019 agent-quality qualification gate."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
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
    AUTHORIZED_CANDIDATES,
    C1_TEMPERATURE,
    C2_TEMPERATURE,
    CANDIDATE_P1,
    CATALOG_WORKLOAD_VERSION,
    DEVELOPMENT_QUALITY_FLOOR,
    DEVELOPMENT_TASKS,
    DEVELOPMENT_TEMPLATE_IDS,
    FORBIDDEN_IDENTITY_MARKERS,
    FREEZE_TASKS,
    FREEZE_WARMUP_PASSES,
    HOLDOUT_MUST_NOT_REVISE_WORDING,
    HOLDOUT_QUALITY_FLOOR,
    HOLDOUT_TASKS,
    HOLDOUT_TEMPLATE_IDS,
    MAX_INTERACTIVE_E2E_P95_MS,
    MAX_INTERACTIVE_TTFT_P95_MS,
    MAX_INVALID_ARGUMENT_RATE,
    MAX_INVALID_TOOL_NAME_RATE,
    MAX_REQUEST_INFERENCE_ERROR_RATE,
    MAX_TIMEOUTS,
    MIN_VALID_NATIVE_TOOL_CALL_RATE,
    P1_WORKLOAD_VERSION,
    PRODUCTION_LIKE_MIN_AGGREGATE,
    PRODUCTION_LIKE_MIN_SCENARIO,
    PROMPT_VARIANT_P1,
    QUALIFICATION_APPROVAL_TEMPLATE,
    QUALIFICATION_ARTIFACT_FAMILY,
    QUALIFICATION_WORKLOAD_VERSION,
    SCREEN_WARMUP_PASSES,
    SPLIT_SEED,
    STRUCTURAL_TOOL_FAILURES,
    STUDY_ENTRY_MIN_AGGREGATE,
    STUDY_ENTRY_MIN_SCENARIO,
    QualificationError,
    approval_phrase,
    candidate_controller,
    candidate_identity_digest,
    candidate_temperature,
    candidate_workload_version,
    compute_qualification_metrics,
    evaluate_production_like_target,
    evaluate_stage_thresholds,
    evaluate_study_entry_gate,
    expected_stage_distribution,
    experimental_behavior_fields,
    experimental_configuration_digest,
    freeze_template_split,
    frozen_candidate_fields,
    refuse_mvl_identities,
    require_approval,
    require_complete_stage_evidence,
    sanitized_receipt,
    serialize_candidate,
    stage_spec,
    validate_authorized_qualification_config,
    validate_stage_request,
)
from blackwell_lab.workload.agent import (
    SYSTEM_PROMPT_V230,
    SYSTEM_PROMPT_V240,
    SYSTEM_PROMPT_V241,
    run_task,
    system_prompt,
    task_prompt,
)
from blackwell_lab.workload.evaluator import EVALUATOR_VERSION, QUALITY_THRESHOLD
from blackwell_lab.workload.model_client import (
    DeterministicMockClient,
    GenerationSettings,
    StreamEvent,
)
from blackwell_lab.workload.native_tools import (
    TOOL_DESCRIPTIONS,
    TOOL_DESCRIPTIONS_V230,
    TOOL_DESCRIPTIONS_V240,
    openai_tool_definitions,
    require_workload_version,
    tool_descriptions,
)
from blackwell_lab.workload.scenarios import WORKLOAD_VERSION, catalog
from blackwell_lab.workload.tools import SimulatedToolbox
from blackwell_lab.workload.validation import ConfigError

COMMIT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
RUN_TAG = "p3-qual-20260918a"
FROZEN_ACCEPTED_ANSWERS_SHA256 = "2edb7134040af2e8e9e9fec4c068bbc0dfaf6bfa55cbd4284b8bf6045c7aea2a"
FROZEN_C1_IDENTITY_SHA256 = "ce95fe585d24bd427aa8cd470f089c051eed6af3ce42ded7bea21bd3c93f7f7c"
FROZEN_C2_IDENTITY_SHA256 = "99ca5e56fd9dc37de069e23c48a08f6b95dd0256f05416eff12791092adfcba9"
FROZEN_SYSTEM_PROMPT_V240 = (
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
    "When an incident may depend on log evidence, gather that evidence "
    "with search_logs before recommending remediation. "
    "Submit exactly one recommendation via recommend_remediation."
)
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
    tool_trace: list[dict] | None = None,
) -> dict:
    if tool_trace is None:
        if error_category in STRUCTURAL_TOOL_FAILURES or error_category in {
            "endpoint_error",
            "task_timeout",
        }:
            tool_trace = []
        else:
            tool_trace = [{"tool": "recommend_remediation"}]
    return {
        "template_id": template_id,
        "success": success,
        "evaluation": {"success": success, "evaluator_version": EVALUATOR_VERSION},
        "error_category": error_category,
        "e2e_ms": e2e_ms,
        "ttft_ms": ttft_ms,
        "turns": [{"ttft_ms": ttft_ms}],
        "tool_trace": tool_trace,
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
        "workload_version": candidate_workload_version(candidate_id),
        "endpoint": {"base_url": "http://127.0.0.1:8000/v1", "model": "m"},
        "cloud": {
            "instance_type": "g3-gpu-rtxpro6000-blackwell-1",
            "region": "us-iad-2",
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
                "region": "us-iad-2",
                "label": "bwlab-gpu-baseline",
            },
            {
                "address": "linode_firewall.gpu_baseline",
                "type": "linode_firewall",
                "provider_id": "555",
                "region": "",
                "label": "bwlab-gpu-baseline",
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
            "region": "us-iad-2",
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

        generic = system_prompt(next(iter(catalog().values())), "2.4.0")
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
        description = tool_descriptions("2.4.0")["retrieve_runbook"]
        assert "service or system" in description
        assert "remediation_ids" in description
        assert "found=false" in description
        schema = next(
            entry["function"]
            for entry in openai_tool_definitions("2.4.0")
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
        description = tool_descriptions("2.4.0")["recommend_remediation"]
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
        assert "log-dependent" in tool_descriptions("2.4.0")["search_logs"]
        assert "search_logs" in system_prompt(next(iter(catalog().values())), "2.4.0")
        for scenario_id in LOG_DEPENDENT_SCENARIOS:
            scenario = catalog()[scenario_id]
            tools = [step["tool"] for step in scenario.reference_tool_sequence]
            assert "search_logs" in tools, scenario_id


class _RecordingClient(DeterministicMockClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen: list[tuple] = []

    def stream_turn(self, messages, settings, **kwargs):
        self.seen.append((list(messages), settings))
        yield from super().stream_turn(messages, settings, **kwargs)
        yield StreamEvent(kind="usage", output_tokens=23)


class TestVersionBoundContracts:
    def test_catalog_default_is_the_legacy_2_3_0_contract(self):
        scenario = next(iter(catalog().values()))
        assert require_workload_version(None) == "2.3.0"
        assert TOOL_DESCRIPTIONS == TOOL_DESCRIPTIONS_V230
        assert system_prompt(scenario) == SYSTEM_PROMPT_V230
        assert openai_tool_definitions() == openai_tool_definitions("2.3.0")

    def test_contracts_are_isolated_and_fail_closed(self):
        scenario = next(iter(catalog().values()))
        prompt_230 = system_prompt(scenario, "2.3.0")
        prompt_240 = system_prompt(scenario, "2.4.0")
        assert prompt_230 == SYSTEM_PROMPT_V230
        assert prompt_240 == SYSTEM_PROMPT_V240
        assert prompt_230 != prompt_240
        assert "search_logs" not in prompt_230
        assert "Required workflow" not in prompt_230
        assert "search_logs" in prompt_240
        assert "Required workflow" in prompt_240
        assert "log-dependent" not in TOOL_DESCRIPTIONS_V230["search_logs"]
        assert "service or system" not in TOOL_DESCRIPTIONS_V230["retrieve_runbook"]
        assert "retrieve_runbook" not in TOOL_DESCRIPTIONS_V230["recommend_remediation"]
        assert "log-dependent" in TOOL_DESCRIPTIONS_V240["search_logs"]
        assert "service or system" in TOOL_DESCRIPTIONS_V240["retrieve_runbook"]
        assert "retrieve_runbook" in TOOL_DESCRIPTIONS_V240["recommend_remediation"]
        defs_230 = {entry["function"]["name"]: entry for entry in openai_tool_definitions("2.3.0")}
        defs_240 = {entry["function"]["name"]: entry for entry in openai_tool_definitions("2.4.0")}
        for name in ("search_logs", "retrieve_runbook", "recommend_remediation"):
            assert defs_230[name]["function"]["description"] == TOOL_DESCRIPTIONS_V230[name]
            assert defs_240[name]["function"]["description"] == TOOL_DESCRIPTIONS_V240[name]
            assert (
                defs_230[name]["function"]["description"]
                != defs_240[name]["function"]["description"]
            )
        with pytest.raises(ConfigError, match="unknown workload version"):
            require_workload_version("9.9.9")
        with pytest.raises(ConfigError, match="unknown workload version"):
            openai_tool_definitions("9.9.9")
        with pytest.raises(ConfigError, match="unknown workload version"):
            GenerationSettings(workload_version="9.9.9")

    def test_run_task_and_openai_request_execute_the_selected_contract(self):
        from fakes import FakeClock

        from blackwell_lab.workload.openai_client import OpenAICompatibleClient

        scenario = catalog()["pod-failures-001"]
        openai = OpenAICompatibleClient("http://127.0.0.1:8000/v1", "nemotron")
        for version, expected_prompt, expected_tools in (
            ("2.3.0", SYSTEM_PROMPT_V230, openai_tool_definitions("2.3.0")),
            ("2.4.0", SYSTEM_PROMPT_V240, openai_tool_definitions("2.4.0")),
        ):
            client = _RecordingClient()
            toolbox = SimulatedToolbox(scenario, clock=FakeClock())
            run_task(
                scenario,
                client,
                toolbox,
                GenerationSettings(workload_version=version),
                timeout_s=30.0,
                clock=FakeClock(),
            )
            assert client.seen
            messages, settings = client.seen[0]
            assert settings.workload_version == version
            assert messages[0].role == "system"
            assert messages[0].content == expected_prompt
            other_prompt = SYSTEM_PROMPT_V240 if version == "2.3.0" else SYSTEM_PROMPT_V230
            assert messages[0].content != other_prompt
            body = openai._request_body(
                messages,
                GenerationSettings(max_tokens=64, workload_version=version),
            )
            assert body["tools"] == expected_tools
            leaked = openai_tool_definitions("2.4.0" if version == "2.3.0" else "2.3.0")
            assert body["tools"] != leaked


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

    def test_unknown_candidate_is_rejected(self):
        with pytest.raises(ConfigError, match="C1, C2, P1, P2, or P2C"):
            frozen_candidate_fields("C3")
        with pytest.raises(ConfigError, match="C1, C2, P1, P2, or P2C"):
            candidate_temperature("C3")
        with pytest.raises(ConfigError, match="C1, C2, P1, P2, or P2C"):
            candidate_workload_version("C3")
        with pytest.raises(ConfigError, match="C1, C2, P1, P2, or P2C"):
            candidate_controller("C3")


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


class TestStageEvidenceCompleteness:
    def test_complete_development_schedule_is_accepted(self):
        outcomes = _stage_outcomes("development")
        require_complete_stage_evidence("development", outcomes)
        assert expected_stage_distribution("development") == {
            outcome["template_id"]: sum(
                1 for item in outcomes if item["template_id"] == outcome["template_id"]
            )
            for outcome in outcomes
        }

    def test_missing_template_fails_closed_and_does_not_vanish_from_quality(self):
        spec = stage_spec("development")
        missing_id = spec["template_ids"][0]
        outcomes = [
            outcome
            for outcome in _stage_outcomes("development")
            if outcome["template_id"] != missing_id
        ]
        with pytest.raises(QualificationError, match="exactly 20"):
            require_complete_stage_evidence("development", outcomes)
        metrics = compute_qualification_metrics(
            outcomes,
            provenance_ok=True,
            verification_ok=True,
            expected_template_ids=spec["template_ids"],
        )
        assert missing_id in metrics.scenario_quality
        assert metrics.scenario_quality[missing_id] == 0.0
        gate = evaluate_study_entry_gate(metrics)
        assert gate["checks"]["every_scenario"] is False
        assert missing_id in gate["scenario_failures"]

    def test_duplicate_replacement_fails_closed(self):
        outcomes = _stage_outcomes("development")
        donor, extra = stage_spec("development")["template_ids"][:2]
        mutated = []
        for outcome in outcomes:
            if outcome["template_id"] == donor:
                mutated.append({**outcome, "template_id": extra})
            else:
                mutated.append(outcome)
        assert len(mutated) == 20
        assert donor not in {item["template_id"] for item in mutated}
        with pytest.raises(QualificationError, match="missing expected templates"):
            require_complete_stage_evidence("development", mutated)

    def test_unexpected_template_fails_closed(self):
        outcomes = _stage_outcomes("development")
        unexpected = HOLDOUT_TEMPLATE_IDS[0]
        mutated = [{**outcomes[0], "template_id": unexpected}, *outcomes[1:]]
        with pytest.raises(QualificationError, match="unexpected template"):
            require_complete_stage_evidence("development", mutated)

    def test_blank_template_fails_closed(self):
        outcomes = _stage_outcomes("development")
        mutated = [{**outcomes[0], "template_id": ""}, *outcomes[1:]]
        with pytest.raises(QualificationError, match="blank template"):
            require_complete_stage_evidence("development", mutated)

    def test_wrong_distribution_fails_closed(self):
        templates = list(stage_spec("development")["template_ids"])
        expected = expected_stage_distribution("development")
        first, second = templates[0], templates[1]
        mutated = []
        swapped = False
        for outcome in _stage_outcomes("development"):
            if not swapped and outcome["template_id"] == first:
                mutated.append({**outcome, "template_id": second})
                swapped = True
            else:
                mutated.append(outcome)
        assert Counter(item["template_id"] for item in mutated) != expected
        assert set(item["template_id"] for item in mutated) == set(templates)
        with pytest.raises(QualificationError, match="frozen per-template distribution"):
            require_complete_stage_evidence("development", mutated)


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
        assert _metrics(passing).valid_native_tool_call_rate == 0.99
        assert _metrics(failing).valid_native_tool_call_rate == 0.98
        assert evaluate_study_entry_gate(_metrics(passing))["passed"] is True
        assert evaluate_study_entry_gate(_metrics(failing))["passed"] is False

    def test_tool_call_rate_counts_turns_not_tasks(self):
        multi_turn = [_outcome(tool_trace=[{"tool": "search_logs"}] * 2)] * 49 + [
            _outcome(success=False, error_category="malformed_tool_call")
        ]
        multi_metrics = _metrics(multi_turn)
        assert multi_metrics.valid_native_tool_call_rate == 98 / 99
        assert (
            evaluate_study_entry_gate(multi_metrics)["checks"]["valid_native_tool_call_rate"]
            is False
        )
        one_task_many_valid = [
            _outcome(tool_trace=[{"tool": "search_logs"}] * 99),
            _outcome(success=False, error_category="malformed_tool_call"),
        ]
        turn_metrics = _metrics(one_task_many_valid)
        assert turn_metrics.valid_native_tool_call_rate == 0.99
        assert (
            evaluate_study_entry_gate(turn_metrics)["checks"]["valid_native_tool_call_rate"] is True
        )

    def test_endpoint_failures_are_not_tool_call_attempts(self):
        outcomes = [_outcome()] * 99 + [_outcome(success=False, error_category="endpoint_error")]
        metrics = _metrics(outcomes)
        assert metrics.valid_native_tool_call_rate == 1.0
        assert metrics.request_inference_error_rate == 0.01

    def test_zero_tool_call_attempts_fail_closed(self):
        empty = [
            _outcome(success=False, error_category="endpoint_error"),
            _outcome(success=False, error_category="task_timeout"),
        ]
        with pytest.raises(QualificationError, match="no native tool-call attempts"):
            _metrics(empty)

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
        assert calls[0].generation.workload_version == "2.4.0"
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


def _differing_paths(left: object, right: object, prefix: str = "") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        paths: list[str] = []
        for key in sorted(set(left) | set(right)):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in left or key not in right:
                paths.append(path)
            else:
                paths.extend(_differing_paths(left[key], right[key], path))
        return paths
    if left != right:
        return [prefix]
    return []


def _catalog_answer_tokens() -> set[str]:
    """Scenario-specific strings that must not appear in a generic prompt."""
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
        for key, runbook in scenario.runbooks.items():
            add(key)
            add(runbook.get("title"))
            for step in runbook.get("steps") or ():
                add(step)
            for remediation_id in runbook.get("remediation_ids") or ():
                add(remediation_id)
        for predicate in scenario.evidence_predicates:
            add(predicate.predicate_id)
            add(predicate.description)
            for alternative in predicate.alternatives:
                for _argument, needle in alternative.argument_contains:
                    add(needle)
                result = alternative.result
                for attr in (
                    "text",
                    "change_id",
                    "remediation_id",
                    "metric",
                    "component",
                    "status",
                ):
                    if hasattr(result, attr):
                        add(getattr(result, attr))
        for sequence in (scenario.reference_tool_sequence, scenario.alternative_tool_sequence):
            for step in sequence:
                for value in (step.get("arguments") or {}).values():
                    add(value)
    return tokens


class TestWorkload241Prompt:
    def test_2_4_0_prompt_is_byte_for_byte_unchanged(self):
        scenario = next(iter(catalog().values()))
        assert SYSTEM_PROMPT_V240 == FROZEN_SYSTEM_PROMPT_V240
        assert system_prompt(scenario, "2.4.0") == FROZEN_SYSTEM_PROMPT_V240
        for other in catalog().values():
            assert system_prompt(other, "2.4.0") == FROZEN_SYSTEM_PROMPT_V240

    def test_2_4_1_replaces_only_the_evidence_instruction(self):
        scenario = next(iter(catalog().values()))
        prompt = system_prompt(scenario, "2.4.1")
        assert prompt == SYSTEM_PROMPT_V241
        old = (
            "When an incident may depend on log evidence, gather that evidence "
            "with search_logs before recommending remediation."
        )
        assert SYSTEM_PROMPT_V240.count(old) == 1
        prefix, suffix = SYSTEM_PROMPT_V240.split(old, 1)
        assert prompt.startswith(prefix)
        assert prompt.endswith(suffix)
        replacement = prompt[len(prefix) : len(prompt) - len(suffix)]
        assert replacement == (
            "search_logs uses literal substring matching, not semantic search. "
            "Search queries should use exact identifiers, service names, "
            "configuration IDs, job IDs, or diagnostic terms supported by "
            "information already available to the agent. "
            "A zero-match search must be retried with a different specific "
            "token before making a terminal recommendation. "
            "Gather direct supporting evidence for the diagnosis before "
            "submitting the terminal recommendation. "
            "Seeing a plausible change record or runbook remediation is not "
            "a substitute for collecting the required incident evidence."
        )
        assert old not in prompt
        assert "2.4.0" not in prompt
        assert "2.4.1" not in prompt

    def test_new_prompt_contains_no_scenario_answers(self):
        prompt = SYSTEM_PROMPT_V241.casefold()
        leaked = sorted(token for token in _catalog_answer_tokens() if token.casefold() in prompt)
        assert leaked == []
        for forbidden in (
            "predicate",
            "total_matches",
            "argument_contains",
            "accepted_diagnoses",
            "accepted_remediations",
        ):
            assert forbidden not in prompt

    def test_run_task_selects_the_version_bound_prompt_and_fails_closed(self):
        from fakes import FakeClock

        from blackwell_lab.workload.openai_client import OpenAICompatibleClient

        scenario = catalog()["pod-failures-001"]
        openai = OpenAICompatibleClient("http://127.0.0.1:8000/v1", "nemotron")
        client = _RecordingClient()
        toolbox = SimulatedToolbox(scenario, clock=FakeClock())
        run_task(
            scenario,
            client,
            toolbox,
            GenerationSettings(workload_version="2.4.1"),
            timeout_s=30.0,
            clock=FakeClock(),
        )
        messages, settings = client.seen[0]
        assert settings.workload_version == "2.4.1"
        assert messages[0].content == SYSTEM_PROMPT_V241
        assert messages[0].content != SYSTEM_PROMPT_V240
        body = openai._request_body(
            messages,
            GenerationSettings(max_tokens=64, workload_version="2.4.1"),
        )
        assert body["messages"][0]["content"] == SYSTEM_PROMPT_V241
        assert body["tools"] == openai_tool_definitions("2.4.0")
        assert body["tools"] == openai_tool_definitions("2.4.1")
        spy = _RecordingClient()
        with pytest.raises(ConfigError, match="unknown workload version"):
            run_task(
                scenario,
                spy,
                toolbox,
                GenerationSettings(workload_version="9.9.9"),
                timeout_s=30.0,
                clock=FakeClock(),
            )
        assert spy.seen == []


class TestPromptVariantP1:
    def test_p1_differs_from_c2_only_in_identity_version_prompt_and_digest(self):
        c2 = experimental_behavior_fields("C2")
        p1 = experimental_behavior_fields("P1")
        assert _differing_paths(c2, p1) == [
            "candidate_id",
            "system_prompt",
            "workload_version",
        ]
        assert c2["candidate_id"] == "C2"
        assert p1["candidate_id"] == PROMPT_VARIANT_P1 == "P1"
        assert c2["workload_version"] == "2.4.0"
        assert p1["workload_version"] == P1_WORKLOAD_VERSION == "2.4.1"
        assert c2["system_prompt"] == SYSTEM_PROMPT_V240
        assert p1["system_prompt"] == SYSTEM_PROMPT_V241
        assert p1["generation"]["temperature"] == C2_TEMPERATURE == 0.2
        assert c2["generation"]["temperature"] == 0.2
        assert experimental_configuration_digest("P1") != experimental_configuration_digest("C2")

    def test_controls_other_than_the_prompt_match_c2(self):
        c2 = experimental_behavior_fields("C2")
        p1 = experimental_behavior_fields("P1")
        assert p1["generation"]["top_p"] == c2["generation"]["top_p"] == 0.95
        assert p1["generation"]["seed"] == c2["generation"]["seed"] == 20260906
        assert p1["generation"]["max_tokens"] == c2["generation"]["max_tokens"] == 1024
        assert p1["generation"]["reasoning_mode"] is True
        assert p1["model"] == c2["model"]
        assert p1["serving"] == c2["serving"]
        assert p1["serving"]["tool_call_transport"] == "openai-native-tools"
        assert tool_descriptions("2.4.1") == tool_descriptions("2.4.0") == TOOL_DESCRIPTIONS_V240
        assert tool_descriptions("2.4.1") is TOOL_DESCRIPTIONS_V240
        assert openai_tool_definitions("2.4.1") == openai_tool_definitions("2.4.0")
        assert EVALUATOR_VERSION == "3.1.0"
        assert QUALITY_THRESHOLD == 1.0
        assert WORKLOAD_VERSION == "2.3.0"
        assert DEVELOPMENT_TASKS == HOLDOUT_TASKS == 20
        assert FREEZE_TASKS == 200
        assert SCREEN_WARMUP_PASSES == 0
        assert FREEZE_WARMUP_PASSES == 1
        assert stage_spec("development")["template_ids"] == DEVELOPMENT_TEMPLATE_IDS
        assert stage_spec("holdout")["template_ids"] == HOLDOUT_TEMPLATE_IDS
        assert stage_spec("development")["warmup_passes"] == 0
        assert stage_spec("holdout")["warmup_passes"] == 0
        assert stage_spec("freeze")["warmup_passes"] == 1
        assert stage_spec("development")["tasks"] == 20
        assert stage_spec("holdout")["tasks"] == 20
        assert stage_spec("freeze")["tasks"] == 200
        assert DEVELOPMENT_QUALITY_FLOOR == 0.40
        assert HOLDOUT_QUALITY_FLOOR == 0.50
        assert STUDY_ENTRY_MIN_AGGREGATE == 0.70
        assert STUDY_ENTRY_MIN_SCENARIO == 0.40
        assert PRODUCTION_LIKE_MIN_AGGREGATE == 0.90
        assert PRODUCTION_LIKE_MIN_SCENARIO == 0.80
        assert MIN_VALID_NATIVE_TOOL_CALL_RATE == 0.99
        assert MAX_INVALID_TOOL_NAME_RATE == 0.0
        assert MAX_INVALID_ARGUMENT_RATE == 0.01
        assert MAX_REQUEST_INFERENCE_ERROR_RATE == 0.01
        assert MAX_TIMEOUTS == 0
        assert MAX_INTERACTIVE_TTFT_P95_MS == 2_500.0
        assert MAX_INTERACTIVE_E2E_P95_MS == 60_000.0
        assert hashlib.sha256(_accepted_answers_payload()).hexdigest() == (
            FROZEN_ACCEPTED_ANSWERS_SHA256
        )

    def test_c1_and_c2_identity_records_are_unchanged(self):
        assert CANDIDATE_P1 in AUTHORIZED_CANDIDATES
        assert PROMPT_VARIANT_P1 in AUTHORIZED_CANDIDATES
        assert candidate_identity_digest("C1") == FROZEN_C1_IDENTITY_SHA256
        assert candidate_identity_digest("C2") == FROZEN_C2_IDENTITY_SHA256
        c1 = json.loads(serialize_candidate("C1"))
        c2 = json.loads(serialize_candidate("C2"))
        p1 = json.loads(serialize_candidate("P1"))
        assert "system_prompt" not in c1
        assert "system_prompt" not in c2
        assert "system_prompt" not in p1
        assert c1["workload_version"] == c2["workload_version"] == "2.4.0"
        assert p1["workload_version"] == "2.4.1"
        assert c1["generation"]["temperature"] == 1.0
        assert c2["generation"]["temperature"] == p1["generation"]["temperature"] == 0.2
        assert _differing_paths(c2, p1) == ["candidate_id", "workload_version"]
        assert candidate_identity_digest("P1") != candidate_identity_digest("C2")
        assert candidate_identity_digest("P1") != experimental_configuration_digest("P1")
        assert QUALIFICATION_WORKLOAD_VERSION == "2.4.0"
        held = qualification_config_dict("C2")
        held["workload_version"] = "2.4.1"
        with pytest.raises(ConfigError, match=r"2\.4\.0"):
            validate_authorized_qualification_config(held, candidate_id="C2", stage="development")
        c1_wrong = qualification_config_dict("C1")
        c1_wrong["workload_version"] = "2.4.1"
        with pytest.raises(ConfigError, match=r"2\.4\.0"):
            validate_authorized_qualification_config(
                c1_wrong, candidate_id="C1", stage="development"
            )
        p1_wrong = qualification_config_dict("P1")
        p1_wrong["workload_version"] = "2.4.0"
        with pytest.raises(ConfigError, match=r"2\.4\.1"):
            validate_authorized_qualification_config(
                p1_wrong, candidate_id="P1", stage="development"
            )

    def test_p1_validates_for_every_stage_on_workload_241(self):
        for stage in ("development", "holdout", "freeze"):
            config = qualification_config_dict("P1", stage)
            validate_authorized_qualification_config(config, candidate_id="P1", stage=stage)
            assert config["candidate_id"] == "P1"
            assert config["workload_version"] == "2.4.1"
            assert config["generation"]["temperature"] == 0.2
            assert config["tasks_per_repetition"] == stage_spec(stage)["tasks"]
            assert config["warmup_passes"] == stage_spec(stage)["warmup_passes"]
        assert stage_spec("development")["tasks"] == 20
        assert stage_spec("development")["warmup_passes"] == 0
        assert stage_spec("holdout")["tasks"] == 20
        assert stage_spec("freeze")["tasks"] == 200
        assert stage_spec("freeze")["warmup_passes"] == 1

    def test_qualify_agent_binds_p1_before_inference(self, tmp_path, monkeypatch, capsys):
        from fakes import FakeClock

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
            cell = external / "qualification-runs" / "qual-a-p1-development"
            cell.mkdir(parents=True, exist_ok=True)
            (cell / "p1.result.json").write_text("{}\n", encoding="utf-8")
            return [_FakeRecord(run_id="qual", outcomes=_stage_outcomes("development"))]

        monkeypatch.setattr(provenance, "verify_live_provenance", lambda **kwargs: _observed())
        monkeypatch.setattr(realbench, "run_real_cell", fake_run)
        config = qualification_config_dict("P1", "development")
        path = tmp_path / "p1.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        phrase = approval_phrase(RUN_TAG, "qual-a", "P1", config_sha256(path))
        assert "candidate P1" in phrase
        assert main(qual_argv(path, candidate="P1", approve=phrase)) == 0
        spec = calls[0]
        assert spec.generation.temperature == 0.2
        assert spec.generation.top_p == 0.95
        assert spec.generation.seed == 20260906
        assert spec.generation.workload_version == "2.4.1"
        assert spec.workload_version == "2.4.1"
        assert spec.run_label == "qual-a-p1-development"
        assert spec.tasks_per_repetition == 20
        assert spec.warmup_passes == 0
        assert spec.template_ids == DEVELOPMENT_TEMPLATE_IDS
        report = json.loads(capsys.readouterr().out)
        assert report["candidate_id"] == "P1"
        assert report["workload_version"] == "2.4.1"
        assert report["stage"] == "development"
        assert report["candidate_identity_sha256"] == candidate_identity_digest("P1")
        assert report["config_sha256"] == config_sha256(path)
        assert report["config_sha256"] != experimental_configuration_digest("P1")
        on_disk = json.loads(
            (external / "qualification-runs" / "qual-a-p1-development-receipt.json").read_text(
                encoding="utf-8"
            )
        )
        assert on_disk["candidate_id"] == "P1"
        assert on_disk["workload_version"] == "2.4.1"
        control = json.loads(
            (external / "qualification-runs" / "qual-a-p1-development-control.json").read_text(
                encoding="utf-8"
            )
        )
        assert control["region"] == "us-iad-2"
        assert control["precision"] == "bf16"
        assert control["stopped"] is False
        assert control["terminal_event"] == "qualification_completed"
        assert control["candidate_id"] == "P1"
        assert control["failure_record"] is False
        assert (
            control["receipt_sha256"]
            == hashlib.sha256(
                (
                    external / "qualification-runs" / "qual-a-p1-development-receipt.json"
                ).read_bytes()
            ).hexdigest()
        )
        session = json.loads(paths.session_path.read_text(encoding="utf-8"))
        assert any(
            event["event"] == "qualification_completed"
            and event["detail"]["candidate_id"] == "P1"
            and event["detail"]["stopped"] is False
            for event in session["events"]
        )
        rendered = json.dumps(control)
        assert str(external) not in rendered
        assert "provider_id" not in rendered
        assert '"42"' not in rendered
        scenario = catalog()["pod-failures-001"]
        recording = _RecordingClient()
        run_task(
            scenario,
            recording,
            SimulatedToolbox(scenario, clock=FakeClock()),
            spec.generation,
            timeout_s=30.0,
            clock=FakeClock(),
        )
        messages, settings = recording.seen[0]
        assert messages[0].content == SYSTEM_PROMPT_V241
        assert settings.workload_version == "2.4.1"
        assert settings.temperature == 0.2
        calls.clear()
        mismatched = qualification_config_dict("P1", "development")
        mismatched["workload_version"] = "9.9.9"
        bad_path = tmp_path / "bad.json"
        bad_path.write_text(json.dumps(mismatched), encoding="utf-8")
        assert main(qual_argv(bad_path, candidate="P1")) == 1
        assert calls == []

    def _drive_p1(self, tmp_path, monkeypatch):
        from blackwell_lab.cloud import provenance, realbench

        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        monkeypatch.setattr(cli, "_git_head", lambda: COMMIT)
        monkeypatch.setattr(cli, "_tree_clean", lambda: True)
        external = tmp_path / "external"
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        paths = lifecycle.lifecycle_paths(external, RUN_TAG)
        lifecycle.write_private_json(paths.ledger_path, ready_ledger())

        def fake_run(spec, *_args, **_kwargs):
            cell = external / "qualification-runs" / "qual-a-p1-development"
            cell.mkdir(parents=True, exist_ok=True)
            (cell / "p1.result.json").write_text("{}\n", encoding="utf-8")
            return [_FakeRecord(run_id="qual", outcomes=_stage_outcomes("development"))]

        monkeypatch.setattr(provenance, "verify_live_provenance", lambda **kwargs: _observed())
        monkeypatch.setattr(realbench, "run_real_cell", fake_run)
        config = qualification_config_dict("P1", "development")
        path = tmp_path / "p1.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        phrase = approval_phrase(RUN_TAG, "qual-a", "P1", config_sha256(path))
        return external, paths, path, phrase

    def test_stopped_gate_does_not_mint_a_control(self, tmp_path, monkeypatch):
        from blackwell_lab.cloud import qualification as qualification_mod

        external, paths, path, phrase = self._drive_p1(tmp_path, monkeypatch)
        monkeypatch.setattr(
            qualification_mod,
            "evaluate_stage_thresholds",
            lambda stage, metrics: {
                "stage": stage,
                "continue": False,
                "stopped": True,
                "quality_floor": 0.4,
                "aggregate_quality": 0.0,
            },
        )
        assert main(qual_argv(path, candidate="P1", approve=phrase)) == 1
        family = external / "qualification-runs"
        assert not (family / "qual-a-p1-development-control.json").exists()
        receipt = json.loads(
            (family / "qual-a-p1-development-receipt.json").read_text(encoding="utf-8")
        )
        assert receipt["stopped"] is True
        assert receipt["gates"]["stopped"] is True
        session = json.loads(paths.session_path.read_text(encoding="utf-8"))
        names = [event["event"] for event in session["events"]]
        assert "qualification_stopped" in names
        assert "qualification_completed" not in names

    def test_control_is_absent_when_persistence_stops_between_artifacts(
        self, tmp_path, monkeypatch
    ):
        from blackwell_lab.cloud import qualification as qualification_mod

        external, paths, path, phrase = self._drive_p1(tmp_path, monkeypatch)
        family = external / "qualification-runs"
        control = family / "qual-a-p1-development-control.json"
        receipt = family / "qual-a-p1-development-receipt.json"

        def fail_receipt(**kwargs):
            raise QualificationError("receipt failed")

        monkeypatch.setattr(qualification_mod, "sanitized_receipt", fail_receipt)
        assert main(qual_argv(path, candidate="P1", approve=phrase)) == 1
        assert (family / "qual-a-p1-development" / "p1.result.json").is_file()
        assert not receipt.exists()
        assert not control.exists()
        session = json.loads(paths.session_path.read_text(encoding="utf-8"))
        assert [event["event"] for event in session["events"]] == ["qualification_started"]

        monkeypatch.undo()
        external, paths, path, phrase = self._drive_p1(tmp_path, monkeypatch)
        real_event = lifecycle.record_session_event

        def fail_terminal(paths_arg, event, detail=None):
            if event in {"qualification_completed", "qualification_stopped"}:
                raise QualificationError("event failed")
            real_event(paths_arg, event, detail)

        monkeypatch.setattr(lifecycle, "record_session_event", fail_terminal)
        assert main(qual_argv(path, candidate="P1", approve=phrase)) == 1
        assert receipt.is_file()
        assert not control.exists()
        session = json.loads(paths.session_path.read_text(encoding="utf-8"))
        assert "qualification_completed" not in [event["event"] for event in session["events"]]

        monkeypatch.undo()
        external, paths, path, phrase = self._drive_p1(tmp_path, monkeypatch)

        def fail_persist(**kwargs):
            raise QualificationError("persist failed")

        monkeypatch.setattr(
            "blackwell_lab.cloud.matched_control.persist_completed_p1_development_control",
            fail_persist,
        )
        assert main(qual_argv(path, candidate="P1", approve=phrase)) == 1
        assert receipt.is_file()
        assert not control.exists()
        session = json.loads(paths.session_path.read_text(encoding="utf-8"))
        assert any(event["event"] == "qualification_completed" for event in session["events"])
