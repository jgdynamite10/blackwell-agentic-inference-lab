"""Public evidence overlay for workloads 2.8.0 and 2.8.1 (decision D-0034).

The overlay publishes reviewed hypothesis claims and log/health findings for
the six development templates. It does not read accepted answers, evaluator
predicates, or holdout annotations. Historical workloads never call it.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

PUBLIC_METADATA_VERSION = "1.0.0"
EVIDENCE_SCHEMA_VERSION = "1.0.0"
NORMALIZATION_POLICY_VERSION = "1.0.0"
CONTROLLER_ID = "workflow-controller-v3"
CONTRACT_VERSION = "1.0.0"
VALIDATION_SCOPE = "public-catalog-pre-exposed-holdout"
STRUCTURED_WORKLOADS = frozenset({"2.8.0", "2.8.1"})

#: Generic explanation shared by the 2.8.0 and 2.8.1 system prompts.
#: It names no scenario, diagnosis, query, or accepted answer.
STRUCTURED_FIELD_EXPLANATION = " ".join(
    [
        "Structured public evidence may appear on search_logs lines and",
        "get_service_health results as finding and mentions arrays, and on a",
        "successful retrieve_runbook result as diagnosis_hypotheses.",
        "A finding states one subject, condition, polarity, certainty, and source.",
        "A mention is not a witness.",
        "Entity mentions, symptoms, and context do not by themselves support a hypothesis.",
        "support_all_of is the evidence pattern for that hypothesis.",
        "Covering the pattern does not prove a unique cause or a correct diagnosis.",
        "An empty finding array is not a license to ignore the raw message.",
    ]
)

CONTROLLER_CLOSURE = (
    "src/blackwell_lab/workload/relevance_v3.py",
    "src/blackwell_lab/workload/workflow_v3.py",
    "src/blackwell_lab/workload/workflow.py",
)
RENDERER_CLOSURE = ("src/blackwell_lab/workload/public_metadata/__init__.py",)

_PACKAGE = Path(__file__).resolve().parent
_REPO = _PACKAGE.parents[3]
_V1 = _PACKAGE / "v1"
_SCHEMA = _REPO / "schemas" / "public-evidence-v1.schema.json"
_CONTRACT_SCHEMA = _REPO / "schemas" / "evidence-contract-v1.schema.json"


class PublicMetadataError(RuntimeError):
    """The public-evidence overlay is missing, drifted, or ungrounded."""


def structured_evidence_workload(workload_version: str | None) -> bool:
    return workload_version in STRUCTURED_WORKLOADS


def canonical_bytes(document: Any) -> bytes:
    """UTF-8 JSON with sorted keys, stable arrays, and compact separators."""
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def content_sha256(document: Any) -> str:
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_document() -> dict[str, Any]:
    return _read_json(_SCHEMA)


def _contract_schema() -> dict[str, Any]:
    return _read_json(_CONTRACT_SCHEMA)


def evidence_schema_sha256() -> str:
    return content_sha256(_schema_document())


def _policy() -> dict[str, Any]:
    policy = _read_json(_V1 / "normalization-policy.json")
    if policy.get("version") != NORMALIZATION_POLICY_VERSION:
        raise PublicMetadataError("normalization policy version is not 1.0.0")
    return policy


def normalization_policy_sha256() -> str:
    return content_sha256(_policy())


def _hash_files(relative_paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in relative_paths:
        path = _REPO / relative
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
    return digest.hexdigest()


def controller_sha256() -> str:
    return _hash_files(CONTROLLER_CLOSURE)


def surface_renderer_sha256() -> str:
    return _hash_files(RENDERER_CLOSURE)


def _span(message: str) -> dict[str, Any]:
    if not message:
        raise PublicMetadataError("a log span requires a non-empty message")
    return {"kind": "log_span", "start": 0, "end": len(message), "quote": message}


def _subject(rule: dict[str, Any], host: str) -> dict[str, str]:
    subject = dict(rule["subject"])
    if rule.get("ground_scope") == "host":
        subject["scope"] = host
    return subject


def _apply_log_rules(message: str, host: str, rules: list[dict[str, Any]]) -> tuple[list, list]:
    findings: list[dict[str, Any]] = []
    mentions: list[dict[str, Any]] = []
    span = _span(message)
    for rule in rules:
        if not all(part in message for part in rule["require_substrings"]):
            continue
        if rule["emit"] == "mention":
            mentions.append({"text": rule["text"], "role": rule["role"], "sources": [dict(span)]})
            continue
        if rule["emit"] != "finding":
            raise PublicMetadataError(f"unknown normalization emit {rule['emit']!r}")
        findings.append(
            {
                "subject": _subject(rule, host),
                "condition": dict(rule["condition"]),
                "polarity": rule["polarity"],
                "certainty": rule["certainty"],
                "role": rule["role"],
                "sources": [dict(span)],
            }
        )
    return findings, mentions


def _health_finding(service: str, component: str, status: str, rule: dict[str, Any]) -> dict:
    name = str(rule["condition_name"]).replace("{component}", component)
    return {
        "subject": {"kind": rule["subject_kind"], "id": service, "scope": rule["scope"]},
        "condition": {"name": name, "state": status},
        "polarity": rule["polarity"],
        "certainty": rule["certainty"],
        "role": rule["role"],
        "sources": [
            {
                "kind": "health_field",
                "path": f"/services/{service}/{component}",
                "value": status,
            }
        ],
    }


def log_fingerprint(position: int, line: dict[str, Any]) -> str:
    return content_sha256(
        {
            "position": position,
            "t_offset_s": line["t_offset_s"],
            "host": line["host"],
            "level": line["level"],
            "message": line["message"],
        }
    )


def _validate_catalog(
    scenario_id: str, catalog: dict[str, Any], candidate_ids: tuple[str, ...]
) -> None:
    schema = _schema_document()
    jsonschema.validate(catalog, schema)
    hypotheses = catalog["diagnosis_hypotheses"]
    ids = [item["id"] for item in hypotheses]
    if ids != list(candidate_ids):
        raise PublicMetadataError(
            f"{scenario_id} hypothesis ids are not the public candidate list in order"
        )
    if len(ids) != len(set(ids)):
        raise PublicMetadataError(f"{scenario_id} hypothesis ids are not unique")
    for hypothesis in hypotheses:
        about = [(item["kind"], item["id"], item["scope"]) for item in hypothesis["about"]]
        assertions = hypothesis["claim"]["support_all_of"]
        if not any(item["role"] == "condition" for item in assertions):
            raise PublicMetadataError(f"{hypothesis['id']} has no condition requirement")
        for assertion in assertions:
            subject = assertion["subject"]
            key = (subject["kind"], subject["id"], subject["scope"])
            if key not in about:
                raise PublicMetadataError(f"{hypothesis['id']} assertion subject is not in about")
        if "accepted" in hypothesis or "correct" in hypothesis:
            raise PublicMetadataError(f"{hypothesis['id']} carries a correctness flag")


def compile_overlay() -> dict[str, Any]:
    """Build the reviewed overlay from public logs, health, and the frozen policy."""
    from blackwell_lab.workload.scenarios import catalog as scenario_catalog

    policy = _policy()
    hypotheses = _read_json(_V1 / "hypotheses.json")
    scenarios = scenario_catalog()
    missing = [key for key in hypotheses if key not in scenarios]
    if missing:
        raise PublicMetadataError(f"hypothesis templates are not in the public catalog: {missing}")
    templates: dict[str, Any] = {}
    for scenario_id, scenario in scenarios.items():
        if scenario_id not in hypotheses:
            continue
        catalog_block = hypotheses[scenario_id]
        _validate_catalog(scenario_id, catalog_block, scenario.candidate_diagnoses)
        logs = []
        for position, line in enumerate(scenario.logs):
            findings, mentions = _apply_log_rules(
                line["message"], line["host"], policy["log_rules"]
            )
            logs.append(
                {
                    "position": position,
                    "fingerprint": log_fingerprint(position, line),
                    "finding": findings,
                    "mentions": mentions,
                }
            )
        health_rule = policy["health_rule"]
        skip = set(health_rule["skip_statuses"])
        health = []
        for service, components in scenario.health.items():
            for component, status in components.items():
                if status in skip:
                    continue
                health.append(
                    {
                        "service": service,
                        "component": component,
                        "status": status,
                        "finding": _health_finding(service, component, status, health_rule),
                    }
                )
        templates[scenario_id] = {
            "hypotheses": catalog_block,
            "logs": logs,
            "health": health,
        }
    return {"public_metadata_version": PUBLIC_METADATA_VERSION, "templates": templates}


def overlay_path() -> Path:
    return _V1 / "overlay.json"


@lru_cache(maxsize=1)
def load_overlay() -> dict[str, Any]:
    path = overlay_path()
    if not path.is_file():
        raise PublicMetadataError("public metadata overlay is missing")
    stored = _read_json(path)
    compiled = compile_overlay()
    if stored != compiled:
        raise PublicMetadataError("public metadata overlay does not match the frozen policy")
    if stored.get("public_metadata_version") != PUBLIC_METADATA_VERSION:
        raise PublicMetadataError("public metadata version is not 1.0.0")
    return stored


def public_metadata_sha256() -> str:
    return content_sha256(load_overlay())


def evidence_contract() -> dict[str, Any]:
    contract = {
        "contract_version": CONTRACT_VERSION,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "evidence_schema_sha256": evidence_schema_sha256(),
        "public_metadata_version": PUBLIC_METADATA_VERSION,
        "public_metadata_sha256": public_metadata_sha256(),
        "normalization_policy_sha256": normalization_policy_sha256(),
        "controller_id": CONTROLLER_ID,
        "controller_sha256": controller_sha256(),
        "surface_renderer_sha256": surface_renderer_sha256(),
    }
    jsonschema.validate(contract, _contract_schema())
    return contract


def require_evidence_contract(contract: object) -> dict[str, Any]:
    """Refuse a binding that is not the contract of this implementation."""
    expected = evidence_contract()
    if contract != expected:
        raise PublicMetadataError("evidence contract does not match the implementation")
    return expected


def manifest_workload_binding(workload_version: str | None) -> dict[str, Any]:
    """Extra manifest fields for 2.8.x. Historical versions add nothing."""
    if not structured_evidence_workload(workload_version):
        return {}
    return {
        "evidence_contract": evidence_contract(),
        "validation_scope": VALIDATION_SCOPE,
        "blind_generalization_evidence": False,
    }


def _template(scenario_id: str) -> dict[str, Any]:
    templates = load_overlay()["templates"]
    try:
        return templates[scenario_id]
    except KeyError as exc:
        raise PublicMetadataError(
            f"no reviewed public metadata for template {scenario_id}"
        ) from exc


def _line_record(scenario_logs: list[dict[str, Any]], line: dict[str, Any]) -> dict[str, Any]:
    matches = [
        (index, record)
        for index, record in enumerate(scenario_logs)
        if record["message"] == line["message"]
        and record["host"] == line["host"]
        and record["level"] == line["level"]
        and record["t_offset_s"] == line["t_offset_s"]
    ]
    if len(matches) != 1:
        raise PublicMetadataError("returned log line is not one raw catalog record")
    return {"position": matches[0][0], "line": matches[0][1]}


def _with_host_source(finding: dict[str, Any], index: int, host: str) -> dict[str, Any]:
    copied = json.loads(json.dumps(finding))
    if copied["subject"]["scope"] != host:
        return copied
    copied["sources"].append({"kind": "log_field", "path": f"/lines/{index}/host", "value": host})
    return copied


def render_search_logs(scenario: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Attach reviewed findings to returned lines. Matching and truncation stay put."""
    template = _template(scenario.scenario_id)
    by_position = {record["position"]: record for record in template["logs"]}
    lines = []
    for index, line in enumerate(payload.get("lines") or []):
        located = _line_record(list(scenario.logs), line)
        record = by_position.get(located["position"])
        if record is None or record["fingerprint"] != log_fingerprint(located["position"], line):
            raise PublicMetadataError("returned log line failed the raw-record fingerprint")
        rendered = {
            "t_offset_s": line["t_offset_s"],
            "host": line["host"],
            "level": line["level"],
            "message": line["message"],
            "finding": [
                _with_host_source(finding, index, line["host"]) for finding in record["finding"]
            ],
            "mentions": json.loads(json.dumps(record["mentions"])),
        }
        lines.append(rendered)
    rendered_payload = {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "query": payload["query"],
        "total_matches": payload["total_matches"],
        "lines": lines,
    }
    jsonschema.validate(rendered_payload, _schema_document())
    return rendered_payload


