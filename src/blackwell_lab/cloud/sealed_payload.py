"""Versioned private-task payload contract for sealed qualification sets.

D-0023 custody treats every task body as opaque bytes. This module is the
smallest contract that lets the local P2 execution adapter (decision
D-0024) turn one verified blob into the existing runtime objects — a
:class:`~blackwell_lab.workload.scenarios.Scenario` and a
:class:`~blackwell_lab.workload.sampling.TaskInstance` — without changing
evaluator or scenario semantics.

Decoding is strict: exact key sets, typed fields, no booleans where
integers are required, no duplicate JSON keys, no NaN/Infinity, bounded
sizes, unique and disjoint answer sets, and a closed set of typed result
constraints. Anything else is ``malformed-task``. Error messages never
include payload content.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from blackwell_lab.paths import repository_root
from blackwell_lab.workload.sampling import TaskInstance
from blackwell_lab.workload.scenarios import (
    ChangeIdEquals,
    EvidenceAlternative,
    EvidencePredicate,
    HealthComponentStatus,
    LogLineContains,
    MetricAvailable,
    RunbookHasRemediation,
    Scenario,
)
from blackwell_lab.workload.tools import TOOL_SPECS

PAYLOAD_SCHEMA_VERSION = "1.0.0"
PAYLOAD_KIND = "sealed-qualification-task"
MAX_PAYLOAD_BYTES = 1_048_576

_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")

#: Typed result-constraint classes keyed by their serialized ``kind``.
_CONSTRAINT_FIELDS: dict[str, tuple[type, tuple[str, ...]]] = {
    "log_line_contains": (LogLineContains, ("text",)),
    "change_id_equals": (ChangeIdEquals, ("change_id",)),
    "runbook_has_remediation": (RunbookHasRemediation, ("remediation_id",)),
    "metric_available": (MetricAvailable, ("metric",)),
    "health_component_status": (HealthComponentStatus, ("component", "status")),
}

#: Tool names a sealed scenario may reference (closed public tool surface).
_KNOWN_TOOLS = frozenset(TOOL_SPECS)


class SealedPayloadError(ValueError):
    """Fail-closed payload error. ``reason`` is a short content-free code."""

    def __init__(self, reason: str = "malformed-task") -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SealedTask:
    """One decoded sealed task: an opaque id, its instance surface, and its scenario."""

    task_id: str
    instance: TaskInstance
    scenario: Scenario


def schema_path() -> Path:
    return repository_root() / "schemas" / "sealed-task-payload.schema.json"


def load_payload_schema() -> dict[str, Any]:
    try:
        document = json.loads(schema_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SealedPayloadError("payload-schema-unavailable") from None
    if not isinstance(document, dict):
        raise SealedPayloadError("payload-schema-unavailable")
    return document


def _reject_constant(_name: str) -> None:
    raise SealedPayloadError()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise SealedPayloadError()
        document[key] = value
    return document


def _parse(payload: bytes) -> dict[str, Any]:
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_PAYLOAD_BYTES:
        raise SealedPayloadError()
    try:
        document = json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise SealedPayloadError() from None
    if not isinstance(document, dict):
        raise SealedPayloadError()
    return document


def _validate_shape(document: dict[str, Any]) -> None:
    schema = load_payload_schema()
    try:
        jsonschema.validate(document, schema)
    except jsonschema.ValidationError:
        raise SealedPayloadError() from None


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SealedPayloadError()
    return value


def _unique_strings(values: object) -> tuple[str, ...]:
    if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
        raise SealedPayloadError()
    if len(set(values)) != len(values):
        raise SealedPayloadError()
    return tuple(values)


def _constraint(raw: dict[str, Any]) -> Any:
    kind = raw.get("kind")
    try:
        constraint_cls, fields = _CONSTRAINT_FIELDS[str(kind)]
    except KeyError:
        raise SealedPayloadError() from None
    if set(raw) != {"kind", *fields}:
        raise SealedPayloadError()
    values = {name: raw[name] for name in fields}
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise SealedPayloadError()
    return constraint_cls(**values)


def _alternative(raw: dict[str, Any]) -> EvidenceAlternative:
    tool = raw["tool"]
    if tool not in _KNOWN_TOOLS:
        raise SealedPayloadError()
    pairs = []
    for pair in raw["argument_contains"]:
        argument, substring = pair
        if not argument:
            raise SealedPayloadError()
        pairs.append((argument, substring))
    return EvidenceAlternative(
        tool=tool, result=_constraint(raw["result"]), argument_contains=tuple(pairs)
    )


def _predicates(raw: list[dict[str, Any]]) -> tuple[EvidencePredicate, ...]:
    predicates = []
    seen: set[str] = set()
    for item in raw:
        predicate_id = item["predicate_id"]
        if predicate_id in seen:
            raise SealedPayloadError()
        seen.add(predicate_id)
        predicates.append(
            EvidencePredicate(
                predicate_id=predicate_id,
                description=item["description"],
                alternatives=tuple(_alternative(alt) for alt in item["alternatives"]),
            )
        )
    return tuple(predicates)


def _tool_sequence(raw: list[dict[str, Any]]) -> tuple[dict, ...]:
    steps = []
    for step in raw:
        if step["tool"] not in _KNOWN_TOOLS:
            raise SealedPayloadError()
        steps.append({"tool": step["tool"], "arguments": dict(step["arguments"])})
    return tuple(steps)


def _scenario(raw: dict[str, Any]) -> Scenario:
    accepted_diagnoses = _unique_strings(raw["accepted_diagnoses"])
    distractor_diagnoses = _unique_strings(raw["distractor_diagnoses"])
    accepted_remediations = _unique_strings(raw["accepted_remediations"])
    distractor_remediations = _unique_strings(raw["distractor_remediations"])
    if set(accepted_diagnoses) & set(distractor_diagnoses):
        raise SealedPayloadError()
    if set(accepted_remediations) & set(distractor_remediations):
        raise SealedPayloadError()
    if raw["root_cause_id"] not in accepted_diagnoses:
        raise SealedPayloadError()
    for series in raw["metrics"].values():
        for point in series["points"]:
            _int(point["t_offset_s"])
            if isinstance(point["value"], bool):
                raise SealedPayloadError()
    for line in raw["logs"]:
        _int(line["t_offset_s"])
    for change in raw["recent_changes"]:
        _int(change["t_offset_s"])
    return Scenario(
        scenario_id=raw["scenario_id"],
        incident_class=raw["incident_class"],
        title=raw["title"],
        description=raw["description"],
        affected_service=raw["affected_service"],
        health={service: dict(components) for service, components in raw["health"].items()},
        metrics={
            name: {"unit": series["unit"], "points": [dict(point) for point in series["points"]]}
            for name, series in raw["metrics"].items()
        },
        logs=[dict(line) for line in raw["logs"]],
        runbooks={
            key: {
                "title": book["title"],
                "steps": list(book["steps"]),
                "remediation_ids": list(book["remediation_ids"]),
            }
            for key, book in raw["runbooks"].items()
        },
        recent_changes=[dict(change) for change in raw["recent_changes"]],
        root_cause_id=raw["root_cause_id"],
        root_cause_summary=raw["root_cause_summary"],
        accepted_diagnoses=accepted_diagnoses,
        distractor_diagnoses=distractor_diagnoses,
        accepted_remediations=accepted_remediations,
        distractor_remediations=distractor_remediations,
        evidence_predicates=_predicates(raw["evidence_predicates"]),
        reference_tool_sequence=_tool_sequence(raw["reference_tool_sequence"]),
        alternative_tool_sequence=_tool_sequence(raw["alternative_tool_sequence"]),
    )


def decode_sealed_task(payload: bytes, *, expected_task_id: str) -> SealedTask:
    """Strictly decode one verified blob into runtime objects.

    ``expected_task_id`` is the custody index id of the blob. The payload
    must name the same id, so an index row can never be pointed at another
    task's body without changing the content digest *and* failing here.
    """
    if not isinstance(expected_task_id, str) or not _TASK_ID_RE.fullmatch(expected_task_id):
        raise SealedPayloadError()
    document = _parse(payload)
    _validate_shape(document)
    if document["payload_schema_version"] != PAYLOAD_SCHEMA_VERSION:
        raise SealedPayloadError("payload-version-mismatch")
    if document["kind"] != PAYLOAD_KIND:
        raise SealedPayloadError()
    if document["task_id"] != expected_task_id:
        raise SealedPayloadError()
    raw_instance = document["instance"]
    scenario = _scenario(document["scenario"])
    instance = TaskInstance(
        template_id=scenario.scenario_id,
        instance_id=expected_task_id,
        instance_seed=_int(raw_instance["instance_seed"]),
        tracking_id=raw_instance["tracking_id"],
        reported_minute=_int(raw_instance["reported_minute"]),
    )
    return SealedTask(task_id=expected_task_id, instance=instance, scenario=scenario)


def scenario_document(scenario: Scenario) -> dict[str, Any]:
    """JSON-ready form of one scenario in the payload contract's shape."""
    from dataclasses import asdict

    document = asdict(scenario)
    for predicate in document["evidence_predicates"]:
        for alternative in predicate["alternatives"]:
            alternative["argument_contains"] = [
                list(pair) for pair in alternative["argument_contains"]
            ]
    return document


def encode_sealed_task(
    *,
    task_id: str,
    scenario: Scenario,
    instance_seed: int,
    tracking_id: str,
    reported_minute: int,
) -> bytes:
    """Canonical payload bytes for one task (used to build synthetic fixtures).

    The encoder is the inverse of :func:`decode_sealed_task`; a decoded
    payload re-encodes to the same scenario and instance. It never reads a
    custody directory and is not an import path.
    """
    document = {
        "payload_schema_version": PAYLOAD_SCHEMA_VERSION,
        "kind": PAYLOAD_KIND,
        "task_id": task_id,
        "instance": {
            "instance_seed": instance_seed,
            "tracking_id": tracking_id,
            "reported_minute": reported_minute,
        },
        "scenario": scenario_document(scenario),
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
