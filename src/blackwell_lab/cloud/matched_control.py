"""Same-session P1 development control for P2C development (decision D-0027).

P2C development may run only when a fully completed, non-stopped, verified
P1 development control from the same run tag, lifecycle ledger, and resource
identity is present. The control binds that P1 receipt, the terminal
qualification_completed event, and the frozen precision. Authentication is
read-only and fails closed before a model client exists. Records store
digests only: no private paths, provider ids, addresses, prompts,
completions, reasoning, or task bodies.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import jsonschema

from blackwell_lab.cloud.artifacts import write_private_json
from blackwell_lab.cloud.lifecycle import EXPECTED_RESOURCE_ADDRESSES, EXPECTED_RESOURCE_TYPES
from blackwell_lab.workload.validation import ConfigError

CONTROL_SCHEMA_VERSION = "1.0.0"
CONTROL_KIND = "matched-p1-development-control"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SECTION_KEYS = (
    "schema_version",
    "run_tag",
    "p1_run_label",
    "canonical_commit",
    "region",
    "config_sha256",
    "result_sha256",
    "control_record_sha256",
    "resource_identity_sha256",
    "ledger_sha256",
)
_MISSING = "P2C development requires a completed same-session P1 control"
_HISTORICAL = "P2C development control is historical or cross-region"
_RUN_TAG = "P2C development control run tag does not match this session"
_LABEL = "P2C development control label does not match"
_COMMIT = "P2C development control canonical commit does not match"
_RESOURCE = "P2C development control resource identity does not match this session"
_DIGEST = "P2C development control digest mismatch"
_FAILED = "P2C development control is failed or incomplete"
_STOPPED = "P2C development control is not a completed non-stopped P1"
_PINS = "P2C development control pins do not match the frozen contract"
_PROVENANCE = "P2C development receipt is missing matched-control provenance"
_RELATION = "P2C development matched-control provenance does not match the completed P1"
_OWN_LABEL = "P2C cannot bind its own run label as the P1 control"
_MALFORMED = "P2C development control record is malformed"
_PRIVATE = "P2C development control record contains a private path"
_PENDING = "P2C development control session has a pending lifecycle operation"
_P1_SESSION = "P1 development session is not a reconciled us-iad-2 resource session"
_EVENT = "P1 development terminal event does not match the selected control"
_UNSAFE = "P2C development control label is unsafe"


def schema_path() -> Path:
    root = Path(__file__).resolve().parents[3]
    return root / "schemas" / "matched-development-control.schema.json"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(document: dict) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fail(message: str) -> None:
    from blackwell_lab.cloud.qualification import QualificationError

    raise QualificationError(message)


def development_schedule_digest() -> str:
    """Digest of the frozen public development schedule. No task bodies."""
    from blackwell_lab.cloud.qualification import (
        FROZEN_SEED,
        MEASURED_REPETITION_SEED,
        stage_spec,
    )

    spec = stage_spec("development")
    payload = {
        "stage": "development",
        "schedule": "d-0019-catalog",
        "template_ids": list(spec["template_ids"]),
        "tasks": spec["tasks"],
        "repetitions": spec["repetitions"],
        "warmup_passes": spec["warmup_passes"],
        "seed": FROZEN_SEED,
        "measured_repetition_seed": MEASURED_REPETITION_SEED,
    }
    return _sha256_bytes(_canonical(payload))


def _stored_region(resource: dict) -> str:
    """Region as the production ledger stores it. A firewall may omit it."""
    region = resource.get("region", "")
    if region is None:
        region = ""
    if not isinstance(region, str):
        _fail(_RESOURCE)
    return region


def session_region(resources: list[dict]) -> str:
    """Authenticated instance region. A firewall may carry an empty region."""
    from blackwell_lab.cloud.mvl import FROZEN_REGION

    instance = None
    for resource in resources:
        if not isinstance(resource, dict):
            _fail(_RESOURCE)
        address = resource.get("address")
        region = _stored_region(resource)
        if address == "linode_instance.gpu_baseline":
            instance = resource
            if region != FROZEN_REGION:
                _fail(_HISTORICAL)
        elif address == "linode_firewall.gpu_baseline" and region not in ("", FROZEN_REGION):
            _fail(_HISTORICAL)
    if instance is None:
        _fail(_RESOURCE)
    return FROZEN_REGION


def resource_identity_sha256(resources: list[dict]) -> str:
    """Hash address, type, region, label, and the provider-id digest.

    The raw provider id is never stored in the preimage that leaves this
    function's input: only its SHA-256 is hashed into the identity. Region
    is required on the instance. A firewall may contribute an empty region,
    which is still part of the digest.
    """
    if len(resources) != len(EXPECTED_RESOURCE_ADDRESSES):
        _fail(_RESOURCE)
    rows = []
    seen: set[str] = set()
    for resource in sorted(resources, key=lambda item: str(item.get("address", ""))):
        if not isinstance(resource, dict):
            _fail(_RESOURCE)
        address = resource.get("address")
        if address not in EXPECTED_RESOURCE_ADDRESSES or address in seen:
            _fail(_RESOURCE)
        seen.add(address)
        provider_id = resource.get("provider_id")
        region = _stored_region(resource)
        if not isinstance(provider_id, str) or not provider_id:
            _fail(_RESOURCE)
        if address == "linode_instance.gpu_baseline" and not region:
            _fail(_RESOURCE)
        if resource.get("type") != EXPECTED_RESOURCE_TYPES[address]:
            _fail(_RESOURCE)
        label = resource.get("label")
        if not isinstance(label, str) or not label:
            _fail(_RESOURCE)
        rows.append(
            {
                "address": address,
                "type": resource["type"],
                "region": region,
                "label": label,
                "provider_id_sha256": _sha256_bytes(provider_id.encode("utf-8")),
            }
        )
    return _sha256_bytes(_canonical({"resources": rows}))


def validate_p1_development_session(results_dir: Path, *, run_tag: str) -> str:
    """Read-only gate for a live P1 development command.

    Fails before live provenance, endpoint contact, a model client, streaming,
    or any result write. Returns the resource-identity digest when the
    reconciled session matches the frozen region lock.
    """
    from blackwell_lab.cloud.qualification import QualificationError

    path = ledger_path_for(results_dir, run_tag)
    if not path.is_file():
        _fail(_P1_SESSION)
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _fail(_P1_SESSION)
    if not isinstance(ledger, dict) or ledger.get("run_tag") != run_tag:
        _fail(_P1_SESSION)
    if ledger.get("reconciled") is not True:
        _fail(_P1_SESSION)
    reconciliation = ledger.get("reconciliation")
    if not isinstance(reconciliation, dict) or reconciliation.get("provider_checked") is not True:
        _fail(_P1_SESSION)
    resources = ledger.get("resources")
    if not isinstance(resources, list):
        _fail(_P1_SESSION)
    try:
        session_region(resources)
        return resource_identity_sha256(resources)
    except QualificationError:
        _fail(_P1_SESSION)


def ledger_path_for(results_dir: Path, run_tag: str) -> Path:
    """Ledger location. Does not create directories."""
    return results_dir / "infra-lifecycle" / run_tag / "ledger.json"


def control_record_path(results_dir: Path, p1_run_label: str) -> Path:
    from blackwell_lab.cloud.qualification import (
        CANDIDATE_P1,
        QUALIFICATION_ARTIFACT_FAMILY,
        STAGE_DEVELOPMENT,
        output_label,
    )

    label = output_label(p1_run_label, STAGE_DEVELOPMENT, CANDIDATE_P1)
    return results_dir / QUALIFICATION_ARTIFACT_FAMILY / f"{label}-control.json"


def p1_receipt_path(results_dir: Path, p1_run_label: str) -> Path:
    from blackwell_lab.cloud.qualification import (
        CANDIDATE_P1,
        QUALIFICATION_ARTIFACT_FAMILY,
        STAGE_DEVELOPMENT,
        output_label,
    )

    label = output_label(p1_run_label, STAGE_DEVELOPMENT, CANDIDATE_P1)
    return results_dir / QUALIFICATION_ARTIFACT_FAMILY / f"{label}-receipt.json"


def session_path_for(results_dir: Path, run_tag: str) -> Path:
    return results_dir / "infra-lifecycle" / run_tag / "session.json"


def result_dir_for(results_dir: Path, p1_run_label: str) -> Path:
    from blackwell_lab.cloud.qualification import (
        CANDIDATE_P1,
        QUALIFICATION_ARTIFACT_FAMILY,
        STAGE_DEVELOPMENT,
        output_label,
    )

    label = output_label(p1_run_label, STAGE_DEVELOPMENT, CANDIDATE_P1)
    return results_dir / QUALIFICATION_ARTIFACT_FAMILY / label


def _load_schema() -> dict:
    return json.loads(schema_path().read_text(encoding="utf-8"))


def _reject_private_strings(record: dict) -> None:
    for value in record.values():
        if not isinstance(value, str):
            continue
        if (
            value.startswith("/")
            or "\\" in value
            or "\n" in value
            or "\r" in value
            or ".." in value.split("/")
        ):
            _fail(_PRIVATE)


def _pin_record() -> dict[str, Any]:
    from blackwell_lab.cloud.bootstrap_pins import APPROVED_SERVED_MODEL_NAME
    from blackwell_lab.cloud.mvl import (
        FROZEN_CONTAINER_DIGEST,
        FROZEN_ENGINE,
        FROZEN_ENGINE_VERSION,
        FROZEN_MODEL_ARTIFACT,
        FROZEN_MODEL_ARTIFACT_HASH,
        FROZEN_MODEL_REVISION,
        FROZEN_PRECISION,
        FROZEN_REGION,
        FROZEN_SEED,
        FROZEN_TOP_P,
    )
    from blackwell_lab.cloud.qualification import (
        CANDIDATE_P1,
        FROZEN_MAX_TOKENS,
        P2C_EVALUATOR_VERSION,
        candidate_identity_digest,
        candidate_temperature,
        candidate_workload_version,
    )
    from blackwell_lab.workload.evaluator import EVALUATOR_VERSION
    from blackwell_lab.workload.scenarios import catalog_digest

    if EVALUATOR_VERSION != P2C_EVALUATOR_VERSION:
        _fail(_PINS)
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "expected_evaluator": P2C_EVALUATOR_VERSION,
        "catalog_digest": catalog_digest(),
        "schedule_digest": development_schedule_digest(),
        "region": FROZEN_REGION,
        "model_artifact": FROZEN_MODEL_ARTIFACT,
        "model_revision": FROZEN_MODEL_REVISION,
        "model_artifact_sha256": FROZEN_MODEL_ARTIFACT_HASH,
        "served_model_name": APPROVED_SERVED_MODEL_NAME,
        "engine": FROZEN_ENGINE,
        "engine_version": FROZEN_ENGINE_VERSION,
        "precision": FROZEN_PRECISION,
        "container_digest": FROZEN_CONTAINER_DIGEST,
        "workload_version": candidate_workload_version(CANDIDATE_P1),
        "temperature": candidate_temperature(CANDIDATE_P1),
        "top_p": FROZEN_TOP_P,
        "seed": FROZEN_SEED,
        "max_tokens": FROZEN_MAX_TOKENS,
        "reasoning_mode": True,
        "identity_sha256": candidate_identity_digest(CANDIDATE_P1),
    }


def require_development_control_section(config: dict, *, candidate_id: str, stage: str) -> None:
    """Shape-check the binding. File authentication happens later."""
    from blackwell_lab.cloud.lifecycle import LifecycleError, validate_run_tag
    from blackwell_lab.cloud.mvl import FROZEN_REGION
    from blackwell_lab.cloud.qualification import (
        CANDIDATE_P2C,
        STAGE_DEVELOPMENT,
        require_safe_run_label,
    )

    section = config.get("development_control")
    if candidate_id != CANDIDATE_P2C or stage != STAGE_DEVELOPMENT:
        if section is not None:
            raise ConfigError("development_control is valid only on P2C development")
        return
    if not isinstance(section, dict):
        raise ConfigError(_MISSING)
    if set(section) != set(_SECTION_KEYS):
        raise ConfigError("development_control has unexpected or missing fields")
    if section.get("schema_version") != CONTROL_SCHEMA_VERSION:
        raise ConfigError("development_control schema_version must equal 1.0.0")
    run_tag = section.get("run_tag")
    try:
        if not isinstance(run_tag, str):
            raise LifecycleError("run_tag")
        validate_run_tag(run_tag)
    except LifecycleError:
        raise ConfigError("development_control run_tag is malformed") from None
    label = section.get("p1_run_label")
    try:
        if not isinstance(label, str):
            raise ConfigError("development_control p1_run_label is malformed")
        require_safe_run_label(label)
    except ConfigError:
        raise ConfigError("development_control p1_run_label is malformed") from None
    commit = section.get("canonical_commit")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ConfigError("development_control canonical_commit is malformed")
    if section.get("region") != FROZEN_REGION:
        raise ConfigError(f"development_control region must equal {FROZEN_REGION}")
    for key in (
        "config_sha256",
        "result_sha256",
        "control_record_sha256",
        "resource_identity_sha256",
        "ledger_sha256",
    ):
        value = section.get(key)
        if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
            raise ConfigError("development_control digest is malformed")


def _read_bytes(path: Path, *, missing: str) -> bytes:
    if not path.is_file():
        _fail(missing)
    try:
        return path.read_bytes()
    except OSError:
        _fail(missing)


def _failure_markers(results_dir: Path, p1_run_label: str) -> bool:
    from blackwell_lab.cloud.qualification import QUALIFICATION_ARTIFACT_FAMILY

    family = results_dir / QUALIFICATION_ARTIFACT_FAMILY
    cell = result_dir_for(results_dir, p1_run_label)
    names = (
        family / f"{p1_run_label}-failure.json",
        family / f"{cell.name}-failure.json",
    )
    if any(path.is_file() for path in names):
        return True
    return cell.is_dir() and any(cell.glob("*.failure.json"))


def _result_bytes(results_dir: Path, p1_run_label: str) -> bytes:
    if _failure_markers(results_dir, p1_run_label):
        _fail(_FAILED)
    cell = result_dir_for(results_dir, p1_run_label)
    if not cell.is_dir():
        _fail(_FAILED)
    results = sorted(path for path in cell.glob("*.result.json") if path.is_file())
    if len(results) != 1:
        _fail(_FAILED)
    try:
        return results[0].read_bytes()
    except OSError:
        _fail(_FAILED)


def _load_ledger(results_dir: Path, run_tag: str) -> tuple[dict, bytes]:
    from blackwell_lab.cloud.lifecycle import LifecycleError, load_ledger

    path = ledger_path_for(results_dir, run_tag)
    payload = _read_bytes(path, missing=_MISSING)
    pending = path.parent / "pending.json"
    if pending.is_file():
        _fail(_PENDING)
    try:
        ledger = load_ledger(path)
    except LifecycleError:
        _fail(_MISSING)
    return ledger, payload


def _require_completed_receipt(
    receipt: dict,
    *,
    p1_run_label: str,
    config_sha256: str,
    identity_sha256: str,
    workload_version: str,
) -> None:
    """The receipt's composed claims must match the selected P1 control."""
    from blackwell_lab.cloud.qualification import (
        CANDIDATE_P1,
        STAGE_DEVELOPMENT,
        candidate_controller,
        output_label,
    )

    if not isinstance(receipt, dict):
        _fail(_MALFORMED)
    if receipt.get("candidate_id") != CANDIDATE_P1 or receipt.get("stage") != STAGE_DEVELOPMENT:
        _fail(_FAILED)
    if receipt.get("run_label") != output_label(p1_run_label, STAGE_DEVELOPMENT, CANDIDATE_P1):
        _fail(_LABEL)
    if receipt.get("config_sha256") != config_sha256:
        _fail(_DIGEST)
    if receipt.get("candidate_identity_sha256") != identity_sha256:
        _fail(_DIGEST)
    if receipt.get("workload_version") != workload_version:
        _fail(_PINS)
    if receipt.get("controller") != candidate_controller(CANDIDATE_P1):
        _fail(_PINS)
    gates = receipt.get("gates")
    if (
        receipt.get("stopped") is not False
        or not isinstance(gates, dict)
        or gates.get("stopped") is not False
        or gates.get("continue") is not True
    ):
        _fail(_STOPPED)


