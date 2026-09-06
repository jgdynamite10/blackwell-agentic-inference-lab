"""Offline tests for telemetry collection: injected runners, no real tools.

Everything asserts the truthfulness contract: values come from genuine
command output or the call fails with an explicit reason — never fabricated.
"""

from __future__ import annotations

import hashlib
import time

import pytest

from blackwell_lab.cloud.telemetry import (
    ArtifactVerificationError,
    GpuSample,
    GpuSamplerThread,
    TelemetryUnavailable,
    collect_gpu_facts,
    collect_host_facts,
    observe_container_cuda_version,
    read_gpu_sample,
    resolve_container_digest,
    summarize_gpu_samples,
    verify_model_artifact,
)

FACT_ROW = "NVIDIA RTX PRO 6000 Blackwell Server Edition, 98304, 580.65.06\n"
BANNER = "| NVIDIA-SMI 580.65.06    Driver Version: 580.65.06    CUDA Version: 13.0 |\n"
SAMPLE_ROW = "87, 81920, 512.5, 71\n"


def fake_runner(responses: dict):
    def run(argv):
        for key, value in responses.items():
            if key in " ".join(argv):
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected command: {argv}")

    return run


class TestHostFacts:
    def test_host_facts_have_the_manifest_shape(self):
        facts = collect_host_facts(
            storage_description="plan NVMe (not measured)",
            network_description="plan default networking",
            virtualization="KVM (per provider docs)",
        )
        assert facts["vcpu_count"] >= 1
        assert facts["system_memory_gib"] > 0
        assert facts["cpu_model"]
        assert facts["operating_system"]
        assert facts["network_description"] == "plan default networking"
        assert facts["virtualization"] == "KVM (per provider docs)"

    def test_virtualization_is_omitted_when_unknown_never_defaulted(self):
        facts = collect_host_facts(
            storage_description="s",
            network_description="n",
        )
        assert "virtualization" not in facts


class TestGpuFacts:
    def test_facts_are_parsed_from_nvidia_smi(self):
        runner = fake_runner({"--query-gpu=name": FACT_ROW, "nvidia-smi": BANNER})
        facts = collect_gpu_facts(runner)
        assert facts == {
            "gpu_model": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
            "gpu_count": 1,
            "gpu_memory_gb": 96.0,
            "driver_version": "580.65.06",
            "driver_max_cuda_version": "13.0",
        }
        # The banner value is the DRIVER's max supported CUDA — the key name
        # must never claim it is the runtime CUDA version.
        assert "cuda_version" not in facts

    def test_multiple_gpus_are_rejected_for_the_single_gpu_baseline(self):
        runner = fake_runner({"--query-gpu=name": FACT_ROW + FACT_ROW, "nvidia-smi": BANNER})
        with pytest.raises(TelemetryUnavailable, match="exactly one GPU"):
            collect_gpu_facts(runner)

    def test_missing_tool_is_unavailable_not_fabricated(self):
        runner = fake_runner({"nvidia-smi": TelemetryUnavailable("required tool not found")})
        with pytest.raises(TelemetryUnavailable):
            collect_gpu_facts(runner)

    def test_missing_cuda_version_is_unavailable(self):
        runner = fake_runner({"--query-gpu=name": FACT_ROW, "nvidia-smi": "no cuda here\n"})
        with pytest.raises(TelemetryUnavailable, match="CUDA version"):
            collect_gpu_facts(runner)


class TestContainerCudaRuntime:
    def test_container_runtime_cuda_is_observed_inside_the_container(self):
        runner = fake_runner({"docker exec": "13.0\n"})
        assert observe_container_cuda_version("bwlab-vllm", runner) == "13.0"

    def test_unparseable_container_cuda_fails_visibly(self):
        runner = fake_runner({"docker exec": "None\n"})
        with pytest.raises(TelemetryUnavailable, match="container's CUDA runtime"):
            observe_container_cuda_version("bwlab-vllm", runner)


