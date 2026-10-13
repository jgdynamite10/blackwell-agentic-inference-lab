"""Adversarial offline guards for qualification-bound measurement.

One scripted native-loop fixture supplies schema-valid synthetic artifacts.
These tests verify authentication and refusal, not actual model quality.
Every model, endpoint, and provider-write boundary remains forbidden.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fakes import FakeClock
from test_mvl_baseline import mvl_config_dict
from test_qualified_baseline import COMMIT, RUN_TAG, OracleClient
from test_qualified_baseline import completed_freeze as build_completed_freeze
from test_realbench import HOST, FakeSampler, make_spec

from blackwell_lab.cloud import cli, lifecycle, mvl, qualification, qualified_mvl, realbench
from blackwell_lab.cloud.artifacts import write_private_json
from blackwell_lab.workload.model_client import GenerationSettings, ModelClientTimeout, StreamEvent
from blackwell_lab.workload.runner import PROFILES
from blackwell_lab.workload.validation import ConfigError


@pytest.fixture(scope="module")
def freeze_source(tmp_path_factory):
    """Build once, then copy so mutations cannot leak between refusal cases."""
    root = tmp_path_factory.mktemp("synthetic-freeze-guards")
    with pytest.MonkeyPatch.context() as patches:
        config, ledger, cell = build_completed_freeze.__wrapped__(root, patches)
    return copy.deepcopy(config), copy.deepcopy(ledger), cell


@pytest.fixture()
def valid_freeze(freeze_source, tmp_path, monkeypatch):
    config, ledger, original_cell = freeze_source
    family = tmp_path / "qualification-runs"
    shutil.copytree(original_cell.parent, family)
    monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
    return copy.deepcopy(config), copy.deepcopy(ledger), family / original_cell.name


def _read(path: Path) -> dict:
    return json.loads(path.read_bytes())


def _write(path: Path, document: dict) -> str:
    return write_private_json(path, document)


def _receipt_path(cell: Path) -> Path:
    return cell.parent / f"{cell.name}-receipt.json"


def _change_receipt(config, cell, mutate, *, refresh_artifacts=False):
    path = _receipt_path(cell)
    receipt = _read(path)
    if refresh_artifacts:
        receipt["artifact_sha256"] = {
            name: hashlib.sha256((cell / name).read_bytes()).hexdigest()
            for name in receipt["files"]
        }
    mutate(receipt)
    config["qualification_binding"]["receipt_sha256"] = _write(path, receipt)


def _change_document(config, cell, suffix, mutate):
    path = next(cell.glob(f"*{suffix}"))
    document = _read(path)
    mutate(document)
    _write(path, document)
    _change_receipt(config, cell, lambda receipt: None, refresh_artifacts=True)


def _change_measured(config, cell, mutate):
    result_path = next(cell.glob("*.result.json"))
    result = _read(result_path)
    measured_path = cell / result["observations"]["measured_file"]
    measured = _read(measured_path)
    mutate(measured)
    result["observations"]["measured_sha256"] = _write(measured_path, measured)
    _write(result_path, result)
    _change_receipt(config, cell, lambda receipt: None, refresh_artifacts=True)


def _invoke(config, ledger, tmp_path, monkeypatch, *, validate_only, expect_private_failure=False):
    """Keep all validation real; spies guard only disallowed external work."""
    from blackwell_lab.cloud import provenance, telemetry
    from blackwell_lab.workload import openai_client

    config_path = tmp_path / "qualified-baseline.json"
    _write(config_path, config)
    paths = lifecycle.lifecycle_paths(tmp_path, RUN_TAG)
    _write(paths.ledger_path, ledger)
    calls = []

    def forbidden(name):
        def fail(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"unexpected boundary: {name}")

        return fail

    monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda: None)
    monkeypatch.setattr(cli, "_git_head", lambda: COMMIT)
    monkeypatch.setattr(cli, "_tree_clean", lambda: True)
    monkeypatch.setattr(openai_client, "OpenAICompatibleClient", forbidden("model client"))
    monkeypatch.setattr(provenance, "verify_live_provenance", forbidden("endpoint/provenance"))
    monkeypatch.setattr(telemetry, "GpuSamplerThread", forbidden("GPU sampler"))
    monkeypatch.setattr(lifecycle, "record_session_event", forbidden("session write"))
    for name in ("apply", "destroy", "plan_destroy", "reconcile"):
        monkeypatch.setattr(lifecycle, name, forbidden(f"provider {name}"))
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    args = argparse.Namespace(
        run_tag=RUN_TAG,
        run_label="baseline-a",
        config=config_path,
        validate_only=validate_only,
        approve=qualified_mvl.MVL_APPROVAL_TEMPLATE.format(
            run_tag=RUN_TAG,
            run_label="baseline-a",
            candidate_id=config["candidate_id"],
            config_sha256=digest,
        ),
    )
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    status = qualified_mvl.execute(args)
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    assert calls == []
    if expect_private_failure:
        failure = Path("qualified-baseline-runs/baseline-a-failure.json")
        assert set(after) - set(before) == {failure}
        assert all(after[name] == content for name, content in before.items())
        assert json.loads(after[failure])["is_valid_result"] is False
    else:
        assert before == after
    return status


def test_validate_only_authenticates_without_client_endpoint_or_writes(
    valid_freeze, tmp_path, monkeypatch, capsys
):
    config, ledger, _ = valid_freeze
    assert _invoke(config, ledger, tmp_path, monkeypatch, validate_only=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["executed"] is False
    assert report["model_client_constructed"] is False
    assert report["endpoint_contacted"] is False
    assert report["qualification_binding"]["study_entry_passed"] is True
    assert report["counts"]["measured_task_observations"] == 1800


def test_equivalent_pinned_container_repository_digest_remains_valid(
    valid_freeze, tmp_path, monkeypatch, capsys
):
    config, ledger, cell = valid_freeze
    repository_digest = config["serving"]["container_digest"].split(":v0.27.1@")
    assert len(repository_digest) == 2
    equivalent = "@".join(repository_digest)
    _change_document(
        config,
        cell,
        ".manifest.json",
        lambda manifest: manifest["serving"].update(container_digest=equivalent),
    )
    assert _invoke(config, ledger, tmp_path, monkeypatch, validate_only=True) == 0
    assert json.loads(capsys.readouterr().out)["qualification_binding"]["study_entry_passed"]


@pytest.mark.parametrize("which", ["missing", "naive", "future", "expired", "duplicate"])
def test_invalid_session_budget_stops_before_model_endpoint_or_started_event(
    valid_freeze, tmp_path, monkeypatch, capsys, which
):
    config, ledger, _ = valid_freeze
    paths = lifecycle.lifecycle_paths(tmp_path, RUN_TAG)
    now = datetime.now(timezone.utc)
    timestamps = {
        "naive": now.replace(tzinfo=None).isoformat(),
        "future": (now + timedelta(days=1)).isoformat(),
        "expired": (now - timedelta(hours=7)).isoformat(),
        "duplicate": now.isoformat(),
    }
    if which != "missing":
        event = {"event": "provisioned", "at_utc": timestamps[which]}
        _write(paths.session_path, {"events": [event, event] if which == "duplicate" else [event]})
    assert (
        _invoke(
            config,
            ledger,
            tmp_path,
            monkeypatch,
            validate_only=False,
            expect_private_failure=True,
        )
        == 1
    )
    assert "session budget" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_id", "W5"),
        ("candidate_identity_sha256", "00" * 32),
        ("stage", "development"),
        ("workload_version", "2.8.0"),
        ("controller", "workflow-controller-v2"),
        ("stopped", True),
        ("stopped", 0),
        ("blind_generalization_evidence", True),
        ("blind_generalization_evidence", 0),
        ("evidence_contract", {}),
    ],
)
def test_receipt_contract_mismatch_refuses_live_before_external_work(
    valid_freeze, tmp_path, monkeypatch, capsys, field, value
):
    config, ledger, cell = valid_freeze
    _change_receipt(config, cell, lambda receipt: receipt.update({field: value}))
    assert _invoke(config, ledger, tmp_path, monkeypatch, validate_only=False) == 1
    assert "completed qualified candidate" in capsys.readouterr().err


@pytest.mark.parametrize("mutation", ["digest", "gates", "run-tag", "commit", "resource-id"])
def test_freeze_digest_gates_and_session_are_checked_before_client(
    valid_freeze, tmp_path, monkeypatch, capsys, mutation
):
    config, ledger, cell = valid_freeze
    if mutation == "digest":
        config["qualification_binding"]["receipt_sha256"] = "00" * 32
    elif mutation == "gates":
        _change_receipt(config, cell, lambda receipt: receipt.update(gates={"continue": True}))
    elif mutation == "resource-id":
        ledger["resources"][0]["provider_id"] = "another-synthetic-instance"
    else:
        field = "run_tag" if mutation == "run-tag" else "canonical_commit"
        _change_receipt(
            config, cell, lambda receipt: receipt["execution_context"].update({field: "other"})
        )
    assert _invoke(config, ledger, tmp_path, monkeypatch, validate_only=False) == 1
    assert "BLOCKED" in capsys.readouterr().err


@pytest.mark.parametrize("suffix", [".manifest.json", ".observations.json", ".warmup.json"])
def test_every_referenced_artifact_must_be_in_the_receipt_hash_set(
    valid_freeze, tmp_path, monkeypatch, capsys, suffix
):
    config, ledger, cell = valid_freeze
    result = _read(next(cell.glob("*.result.json")))
    name = {
        ".manifest.json": result["manifest_ref"],
        ".observations.json": result["observations"]["measured_file"],
        ".warmup.json": result["observations"]["warmup_file"],
    }[suffix]

    def omit(receipt):
        receipt["files"].remove(name)
        receipt["artifact_sha256"].pop(name)

    _change_receipt(config, cell, omit)
    assert _invoke(config, ledger, tmp_path, monkeypatch, validate_only=False) == 1
    assert "BLOCKED" in capsys.readouterr().err


@pytest.mark.parametrize("mutation", ["absent", "short", "wrong-template"])
def test_rehashed_freeze_must_retain_the_complete_warmup_schedule(
    valid_freeze, tmp_path, monkeypatch, capsys, mutation
):
    config, ledger, cell = valid_freeze
    result_path = next(cell.glob("*.result.json"))
    result = _read(result_path)
    name = result["observations"]["warmup_file"]
    warmup_path = cell / name
    if mutation == "absent":
        result["observations"]["warmup_count"] = 0
        result["observations"].pop("warmup_file")
        result["observations"].pop("warmup_sha256")
        warmup_path.unlink()
    else:
        warmup = _read(warmup_path)
        if mutation == "short":
            warmup["observations"].pop()
            result["observations"]["warmup_count"] = len(warmup["observations"])
        else:
            rows = warmup["observations"]
            assert rows[0]["template_id"] != rows[1]["template_id"]
            rows[0]["template_id"] = rows[1]["template_id"]
        result["observations"]["warmup_sha256"] = _write(warmup_path, warmup)
    _write(result_path, result)

    def refresh(receipt):
        if mutation == "absent":
            receipt["files"].remove(name)
        receipt["artifact_sha256"] = {
            file: hashlib.sha256((cell / file).read_bytes()).hexdigest()
            for file in receipt["files"]
        }

    _change_receipt(config, cell, refresh)
    assert _invoke(config, ledger, tmp_path, monkeypatch, validate_only=False) == 1
    assert "BLOCKED" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("model", "artifact_hash", "sha256:" + "00" * 32),
        ("model", "precision", "nvfp4"),
        ("serving", "container_digest", "docker.io/vllm/vllm-openai@sha256:" + "00" * 32),
        ("serving", "engine_version", "0.27.2"),
        ("cloud", "region", "us-sea"),
        ("cloud", "instance_type", "other-synthetic-plan"),
        ("generation", "temperature", 1.0),
        ("workload", "profile", "batch-heavy"),
        ("workload", "concurrency", 4),
        ("workload", "scenario_count", 9),
        ("workload", "tasks_per_repetition", 199),
        ("timing_conditions", "repetitions_planned", 3),
        ("timing_conditions", "repetition_index", 2),
    ],
)
def test_rehashed_manifest_cannot_qualify_different_runtime(
    valid_freeze, tmp_path, monkeypatch, capsys, section, field, value
):
    config, ledger, cell = valid_freeze
    _change_document(
        config, cell, ".manifest.json", lambda manifest: manifest[section].update({field: value})
    )
    assert _invoke(config, ledger, tmp_path, monkeypatch, validate_only=False) == 1
    assert "BLOCKED" in capsys.readouterr().err


@pytest.mark.parametrize(
    "mutation", ["missing-task", "wrong-template", "missing-ttft", "slow-ttft"]
)
def test_rehashed_observations_still_require_full_schedule_and_timing(
    valid_freeze, tmp_path, monkeypatch, capsys, mutation
):
    config, ledger, cell = valid_freeze

    def mutate(document):
        rows = document["observations"]
        if mutation == "missing-task":
            rows.pop()
        elif mutation == "wrong-template":
            rows[0]["template_id"] = rows[1]["template_id"]
        else:
            value = None if mutation == "missing-ttft" else 2501
            for row in rows:
                for turn in row["turns"]:
                    turn["ttft_ms"] = value

    _change_measured(config, cell, mutate)
    assert _invoke(config, ledger, tmp_path, monkeypatch, validate_only=False) == 1
    assert "BLOCKED" in capsys.readouterr().err


@pytest.mark.parametrize("which", ["receipt", "manifest", "measured", "cell", "family"])
def test_symlinked_qualification_paths_refuse_before_live_work(
    valid_freeze, tmp_path, monkeypatch, capsys, which
):
    config, ledger, cell = valid_freeze
    result = _read(next(cell.glob("*.result.json")))
    target = {
        "receipt": _receipt_path(cell),
        "manifest": cell / result["manifest_ref"],
        "measured": cell / result["observations"]["measured_file"],
        "cell": cell,
        "family": cell.parent,
    }[which]
    backup = tmp_path / f"relocated-{which}"
    target.rename(backup)
    target.symlink_to(backup, target_is_directory=backup.is_dir())
    assert _invoke(config, ledger, tmp_path, monkeypatch, validate_only=False) == 1
    assert "BLOCKED" in capsys.readouterr().err


def test_changed_raw_artifact_is_rejected_even_if_it_is_valid_json(
    valid_freeze, tmp_path, monkeypatch, capsys
):
    config, ledger, cell = valid_freeze
    path = next(cell.glob("*.observations.json"))
    path.write_bytes(path.read_bytes() + b"\n")
    assert _invoke(config, ledger, tmp_path, monkeypatch, validate_only=False) == 1
    assert "artifact digest mismatch" in capsys.readouterr().err


def test_parent_traversal_in_receipt_cannot_read_external_artifact(
    valid_freeze, tmp_path, monkeypatch, capsys
):
    config, ledger, cell = valid_freeze

    def mutate(receipt):
        receipt["files"].append("../outside.json")
        receipt["artifact_sha256"]["../outside.json"] = "00" * 32

    _change_receipt(config, cell, mutate)
    assert _invoke(config, ledger, tmp_path, monkeypatch, validate_only=False) == 1
    assert "malformed" in capsys.readouterr().err


@pytest.mark.parametrize("extra", ["result", "failure"])
def test_ambiguous_or_failed_freeze_directory_cannot_authorize_measurement(
    valid_freeze, tmp_path, monkeypatch, capsys, extra
):
    config, ledger, cell = valid_freeze
    if extra == "result":
        shutil.copyfile(next(cell.glob("*.result.json")), cell / "second.result.json")
    else:
        _write(cell / "freeze.failure.json", {"stopped": True})
    assert _invoke(config, ledger, tmp_path, monkeypatch, validate_only=False) == 1
    assert "BLOCKED" in capsys.readouterr().err


def _qualified_spec():
    return make_spec(
        artifact_family="qualified-baseline-runs",
        workload_version="2.9.0",
        controller="workflow-controller-v3",
        generation=GenerationSettings(
            **dict(
                qualification.frozen_candidate_fields("W7")["generation"], workload_version="2.9.0"
            )
        ),
        study_entry_binding={
            "candidate_id": "W7",
            "candidate_identity_sha256": qualification.candidate_identity_digest("W7"),
            "receipt_sha256": "11" * 32,
            "result_sha256": "22" * 32,
            "manifest_sha256": "33" * 32,
            "study_entry_passed": True,
        },
    )


@pytest.mark.parametrize(
    "field",
    ["candidate_identity_sha256", "receipt_sha256", "result_sha256", "manifest_sha256"],
)
def test_direct_real_runner_rejects_incomplete_freeze_proofs(field):
    spec = _qualified_spec()
    binding = dict(spec.study_entry_binding)
    binding.pop(field)
    with pytest.raises(ConfigError, match="study-entry"):
        realbench._validate_spec(replace(spec, study_entry_binding=binding))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_identity_sha256", "00" * 32),
        ("receipt_sha256", "not-a-hash"),
        ("result_sha256", None),
        ("manifest_sha256", "33" * 31),
        ("candidate_id", "W6"),
        ("study_entry_passed", 1),
        ("unverified_claim", True),
    ],
)
def test_direct_real_runner_rejects_malformed_or_mismatched_proofs(field, value):
    spec = _qualified_spec()
    binding = dict(spec.study_entry_binding, **{field: value})
    with pytest.raises(ConfigError, match="study-entry"):
        realbench._validate_spec(replace(spec, study_entry_binding=binding))


def test_persisted_timing_cannot_be_replaced_by_top_level_ttft():
    row = {
        "template_id": "synthetic-template",
        "evaluation": {"success": True},
        "tool_trace": [{}],
        "e2e_ms": 100,
        "ttft_ms": 1,
        "turns": [{"ttft_ms": 2501}],
    }
    metrics = qualification.compute_catalog_metrics([row], provenance_ok=True, verification_ok=True)
    assert metrics.interactive_ttft_p95_ms == 2501
    assert not qualification.evaluate_study_entry_gate(metrics)["passed"]
    row["turns"] = [{"ttft_ms": None}]
    metrics = qualification.compute_catalog_metrics([row], provenance_ok=True, verification_ok=True)
    assert metrics.interactive_ttft_p95_ms is None
    assert not qualification.evaluate_study_entry_gate(metrics)["passed"]
    assert row["ttft_ms"] == 1  # Normalization does not rewrite immutable inputs.


def test_session_expiry_clamps_each_request_and_never_calls_client_after_deadline():
    deadlines = []

    class RecordingClient:
        def stream_turn(self, messages, settings, *, deadline=None, clock=None):
            deadlines.append(deadline)
            yield StreamEvent(kind="usage", output_tokens=1)

    clock = FakeClock(start=0)
    client = qualified_mvl.SessionBudgetClient(RecordingClient(), deadline=5)
    settings = GenerationSettings(workload_version="2.9.0")
    assert list(client.stream_turn([], settings, deadline=10, clock=clock))
    assert list(client.stream_turn([], settings, deadline=2, clock=clock))
    assert list(client.stream_turn([], settings, clock=clock))
    assert deadlines == [5, 2, 5]
    clock.sleep(5)
    with pytest.raises(ModelClientTimeout, match="session budget has expired"):
        list(client.stream_turn([], settings, deadline=10, clock=clock))
    assert deadlines == [5, 2, 5]


def test_batch_measurement_executes_the_qualified_generation_budget(tmp_path, monkeypatch):
    observed_settings = []

    class CapturingOracle(OracleClient):
        def stream_turn(self, messages, settings, *, deadline=None, clock=None):
            observed_settings.append(settings)
            yield from super().stream_turn(messages, settings, deadline=deadline, clock=clock)

    pins = mvl_config_dict()
    monkeypatch.setattr(
        realbench,
        "_git_provenance",
        lambda: {
            "commit": COMMIT,
            "repository": "https://example.invalid/synthetic",
            "dirty": False,
        },
    )
    spec = replace(
        _qualified_spec(),
        profile_name="batch-heavy",
        concurrency=4,
        warmup_passes=0,
        model=pins["model"],
        container_digest=pins["serving"]["container_digest"],
        engine_version=pins["serving"]["engine_version"],
        instance_type=pins["cloud"]["instance_type"],
        region=pins["cloud"]["region"],
    )
    records = realbench.run_real_cell(
        spec,
        CapturingOracle(),
        host=HOST,
        sampler_factory=FakeSampler,
        results_dir=tmp_path,
        clock=FakeClock(),
    )
    assert observed_settings
    assert {settings.max_tokens for settings in observed_settings} == {1024}
    assert {settings.temperature for settings in observed_settings} == {0.2}
    assert records[0].manifest["generation"]["max_tokens"] == 1024
    assert PROFILES["batch-heavy"].max_tokens == 4096  # Historical profile is preserved.


def test_new_qualified_workflow_does_not_relax_historical_mvl():
    assert mvl.FROZEN_WORKLOAD_VERSION == "2.3.0"
    assert mvl.FROZEN_TEMPERATURE == 1.0
    assert qualified_mvl.FROZEN_WORKLOAD_VERSION == "2.9.0"
    assert qualified_mvl.MVL_APPROVAL_TEMPLATE != mvl.MVL_APPROVAL_TEMPLATE
    config = mvl_config_dict()
    config["workload_version"] = "2.9.0"
    with pytest.raises(ConfigError, match="workload"):
        mvl.validate_authorized_mvl_config(config)
