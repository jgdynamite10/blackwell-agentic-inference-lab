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
