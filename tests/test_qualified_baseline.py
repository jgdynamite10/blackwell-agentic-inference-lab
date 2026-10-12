"""Offline native-loop integration and qualification-to-measurement refusal.

The scripted client is a test oracle, not a model-quality measurement. It
uses evaluator fixtures only after metadata authoring, to test the unchanged
evaluator and artifact verification path. No genuine inference is performed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from fakes import FakeClock
from test_mvl_baseline import mvl_config_dict, ready_ledger
from test_realbench import HOST, FakeSampler, make_spec

from blackwell_lab.cloud import qualification, qualified_mvl, realbench
from blackwell_lab.cloud.artifacts import write_private_json
from blackwell_lab.cloud.matched_control import resource_identity_sha256
from blackwell_lab.workload.catalog_agent import run_task
from blackwell_lab.workload.catalog_runtime import CatalogToolbox
from blackwell_lab.workload.model_client import GenerationSettings, NativeToolCall, StreamEvent
from blackwell_lab.workload.public_metadata.catalog_v2 import evidence_contract
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.validation import ConfigError

COMMIT = "ab" * 20
RUN_TAG = "w7-test-20261012a"


class OracleClient:
    """Exercises transport/controller/evaluator wiring with synthetic fixture answers."""

    def stream_turn(self, messages, settings, *, deadline=None, clock=None):
        ident = messages[1].content.splitlines()[0].removeprefix("scenario_id: ")
        scenario = catalog()[ident]
        sequence = [dict(step) for step in scenario.reference_tool_sequence]
        sequence.extend(
            {"tool": "search_logs", "arguments": {"query": line["message"]}}
            for line in scenario.logs
            if line["level"] != "INFO"
        )
        sequence.append({"tool": "get_service_health", "arguments": {}})
        sequence.append(
            {
                "tool": "recommend_remediation",
                "arguments": {
                    "diagnosis_id": scenario.accepted_diagnoses[0],
                    "remediation_id": scenario.accepted_remediations[0],
                    "rationale": "Synthetic test oracle: all required observations were retrieved.",
                },
            }
        )
        assert len(sequence) <= 12
        index = sum(message.role == "tool" for message in messages)
        step = sequence[index]
        clock.sleep(0.01)
        yield StreamEvent(kind="native_tool_call_delta", text="test delta")
        yield StreamEvent(
            kind="native_tool_call",
            tool_call=NativeToolCall(f"call-{index}", step["tool"], step["arguments"]),
        )
        yield StreamEvent(kind="usage", output_tokens=20)


@pytest.mark.parametrize("ident", tuple(catalog()))
def test_native_agent_and_unchanged_evaluator_reach_all_templates(ident):
    from blackwell_lab.workload.evaluator import evaluate

    scenario = catalog()[ident]
    clock = FakeClock()
    execution = run_task(
        scenario,
        OracleClient(),
        CatalogToolbox(scenario, clock=clock),
        GenerationSettings(workload_version="2.9.0"),
        timeout_s=120,
        clock=clock,
    )
    assert execution.status == "completed"
    assert evaluate(scenario, execution).success
    assert len(execution.turns) <= 12


def test_persisted_ttft_is_read_and_missing_or_slow_timing_still_fails():
    rows = [
        {
            "template_id": "x",
            "evaluation": {"success": True},
            "e2e_ms": 100,
            "tool_trace": [{}],
            "turns": [{"ttft_ms": 50}, {"ttft_ms": 100}],
        }
    ]
    kwargs = dict(provenance_ok=True, verification_ok=True)
    metrics = qualification.compute_catalog_metrics(rows, **kwargs)
    assert metrics.interactive_ttft_p95_ms == 50
    assert qualification.evaluate_study_entry_gate(metrics)["passed"]
    rows[0]["turns"] = [{"ttft_ms": 2501}]
    assert not qualification.evaluate_study_entry_gate(
        qualification.compute_catalog_metrics(rows, **kwargs)
    )["passed"]
    rows[0]["turns"] = [{"ttft_ms": None}]
    assert not qualification.evaluate_study_entry_gate(
        qualification.compute_catalog_metrics(rows, **kwargs)
    )["passed"]


@pytest.fixture()
def completed_freeze(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(
        realbench,
        "_git_provenance",
        lambda: {
            "commit": COMMIT,
            "repository": "https://github.com/jgdynamite10/blackwell-agentic-inference-lab",
            "dirty": False,
        },
    )
    label = qualification.output_label("freeze-a", "freeze", "W7")
    config = mvl_config_dict(COMMIT)
    config.update(
        workflow="qualified-baseline",
        candidate_id="W7",
        workload_version="2.9.0",
        controller="workflow-controller-v3",
        expected_gpu_model=qualification.FROZEN_GPU,
        evidence_contract=evidence_contract(),
        candidate_identity_sha256=qualification.candidate_identity_digest("W7"),
        generation=qualification.frozen_candidate_fields("W7")["generation"],
    )
    spec = make_spec(
        model=config["model"],
        engine_version="0.27.1",
        instance_type=qualification.FROZEN_INSTANCE_TYPE,
        region="ca-central",
        container_digest=qualification.FROZEN_CONTAINER_DIGEST,
        list_price_usd_per_hour=3.0,
        profile_name="interactive",
        concurrency=1,
        workload_version="2.9.0",
        controller="workflow-controller-v3",
        generation=GenerationSettings(**dict(config["generation"], workload_version="2.9.0")),
        tasks_per_repetition=200,
        repetitions=1,
        warmup_passes=1,
        template_ids=qualification.stage_spec("freeze")["template_ids"],
        artifact_family="qualification-runs",
        run_label=label,
    )
    records = realbench.run_real_cell(
        spec,
        OracleClient(),
        host=HOST,
        sampler_factory=FakeSampler,
        results_dir=tmp_path,
        clock=FakeClock(),
    )
    metrics = qualification.compute_catalog_metrics(
        qualification.outcomes_from_records(records),
        provenance_ok=True,
        verification_ok=True,
        expected_template_ids=qualification.stage_spec("freeze")["template_ids"],
    )
    gates = qualification.evaluate_stage_thresholds("freeze", metrics)
    assert gates["stopped"] is False
    receipt = qualification.sanitized_receipt(
        run_label=label,
        candidate_id="W7",
        stage="freeze",
        config_sha256="12" * 32,
        identity_digest=config["candidate_identity_sha256"],
        gates=gates,
        files=records[0].written_files,
        stopped=False,
    )
    cell = tmp_path / "qualification-runs" / label
    ledger = ready_ledger(run_tag=RUN_TAG)
    for resource in ledger["resources"]:
        resource["label"] = "synthetic-test-resource"
    receipt["execution_context"] = {
        "run_tag": RUN_TAG,
        "canonical_commit": COMMIT,
        "resource_identity_sha256": resource_identity_sha256(ledger["resources"]),
    }
    receipt["artifact_sha256"] = {
        name: hashlib.sha256((cell / name).read_bytes()).hexdigest()
        for name in records[0].written_files
    }
    digest = write_private_json(cell.parent / f"{label}-receipt.json", receipt)
    config["qualification_binding"] = {"run_label": "freeze-a", "receipt_sha256": digest}
    return config, ledger, cell


def test_completed_freeze_authenticates_and_preserves_the_evidence_binding(
    completed_freeze, tmp_path
):
    config, ledger, _ = completed_freeze
    proof = qualified_mvl.authenticate_freeze(
        config, results_dir=tmp_path, run_tag=RUN_TAG, ledger=ledger
    )
    assert proof["study_entry_passed"] is True
    assert proof["candidate_identity_sha256"] == qualification.candidate_identity_digest("W7")
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(config))
    assert qualified_mvl.load_mvl_config(path)[0] == config


def test_modified_artifact_and_cross_session_ledger_are_refused(completed_freeze, tmp_path):
    config, ledger, cell = completed_freeze
    with pytest.raises(qualified_mvl.MvlError, match="another execution session"):
        qualified_mvl.authenticate_freeze(
            config, results_dir=tmp_path, run_tag="w7-another-20261012a", ledger=ledger
        )
    ledger["resources"][0]["provider_id"] = "different-synthetic-resource"
    with pytest.raises(qualified_mvl.MvlError, match="another execution session"):
        qualified_mvl.authenticate_freeze(
            config, results_dir=tmp_path, run_tag=RUN_TAG, ledger=ledger
        )
    ledger["resources"][0]["provider_id"] = "42"
    observation = next(cell.glob("*.observations.json"))
    observation.write_text(observation.read_text() + "\n")
    with pytest.raises(qualified_mvl.MvlError, match="artifact digest mismatch"):
        qualified_mvl.authenticate_freeze(
            config, results_dir=tmp_path, run_tag=RUN_TAG, ledger=ledger
        )


def test_baseline_spec_requires_qualification_and_historical_config_stays_frozen(tmp_path):
    with pytest.raises(ConfigError, match="verified W7"):
        realbench._validate_spec(make_spec(artifact_family="qualified-baseline-runs"))
    with pytest.raises(ConfigError, match="valid only"):
        realbench._validate_spec(
            replace(make_spec(), study_entry_binding={"study_entry_passed": True})
        )
    path = tmp_path / "old.json"
    path.write_text(json.dumps(mvl_config_dict()))
    with pytest.raises(ConfigError, match="candidate W7"):
        qualified_mvl.load_mvl_config(path)