def _require_terminal_event(
    session: dict,
    *,
    run_tag: str,
    p1_run_label: str,
    config_sha256: str,
) -> str:
    """Digest of the single matching P1 development completion event."""
    if not isinstance(session, dict) or session.get("run_tag") != run_tag:
        _fail(_EVENT)
    events = session.get("events")
    if not isinstance(events, list):
        _fail(_EVENT)
    completed: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        if detail.get("candidate_id") != "P1" or detail.get("stage") != "development":
            continue
        name = event.get("event")
        if name not in {"qualification_completed", "qualification_stopped"}:
            continue
        if name == "qualification_stopped" or detail.get("stopped") is True:
            _fail(_STOPPED)
        if name == "qualification_completed" and detail.get("stopped") is False:
            completed.append(event)
    if len(completed) != 1:
        _fail(_EVENT)
    detail = completed[0].get("detail")
    if not isinstance(detail, dict):
        _fail(_EVENT)
    if detail.get("run_label") != p1_run_label or detail.get("config_sha256") != config_sha256:
        _fail(_EVENT)
    if detail.get("candidate_id") != "P1" or detail.get("stage") != "development":
        _fail(_EVENT)
    return _sha256_bytes(_canonical(completed[0]))


def _require_bound_completion(results_dir: Path, run_tag: str, record: dict) -> None:
    if (
        record.get("stopped") is not False
        or record.get("terminal_event") != "qualification_completed"
    ):
        _fail(_STOPPED)
    label = record.get("run_label")
    if not isinstance(label, str):
        _fail(_MALFORMED)
    receipt_bytes = _read_bytes(p1_receipt_path(results_dir, label), missing=_FAILED)
    if _sha256_bytes(receipt_bytes) != record.get("receipt_sha256"):
        _fail(_DIGEST)
    try:
        receipt = json.loads(receipt_bytes)
    except json.JSONDecodeError:
        _fail(_MALFORMED)
    _require_completed_receipt(
        receipt,
        p1_run_label=label,
        config_sha256=str(record.get("config_sha256", "")),
        identity_sha256=str(record.get("identity_sha256", "")),
        workload_version=str(record.get("workload_version", "")),
    )
    session_bytes = _read_bytes(session_path_for(results_dir, run_tag), missing=_FAILED)
    try:
        session = json.loads(session_bytes)
    except json.JSONDecodeError:
        _fail(_MALFORMED)
    if _require_terminal_event(
        session,
        run_tag=run_tag,
        p1_run_label=label,
        config_sha256=str(record.get("config_sha256", "")),
    ) != record.get("terminal_event_sha256"):
        _fail(_DIGEST)


