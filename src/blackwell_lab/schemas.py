"""Access to and validation against the repository's JSON Schemas.

Validation runs with JSON Schema *format* checking enabled, so invalid
``date`` and ``date-time`` strings are rejected rather than silently
accepted. ``date-time`` checking requires the ``rfc3339-validator`` package
(installed via the ``jsonschema[format-nongpl]`` extra); its absence is a
hard error here rather than a silent downgrade.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"

RUN_MANIFEST_SCHEMA = SCHEMAS_DIR / "run-manifest.schema.json"
BENCHMARK_RESULT_SCHEMA = SCHEMAS_DIR / "benchmark-result.schema.json"

REQUIRED_FORMATS = ("date", "date-time")


def _format_checker() -> jsonschema.FormatChecker:
    checker = jsonschema.FormatChecker()
    missing = [f for f in REQUIRED_FORMATS if f not in checker.checkers]
    if missing:
        raise RuntimeError(
            f"JSON Schema format checkers unavailable for: {missing}. "
            "Install the format extras (pip install 'jsonschema[format-nongpl]') "
            "so invalid dates/timestamps fail validation instead of passing."
        )
    return checker


def load_schema(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def validate_document(document: dict[str, Any], schema_path: Path) -> None:
    """Validate ``document`` against the schema at ``schema_path``.

    Raises ``jsonschema.ValidationError`` on failure, including for invalid
    ``date``/``date-time`` strings (format checking is enabled).
    """
    schema = load_schema(schema_path)
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator_cls(schema, format_checker=_format_checker()).validate(document)


def validate_run_manifest(document: dict[str, Any]) -> None:
    validate_document(document, RUN_MANIFEST_SCHEMA)


def validate_benchmark_result(document: dict[str, Any]) -> None:
    validate_document(document, BENCHMARK_RESULT_SCHEMA)
