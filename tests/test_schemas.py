"""Tests for the JSON Schemas (2.x) and the synthetic example documents.

Covers the execution-mode conditionals (mock must not fabricate GPU facts;
GPU runs must record them), the available/unavailable latency-measure
branches with per-repetition percentile suppression, mock-validity
invariants (no production performance or SLO claims from mock execution),
and the raw task-observation schema.
"""

import json
from pathlib import Path

import jsonschema
import pytest

from blackwell_lab.schemas import (
    BENCHMARK_RESULT_SCHEMA,
    RUN_MANIFEST_SCHEMA,
    TASK_OBSERVATION_SCHEMA,
    load_schema,
    validate_benchmark_result,
    validate_run_manifest,
    validate_task_observations,
)

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
EXAMPLE_MANIFEST = EXAMPLES_DIR / "example-run-manifest.json"
EXAMPLE_RESULT = EXAMPLES_DIR / "example-benchmark-result.json"
EXAMPLE_OBSERVATIONS = EXAMPLES_DIR / "example-task-observations.json"


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_schemas_are_valid_json_schema():
    for schema_path in (RUN_MANIFEST_SCHEMA, BENCHMARK_RESULT_SCHEMA, TASK_OBSERVATION_SCHEMA):
        schema = load_schema(schema_path)
        validator_cls = jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)


def test_example_manifest_validates():
    validate_run_manifest(load_json(EXAMPLE_MANIFEST))


def test_example_result_validates():
    validate_benchmark_result(load_json(EXAMPLE_RESULT))


def test_example_observations_validate():
    validate_task_observations(load_json(EXAMPLE_OBSERVATIONS))


def test_examples_are_labeled_synthetic():
    for path in (EXAMPLE_MANIFEST, EXAMPLE_RESULT):
        document = load_json(path)
        assert document["is_synthetic_example"] is True, (
            f"{path.name} must be labeled as a synthetic example"
        )


def test_manifest_rejects_missing_provenance():
    document = load_json(EXAMPLE_MANIFEST)
    del document["model"]["artifact_hash"]
    with pytest.raises(jsonschema.ValidationError):
        validate_run_manifest(document)


def test_manifest_rejects_mutable_container_tag():
    document = load_json(EXAMPLE_MANIFEST)
    document["serving"]["container_digest"] = "docker.io/example/vllm:latest"
    with pytest.raises(jsonschema.ValidationError):
        validate_run_manifest(document)


def test_manifest_rejects_unknown_comparison_mode():
    document = load_json(EXAMPLE_MANIFEST)
    document["cloud"]["comparison_mode"] = "mixed"
    with pytest.raises(jsonschema.ValidationError):
        validate_run_manifest(document)


def test_result_rejects_missing_slo_block():
    document = load_json(EXAMPLE_RESULT)
    del document["slo"]
    with pytest.raises(jsonschema.ValidationError):
        validate_benchmark_result(document)


