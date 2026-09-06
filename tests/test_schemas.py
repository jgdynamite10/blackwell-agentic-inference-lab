"""Tests for the JSON Schemas and the synthetic example documents."""

import json
from pathlib import Path

import jsonschema
import pytest

from blackwell_lab.schemas import (
    BENCHMARK_RESULT_SCHEMA,
    RUN_MANIFEST_SCHEMA,
    load_schema,
    validate_benchmark_result,
    validate_run_manifest,
)

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
EXAMPLE_MANIFEST = EXAMPLES_DIR / "example-run-manifest.json"
EXAMPLE_RESULT = EXAMPLES_DIR / "example-benchmark-result.json"


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_schemas_are_valid_json_schema():
    for schema_path in (RUN_MANIFEST_SCHEMA, BENCHMARK_RESULT_SCHEMA):
        schema = load_schema(schema_path)
        validator_cls = jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)


def test_example_manifest_validates():
    validate_run_manifest(load_json(EXAMPLE_MANIFEST))


def test_example_result_validates():
    validate_benchmark_result(load_json(EXAMPLE_RESULT))


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
