"""Offline adversarial tests for the D-0017 full-baseline workflow."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from blackwell_lab.cloud.artifacts import write_private_json
from blackwell_lab.cloud.baseline import (
    AUTHORIZED_FULL_BASELINE_CELLS,
    FROZEN_CANARY_TASKS,
    FROZEN_MODEL_ARTIFACT,
    FROZEN_MODEL_ARTIFACT_HASH,
    FROZEN_MODEL_REVISION,
    FROZEN_REPETITIONS,
    FROZEN_TASKS_PER_REPETITION,
    FULL_BASELINE_APPROVAL_TEMPLATE,
    FULL_BASELINE_AUTHORIZED,
    BaselineCell,
    BaselineError,
    FullBaselineDeps,
    cell_execution_order,
    evaluate_canary,
    execute_full_baseline,
    full_baseline_cells,
    load_full_baseline_config,
    measured_counts,
    project_duration_hours,
    refuse_if_ceiling_exceeded,
    validate_authorized_full_baseline_config,
)
from blackwell_lab.cloud.cli import main
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.validation import ConfigError

COMMIT = "d123ee2e78fd00877b98272b58a1ad2d7bfcc376"


def _cells_config():
    return [
        {"comparison_mode": mode, "profile": profile, "concurrency": concurrency}
        for mode, profile, concurrency in AUTHORIZED_FULL_BASELINE_CELLS
    ]


def baseline_config_dict(commit=COMMIT):
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
        "model_verification": {
            "artifact_dir": "/opt/models/nemotron",
            "digest_manifest": "/opt/models/nemotron.sha256",
        },
        "canonical_commit": commit,
        "cells": _cells_config(),
        "warmup_passes": 1,
        "repetitions": 5,
        "tasks_per_repetition": 200,
        "generation": {
            "temperature": 1.0,
            "top_p": 0.95,
            "max_tokens": 1024,
            "reasoning_mode": True,
            "seed": 20260906,
        },
        "workload_version": "2.3.0",
    }


def write_config(tmp_path: Path, **overrides) -> Path:
    config = baseline_config_dict()
    config.update(overrides)
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def good_canary_outcomes():
    return [
        {
            "template_id": template_id,
            "error_category": None,
            "tool_trace": [{"tool": "get_service_health", "role": "tool"}],
        }
        for template_id in catalog()
    ]


@dataclass
class FakeRecord:
    run_id: str
    written_files: tuple[str, ...]
    measured_observations: dict
    result: dict
    outcomes: tuple
    digests: dict


class Recorder:
    def __init__(self, *, fail_canary=False, huge_wall=False):
        self.calls: list[dict] = []
        self.modes: list[str] = []
        self.provenance: list[str] = []
        self.teardowns = 0
        self.events: list[str] = []
        self.fail_canary = fail_canary
        self.clock = [0.0]
        self.huge_wall = huge_wall

    def teardown(self) -> dict:
        self.teardowns += 1
        return {"plan_sha256": "deadbeef"}

    def monotonic(self) -> float:
        self.clock[0] += 1_000_000.0 if self.huge_wall and self.clock[0] > 0 else 0.2
        return self.clock[0]

    def run_cell(self, spec: dict) -> list[FakeRecord]:
        self.calls.append(spec)
        if spec.get("kind") == "canary" and self.fail_canary:
            outcomes = [
                {**item, "error_category": "malformed_tool_call"} for item in good_canary_outcomes()
            ]
        else:
            outcomes = good_canary_outcomes() if spec.get("kind") == "canary" else ()
        run_id = f"run-{len(self.calls):04d}-bbbb-cccc-dddd-eeeeeeeeeeee"
        return [
            FakeRecord(
                run_id=run_id,
                written_files=(f"{run_id}.result.json",),
                measured_observations={
                    "observations": list(outcomes),
                    "phase": spec.get("observation_phase"),
                },
                result={"diagnostic_only": spec.get("diagnostic_only", False)},
                outcomes=tuple(outcomes),
                digests={"manifest": "aa", "result": "bb", "observations": "cc"},
            )
        ]


def make_deps(tmp_path: Path, recorder: Recorder, *, commit=COMMIT, clean=True) -> FullBaselineDeps:
    def sha256_file(_path: Path) -> str:
        name = _path.name
        if name.endswith(".manifest.json"):
            return "aa"
        if name.endswith(".result.json"):
            return "bb"
        return "cc"

    return FullBaselineDeps(
        git_head=lambda: commit,
        tree_clean=lambda: clean,
        verify_live_provenance=lambda mode: recorder.provenance.append(mode),
        observe_and_verify_mode=lambda mode: {
            "verified": True,
            "mode": mode,
            "serving_in_slice": mode == "controlled-resource",
            "benchmark_in_slice": mode == "controlled-resource",
            "cpu_max": "1400000 100000",
            "memory_max_bytes": 107374182400,
            "swap_max_bytes": 0,
            "controlled_slice_present": mode == "controlled-resource",
            "docker_cpu_limit": 0,
            "docker_memory_limit": 0,
        },
        transition_serving=lambda mode: recorder.modes.append(mode),
        run_real_cell=recorder.run_cell,
        prepare_teardown_plan=recorder.teardown,
        record_session_event=lambda event, detail=None: recorder.events.append(event),
        monotonic=recorder.monotonic,
        sha256_file=sha256_file,
        client_factory=lambda: None,
        write_failure_receipt=write_private_json,
    )


def test_constant_and_matrix_are_frozen():
    assert FULL_BASELINE_AUTHORIZED is True
    cells = full_baseline_cells()
    assert len(cells) == 12
    counts = measured_counts()
    assert counts["measured_repetitions"] == 60
    assert counts["measured_task_observations"] == 12_000
    order = [(c.comparison_mode, c.profile, c.concurrency) for c in cells]
    assert order == list(AUTHORIZED_FULL_BASELINE_CELLS)
    assert order[0] == ("controlled-resource", "interactive", 1)
    assert order[5] == ("controlled-resource", "batch-heavy", 8)
    assert order[6] == ("provider-native", "interactive", 1)
    assert order[-1] == ("provider-native", "batch-heavy", 8)
    assert [item["key"] for item in cell_execution_order()] == [c.key for c in cells]


def test_config_rejects_expanded_matrix(tmp_path):
    path = write_config(tmp_path)
    config, _digest = load_full_baseline_config(path)
    validate_authorized_full_baseline_config(config)
    config["cells"] = config["cells"] + [config["cells"][0]]
    with pytest.raises(ConfigError, match="12-cell"):
        validate_authorized_full_baseline_config(config)
    config = baseline_config_dict()
    config["repetitions"] = 6
    with pytest.raises(ConfigError, match="repetitions"):
        validate_authorized_full_baseline_config(config)
    config = baseline_config_dict()
    config["model"]["artifact_hash"] = "sha256:" + "00" * 32
    with pytest.raises(ConfigError, match="aggregate"):
        validate_authorized_full_baseline_config(config)


def test_approval_phrase_names_config_digest(tmp_path):
    path = write_config(tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    phrase = FULL_BASELINE_APPROVAL_TEMPLATE.format(
        run_tag="p3-base-20260908a", run_label="baseline-a", config_sha256=digest
    )
    other = FULL_BASELINE_APPROVAL_TEMPLATE.format(
        run_tag="p3-base-20260908a", run_label="baseline-a", config_sha256="ff" * 32
    )
    assert digest in phrase
    assert phrase != other


def test_canary_structural_failure_blocks_measured(tmp_path):
    recorder = Recorder(fail_canary=True)
    config = baseline_config_dict()
    with pytest.raises(BaselineError, match="malformed_tool_call"):
        execute_full_baseline(
            run_tag="p3-base-20260908a",
            run_label="baseline-a",
            config=config,
            config_sha256="ab" * 32,
            results_dir=tmp_path,
            progress_path=tmp_path / "progress.json",
            deps=make_deps(tmp_path, recorder),
            measured_cells=full_baseline_cells()[:1],
        )
    assert recorder.teardowns == 1
    assert all(call.get("kind") == "canary" for call in recorder.calls)
    assert (tmp_path / "real-runs" / "baseline-a-failure.json").is_file()
    receipt = json.loads((tmp_path / "real-runs" / "baseline-a-failure.json").read_text())
    assert receipt["diagnostic_only"] is True
    assert receipt["is_valid_result"] is False


def test_successful_canary_flows_into_measured(tmp_path):
    recorder = Recorder()
    report = execute_full_baseline(
        run_tag="p3-base-20260908a",
        run_label="baseline-a",
        config=baseline_config_dict(),
        config_sha256="ab" * 32,
        results_dir=tmp_path,
        progress_path=tmp_path / "progress.json",
        deps=make_deps(tmp_path, recorder),
        measured_cells=[BaselineCell("controlled-resource", "interactive", 1)],
    )
    kinds = [call.get("kind") for call in recorder.calls]
    assert kinds[0] == "canary"
    assert kinds[1] == "measured"
    assert recorder.calls[0]["concurrency"] == 8
    assert recorder.calls[0]["tasks_per_repetition"] == FROZEN_CANARY_TASKS
    assert recorder.calls[1]["tasks_per_repetition"] == FROZEN_TASKS_PER_REPETITION
    assert recorder.calls[1]["repetitions"] == 1
    assert report["cells"][0]["diagnostic_only"] is True
    assert any(item.get("kind") == "measured" for item in report["cells"])
    assert recorder.provenance  # fresh live provenance


def test_every_cell_requests_fresh_provenance(tmp_path):
    recorder = Recorder()
    execute_full_baseline(
        run_tag="p3-base-20260908a",
        run_label="baseline-a",
        config=baseline_config_dict(),
        config_sha256="ab" * 32,
        results_dir=tmp_path,
        progress_path=tmp_path / "progress.json",
        deps=make_deps(tmp_path, recorder),
        measured_cells=[
            BaselineCell("controlled-resource", "interactive", 1),
            BaselineCell("controlled-resource", "interactive", 4),
        ],
    )
    measured_calls = [call for call in recorder.calls if call.get("kind") == "measured"]
    assert len(measured_calls) == 2 * FROZEN_REPETITIONS
    assert recorder.provenance.count("controlled-resource") >= 1 + (2 * FROZEN_REPETITIONS)


def test_resume_skips_only_fully_verified_repetitions(tmp_path):
    recorder = Recorder()
    cell = BaselineCell("controlled-resource", "interactive", 1)
    progress = {
        "schema_version": "1.0.0",
        "kind": "full-baseline-progress",
        "run_tag": "p3-base-20260908a",
        "canonical_commit": COMMIT,
        "config_sha256": "ab" * 32,
        "model_revision": FROZEN_MODEL_REVISION,
        "model_artifact_hash": FROZEN_MODEL_ARTIFACT_HASH,
        "container_digest": baseline_config_dict()["serving"]["container_digest"],
        "workload_version": "2.3.0",
        "matrix": cell_execution_order(),
        "counts": measured_counts(),
        "completed": {
            "canary:controlled-resource": {"verified": True, "diagnostic_only": True},
            "cell:controlled-resource:interactive:1:rep:1": {
                "verified": True,
                "run_id": "already-done-aaaa-bbbb-cccc-dddddddddddd",
                "sha256": {"manifest": "aa", "result": "bb", "observations": "cc"},
            },
        },
    }
    cell_dir = tmp_path / "real-runs" / "baseline-a-cr-interactive-1"
    cell_dir.mkdir(parents=True)
    run_id = "already-done-aaaa-bbbb-cccc-dddddddddddd"
    for name in (f"{run_id}.manifest.json", f"{run_id}.result.json", f"{run_id}.observations.json"):
        (cell_dir / name).write_text("{}", encoding="utf-8")
    write_private_json(tmp_path / "progress.json", progress)
    execute_full_baseline(
        run_tag="p3-base-20260908a",
        run_label="baseline-a",
        config=baseline_config_dict(),
        config_sha256="ab" * 32,
        results_dir=tmp_path,
        progress_path=tmp_path / "progress.json",
        deps=make_deps(tmp_path, recorder),
        measured_cells=[cell],
    )
    measured = [call for call in recorder.calls if call.get("kind") == "measured"]
    assert len(measured) == 4  # reps 2-5 only
    assert all(call.get("repetition_index") != 1 for call in measured)


def test_resume_rejects_commit_drift(tmp_path):
    recorder = Recorder()
    write_private_json(
        tmp_path / "progress.json",
        {
            "schema_version": "1.0.0",
            "kind": "full-baseline-progress",
            "run_tag": "p3-base-20260908a",
            "canonical_commit": "0" * 40,
            "config_sha256": "ab" * 32,
            "model_revision": FROZEN_MODEL_REVISION,
            "model_artifact_hash": FROZEN_MODEL_ARTIFACT_HASH,
            "container_digest": baseline_config_dict()["serving"]["container_digest"],
            "workload_version": "2.3.0",
            "matrix": cell_execution_order(),
            "counts": measured_counts(),
            "completed": {},
        },
    )
    with pytest.raises(BaselineError, match="canonical_commit"):
        execute_full_baseline(
            run_tag="p3-base-20260908a",
            run_label="baseline-a",
            config=baseline_config_dict(),
            config_sha256="ab" * 32,
            results_dir=tmp_path,
            progress_path=tmp_path / "progress.json",
            deps=make_deps(tmp_path, recorder),
            measured_cells=full_baseline_cells()[:1],
        )
    assert recorder.calls == []


def test_ceiling_stops_before_measured_work(tmp_path):
    recorder = Recorder(huge_wall=True)
    with pytest.raises(BaselineError, match="ceiling"):
        execute_full_baseline(
            run_tag="p3-base-20260908a",
            run_label="baseline-a",
            config=baseline_config_dict(),
            config_sha256="ab" * 32,
            results_dir=tmp_path,
            progress_path=tmp_path / "progress.json",
            deps=make_deps(tmp_path, recorder),
            measured_cells=list(full_baseline_cells()),
        )
    assert all(call.get("kind") == "canary" for call in recorder.calls)
    assert recorder.teardowns == 1


def test_projection_helper_flags_over_ceiling():
    projection = project_duration_hours(
        canary_wall_s=3600.0,
        canary_tasks=10,
        canary_concurrency=8,
        remaining_cells=list(full_baseline_cells()),
    )
    with pytest.raises(BaselineError, match="ceiling"):
        refuse_if_ceiling_exceeded(projection)


def test_canary_requires_native_tools():
    outcomes = [{"template_id": tid, "error_category": None, "tool_trace": []} for tid in catalog()]
    with pytest.raises(BaselineError, match="native"):
        evaluate_canary(outcomes)


def test_dirty_tree_is_rejected(tmp_path):
    recorder = Recorder()
    with pytest.raises(BaselineError, match="dirty"):
        execute_full_baseline(
            run_tag="p3-base-20260908a",
            run_label="baseline-a",
            config=baseline_config_dict(),
            config_sha256="ab" * 32,
            results_dir=tmp_path,
            progress_path=tmp_path / "progress.json",
            deps=make_deps(tmp_path, recorder, clean=False),
        )


def test_cli_approval_cannot_authorize_another_digest(tmp_path, monkeypatch, capsys):
    from blackwell_lab.cloud import lifecycle

    monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
    monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path / "external"))
    path = write_config(tmp_path)
    wrong = FULL_BASELINE_APPROVAL_TEMPLATE.format(
        run_tag="p3-base-20260908a",
        run_label="baseline-a",
        config_sha256="ff" * 32,
    )
    assert (
        main(
            [
                "full-baseline",
                "--run-tag",
                "p3-base-20260908a",
                "--run-label",
                "baseline-a",
                "--config",
                str(path),
                "--approve",
                wrong,
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    assert "nothing was executed" in err


def test_watchdog_recognizes_full_baseline():
    text = (
        Path(__file__).resolve().parents[1] / "infra" / "akamai" / "bootstrap" / "watchdog.sh"
    ).read_text(encoding="utf-8")
    assert "blackwell-cloud full-baseline" in text
    assert "NOT A BILLING CONTROL" in text


def test_output_never_contains_raw_or_private_payloads(tmp_path):
    recorder = Recorder()
    report = execute_full_baseline(
        run_tag="p3-base-20260908a",
        run_label="baseline-a",
        config=baseline_config_dict(),
        config_sha256="ab" * 32,
        results_dir=tmp_path,
        progress_path=tmp_path / "progress.json",
        deps=make_deps(tmp_path, recorder),
        measured_cells=[BaselineCell("controlled-resource", "interactive", 1)],
    )
    dumped = json.dumps(report)
    assert "/opt/models" not in dumped
    assert "TOOL_CALL:" not in dumped
    assert "prompt" not in dumped
    assert "completion" not in dumped
