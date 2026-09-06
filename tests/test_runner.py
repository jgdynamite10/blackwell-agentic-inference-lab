"""Benchmark runner: profiles, concurrency, warm-up, repetitions, timing,
schema-valid output, and results-privacy behavior."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from blackwell_lab.paths import RESULTS_DIR_ENV_VAR
from blackwell_lab.schemas import validate_benchmark_result, validate_run_manifest
from blackwell_lab.workload.evaluator import PROPOSED_QUALITY_THRESHOLD
from blackwell_lab.workload.model_client import DeterministicMockClient
from blackwell_lab.workload.runner import (
    CONCURRENCY_LEVELS,
    DEFAULT_REPETITIONS,
    DEFAULT_WARMUP_PASSES,
    PROFILES,
    main,
    persist_records,
    run_cell,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git") or "/usr/bin/git"

FAST_SCENARIOS = ["elevated-latency-001", "pod-failures-001", "dns-failures-001"]


def fast_cell(**overrides):
    kwargs = {
        "profile_name": "interactive",
        "concurrency": 1,
        "repetitions": 2,
        "warmup_passes": 1,
        "scenario_ids": FAST_SCENARIOS,
    }
    kwargs.update(overrides)
    return run_cell(**kwargs)


class TestProfiles:
    def test_both_documented_profiles_exist(self):
        assert set(PROFILES) == {"interactive", "batch-heavy"}

    def test_profiles_differ_in_context_size_tokens_timeout_and_slo(self):
        interactive, batch = PROFILES["interactive"], PROFILES["batch-heavy"]
        assert interactive.log_limit < batch.log_limit
        assert interactive.metric_window_s < batch.metric_window_s
        assert interactive.max_tokens < batch.max_tokens
        assert interactive.task_timeout_ms < batch.task_timeout_ms
        assert interactive.slo_task_ms < batch.slo_task_ms
        assert interactive.slo_ttft_ms is not None
        assert batch.slo_ttft_ms is None

    def test_defaults_match_the_measurement_contract(self):
        assert DEFAULT_REPETITIONS == 5
        assert DEFAULT_WARMUP_PASSES >= 1
        assert CONCURRENCY_LEVELS == (1, 4, 8)

    def test_unknown_profile_and_concurrency_are_rejected(self):
        with pytest.raises(ValueError):
            run_cell(profile_name="turbo", concurrency=1)
        with pytest.raises(ValueError):
            run_cell(profile_name="interactive", concurrency=3)
        with pytest.raises(ValueError):
            run_cell(profile_name="interactive", concurrency=1, scenario_ids=["nope"])


class TestCellExecution:
    def test_repetitions_and_task_counts(self):
        records = fast_cell()
        assert len(records) == 2
        for index, record in enumerate(records, start=1):
            timing = record.manifest["timing_conditions"]
            assert timing["repetition_index"] == index
            assert timing["repetitions_planned"] == 2
            assert record.result["tasks"]["attempted"] == len(FAST_SCENARIOS)
            assert len(record.executions) == len(FAST_SCENARIOS)

    def test_warmup_is_excluded_from_measurement(self):
        """Warm-up passes change nothing in the measured output."""
        with_warmup = fast_cell(warmup_passes=2, repetitions=1)[0]
        without_warmup = fast_cell(warmup_passes=0, repetitions=1)[0]
        assert with_warmup.result["tasks"] == without_warmup.result["tasks"]
        assert (
            with_warmup.result["latency_ms"]["task_completion"]["count"]
            == without_warmup.result["latency_ms"]["task_completion"]["count"]
            == len(FAST_SCENARIOS)
        )

    @pytest.mark.parametrize("concurrency", CONCURRENCY_LEVELS)
    def test_all_concurrency_levels_produce_identical_semantics(self, concurrency):
        record = fast_cell(concurrency=concurrency, repetitions=1)[0]
        tasks = record.result["tasks"]
        assert tasks["attempted"] == len(FAST_SCENARIOS)
        assert tasks["succeeded"] == len(FAST_SCENARIOS)  # deterministic mock is correct
        assert record.manifest["workload"]["concurrency"] == concurrency

    def test_full_default_cell_runs_five_repetitions_over_all_ten_scenarios(self):
        records = run_cell(profile_name="interactive", concurrency=4, warmup_passes=1)
        assert len(records) == DEFAULT_REPETITIONS
        for record in records:
            assert record.result["tasks"]["attempted"] == 10
            assert record.result["tasks"]["succeeded"] == 10

    def test_deterministic_reproduction_of_scores_and_statuses(self):
        first = fast_cell(repetitions=1)[0]
        second = fast_cell(repetitions=1)[0]
        assert [e.score for e in first.evaluations] == [e.score for e in second.evaluations]
        assert [x.status for x in first.executions] == [x.status for x in second.executions]
        assert first.result["tasks"] == second.result["tasks"]

    def test_error_behaviors_are_counted_in_taxonomy(self):
        record = fast_cell(
            repetitions=1,
            client=DeterministicMockClient(behavior="malformed_tool_call"),
        )[0]
        tasks = record.result["tasks"]
        assert tasks["succeeded"] == 0
        assert tasks["failed"] == len(FAST_SCENARIOS)
        assert record.result["errors"]["taxonomy"] == {"malformed_tool_call": len(FAST_SCENARIOS)}
        assert record.result["errors"]["error_rate"] == 1.0


@pytest.fixture(scope="module")
def record():
    return run_cell(
        profile_name="batch-heavy",
        concurrency=1,
        repetitions=1,
        warmup_passes=0,
        scenario_ids=FAST_SCENARIOS,
    )[0]


class TestOutputDocuments:
    def test_manifest_and_result_are_schema_valid(self, record):
        validate_run_manifest(record.manifest)
        validate_benchmark_result(record.result)

    def test_mock_execution_mode_is_explicit(self, record):
        assert record.manifest["execution_mode"] == "mock"
        assert record.manifest["serving"]["engine"] == "mock"
        # is_synthetic_example marks committed example FILES, not the mode.
        assert record.manifest["is_synthetic_example"] is False

    def test_gpu_telemetry_is_unavailable_with_reason_not_fabricated(self, record):
        gpu = record.result["gpu"]
        assert gpu["telemetry_available"] is False
        assert "no GPU" in gpu["unavailable_reason"]
        assert "utilization_mean_pct" not in gpu
        assert record.result["throughput"]["successful_tasks_per_gpu_hour"] is None
        assert record.result["slo"]["slo_attaining_throughput_per_gpu_hour"] is None
        assert record.result["economics"]["gpu_hours"] == 0.0
        assert record.result["economics"]["cost_per_successful_task_usd"] is None

    def test_no_gpu_host_fields_are_invented(self, record):
        host = record.manifest["host"]
        for field in ("gpu_model", "gpu_count", "gpu_memory_gb", "driver_version", "cuda_version"):
            assert field not in host

    def test_percentiles_are_suppressed_for_small_samples(self, record):
        task_summary = record.result["latency_ms"]["task_completion"]
        assert task_summary["count"] == len(FAST_SCENARIOS)
        assert task_summary["p95"] is None
        assert task_summary["p99"] is None
        assert task_summary["p50"] is not None

    def test_queue_time_is_empty_not_zero_filled(self, record):
        queue = record.result["latency_ms"]["queue_time"]
        assert queue["count"] == 0
        assert queue["mean"] is None

    def test_slo_block_records_the_proposals(self, record):
        slo = record.result["slo"]
        profile = PROFILES["batch-heavy"]
        assert slo["latency_target_ms"] == profile.slo_task_ms
        assert slo["ttft_target_ms"] is None
        assert slo["quality_threshold"] == PROPOSED_QUALITY_THRESHOLD


class TestResultsPrivacy:
    def test_unset_results_dir_means_no_persistence(self, monkeypatch):
        monkeypatch.delenv(RESULTS_DIR_ENV_VAR, raising=False)
        records = fast_cell(repetitions=1)
        assert persist_records(records) is None

    def test_external_absolute_directory_receives_schema_valid_files(self, monkeypatch, tmp_path):
        external = tmp_path / "lab-results"
        monkeypatch.setenv(RESULTS_DIR_ENV_VAR, str(external))
        records = fast_cell(repetitions=1)
        written = persist_records(records)
        assert written is not None and len(written) == 2
        for path in written:
            assert path.is_relative_to(external)
            document = json.loads(path.read_text(encoding="utf-8"))
            if path.name.endswith(".manifest.json"):
                validate_run_manifest(document)
            else:
                validate_benchmark_result(document)

    def test_repository_interior_results_dir_is_refused(self, monkeypatch):
        monkeypatch.setenv(RESULTS_DIR_ENV_VAR, str(REPO_ROOT / "results"))
        records = fast_cell(repetitions=1)
        from blackwell_lab.paths import ResultsLocationError

        with pytest.raises(ResultsLocationError):
            persist_records(records)

    def test_run_writes_nothing_into_the_repository(self, monkeypatch):
        monkeypatch.delenv(RESULTS_DIR_ENV_VAR, raising=False)
        before = subprocess.run(
            [GIT, "-C", str(REPO_ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        records = fast_cell(repetitions=1)
        persist_records(records)
        after = subprocess.run(
            [GIT, "-C", str(REPO_ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert before == after


class TestCli:
    def test_list_scenarios(self, capsys):
        assert main(["--list-scenarios"]) == 0
        out = capsys.readouterr().out
        assert "elevated-latency-001" in out
        assert len(out.strip().splitlines()) == 10

    def test_smoke_run_without_persistence(self, capsys, monkeypatch):
        monkeypatch.delenv(RESULTS_DIR_ENV_VAR, raising=False)
        code = main(
            [
                "--profile",
                "interactive",
                "--concurrency",
                "1",
                "--repetitions",
                "1",
                "--warmup-passes",
                "1",
                "--scenario",
                "rate-limiting-001",
            ]
        )
        assert code == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["persistence"].startswith("disabled")
        assert summary["repetitions"][0]["succeeded"] == 1

    def test_smoke_run_with_external_persistence(self, capsys, monkeypatch, tmp_path):
        external = tmp_path / "external-results"
        monkeypatch.setenv(RESULTS_DIR_ENV_VAR, str(external))
        code = main(
            [
                "--profile",
                "batch-heavy",
                "--concurrency",
                "4",
                "--repetitions",
                "1",
                "--warmup-passes",
                "0",
                "--scenario",
                "gpu-saturation-001",
                "--scenario",
                "capacity-exhaustion-001",
            ]
        )
        assert code == 0
        summary = json.loads(capsys.readouterr().out)
        assert isinstance(summary["persistence"], list)
        assert len(summary["persistence"]) == 2
        for written in summary["persistence"]:
            assert Path(written).is_relative_to(external)

    def test_cli_refuses_repository_interior_persistence(self, capsys, monkeypatch):
        monkeypatch.setenv(RESULTS_DIR_ENV_VAR, str(REPO_ROOT / "results"))
        code = main(
            [
                "--profile",
                "interactive",
                "--repetitions",
                "1",
                "--warmup-passes",
                "0",
                "--scenario",
                "storage-latency-001",
            ]
        )
        assert code == 3
        captured = capsys.readouterr()
        assert "refusing to persist" in captured.err
