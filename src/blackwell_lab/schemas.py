"""Access to and validation against the repository's JSON Schemas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"

RUN_MANIFEST_SCHEMA = SCHEMAS_DIR / "run-manifest.schema.json"
BENCHMARK_RESULT_SCHEMA = SCHEMAS_DIR / "benchmark-result.schema.json"


def load_schema(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def validate_document(document: dict[str, Any], schema_path: Path) -> None:
    """Validate ``document`` against the schema at ``schema_path``.

    Raises ``jsonschema.ValidationError`` on failure.
    """
    schema = load_schema(schema_path)
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator_cls(schema).validate(document)


def validate_run_manifest(document: dict[str, Any]) -> None:
    validate_document(document, RUN_MANIFEST_SCHEMA)


def validate_benchmark_result(document: dict[str, Any]) -> None:
    validate_document(document, BENCHMARK_RESULT_SCHEMA)