def _completed_receipt(*, p1_run_label: str, config_sha256: str) -> dict[str, Any]:
    from blackwell_lab.cloud.qualification import (
        CANDIDATE_P1,
        STAGE_DEVELOPMENT,
        candidate_controller,
        candidate_identity_digest,
        candidate_workload_version,
        output_label,
    )

    return {
        "candidate_id": CANDIDATE_P1,
        "stage": STAGE_DEVELOPMENT,
        "run_label": output_label(p1_run_label, STAGE_DEVELOPMENT, CANDIDATE_P1),
        "config_sha256": config_sha256,
        "candidate_identity_sha256": candidate_identity_digest(CANDIDATE_P1),
        "workload_version": candidate_workload_version(CANDIDATE_P1),
        "controller": candidate_controller(CANDIDATE_P1),
        "stopped": False,
        "gates": {"stopped": False, "continue": True},
    }


def _completed_session(*, run_tag: str, p1_run_label: str, config_sha256: str) -> dict[str, Any]:
    return {
        "run_tag": run_tag,
        "events": [
            {
                "event": "qualification_completed",
                "detail": {
                    "stage": "development",
                    "candidate_id": "P1",
                    "run_label": p1_run_label,
                    "config_sha256": config_sha256,
                    "stopped": False,
                },
            }
        ],
    }