def render_health(scenario: Any, payload: dict[str, Any]) -> dict[str, Any]:
    template = _template(scenario.scenario_id)
    services = payload.get("services")
    if not isinstance(services, dict):
        raise PublicMetadataError("health payload has no services object")
    findings = []
    indexed = {
        (item["service"], item["component"], item["status"]): item["finding"]
        for item in template["health"]
    }
    for service, components in services.items():
        if not isinstance(components, dict):
            raise PublicMetadataError("health service entry is not an object")
        for component, status in components.items():
            if status in set(_policy()["health_rule"]["skip_statuses"]):
                continue
            finding = indexed.get((service, component, status))
            if finding is None:
                raise PublicMetadataError("returned health status has no reviewed finding")
            findings.append(json.loads(json.dumps(finding)))
    rendered = {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "services": json.loads(json.dumps(services)),
        "finding": findings,
        "mentions": [],
    }
    jsonschema.validate(rendered, _schema_document())
    return rendered


def render_runbook(scenario: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("found") is not True:
        return payload
    template = _template(scenario.scenario_id)
    catalog_block = template["hypotheses"]
    return {
        **payload,
        "evidence_schema_version": catalog_block["evidence_schema_version"],
        "diagnosis_hypotheses": json.loads(json.dumps(catalog_block["diagnosis_hypotheses"])),
    }


def render_tool_payload(scenario: Any, tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    if tool == "search_logs":
        return render_search_logs(scenario, payload)
    if tool == "get_service_health":
        return render_health(scenario, payload)
    if tool == "retrieve_runbook":
        return render_runbook(scenario, payload)
    return payload


def hypothesis_prompt(scenario: Any) -> str:
    """The same hypothesis catalog a successful runbook returns, for the task prompt."""
    catalog_block = _template(scenario.scenario_id)["hypotheses"]
    body = json.dumps(catalog_block, indent=2, ensure_ascii=False)
    return f"\n\nDiagnosis hypotheses (evidence patterns, not correctness labels):\n{body}"
