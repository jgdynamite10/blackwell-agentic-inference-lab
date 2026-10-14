"""Qualified baseline manifests may name the candidate that passed study entry."""

import json
from pathlib import Path

import jsonschema
import pytest


def _binding_schema():
    document = json.loads(Path("schemas/run-manifest.schema.json").read_text(encoding="utf-8"))
    return document["properties"]["workload"]["properties"]["study_entry_binding"]


def _binding(candidate_id: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "candidate_identity_sha256": "a" * 64,
        "receipt_sha256": "b" * 64,
        "result_sha256": "c" * 64,
        "manifest_sha256": "d" * 64,
        "study_entry_passed": True,
    }


@pytest.mark.parametrize("candidate_id", ("W7", "W8", "W9", "W10"))
def test_baseline_manifest_accepts_the_qualified_candidates(candidate_id):
    jsonschema.validate(_binding(candidate_id), _binding_schema())


def test_baseline_manifest_rejects_an_unqualified_candidate():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_binding("W11"), _binding_schema())