def _compose_record(
    *,
    run_tag: str,
    p1_run_label: str,
    canonical_commit: str,
    config_sha256: str,
    result_sha256: str,
    ledger_sha256: str,
    resource_identity: str,
    receipt_sha256: str,
    terminal_event_sha256: str,
) -> dict[str, Any]:
    pins = _pin_record()
    if pins["evaluator_version"] != pins["expected_evaluator"]:
        _fail(_PINS)
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "kind": CONTROL_KIND,
        "run_tag": run_tag,
        "run_label": p1_run_label,
        "stage": "development",
        "candidate_id": "P1",
        "canonical_commit": canonical_commit,
        "region": pins["region"],
        "config_sha256": config_sha256,
        "result_sha256": result_sha256,
        "evaluator_version": pins["evaluator_version"],
        "catalog_digest": pins["catalog_digest"],
        "schedule_digest": pins["schedule_digest"],
        "resource_identity_sha256": resource_identity,
        "ledger_sha256": ledger_sha256,
        "receipt_sha256": receipt_sha256,
        "terminal_event_sha256": terminal_event_sha256,
        "model_artifact": pins["model_artifact"],
        "model_revision": pins["model_revision"],
        "model_artifact_sha256": pins["model_artifact_sha256"],
        "served_model_name": pins["served_model_name"],
        "engine": pins["engine"],
        "engine_version": pins["engine_version"],
        "precision": pins["precision"],
        "container_digest": pins["container_digest"],
        "workload_version": pins["workload_version"],
        "temperature": pins["temperature"],
        "top_p": pins["top_p"],
        "seed": pins["seed"],
        "max_tokens": pins["max_tokens"],
        "reasoning_mode": True,
        "completed": True,
        "verified": True,
        "stopped": False,
        "terminal_event": "qualification_completed",
        "failure_record": False,
        "identity_sha256": pins["identity_sha256"],
    }


