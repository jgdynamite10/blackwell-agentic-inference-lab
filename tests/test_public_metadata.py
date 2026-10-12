"""D-0034 public metadata, schema, and identity binding."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from blackwell_lab.cloud.qualification import (
    candidate_identity_digest,
    frozen_candidate_fields,
)
from blackwell_lab.schemas import validate_run_manifest
from blackwell_lab.workload.agent import SYSTEM_PROMPT_V241, system_prompt, task_prompt
from blackwell_lab.workload.public_metadata import (
    CONTROLLER_CLOSURE,
    RENDERER_CLOSURE,
    STRUCTURED_FIELD_EXPLANATION,
    PublicMetadataError,
    controller_sha256,
    evidence_contract,
    hash_files,
    load_overlay,
    manifest_workload_binding,
    render_tool_payload,
    require_evidence_contract,
    surface_renderer_sha256,
)
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.tools import SimulatedToolbox

ROOT = Path(__file__).resolve().parents[1]
FROZEN = {
    "W1": "2b7c5745084f4459af66e58fa5701d5c513108a9536385acbed00722c02e68cf",
    "W2": "06d673972a696efcddc9ce00d6c9f6a15a032e0c516d4dc8b54f01dbba0917a2",
    "W3": "bdb9947bf5c0934833d077536ae85740857c36bfd08d88f7fee8b7b54f0f857e",
    "W4": "66df267f60f062f867678331fc466305cde5432bc9cba861ebe90e8cef108414",
}


def _schema(name: str) -> dict:
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


def test_schemas_are_draft_2020_12():
    for name in (
        "public-evidence-v1.schema.json",
        "evidence-contract-v1.schema.json",
        "matched-development-control-v2.schema.json",
    ):
        schema = _schema(name)
        validator = jsonschema.validators.validator_for(schema)
        validator.check_schema(schema)
        jsonschema.validate(evidence_contract(), _schema("evidence-contract-v1.schema.json"))


def test_malformed_catalog_is_refused():
    schema = _schema("public-evidence-v1.schema.json")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"evidence_schema_version": "1.0.0", "diagnosis_hypotheses": []},
            schema,
        )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "evidence_schema_version": "1.0.0",
                "diagnosis_hypotheses": [
                    {"id": "x", "about": [], "claim": {"text": "x", "support_all_of": []}}
                ],
            },
            schema,
        )


def test_development_catalog_matches_public_candidate_order():
    overlay = load_overlay()
    scenarios = catalog()
    seen: list[str] = []
    for scenario_id, scenario in scenarios.items():
        if scenario_id not in overlay["templates"]:
            continue
        hypotheses = overlay["templates"][scenario_id]["hypotheses"]["diagnosis_hypotheses"]
        ids = [item["id"] for item in hypotheses]
        assert ids == list(scenario.candidate_diagnoses)
        assert len(ids) == len(set(ids))
        seen.extend(ids)
        for item in hypotheses:
            assert "accepted" not in item
            assert any(part["role"] == "condition" for part in item["claim"]["support_all_of"])
    assert len(seen) == 18


def test_gpu_claim_keeps_contention_and_no_finding_asserts_it():
    gpu = load_overlay()["templates"]["gpu-saturation-001"]
    claim = next(
        item["claim"]["text"]
        for item in gpu["hypotheses"]["diagnosis_hypotheses"]
        if item["id"] == "batch-job-gpu-contention"
    )
    assert "contending" in claim
    for line in gpu["logs"]:
        for finding in line["finding"]:
            assert finding["condition"]["name"] != "contention"
            assert "starvation" not in json.dumps(finding)


def test_rendered_lines_keep_messages_and_carry_arrays():
    scenario = catalog()["memory-pressure-001"]
    box = SimulatedToolbox(scenario)
    rendered = box.execute("search_logs", {"query": "cache"}, workload_version="2.8.0")
    assert rendered.payload["total_matches"] == 1
    line = rendered.payload["lines"][0]
    assert line["message"].startswith("cache eviction disabled")
    assert {item["condition"]["name"] for item in line["finding"]} == {
        "cache_max_entries",
        "cache_bound",
    }
    assert "mentions" in line
    historical = box.execute("search_logs", {"query": "cache"}, workload_version="2.7.0")
    assert "finding" not in historical.payload
    assert "evidence_schema_version" not in historical.payload
    assert historical.payload["lines"][0]["message"] == line["message"]


def test_holdout_template_has_no_overlay():
    overlay = load_overlay()["templates"]
    from blackwell_lab.cloud.qualification import HOLDOUT_TEMPLATE_IDS

    assert set(overlay).isdisjoint(HOLDOUT_TEMPLATE_IDS)
    scenario = catalog()[HOLDOUT_TEMPLATE_IDS[0]]
    with pytest.raises(PublicMetadataError, match="no reviewed public metadata"):
        render_tool_payload(
            scenario,
            "search_logs",
            {"query": "a", "total_matches": 0, "lines": []},
        )


def test_historical_identities_and_w5_w6_binding():
    for candidate, digest in FROZEN.items():
        assert candidate_identity_digest(candidate) == digest
        assert "evidence_contract" not in frozen_candidate_fields(candidate)
    w5 = frozen_candidate_fields("W5")
    w6 = frozen_candidate_fields("W6")
    assert w5["evidence_contract"] == w6["evidence_contract"] == evidence_contract()
    assert w5["system_prompt_sha256"] == w6["system_prompt_sha256"]
    assert w5["native_tool_contract_sha256"] != w6["native_tool_contract_sha256"]
    assert candidate_identity_digest("W5") != candidate_identity_digest("W6")
    tampered = dict(evidence_contract())
    tampered["controller_sha256"] = "0" * 64
    with pytest.raises(PublicMetadataError, match="does not match"):
        require_evidence_contract(tampered)


def test_observation_eligibility_changes_the_controller_binding():
    evidence = next(path for path in CONTROLLER_CLOSURE if path.endswith("evidence.py"))
    source = (ROOT / evidence).read_bytes()
    needle = b"_UNUSABLE_HEALTH_STATUSES"
    assert needle in source
    mutated = source.replace(needle, b"_USABLE_HEALTH_STATUSES", 1)
    changed = hash_files(CONTROLLER_CLOSURE, {evidence: mutated})
    assert changed != controller_sha256()
    assert hash_files(tuple(path for path in CONTROLLER_CLOSURE if path != evidence)) != (
        controller_sha256()
    )
    stale = dict(evidence_contract())
    stale["controller_sha256"] = changed
    with pytest.raises(PublicMetadataError, match="does not match"):
        require_evidence_contract(stale)


def test_task_rendering_and_dispatch_change_the_renderer_binding():
    tools = next(path for path in RENDERER_CLOSURE if path.endswith("tools.py"))
    agent = next(path for path in RENDERER_CLOSURE if path.endswith("agent.py"))
    tools_bytes = (ROOT / tools).read_bytes()
    agent_bytes = (ROOT / agent).read_bytes()
    dispatch = b"payload = render_tool_payload(self._scenario, name, payload)"
    explanation = b"STRUCTURED_FIELD_EXPLANATION"
    assert dispatch in tools_bytes
    assert explanation in agent_bytes
    dispatched = tools_bytes.replace(dispatch, b"payload = payload", 1)
    assert hash_files(RENDERER_CLOSURE, {tools: dispatched}) != surface_renderer_sha256()
    assert (
        hash_files(
            RENDERER_CLOSURE,
            {agent: agent_bytes.replace(explanation, b"STRUCTURED_FIELD_OMITTED", 1)},
        )
        != surface_renderer_sha256()
    )
    stale = dict(evidence_contract())
    stale["surface_renderer_sha256"] = "f" * 64
    with pytest.raises(PublicMetadataError, match="does not match"):
        require_evidence_contract(stale)


def test_structured_explanation_is_on_the_task_prompt():
    from blackwell_lab.cloud.qualification import DEVELOPMENT_TEMPLATE_IDS

    scenario = catalog()[DEVELOPMENT_TEMPLATE_IDS[0]]
    for version in ("2.8.0", "2.8.1"):
        assert system_prompt(scenario, version) == SYSTEM_PROMPT_V241
        rendered = task_prompt(scenario, None, version)
        assert STRUCTURED_FIELD_EXPLANATION in rendered
        assert "Diagnosis hypotheses" in rendered
    historical = task_prompt(scenario, None, "2.7.0")
    assert STRUCTURED_FIELD_EXPLANATION not in historical
    assert system_prompt(scenario, "2.7.0") == SYSTEM_PROMPT_V241


def _example_manifest() -> dict:
    return json.loads((ROOT / "examples" / "example-run-manifest.json").read_text(encoding="utf-8"))


def _structured_manifest(version: str = "2.8.0") -> dict:
    document = _example_manifest()
    document["workload"]["version"] = version
    document["workload"]["controller"] = "workflow-controller-v3"
    document["workload"].update(manifest_workload_binding(version))
    return document


def test_historical_manifest_omits_the_v3_binding():
    document = _example_manifest()
    assert "evidence_contract" not in document["workload"]
    validate_run_manifest(document)
    document["workload"]["version"] = "2.7.0"
    document["workload"]["controller"] = "workflow-controller-v2"
    validate_run_manifest(document)


def test_v3_manifest_requires_the_evidence_binding():
    for version in ("2.8.0", "2.8.1"):
        document = _example_manifest()
        document["workload"]["version"] = version
        document["workload"]["controller"] = "workflow-controller-v3"
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(document)
    controller_only = _example_manifest()
    controller_only["workload"]["controller"] = "workflow-controller-v3"
    with pytest.raises(jsonschema.ValidationError):
        validate_run_manifest(controller_only)
    bound = _structured_manifest()
    validate_run_manifest(bound)
    bound["workload"]["validation_scope"] = "holdout"
    with pytest.raises(jsonschema.ValidationError):
        validate_run_manifest(bound)
    mismatched = _structured_manifest("2.8.1")
    mismatched["workload"]["blind_generalization_evidence"] = True
    with pytest.raises(jsonschema.ValidationError):
        validate_run_manifest(mismatched)
    stale = _structured_manifest()
    stale["workload"]["evidence_contract"]["controller_sha256"] = "0" * 64
    with pytest.raises(PublicMetadataError, match="does not match"):
        validate_run_manifest(stale)


def test_annotation_modules_do_not_name_evaluator_answers():
    root = ROOT / "src" / "blackwell_lab" / "workload"
    for name in ("public_metadata/__init__.py", "relevance_v3.py", "workflow_v3.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "accepted_diagnoses" not in text
        assert "evidence_predicates" not in text
        assert "import blackwell_lab.workload.evaluator" not in text
