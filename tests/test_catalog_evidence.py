"""Full-catalog support coverage without consulting evaluator answers."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from blackwell_lab.cloud import qualification
from blackwell_lab.workload.catalog_runtime import CatalogToolbox, manifest_workload_binding
from blackwell_lab.workload.native_tools import openai_tool_definitions
from blackwell_lab.workload.public_metadata import load_overlay as historical_overlay
from blackwell_lab.workload.public_metadata.catalog_v2 import (
    CONTROLLER_CLOSURE,
    RENDERER_CLOSURE,
    PublicMetadataError,
    compile_overlay,
    controller_sha256,
    evidence_contract,
    hash_files,
    load_overlay,
    render_tool_payload,
    require_manifest_binding,
    surface_renderer_sha256,
)
from blackwell_lab.workload.relevance_v3 import SUPPORT_SUPPORTED, pattern_support
from blackwell_lab.workload.scenarios import catalog

FROZEN = {
    "W1": "2b7c5745084f4459af66e58fa5701d5c513108a9536385acbed00722c02e68cf",
    "W2": "06d673972a696efcddc9ce00d6c9f6a15a032e0c516d4dc8b54f01dbba0917a2",
    "W3": "bdb9947bf5c0934833d077536ae85740857c36bfd08d88f7fee8b7b54f0f857e",
    "W4": "66df267f60f062f867678331fc466305cde5432bc9cba861ebe90e8cef108414",
    "W5": "4aaec2742359ce7d35f04ed5b167132e68783dc073b3d8516c30851a8f7e5f27",
    "W6": "cbff1ba63b6f43e2b4b672ee5559b95ebcdb464ab9ec6ea1b6b2f288a32c541a",
}


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _public_witnesses(scenario):
    box = CatalogToolbox(scenario, log_limit=100, clock=FakeClock())
    witnesses = []
    for index, line in enumerate(scenario.logs):
        if line["level"] == "INFO":
            continue
        payload = box.execute("search_logs", {"query": line["message"]}, "2.9.0").payload
        findings = [fact for row in payload["lines"] for fact in row["finding"]]
        witnesses.append((f"log-{index}", "log", findings))
    health = box.execute("get_service_health", {}, "2.9.0").payload
    witnesses.append(("health", "health", health["finding"]))
    return witnesses


def test_all_thirty_hypotheses_and_unchanged_development_metadata():
    templates = load_overlay()["templates"]
    assert set(templates) == set(catalog())
    count = 0
    for ident, scenario in catalog().items():
        hypotheses = templates[ident]["hypotheses"]["diagnosis_hypotheses"]
        assert [item["id"] for item in hypotheses] == list(scenario.candidate_diagnoses)
        count += len(hypotheses)
    assert count == 30
    for ident, template in historical_overlay()["templates"].items():
        assert templates[ident] == template


@pytest.mark.parametrize("ident", tuple(catalog()))
def test_every_template_has_a_public_pattern_witness_within_budget(ident):
    scenario = catalog()[ident]
    hypotheses = load_overlay()["templates"][ident]["hypotheses"]["diagnosis_hypotheses"]
    witnesses = _public_witnesses(scenario)
    # These observations plus a runbook and terminal fit the existing budget.
    assert len(witnesses) + 2 <= 12
    supported = [
        item["id"]
        for item in hypotheses
        if pattern_support(
            catalog=hypotheses, diagnosis_id=item["id"], witnesses=witnesses, cited_ids=None
        )
        == SUPPORT_SUPPORTED
    ]
    assert supported
    assert len(supported) < len(hypotheses)


@pytest.mark.parametrize("field,value", [("polarity", "denied"), ("certainty", "ambiguous")])
def test_negated_or_ambiguous_facts_do_not_support(field, value):
    scenario = catalog()["pod-failures-001"]
    hypotheses = load_overlay()["templates"][scenario.scenario_id]["hypotheses"][
        "diagnosis_hypotheses"
    ]
    witnesses = copy.deepcopy(_public_witnesses(scenario))
    for _, _, facts in witnesses:
        for fact in facts:
            fact[field] = value
    assert all(
        pattern_support(
            catalog=hypotheses, diagnosis_id=item["id"], witnesses=witnesses, cited_ids=None
        )
        != SUPPORT_SUPPORTED
        for item in hypotheses
    )


def test_env_var_entity_mention_does_not_establish_ledger_outage():
    scenario = catalog()["pod-failures-001"]
    hypotheses = load_overlay()["templates"][scenario.scenario_id]["hypotheses"][
        "diagnosis_hypotheses"
    ]
    assert (
        pattern_support(
            catalog=hypotheses,
            diagnosis_id="upstream-ledger-outage",
            witnesses=_public_witnesses(scenario),
            cited_ids=None,
        )
        != SUPPORT_SUPPORTED
    )


def test_compiler_only_requires_public_fields(monkeypatch):
    public = {
        key: SimpleNamespace(
            candidate_diagnoses=s.candidate_diagnoses, logs=s.logs, health=s.health
        )
        for key, s in catalog().items()
    }
    monkeypatch.setattr("blackwell_lab.workload.scenarios.catalog", lambda: public)
    assert compile_overlay() == load_overlay()


def test_full_catalog_required_and_unknown_raw_lines_refused(monkeypatch):
    scenarios = dict(catalog())
    scenarios.pop("pod-failures-001")
    monkeypatch.setattr("blackwell_lab.workload.scenarios.catalog", lambda: scenarios)
    with pytest.raises(PublicMetadataError, match="cover the public catalog"):
        compile_overlay()
    scenario = catalog()["storage-latency-001"]
    payload = {
        "query": "volume",
        "total_matches": 1,
        "lines": [dict(scenario.logs[-1], message="volume-7 might be degraded")],
    }
    with pytest.raises(PublicMetadataError):
        render_tool_payload(scenario, "search_logs", payload)


def test_historical_identities_and_new_dependency_closure():
    assert all(
        qualification.candidate_identity_digest(key) == value for key, value in FROZEN.items()
    )
    assert openai_tool_definitions("2.9.0") == openai_tool_definitions("2.8.0")
    assert qualification.candidate_identity_digest("W7") not in FROZEN.values()
    assert evidence_contract()["public_metadata_version"] == "2.0.0"
    for closure, digest in [
        (CONTROLLER_CLOSURE, controller_sha256()),
        (RENDERER_CLOSURE, surface_renderer_sha256()),
    ]:
        for path in closure:
            assert hash_files(closure, {path: b"changed"}) != digest


def test_w7_accepts_all_stages_while_w5_and_w6_stay_development_only():
    qualification.require_catalog_contract()
    for stage in qualification.AUTHORIZED_STAGES:
        ids = qualification.stage_spec(stage)["template_ids"]
        qualification.require_w_catalog_execution(
            "W7", stage=stage, template_ids=ids, sealed_set=None, sealed_tasks=None
        )
        if stage != "development":
            for candidate in ("W5", "W6"):
                with pytest.raises(qualification.ConfigError, match="development catalog only"):
                    qualification.require_w_catalog_execution(
                        candidate, stage=stage, template_ids=ids, sealed_set=None, sealed_tasks=None
                    )


def test_manifest_refuses_v1_contract_for_w7():
    workload = {
        "version": "2.9.0",
        "controller": "workflow-controller-v3",
        **manifest_workload_binding("2.9.0"),
    }
    require_manifest_binding(workload)
    for field in ("evidence_contract", "validation_scope", "blind_generalization_evidence"):
        broken = dict(workload)
        broken.pop(field)
        with pytest.raises(PublicMetadataError):
            require_manifest_binding(broken)
    broken = json.loads(json.dumps(workload))
    broken["evidence_contract"]["contract_version"] = "1.0.0"
    with pytest.raises(PublicMetadataError):
        require_manifest_binding(broken)