def receipt_block(record: dict, control_record_sha256: str) -> dict[str, Any]:
    """Content-free provenance copied onto a P2C development receipt."""
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "kind": CONTROL_KIND,
        "p1_run_label": record["run_label"],
        "control_record_sha256": control_record_sha256,
        "p1_config_sha256": record["config_sha256"],
        "p1_result_sha256": record["result_sha256"],
        "p1_identity_sha256": record["identity_sha256"],
        "resource_identity_sha256": record["resource_identity_sha256"],
        "ledger_sha256": record["ledger_sha256"],
        "catalog_digest": record["catalog_digest"],
        "schedule_digest": record["schedule_digest"],
        "precision": record["precision"],
        "region": record["region"],
        "canonical_commit": record["canonical_commit"],
        "p1_receipt_sha256": record["receipt_sha256"],
        "terminal_event_sha256": record["terminal_event_sha256"],
        "stopped": record["stopped"],
        "terminal_event": record["terminal_event"],
    }


def section_from_record(record: dict, control_record_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "run_tag": record["run_tag"],
        "p1_run_label": record["run_label"],
        "canonical_commit": record["canonical_commit"],
        "region": record["region"],
        "config_sha256": record["config_sha256"],
        "result_sha256": record["result_sha256"],
        "control_record_sha256": control_record_sha256,
        "resource_identity_sha256": record["resource_identity_sha256"],
        "ledger_sha256": record["ledger_sha256"],
    }


