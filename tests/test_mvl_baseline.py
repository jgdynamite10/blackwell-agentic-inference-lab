"""Focused offline tests for the D-0017 Akamai minimum valuable lab."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from blackwell_lab.cloud import cli, lifecycle
from blackwell_lab.cloud.cli import main
from blackwell_lab.cloud.mvl import (
    AUTHORIZED_MVL_CELLS,
    FROZEN_MODEL_ARTIFACT,
    FROZEN_MODEL_ARTIFACT_HASH,
    FROZEN_MODEL_REVISION,
    FROZEN_REPETITIONS,
    FROZEN_TASKS_PER_REPETITION,
    MVL_APPROVAL_TEMPLATE,
    evaluate_canary,
    measured_counts,
    refuse_if_session_exceeded,
    validate_authorized_mvl_config,
)
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.validation import ConfigError

COMMIT = "d123ee2e78fd00877b98272b58a1ad2d7bfcc376"
RUN_TAG = "p3-mvl-20260908a"


@pytest.fixture()
def real_results_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
    return tmp_path


@dataclass
class _FakeRecord:
    run_id: str
    outcomes: list
    written_files: tuple = ()
    result: dict | None = None
    measured_observations: dict | None = None


def _cells():
    return [
        {"profile": profile, "concurrency": concurrency}
        for profile, concurrency in AUTHORIZED_MVL_CELLS
    ]


def mvl_config_dict(commit=COMMIT):
    return {
        "endpoint": {"base_url": "http://127.0.0.1:8000/v1", "model": "m"},
        "cloud": {
            "instance_type": "g3-gpu-rtxpro6000-blackwell-1",
            "region": "us-sea",
            "list_price_usd_per_hour": 3.0,
            "price_source_date": "2026-09-08",
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
        "canonical_commit": commit,
        "cells": _cells(),
        "warmup_passes": 1,
        "repetitions": 3,
        "tasks_per_repetition": 200,
    }


def write_mvl_config(tmp_path: Path, **overrides) -> Path:
    config = mvl_config_dict()
    config.update(overrides)
    path = tmp_path / "mvl.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def config_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mvl_argv(config_path: Path, run_label="mvl-a", approve=None):
    phrase = approve or MVL_APPROVAL_TEMPLATE.format(
        run_tag=RUN_TAG,
        run_label=run_label,
        config_sha256=config_sha256(config_path),
    )
    return [
        "mvl-baseline",
        "--run-tag",
        RUN_TAG,
        "--run-label",
        run_label,
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


def _canary_outcomes(*, fail: bool = False):
    outcomes = []
    for template_id in catalog():
        outcomes.append(
            {
                "template_id": template_id,
                "error_category": "endpoint_error" if fail else None,
                "tool_trace": [{"role": "tool", "name": "get_service_health"}],
            }
        )
    return outcomes


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


def _prepare_mvl(tmp_path, monkeypatch):
    from blackwell_lab.cloud import provenance, realbench

    monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
    monkeypatch.setattr(cli, "_git_head", lambda: COMMIT)
    monkeypatch.setattr(cli, "_tree_clean", lambda: True)
    external = tmp_path / "external"
    monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
    paths = lifecycle.lifecycle_paths(external, RUN_TAG)
    lifecycle.write_private_json(paths.ledger_path, ready_ledger())
    calls: list[object] = []
    provenance_calls = {"n": 0}
    destroy_calls: list[int] = []

    def fake_verify(**kwargs):
        provenance_calls["n"] += 1
        return _observed()

    def fake_run(spec, *_args, **_kwargs):
        calls.append(spec)
        if spec.tasks_per_repetition == 10:
            return [
                _FakeRecord(
                    run_id="canary",
                    outcomes=_canary_outcomes(),
                    written_files=("canary.result.json",),
                )
            ]
        return [
            _FakeRecord(run_id=f"{spec.run_label}-r{index}", outcomes=[], written_files=())
            for index in range(1, spec.repetitions + 1)
        ]

    monkeypatch.setattr(provenance, "verify_live_provenance", fake_verify)
    monkeypatch.setattr(realbench, "run_real_cell", fake_run)
    monkeypatch.setattr(
        lifecycle,
        "plan_destroy",
        lambda *args, **kwargs: destroy_calls.append(1) or (_ for _ in ()).throw(AssertionError()),
    )
    return write_mvl_config(tmp_path), calls, provenance_calls, destroy_calls


def test_exact_three_cell_matrix_and_1800_count():
    counts = measured_counts()
    assert AUTHORIZED_MVL_CELLS == (
        ("interactive", 1),
        ("batch-heavy", 4),
        ("batch-heavy", 8),
    )
    assert counts == {
        "cells": 3,
        "measured_repetitions": 9,
        "measured_task_observations": 1800,
    }
    validate_authorized_mvl_config(mvl_config_dict())


def test_config_rejects_a_fourth_cell():
    config = mvl_config_dict()
    config["cells"] = [*config["cells"], {"profile": "interactive", "concurrency": 8}]
    with pytest.raises(ConfigError, match="exactly interactive/1"):
        validate_authorized_mvl_config(config)


def test_canary_pass_continues_to_all_three_cells(tmp_path, monkeypatch, capsys):
    config, calls, provenance_calls, destroy_calls = _prepare_mvl(tmp_path, monkeypatch)
    assert main(mvl_argv(config)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["workflow"] == "mvl-baseline"
    assert report["counts"]["measured_task_observations"] == 1800
    assert [spec.profile_name for spec in calls if spec.tasks_per_repetition == 200] == [
        "interactive",
        "batch-heavy",
        "batch-heavy",
    ]
    assert [spec.concurrency for spec in calls if spec.tasks_per_repetition == 200] == [1, 4, 8]
    assert provenance_calls["n"] == 4
    assert destroy_calls == []
    assert "owner's laptop" in report["note"]
    assert "destroy plan" in report["note"]


def test_canary_structural_failure_produces_zero_measured_calls(tmp_path, monkeypatch, capsys):
    from blackwell_lab.cloud import realbench

    config, calls, _provenance_calls, destroy_calls = _prepare_mvl(tmp_path, monkeypatch)

    def failing_canary(spec, *_args, **_kwargs):
        calls.append(spec)
        return [
            _FakeRecord(run_id="canary", outcomes=_canary_outcomes(fail=True), written_files=())
        ]

    monkeypatch.setattr(realbench, "run_real_cell", failing_canary)
    assert main(mvl_argv(config)) == 1
    err = capsys.readouterr().err
    assert "canary failed structurally" in err
    assert "teardown-plan" in err
    assert "laptop" in err
    assert [spec.tasks_per_repetition for spec in calls] == [10]
    assert destroy_calls == []
    failure = next((tmp_path / "external" / "real-runs").rglob("*-failure.json"))
    payload = json.loads(failure.read_text(encoding="utf-8"))
    assert payload["is_valid_result"] is False
    assert payload["diagnostic_only"] is True


def test_each_cell_uses_three_reps_and_200_tasks(tmp_path, monkeypatch):
    config, calls, _, _ = _prepare_mvl(tmp_path, monkeypatch)
    assert main(mvl_argv(config)) == 0
    measured = [spec for spec in calls if spec.tasks_per_repetition == 200]
    assert len(measured) == 3
    for spec in measured:
        assert spec.repetitions == FROZEN_REPETITIONS
        assert spec.tasks_per_repetition == FROZEN_TASKS_PER_REPETITION
        assert spec.warmup_passes == 1
        assert spec.comparison_mode == "provider-native"


def test_production_multi_repetition_does_not_overwrite(real_results_dir):
    from fakes import FakeClock
    from test_realbench import HOST, FakeSampler, UsageMockClient, make_spec

    from blackwell_lab.cloud.realbench import run_real_cell

    first = run_real_cell(
        make_spec(repetitions=3, warmup_passes=0, tasks_per_repetition=10, run_label="mvl-rep"),
        UsageMockClient(),
        host=HOST,
        sampler_factory=FakeSampler,
        clock=FakeClock(),
    )
    assert len(first) == 3
    assert len({record.run_id for record in first}) == 3
    with pytest.raises(ConfigError, match="overwrite"):
        run_real_cell(
            make_spec(repetitions=3, warmup_passes=0, tasks_per_repetition=10, run_label="mvl-rep"),
            UsageMockClient(),
            host=HOST,
            sampler_factory=FakeSampler,
            clock=FakeClock(),
        )


def test_wrong_approval_digest_and_unsafe_run_labels_fail(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
    config = write_mvl_config(tmp_path)
    assert main(mvl_argv(config, approve="I approve some other digest")) == 1
    assert "exact owner approval phrase" in capsys.readouterr().err
    unsafe = MVL_APPROVAL_TEMPLATE.format(
        run_tag=RUN_TAG,
        run_label="Unsafe_Label",
        config_sha256=config_sha256(config),
    )
    assert main(mvl_argv(config, run_label="Unsafe_Label", approve=unsafe)) == 1
    assert "run_label" in capsys.readouterr().err


def test_live_provenance_runs_before_canary_and_each_cell(tmp_path, monkeypatch):
    config, _calls, provenance_calls, _ = _prepare_mvl(tmp_path, monkeypatch)
    assert main(mvl_argv(config)) == 0
    assert provenance_calls["n"] == 4


def test_no_automatic_destroy_or_provider_credential_use(tmp_path, monkeypatch):
    config, _, _, destroy_calls = _prepare_mvl(tmp_path, monkeypatch)
    monkeypatch.delenv("LINODE_TOKEN", raising=False)
    assert main(mvl_argv(config)) == 0
    assert destroy_calls == []


def test_watchdog_recognizes_mvl_baseline():
    text = Path("infra/akamai/bootstrap/watchdog.sh").read_text(encoding="utf-8")
    assert "blackwell-cloud mvl-baseline" in text
    assert "NOT A BILLING CONTROL" in text
    assert "blackwell-cloud full-baseline" not in text


def test_session_projection_can_stop_measured_work():
    with pytest.raises(Exception, match="six-hour session"):
        refuse_if_session_exceeded(10_000, remaining_s=60)


def test_canary_requires_native_tools():
    outcomes = [
        {"template_id": template_id, "error_category": None, "tool_trace": []}
        for template_id in catalog()
    ]
    with pytest.raises(Exception, match="native tool"):
        evaluate_canary(outcomes)
