"""Offline tests for the gpu-mode real-benchmark assembly.

The serving endpoint is stood in for by mock clients (with authoritative
usage events) and GPU telemetry by fake samplers — no GPU, no network. The
tests assert that the assembly preserves the Phase 2 contracts, produces
schema-valid gpu-mode documents, respects the RunMode.REAL privacy guard,
and fails visibly whenever a required measurement is unavailable.
"""

from __future__ import annotations

import json

import pytest
from fakes import FakeClock

from blackwell_lab.cloud.realbench import (
    RealRunSpec,
    RequiredMeasurementError,
    run_real_cell,
)
from blackwell_lab.cloud.telemetry import GpuSample, TelemetryUnavailable, summarize_gpu_samples
from blackwell_lab.paths import ResultsLocationError
from blackwell_lab.workload.clock import SYSTEM_CLOCK
from blackwell_lab.workload.model_client import DeterministicMockClient, StreamEvent
from blackwell_lab.workload.validation import ConfigError

MODEL_BLOCK = {
    "artifact": "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16",
    "revision": "0123456789abcdef0123456789abcdef01234567",
    "artifact_hash": "sha256:" + "ab" * 32,
    "precision": "bf16",
    "license": "OpenMDW-1.1",
}
CONTAINER_DIGEST = "docker.io/vllm/vllm-openai@sha256:" + "cd" * 32

HOST = {
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
    "cuda_version": "13.0",
}


def make_spec(**overrides) -> RealRunSpec:
    defaults = dict(
        profile_name="interactive",
        concurrency=1,
        comparison_mode="provider-native",
        instance_type="g9-fake-gpu-plan",
        region="us-fake-1",
        list_price_usd_per_hour=2.5,
        price_source_date="2026-09-06",
        model=MODEL_BLOCK,
        engine="vllm",
        engine_version="0.28.0",
        container_digest=CONTAINER_DIGEST,
        repetitions=1,
        warmup_passes=1,
        tasks_per_repetition=10,
        run_label="test-pilot",
    )
    defaults.update(overrides)
    return RealRunSpec(**defaults)


class UsageMockClient(DeterministicMockClient):
    """Mock client that also reports authoritative usage token counts."""

    def stream_turn(self, messages, settings, *, deadline=None, clock=SYSTEM_CLOCK):
        yield from super().stream_turn(messages, settings, deadline=deadline, clock=clock)
        yield StreamEvent(kind="usage", output_tokens=23)


class SilentClient(DeterministicMockClient):
    """Emits nothing at all: no content, no usage (adversarial)."""

    def stream_turn(self, messages, settings, *, deadline=None, clock=SYSTEM_CLOCK):
        return iter(())


class FakeSampler:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def summary(self, *, successful_tasks: int) -> dict:
        if self.fail:
            raise TelemetryUnavailable("GPU sampling failed mid-run: fake failure")
        return summarize_gpu_samples(
            [GpuSample(80.0, 70.0, 400.0, 65.0), GpuSample(90.0, 75.0, 420.0, 66.0)],
            sample_interval_s=1.0,
            successful_tasks=successful_tasks,
        )


@pytest.fixture()
def real_results_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
    return tmp_path


class TestSpecValidation:
    def test_mock_engine_is_refused(self):
        with pytest.raises(ConfigError, match="never 'mock'"):
            run_real_cell(
                make_spec(engine="mock"),
                UsageMockClient(),
                host=HOST,
                sampler_factory=FakeSampler,
            )

    def test_controlled_resource_requires_joint_limits(self):
        with pytest.raises(ConfigError, match="resource_limits"):
            run_real_cell(
                make_spec(comparison_mode="controlled-resource"),
                UsageMockClient(),
                host=HOST,
                sampler_factory=FakeSampler,
            )

    def test_not_applicable_comparison_mode_is_refused_for_genuine_runs(self):
        with pytest.raises(ConfigError, match="comparison_mode"):
            run_real_cell(
                make_spec(comparison_mode="not-applicable"),
                UsageMockClient(),
                host=HOST,
                sampler_factory=FakeSampler,
            )

    def test_unpinned_model_and_container_are_refused(self):
        with pytest.raises(ConfigError, match="artifact_hash"):
            run_real_cell(
                make_spec(model={**MODEL_BLOCK, "artifact_hash": "latest"}),
                UsageMockClient(),
                host=HOST,
                sampler_factory=FakeSampler,
            )
        with pytest.raises(ConfigError, match="container_digest"):
            run_real_cell(
                make_spec(container_digest="vllm/vllm-openai:v0.28.0"),
                UsageMockClient(),
                host=HOST,
                sampler_factory=FakeSampler,
            )


class TestPrivacyGuard:
    def test_unset_results_dir_fails_closed_before_any_work(self, monkeypatch):
        monkeypatch.delenv("LAB_RESULTS_DIR", raising=False)
        client = UsageMockClient()
        with pytest.raises(ResultsLocationError):
            run_real_cell(
                make_spec(), client, host=HOST, sampler_factory=FakeSampler, clock=FakeClock()
            )

    def test_repository_interior_results_dir_is_refused(self, monkeypatch):
        from pathlib import Path

        import blackwell_lab

        repo_root = str(Path(blackwell_lab.__file__).parents[2])
        monkeypatch.setenv("LAB_RESULTS_DIR", repo_root)
        with pytest.raises(ResultsLocationError):
            run_real_cell(
                make_spec(),
                UsageMockClient(),
                host=HOST,
                sampler_factory=FakeSampler,
                clock=FakeClock(),
            )