def persist_completed_p1_development_control(
    *,
    results_dir: Path,
    run_tag: str,
    p1_run_label: str,
    canonical_commit: str,
    config_sha256: str,
    ledger_path: Path,
    receipt_sha256: str,
    session_path: Path,
) -> None:
    """Write the control only after a completed, non-stopped P1 receipt and event."""
    if not ledger_path.is_file():
        _fail(_FAILED)
    try:
        ledger_bytes = ledger_path.read_bytes()
        ledger = json.loads(ledger_bytes)
    except (OSError, json.JSONDecodeError):
        _fail(_FAILED)
    resources = ledger.get("resources")
    if not isinstance(resources, list):
        _fail(_FAILED)
    session_region(resources)
    receipt_bytes = _read_bytes(p1_receipt_path(results_dir, p1_run_label), missing=_FAILED)
    if _sha256_bytes(receipt_bytes) != receipt_sha256:
        _fail(_DIGEST)
    try:
        receipt = json.loads(receipt_bytes)
    except json.JSONDecodeError:
        _fail(_MALFORMED)
    pins = _pin_record()
    _require_completed_receipt(
        receipt,
        p1_run_label=p1_run_label,
        config_sha256=config_sha256,
        identity_sha256=pins["identity_sha256"],
        workload_version=pins["workload_version"],
    )
    session_bytes = _read_bytes(session_path, missing=_FAILED)
    try:
        session = json.loads(session_bytes)
    except json.JSONDecodeError:
        _fail(_MALFORMED)
    terminal_digest = _require_terminal_event(
        session,
        run_tag=run_tag,
        p1_run_label=p1_run_label,
        config_sha256=config_sha256,
    )
    result = _result_bytes(results_dir, p1_run_label)
    record = _compose_record(
        run_tag=run_tag,
        p1_run_label=p1_run_label,
        canonical_commit=canonical_commit,
        config_sha256=config_sha256,
        result_sha256=_sha256_bytes(result),
        ledger_sha256=_sha256_bytes(ledger_bytes),
        resource_identity=resource_identity_sha256(resources),
        receipt_sha256=receipt_sha256,
        terminal_event_sha256=terminal_digest,
    )
    _validate_record(record)
    write_private_json(control_record_path(results_dir, p1_run_label), record)


def install_verified_p1_development_control(
    results_dir: Path,
    config: dict,
    *,
    run_tag: str,
    p1_run_label: str,
    canonical_commit: str,
) -> dict[str, Any]:
    """Test helper path and offline fixture writer. Reads an existing ledger.

    Writes one synthetic result, a non-stopped P1 receipt, a terminal
    qualification_completed event, and the control record, then returns the
    config section. It does not contact a provider or construct a client.
    """
    ledger, ledger_bytes = _load_ledger(results_dir, run_tag)
    resources = ledger["resources"]
    session_region(resources)
    result_body = {
        "schema_version": "1.0.0",
        "kind": "synthetic-matched-control-result",
        "completed": True,
    }
    result_digest = write_private_json(
        result_dir_for(results_dir, p1_run_label) / "p1.result.json",
        result_body,
    )
    config_digest = _sha256_bytes(
        _canonical(
            {
                "candidate_id": "P1",
                "stage": "development",
                "canonical_commit": canonical_commit,
            }
        )
    )
    receipt_digest = write_private_json(
        p1_receipt_path(results_dir, p1_run_label),
        _completed_receipt(p1_run_label=p1_run_label, config_sha256=config_digest),
    )
    session = _completed_session(
        run_tag=run_tag,
        p1_run_label=p1_run_label,
        config_sha256=config_digest,
    )
    write_private_json(session_path_for(results_dir, run_tag), session)
    record = _compose_record(
        run_tag=run_tag,
        p1_run_label=p1_run_label,
        canonical_commit=canonical_commit,
        config_sha256=config_digest,
        result_sha256=result_digest,
        ledger_sha256=_sha256_bytes(ledger_bytes),
        resource_identity=resource_identity_sha256(resources),
        receipt_sha256=receipt_digest,
        terminal_event_sha256=_require_terminal_event(
            session,
            run_tag=run_tag,
            p1_run_label=p1_run_label,
            config_sha256=config_digest,
        ),
    )
    control_digest = write_private_json(control_record_path(results_dir, p1_run_label), record)
    section = section_from_record(record, control_digest)
    config["development_control"] = section
    return section


def _validate_record(record: dict) -> None:
    try:
        jsonschema.validate(record, _load_schema())
    except jsonschema.ValidationError:
        _fail(_MALFORMED)
    _reject_private_strings(record)


def _require_pins(record: dict) -> None:
    pins = _pin_record()
    comparable = (
        "evaluator_version",
        "catalog_digest",
        "schedule_digest",
        "region",
        "model_artifact",
        "model_revision",
        "model_artifact_sha256",
        "served_model_name",
        "engine",
        "engine_version",
        "precision",
        "container_digest",
        "workload_version",
        "temperature",
        "top_p",
        "seed",
        "max_tokens",
        "reasoning_mode",
        "identity_sha256",
    )
    for key in comparable:
        if record.get(key) != pins[key]:
            if key == "region":
                _fail(_HISTORICAL)
            _fail(_PINS)