class TestGpuSampling:
    def test_sample_parses_all_four_metrics_and_is_timestamped(self):
        runner = fake_runner({"--query-gpu=utilization.gpu": SAMPLE_ROW})
        sample = read_gpu_sample(runner)
        assert sample.utilization_pct == 87.0
        assert sample.memory_used_gib == 80.0
        assert sample.power_watts == 512.5
        assert sample.temperature_c == 71.0
        assert sample.monotonic_s > 0  # monotonic-clock timestamp

    def test_energy_integrates_actual_intervals_not_nominal_interval(self):
        # Two intervals of DIFFERENT actual lengths (1 s and 3 s); a
        # sample-count x nominal-interval scheme would get this wrong.
        samples = [
            GpuSample(100.0, 50.0, 10.0, 100.0, 60.0),
            GpuSample(101.0, 90.0, 80.0, 300.0, 70.0),
            GpuSample(104.0, 70.0, 40.0, 100.0, 65.0),
        ]
        block = summarize_gpu_samples(
            samples,
            window_started_monotonic_s=100.0,
            window_ended_monotonic_s=104.0,
            successful_tasks=4,
        )
        # Trapezoid: (100+300)/2 x 1 + (300+100)/2 x 3 = 200 + 600 = 800 J.
        assert block["energy_joules"] == 800.0
        assert block["power_mean_watts"] == 200.0  # energy / covered span
        assert block["energy_per_successful_task_joules"] == 200.0
        assert block["utilization_p95_pct"] == 90.0
        assert block["memory_peak_gib"] == 80.0

    def test_sampling_block_records_coverage_intervals_and_method(self):
        samples = [
            GpuSample(100.0, 10.0, 1.0, 100.0, 50.0),
            GpuSample(101.0, 20.0, 2.0, 100.0, 74.5),
            GpuSample(102.5, 30.0, 3.0, 100.0, 60.0),
        ]
        block = summarize_gpu_samples(
            samples,
            window_started_monotonic_s=100.0,
            window_ended_monotonic_s=103.0,
            successful_tasks=1,
        )
        sampling = block["sampling"]
        assert sampling["sample_count"] == 3
        assert sampling["window_duration_s"] == 3.0
        assert sampling["covered_duration_s"] == 2.5
        assert sampling["coverage_fraction"] == round(2.5 / 3.0, 4)
        assert sampling["interval_mean_s"] == 1.25
        assert sampling["interval_max_s"] == 1.5
        assert sampling["peak_temperature_c"] == 74.5
        assert "trapezoidal" in sampling["integration_method"]
        assert "no extrapolation" in sampling["integration_method"]

    def test_insufficient_coverage_fails_visibly(self):
        samples = [
            GpuSample(100.0, 1.0, 1.0, 1.0, 1.0),
            GpuSample(100.5, 1.0, 1.0, 1.0, 1.0),
        ]
        with pytest.raises(TelemetryUnavailable, match="coverage"):
            summarize_gpu_samples(
                samples,
                window_started_monotonic_s=100.0,
                window_ended_monotonic_s=110.0,
                successful_tasks=1,
            )

    def test_fewer_than_two_samples_is_unavailable_never_zero_filled(self):
        with pytest.raises(TelemetryUnavailable, match="at least two"):
            summarize_gpu_samples(
                [],
                window_started_monotonic_s=0.0,
                window_ended_monotonic_s=1.0,
                successful_tasks=1,
            )
        with pytest.raises(TelemetryUnavailable, match="at least two"):
            summarize_gpu_samples(
                [GpuSample(1.0, 1.0, 1.0, 1.0, 1.0)],
                window_started_monotonic_s=0.0,
                window_ended_monotonic_s=1.0,
                successful_tasks=1,
            )

    def test_non_increasing_timestamps_are_rejected(self):
        samples = [
            GpuSample(101.0, 1.0, 1.0, 1.0, 1.0),
            GpuSample(100.0, 1.0, 1.0, 1.0, 1.0),
        ]
        with pytest.raises(TelemetryUnavailable, match="strictly increasing"):
            summarize_gpu_samples(
                samples,
                window_started_monotonic_s=100.0,
                window_ended_monotonic_s=102.0,
                successful_tasks=1,
            )

    def test_zero_successful_tasks_yield_null_energy_per_task(self):
        samples = [
            GpuSample(0.0, 1.0, 1.0, 1.0, 1.0),
            GpuSample(1.0, 1.0, 1.0, 1.0, 1.0),
        ]
        block = summarize_gpu_samples(
            samples,
            window_started_monotonic_s=0.0,
            window_ended_monotonic_s=1.0,
            successful_tasks=0,
        )
        assert block["energy_per_successful_task_joules"] is None

    def test_sampler_thread_collects_and_summarizes(self):
        runner = fake_runner({"--query-gpu=utilization.gpu": SAMPLE_ROW})
        sampler = GpuSamplerThread(runner, interval_s=0.01)
        sampler.start()
        time.sleep(0.15)
        sampler.stop()
        block = sampler.summary(successful_tasks=1)
        assert block["telemetry_available"] is True
        assert block["memory_peak_gib"] == 80.0
        assert block["sampling"]["sample_count"] >= 2

    def test_sampler_failure_fails_visibly_from_summary(self):
        runner = fake_runner({"--query-gpu=utilization.gpu": TelemetryUnavailable("tool vanished")})
        sampler = GpuSamplerThread(runner, interval_s=0.01)
        sampler.start()
        time.sleep(0.05)
        sampler.stop()
        with pytest.raises(TelemetryUnavailable, match="failed mid-run"):
            sampler.summary(successful_tasks=1)