class TestGenuineCell:
    def _run(self, real_results_dir, **spec_overrides):
        return run_real_cell(
            make_spec(**spec_overrides),
            UsageMockClient(),
            host=HOST,
            sampler_factory=FakeSampler,
            clock=FakeClock(),
        )

    def test_documents_are_gpu_mode_and_schema_valid(self, real_results_dir):
        records = self._run(real_results_dir)
        assert len(records) == 1
        manifest, result = records[0].manifest, records[0].result
        # run_real_cell already schema-validates; assert the gpu-mode shape.
        assert manifest["execution_mode"] == "gpu"
        assert manifest["model"]["artifact_hash"].startswith("sha256:")
        assert manifest["serving"]["container_digest"] == CONTAINER_DIGEST
        assert manifest["host"]["gpu_model"].startswith("NVIDIA RTX PRO 6000")
        assert manifest["cloud"]["comparison_mode"] == "provider-native"
        assert result["execution_mode"] == "gpu"
        assert "mock_diagnostics" not in result
        assert result["latency_ms"]["task_completion"]["available"] is True
        assert result["throughput"]["available"] is True
        assert result["throughput"]["total_output_tokens"] > 0
        assert isinstance(result["slo"]["slo_attaining_tasks"], int)
        assert result["gpu"]["telemetry_available"] is True

    def test_chunks_are_never_tokens_itl_is_unavailable_with_reason(self, real_results_dir):
        result = self._run(real_results_dir)[0].result
        itl = result["latency_ms"]["inter_token"]
        assert itl["available"] is False
        assert "transport chunks are never tokens" in itl["reason"]

    def test_queue_time_is_unavailable_when_the_engine_reports_none(self, real_results_dir):
        result = self._run(real_results_dir)[0].result
        queue = result["latency_ms"]["queue_time"]
        assert queue["available"] is False
        assert "only when genuinely available" in queue["reason"]

    def test_raw_observations_are_persisted_promptly_and_hashed(self, real_results_dir):
        records = self._run(real_results_dir)
        record = records[0]
        run_dir = real_results_dir / "real-runs" / "test-pilot"
        assert set(record.written_files) == {
            f"{record.run_id}.observations.json",
            f"{record.run_id}.warmup-observations.json",
            f"{record.run_id}.manifest.json",
            f"{record.run_id}.result.json",
        }
        for name in record.written_files:
            assert (run_dir / name).is_file()
        observations = record.result["observations"]
        assert observations["persisted"] is True
        assert observations["measured_count"] == 10
        assert observations["warmup_count"] == 10  # warm-up retained separately

    def test_nothing_is_written_inside_the_repository(self, real_results_dir):
        import shutil
        import subprocess

        git = shutil.which("git")
        if git is None:
            pytest.skip("git not available")
        self._run(real_results_dir)
        status = subprocess.run(
            [git, "status", "--porcelain", "results/"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
        assert status.strip() == ""

    def test_controlled_resource_mode_records_the_joint_limits(self, real_results_dir):
        records = self._run(
            real_results_dir,
            comparison_mode="controlled-resource",
            resource_limits={"vcpu_limit": 14, "memory_limit_gib": 100},
        )
        cloud = records[0].manifest["cloud"]
        assert cloud["comparison_mode"] == "controlled-resource"
        assert cloud["resource_limits"] == {"vcpu_limit": 14, "memory_limit_gib": 100}


class TestFailVisible:
    def test_missing_usage_data_raises_instead_of_downgrading(self, real_results_dir):
        with pytest.raises(RequiredMeasurementError, match="usage"):
            run_real_cell(
                make_spec(),
                DeterministicMockClient(),  # no usage events
                host=HOST,
                sampler_factory=FakeSampler,
                clock=FakeClock(),
            )

    def test_broken_gpu_telemetry_raises_instead_of_zero_filling(self, real_results_dir):
        with pytest.raises(TelemetryUnavailable, match="fake failure"):
            run_real_cell(
                make_spec(),
                UsageMockClient(),
                host=HOST,
                sampler_factory=lambda: FakeSampler(fail=True),
                clock=FakeClock(),
            )

    def test_interactive_without_ttft_raises(self, real_results_dir):
        with pytest.raises(RequiredMeasurementError, match="TTFT"):
            run_real_cell(
                make_spec(),
                SilentClient(),
                host=HOST,
                sampler_factory=FakeSampler,
                clock=FakeClock(),
            )


class TestVerifiability:
    def test_persisted_documents_round_trip_through_json(self, real_results_dir):
        record = run_real_cell(
            make_spec(),
            UsageMockClient(),
            host=HOST,
            sampler_factory=FakeSampler,
            clock=FakeClock(),
        )[0]
        run_dir = real_results_dir / "real-runs" / "test-pilot"
        loaded = json.loads((run_dir / f"{record.run_id}.result.json").read_text())
        assert loaded == record.result