def authenticate_matched_development_control(
    config: dict,
    *,
    results_dir: Path,
    run_tag: str,
    p2c_run_label: str,
) -> dict[str, Any]:
    """Read-only authentication. Raises before any client or result write."""
    from blackwell_lab.cloud.mvl import FROZEN_REGION

    require_development_control_section(config, candidate_id="P2C", stage="development")
    section = config["development_control"]
    if section["run_tag"] != run_tag:
        _fail(_RUN_TAG)
    if section["p1_run_label"] == p2c_run_label:
        _fail(_OWN_LABEL)
    if section["canonical_commit"] != config.get("canonical_commit"):
        _fail(_COMMIT)
    cloud = config.get("cloud") if isinstance(config.get("cloud"), dict) else {}
    if section["region"] != FROZEN_REGION or cloud.get("region") != FROZEN_REGION:
        _fail(_HISTORICAL)
    ledger, ledger_bytes = _load_ledger(results_dir, run_tag)
    if ledger.get("run_tag") != run_tag or ledger.get("reconciled") is not True:
        _fail(_RESOURCE)
    reconciliation = ledger.get("reconciliation") or {}
    if reconciliation.get("provider_checked") is not True:
        _fail(_RESOURCE)
    resources = ledger.get("resources")
    if not isinstance(resources, list):
        _fail(_RESOURCE)
    if session_region(resources) != FROZEN_REGION:
        _fail(_HISTORICAL)
    if _sha256_bytes(ledger_bytes) != section["ledger_sha256"]:
        _fail(_DIGEST)
    if resource_identity_sha256(resources) != section["resource_identity_sha256"]:
        _fail(_RESOURCE)
    control_path = control_record_path(results_dir, section["p1_run_label"])
    control_bytes = _read_bytes(control_path, missing=_MISSING)
    if _sha256_bytes(control_bytes) != section["control_record_sha256"]:
        _fail(_DIGEST)
    try:
        record = json.loads(control_bytes)
    except json.JSONDecodeError:
        _fail(_MALFORMED)
    if not isinstance(record, dict):
        _fail(_MALFORMED)
    _validate_record(record)
    if (
        record.get("completed") is not True
        or record.get("verified") is not True
        or record.get("failure_record") is not False
    ):
        _fail(_FAILED)
    if record.get("run_tag") != run_tag:
        _fail(_RUN_TAG)
    if record.get("run_label") != section["p1_run_label"]:
        _fail(_LABEL)
    if record.get("canonical_commit") != section["canonical_commit"]:
        _fail(_COMMIT)
    if record.get("region") != FROZEN_REGION:
        _fail(_HISTORICAL)
    if record.get("config_sha256") != section["config_sha256"]:
        _fail(_DIGEST)
    if record.get("result_sha256") != section["result_sha256"]:
        _fail(_DIGEST)
    if record.get("ledger_sha256") != section["ledger_sha256"]:
        _fail(_DIGEST)
    if record.get("resource_identity_sha256") != section["resource_identity_sha256"]:
        _fail(_RESOURCE)
    _require_pins(record)
    result = _result_bytes(results_dir, section["p1_run_label"])
    if _sha256_bytes(result) != record["result_sha256"]:
        _fail(_DIGEST)
    _require_bound_completion(results_dir, run_tag, record)
    return receipt_block(record, section["control_record_sha256"])


def _is_p2c_development_receipt(name: str, receipt: object) -> bool:
    if name.endswith("-p2c-development-receipt.json"):
        return True
    return (
        isinstance(receipt, dict)
        and receipt.get("candidate_id") == "P2C"
        and receipt.get("stage") == "development"
    )


def _safe_control_label(label: object) -> str | None:
    """Existing safe-label contract. Returns None before any path is built."""
    from blackwell_lab.cloud.qualification import require_safe_run_label

    if not isinstance(label, str):
        return None
    try:
        return require_safe_run_label(label)
    except ConfigError:
        return None


def _ledger_resource_identity(results_dir: Path, run_tag: str) -> str:
    """Parse the ledger and recompute its resource identity."""
    from blackwell_lab.cloud.lifecycle import LifecycleError, load_ledger, validate_run_tag

    try:
        validate_run_tag(run_tag)
        ledger = load_ledger(ledger_path_for(results_dir, run_tag))
    except LifecycleError:
        _fail(_RESOURCE)
    if ledger.get("run_tag") != run_tag or ledger.get("reconciled") is not True:
        _fail(_RESOURCE)
    reconciliation = ledger.get("reconciliation")
    if not isinstance(reconciliation, dict) or reconciliation.get("provider_checked") is not True:
        _fail(_RESOURCE)
    resources = ledger.get("resources")
    if not isinstance(resources, list):
        _fail(_RESOURCE)
    session_region(resources)
    return resource_identity_sha256(resources)