class TestFormatChecking:
    """Invalid date and date-time strings must fail, not silently pass."""

    def test_invalid_datetime_is_rejected(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["created_at_utc"] = "not-a-timestamp"
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_impossible_datetime_is_rejected(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["timing_conditions"]["started_at_utc"] = "2026-01-32T00:00:00Z"
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_invalid_date_is_rejected(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["cloud"]["price_source_date"] = "2026-13-99"
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_valid_datetime_still_passes(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["created_at_utc"] = "2026-02-03T04:05:06+00:00"
        validate_run_manifest(document)


class TestDigestValidation:
    """Artifact digests are algorithm-specific (e.g. exactly 64 hex for SHA-256)."""

    def test_sha256_with_63_hex_chars_is_rejected(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["model"]["artifact_hash"] = "sha256:" + "0" * 63
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_sha256_with_65_hex_chars_is_rejected(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["model"]["artifact_hash"] = "sha256:" + "0" * 65
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_sha256_with_non_hex_characters_is_rejected(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["model"]["artifact_hash"] = "sha256:" + "g" * 64
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_sha512_requires_128_hex_chars(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["model"]["artifact_hash"] = "sha512:" + "0" * 64
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)
        document["model"]["artifact_hash"] = "sha512:" + "0" * 128
        validate_run_manifest(document)

    def test_container_digest_requires_64_hex_chars(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["serving"]["container_digest"] = "docker.io/example/vllm@sha256:" + "0" * 63
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_workload_catalog_digest_is_a_dedicated_field(self):
        """The catalog digest lives under workload — it is never the model
        artifact hash — and must be a well-formed sha256 content address."""
        document = load_json(EXAMPLE_MANIFEST)
        assert document["workload"]["catalog_digest"].startswith("sha256:")
        document["workload"]["catalog_digest"] = "sha256:" + "0" * 63
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)
        del document["workload"]["catalog_digest"]
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)


class TestControlledResourceMode:
    """controlled-resource manifests must record the applied resource limits."""

    def test_controlled_resource_without_limits_is_rejected(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["cloud"]["comparison_mode"] = "controlled-resource"
        document["cloud"].pop("resource_limits", None)
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_controlled_resource_with_limits_passes(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["cloud"]["comparison_mode"] = "controlled-resource"
        document["cloud"]["resource_limits"] = {
            "vcpu_limit": 14,
            "memory_limit_gib": 100,
        }
        validate_run_manifest(document)

    def test_partial_resource_limits_are_rejected(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["cloud"]["comparison_mode"] = "controlled-resource"
        document["cloud"]["resource_limits"] = {"vcpu_limit": 14}
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_provider_native_mode_does_not_require_limits(self):
        document = load_json(EXAMPLE_MANIFEST)
        assert document["cloud"]["comparison_mode"] == "provider-native"
        assert "resource_limits" not in document["cloud"]
        validate_run_manifest(document)


def as_mock_manifest(document: dict) -> dict:
    """Converts the GPU example manifest into a well-formed mock-mode one."""
    document["execution_mode"] = "mock"
    document.pop("model", None)
    document["serving"] = {"engine": "mock", "engine_version": "3.0.0"}
    gpu_fields = (
        "gpu_model",
        "gpu_count",
        "gpu_memory_gb",
        "driver_version",
        "driver_max_cuda_version",
    )
    for gpu_field in gpu_fields:
        document["host"].pop(gpu_field, None)
    document["cloud"]["comparison_mode"] = "not-applicable"
    return document


class TestManifestExecutionMode:
    """Mock runs must not fabricate GPU facts; GPU runs must record them."""

    def test_execution_mode_is_required(self):
        document = load_json(EXAMPLE_MANIFEST)
        del document["execution_mode"]
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_unknown_execution_mode_is_rejected(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["execution_mode"] = "cpu"
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_well_formed_mock_manifest_is_valid(self):
        validate_run_manifest(as_mock_manifest(load_json(EXAMPLE_MANIFEST)))

    def test_gpu_mode_requires_gpu_host_fields(self):
        document = load_json(EXAMPLE_MANIFEST)
        del document["host"]["gpu_model"]
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_gpu_mode_requires_container_digest(self):
        document = load_json(EXAMPLE_MANIFEST)
        del document["serving"]["container_digest"]
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_gpu_mode_requires_the_model_block(self):
        document = load_json(EXAMPLE_MANIFEST)
        del document["model"]
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_gpu_mode_rejects_the_mock_engine(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["serving"]["engine"] = "mock"
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_gpu_mode_rejects_not_applicable_comparison(self):
        document = load_json(EXAMPLE_MANIFEST)
        document["cloud"]["comparison_mode"] = "not-applicable"
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_mock_mode_requires_mock_engine(self):
        document = as_mock_manifest(load_json(EXAMPLE_MANIFEST))
        document["serving"]["engine"] = "vllm"
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_mock_mode_forbids_gpu_host_fields(self):
        document = as_mock_manifest(load_json(EXAMPLE_MANIFEST))
        document["host"]["gpu_model"] = "NVIDIA RTX PRO 6000 Blackwell Server Edition"
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_mock_mode_forbids_container_digest(self):
        document = as_mock_manifest(load_json(EXAMPLE_MANIFEST))
        document["serving"]["container_digest"] = "docker.io/example/vllm@sha256:" + "0" * 64
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_mock_mode_forbids_a_model_block(self):
        """No workload digest may be dressed up as a mock model artifact."""
        gpu_example = load_json(EXAMPLE_MANIFEST)
        document = as_mock_manifest(load_json(EXAMPLE_MANIFEST))
        document["model"] = gpu_example["model"]
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)

    def test_mock_mode_requires_not_applicable_comparison(self):
        document = as_mock_manifest(load_json(EXAMPLE_MANIFEST))
        document["cloud"]["comparison_mode"] = "provider-native"
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)


class TestGpuTelemetryAvailability:
    """GPU telemetry is either genuinely present or explicitly unavailable."""

    def test_example_gpu_block_declares_telemetry_available(self):
        document = load_json(EXAMPLE_RESULT)
        assert document["gpu"]["telemetry_available"] is True

    def test_unavailable_telemetry_requires_a_reason(self):
        document = load_json(EXAMPLE_RESULT)
        document["gpu"] = {"telemetry_available": False}
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_unavailable_telemetry_with_reason_is_valid(self):
        document = load_json(EXAMPLE_RESULT)
        document["gpu"] = {
            "telemetry_available": False,
            "unavailable_reason": "no GPU present in this configuration",
        }
        validate_benchmark_result(document)

    def test_unavailable_telemetry_forbids_metric_fields(self):
        document = load_json(EXAMPLE_RESULT)
        document["gpu"] = {
            "telemetry_available": False,
            "unavailable_reason": "no GPU present in this configuration",
            "utilization_mean_pct": 0.0,
        }
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_available_telemetry_requires_metrics(self):
        document = load_json(EXAMPLE_RESULT)
        document["gpu"] = {"telemetry_available": True}
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)


class TestLatencyMeasureBranches:
    """Every measure is available-with-data or unavailable-with-reason."""

    def test_unavailable_measure_requires_a_reason(self):
        document = load_json(EXAMPLE_RESULT)
        document["latency_ms"]["queue_time"] = {"available": False}
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_unavailable_measure_with_reason_is_valid(self):
        document = load_json(EXAMPLE_RESULT)
        document["latency_ms"]["queue_time"] = {
            "available": False,
            "reason": "the serving engine reported no queue telemetry",
        }
        validate_benchmark_result(document)

    def test_unavailable_measure_forbids_statistics(self):
        document = load_json(EXAMPLE_RESULT)
        document["latency_ms"]["queue_time"] = {
            "available": False,
            "reason": "no telemetry",
            "mean": 5.0,
        }
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_task_accounting_fields_are_all_required(self):
        for field in ("attempted", "succeeded", "quality_failed", "errored", "timed_out"):
            document = load_json(EXAMPLE_RESULT)
            del document["tasks"][field]
            with pytest.raises(jsonschema.ValidationError):
                validate_benchmark_result(document)


class TestPercentileSuppression:
    """The schema itself enforces the p95/p99 sample-count rules."""

    def test_p95_with_insufficient_count_is_rejected(self):
        document = load_json(EXAMPLE_RESULT)
        summary = document["latency_ms"]["task_completion"]
        summary["count"] = 199
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_p95_at_200_observations_is_reported(self):
        document = load_json(EXAMPLE_RESULT)
        summary = document["latency_ms"]["task_completion"]
        assert summary["count"] == 200
        assert summary["p95"] is not None
        validate_benchmark_result(document)

    def test_p99_below_1000_observations_must_be_null(self):
        document = load_json(EXAMPLE_RESULT)
        summary = document["latency_ms"]["task_completion"]
        assert summary["count"] < 1000
        summary["p99"] = 60000
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_sufficient_count_requires_reported_percentiles(self):
        document = load_json(EXAMPLE_RESULT)
        summary = document["latency_ms"]["inter_token"]
        assert summary["count"] >= 1000
        summary["p99"] = None
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_count_is_required(self):
        document = load_json(EXAMPLE_RESULT)
        del document["latency_ms"]["inter_token"]["count"]
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)


def as_mock_result(document: dict) -> dict:
    """Converts the GPU example result into a well-formed mock-mode one."""
    document["execution_mode"] = "mock"
    for measure in (
        "task_completion",
        "time_to_first_token",
        "inter_token",
        "queue_time",
        "model_serving_time",
    ):
        document["latency_ms"][measure] = {
            "available": False,
            "reason": "mock execution is functional-only",
        }
    document["throughput"] = {
        "available": False,
        "reason": "mock Python replay speed is never model tokens/sec",
    }
    document["gpu"] = {
        "telemetry_available": False,
        "unavailable_reason": "mock execution mode: no GPU present",
    }
    document["slo"]["slo_attaining_tasks"] = None
    document["slo"]["attainment_unavailable_reason"] = (
        "host-clock replay latency is not production latency"
    )
    document["slo"]["slo_attaining_throughput_per_gpu_hour"] = None
    document["mock_diagnostics"] = {
        "note": "functional diagnostics only; not benchmark performance",
        "host_clock_task_completion_ms": {"mean": 90.0, "min": 85.0, "max": 130.0, "count": 200},
        "wall_time_s": 18.5,
    }
    return document


class TestMockResultValidity:
    """Mock execution can never claim production performance or SLOs."""

    def test_well_formed_mock_result_is_valid(self):
        validate_benchmark_result(as_mock_result(load_json(EXAMPLE_RESULT)))

    def test_mock_mode_cannot_claim_task_latency(self):
        document = as_mock_result(load_json(EXAMPLE_RESULT))
        document["latency_ms"]["task_completion"] = load_json(EXAMPLE_RESULT)["latency_ms"][
            "task_completion"
        ]
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_mock_mode_cannot_claim_throughput(self):
        document = as_mock_result(load_json(EXAMPLE_RESULT))
        document["throughput"] = load_json(EXAMPLE_RESULT)["throughput"]
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_mock_mode_cannot_claim_slo_attainment(self):
        document = as_mock_result(load_json(EXAMPLE_RESULT))
        document["slo"]["slo_attaining_tasks"] = 170
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_mock_mode_requires_the_diagnostics_namespace(self):
        document = as_mock_result(load_json(EXAMPLE_RESULT))
        del document["mock_diagnostics"]
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_null_slo_attainment_requires_a_reason(self):
        document = as_mock_result(load_json(EXAMPLE_RESULT))
        del document["slo"]["attainment_unavailable_reason"]
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_gpu_mode_forbids_mock_diagnostics(self):
        document = load_json(EXAMPLE_RESULT)
        document["mock_diagnostics"] = {
            "note": "does not belong here",
            "host_clock_task_completion_ms": {"mean": 1.0, "min": 1.0, "max": 1.0, "count": 1},
            "wall_time_s": 1.0,
        }
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_gpu_mode_requires_integer_slo_attainment(self):
        document = load_json(EXAMPLE_RESULT)
        document["slo"]["slo_attaining_tasks"] = None
        document["slo"]["attainment_unavailable_reason"] = "not measurable"
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)


class TestObservationReferences:
    def test_persisted_observations_require_file_and_hash(self):
        document = load_json(EXAMPLE_RESULT)
        del document["observations"]["measured_sha256"]
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_unpersisted_observations_forbid_file_references(self):
        document = load_json(EXAMPLE_RESULT)
        document["observations"]["persisted"] = False
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)

    def test_observation_references_must_be_safe_relative_names(self):
        for bad_name in ("../escape.json", "/etc/passwd", "a/b.json", ".hidden"):
            document = load_json(EXAMPLE_RESULT)
            document["observations"]["measured_file"] = bad_name
            with pytest.raises(jsonschema.ValidationError):
                validate_benchmark_result(document)

    def test_manifest_ref_must_be_a_safe_relative_name(self):
        document = load_json(EXAMPLE_RESULT)
        document["manifest_ref"] = "../../outside.json"
        with pytest.raises(jsonschema.ValidationError):
            validate_benchmark_result(document)


class TestTaskObservationSchema:
    def test_phase_labels_are_closed_vocabulary(self):
        document = load_json(EXAMPLE_OBSERVATIONS)
        document["phase"] = "production"
        with pytest.raises(jsonschema.ValidationError):
            validate_task_observations(document)

    def test_warmup_phase_is_first_class(self):
        document = load_json(EXAMPLE_OBSERVATIONS)
        document["phase"] = "warmup"
        validate_task_observations(document)

    def test_observation_requires_timing_and_identity(self):
        for field in ("instance_id", "submitted_offset_ms", "e2e_ms", "tool_trace", "evaluation"):
            document = load_json(EXAMPLE_OBSERVATIONS)
            del document["observations"][0][field]
            with pytest.raises(jsonschema.ValidationError):
                validate_task_observations(document)

    def test_score_is_binary(self):
        document = load_json(EXAMPLE_OBSERVATIONS)
        document["observations"][0]["evaluation"]["score"] = 0.66
        with pytest.raises(jsonschema.ValidationError):
            validate_task_observations(document)

    def test_status_vocabulary_is_closed(self):
        document = load_json(EXAMPLE_OBSERVATIONS)
        document["observations"][0]["status"] = "aborted"
        with pytest.raises(jsonschema.ValidationError):
            validate_task_observations(document)