class TestContainerDigest:
    def test_immutable_digest_is_returned(self):
        digest = "docker.io/vllm/vllm-openai@sha256:" + "ab" * 32
        runner = fake_runner({"docker inspect": digest + "\n"})
        assert resolve_container_digest("vllm/vllm-openai:v0.28.0", runner) == digest

    def test_tag_only_output_is_rejected(self):
        runner = fake_runner({"docker inspect": "vllm/vllm-openai:v0.28.0\n"})
        with pytest.raises(TelemetryUnavailable, match="immutable"):
            resolve_container_digest("vllm/vllm-openai:v0.28.0", runner)


class TestArtifactVerification:
    def _write_artifact(self, tmp_path, contents: dict):
        artifact_dir = tmp_path / "model"
        artifact_dir.mkdir()
        lines = []
        for name, data in contents.items():
            (artifact_dir / name).write_bytes(data)
            lines.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
        manifest = tmp_path / "model.sha256"
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return artifact_dir, manifest

    def test_verified_artifact_returns_a_deterministic_aggregate(self, tmp_path):
        artifact_dir, manifest = self._write_artifact(
            tmp_path, {"a.safetensors": b"AAAA", "b.safetensors": b"BBBB"}
        )
        aggregate_1 = verify_model_artifact(artifact_dir, manifest)
        aggregate_2 = verify_model_artifact(artifact_dir, manifest)
        assert aggregate_1 == aggregate_2
        assert aggregate_1.startswith("sha256:") and len(aggregate_1) == 71

    def test_tampered_file_fails_verification(self, tmp_path):
        artifact_dir, manifest = self._write_artifact(tmp_path, {"a.safetensors": b"AAAA"})
        (artifact_dir / "a.safetensors").write_bytes(b"tampered")
        with pytest.raises(ArtifactVerificationError, match="digest mismatch"):
            verify_model_artifact(artifact_dir, manifest)

    def test_missing_file_fails_verification(self, tmp_path):
        artifact_dir, manifest = self._write_artifact(tmp_path, {"a.safetensors": b"AAAA"})
        (artifact_dir / "a.safetensors").unlink()
        with pytest.raises(ArtifactVerificationError, match="missing"):
            verify_model_artifact(artifact_dir, manifest)

    def test_empty_or_malformed_manifest_is_rejected(self, tmp_path):
        manifest = tmp_path / "empty.sha256"
        manifest.write_text("", encoding="utf-8")
        with pytest.raises(ArtifactVerificationError, match="empty"):
            verify_model_artifact(tmp_path, manifest)
        manifest.write_text("not-a-digest-line\n", encoding="utf-8")
        with pytest.raises(ArtifactVerificationError, match="malformed"):
            verify_model_artifact(tmp_path, manifest)

    def test_path_traversal_in_manifest_is_rejected(self, tmp_path):
        manifest = tmp_path / "evil.sha256"
        manifest.write_text(f"{'0' * 64}  ../outside.bin\n", encoding="utf-8")
        with pytest.raises(ArtifactVerificationError, match="unsafe path"):
            verify_model_artifact(tmp_path, manifest)