def _audit_p2c_relationship(family: Path, matched: object) -> str | None:
    """Return a content-free error, or None when the P1/P2C relationship holds."""
    from blackwell_lab.cloud.qualification import QualificationError

    if not isinstance(matched, dict):
        return _PROVENANCE
    label = _safe_control_label(matched.get("p1_run_label"))
    if label is None:
        return _UNSAFE
    control_path = family / f"{label}-p1-development-control.json"
    try:
        payload = control_path.read_bytes()
        record = json.loads(payload)
    except (OSError, json.JSONDecodeError):
        return _RELATION
    if not isinstance(record, dict):
        return _MALFORMED
    try:
        _validate_record(record)
        _require_pins(record)
        if (
            record.get("completed") is not True
            or record.get("verified") is not True
            or record.get("failure_record") is not False
        ):
            return _FAILED
        run_tag = record.get("run_tag")
        if not isinstance(run_tag, str):
            return _MALFORMED
        _require_bound_completion(family.parent, run_tag, record)
        identity = _ledger_resource_identity(family.parent, run_tag)
        if identity != record.get("resource_identity_sha256") or identity != matched.get(
            "resource_identity_sha256"
        ):
            return _RESOURCE
        expected = receipt_block(record, _sha256_bytes(payload))
    except QualificationError as exc:
        return str(exc)
    if matched != expected:
        return _RELATION
    return None


def audit_matched_controls(base: Path) -> list[dict[str, str]]:
    """Content-free checks for verify-results. Filenames only, never paths."""
    from blackwell_lab.cloud.qualification import QualificationError

    if not base.is_dir():
        return []
    failures: list[dict[str, str]] = []
    family = base if base.name == "qualification-runs" else base / "qualification-runs"
    if not family.is_dir():
        return []
    for result_dir in sorted(family.glob("*-p2c-development")):
        if not result_dir.is_dir() or not any(result_dir.glob("*.result.json")):
            continue
        receipt_name = f"{result_dir.name}-receipt.json"
        if not (family / receipt_name).is_file():
            failures.append({"file": receipt_name, "error": _PROVENANCE})
    for control_path in sorted(family.glob("*-control.json")):
        name = control_path.name
        try:
            payload = control_path.read_bytes()
            record = json.loads(payload)
        except (OSError, json.JSONDecodeError):
            failures.append({"file": name, "error": "matched control record is unreadable"})
            continue
        if not isinstance(record, dict):
            failures.append({"file": name, "error": _MALFORMED})
            continue
        try:
            _validate_record(record)
            _require_pins(record)
        except QualificationError as exc:
            failures.append({"file": name, "error": str(exc)})
            continue
        if (
            record.get("completed") is not True
            or record.get("verified") is not True
            or record.get("failure_record") is not False
        ):
            failures.append({"file": name, "error": _FAILED})
            continue
        label = _safe_control_label(record.get("run_label"))
        if label is None:
            failures.append({"file": name, "error": _UNSAFE})
            continue
        result_directory = family / f"{label}-p1-development"
        results = (
            sorted(result_directory.glob("*.result.json")) if result_directory.is_dir() else []
        )
        if len(results) != 1:
            failures.append({"file": name, "error": _FAILED})
            continue
        try:
            result_hash = _sha256_bytes(results[0].read_bytes())
        except OSError:
            failures.append({"file": name, "error": _FAILED})
            continue
        if result_hash != record.get("result_sha256"):
            failures.append({"file": name, "error": _DIGEST})
        run_tag = record.get("run_tag")
        if not isinstance(run_tag, str):
            failures.append({"file": name, "error": _MALFORMED})
            continue
        ledger = ledger_path_for(family.parent, run_tag)
        try:
            ledger_hash = _sha256_bytes(ledger.read_bytes()) if ledger.is_file() else ""
        except OSError:
            ledger_hash = ""
        if ledger_hash != record.get("ledger_sha256"):
            failures.append({"file": name, "error": _DIGEST})
            continue
        try:
            identity = _ledger_resource_identity(family.parent, run_tag)
            if identity != record.get("resource_identity_sha256"):
                raise QualificationError(_RESOURCE)
            _require_bound_completion(family.parent, run_tag, record)
        except QualificationError as exc:
            failures.append({"file": name, "error": str(exc)})
    for receipt_path in sorted(family.glob("*-receipt.json")):
        name = receipt_path.name
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if name.endswith("-p2c-development-receipt.json"):
                failures.append({"file": name, "error": _PROVENANCE})
            continue
        if not _is_p2c_development_receipt(name, receipt):
            continue
        matched = receipt.get("matched_control") if isinstance(receipt, dict) else None
        error = _audit_p2c_relationship(family, matched)
        if error is not None:
            failures.append({"file": name, "error": error})
    return failures
