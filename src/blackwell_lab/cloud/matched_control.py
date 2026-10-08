"""Same-session P1 development control for P2C development (decision D-0027).

Decision D-0030 fixes the region at us-ord and leaves the same-session
requirement in force. P2C development may run only when a fully completed,
non-stopped, verified P1 development control from the same run tag,
canonical commit, configuration digest, lifecycle ledger, resource
identity, and region is present. A us-sea or us-iad-2 ledger is historical
and refused. An old us-ord control does not authenticate a new session
unless those fields all match. The control binds that P1 receipt, the
terminal qualification_completed event, and the frozen precision.
Authentication is read-only and fails closed before a model client exists.
Records store digests only: no private paths, provider ids, addresses,
prompts, completions, reasoning, or task bodies.

Decision D-0031 adds a second, structurally identical pair: W1 is the
same-session development control for W2. Every function takes a
:class:`ControlPair`; the default is the historical P1/P2C pair, whose
record kind, section keys, and refusal messages are byte-identical to the
D-0027 contract. W1 records carry a distinct kind and ``w1_``-prefixed keys
so neither pair can authenticate the other.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from blackwell_lab.cloud.artifacts import write_private_json
from blackwell_lab.cloud.lifecycle import EXPECTED_RESOURCE_ADDRESSES, EXPECTED_RESOURCE_TYPES
from blackwell_lab.workload.validation import ConfigError

CONTROL_SCHEMA_VERSION = "1.0.0"
CONTROL_KIND = "matched-p1-development-control"
W_CONTROL_KIND = "matched-w1-development-control"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ControlPair:
    """One same-session development control relationship (control -> treatment)."""

    control: str
    treatment: str
    kind: str
    prefix: str

    @property
    def label_key(self) -> str:
        return f"{self.prefix}_run_label"

    @property
    def section_keys(self) -> tuple[str, ...]:
        return (
            "schema_version",
            "run_tag",
            self.label_key,
            "canonical_commit",
            "region",
            "config_sha256",
            "result_sha256",
            "control_record_sha256",
            "resource_identity_sha256",
            "ledger_sha256",
        )

    @property
    def control_suffix(self) -> str:
        return f"-{self.control.lower()}-development"

    @property
    def treatment_suffix(self) -> str:
        return f"-{self.treatment.lower()}-development"

    def message(self, key: str) -> str:
        return _MESSAGES[key].format(c=self.control, t=self.treatment)


#: Historical pair (D-0027). Its strings are byte-identical to before D-0031.
P1_P2C_PAIR = ControlPair(control="P1", treatment="P2C", kind=CONTROL_KIND, prefix="p1")
#: Workflow-controlled pair (D-0031).
W1_W2_PAIR = ControlPair(control="W1", treatment="W2", kind=W_CONTROL_KIND, prefix="w1")
CONTROL_PAIRS: tuple[ControlPair, ...] = (P1_P2C_PAIR, W1_W2_PAIR)


def pair_for_treatment(candidate_id: object) -> ControlPair | None:
    for pair in CONTROL_PAIRS:
        if pair.treatment == candidate_id:
            return pair
    return None


def pair_for_control(candidate_id: object) -> ControlPair | None:
    for pair in CONTROL_PAIRS:
        if pair.control == candidate_id:
            return pair
    return None


def pair_for_kind(kind: object) -> ControlPair | None:
    for pair in CONTROL_PAIRS:
        if pair.kind == kind:
            return pair
    return None


_MESSAGES = {
    "missing": "{t} development requires a completed same-session {c} control",
    "historical": "{t} development control is historical or cross-region",
    "run_tag": "{t} development control run tag does not match this session",
    "label": "{t} development control label does not match",
    "commit": "{t} development control canonical commit does not match",
    "resource": "{t} development control resource identity does not match this session",
    "digest": "{t} development control digest mismatch",
    "failed": "{t} development control is failed or incomplete",
    "stopped": "{t} development control is not a completed non-stopped {c}",
    "pins": "{t} development control pins do not match the frozen contract",
    "provenance": "{t} development receipt is missing matched-control provenance",
    "relation": "{t} development matched-control provenance does not match the completed {c}",
    "own_label": "{t} cannot bind its own run label as the {c} control",
    "malformed": "{t} development control record is malformed",
    "private": "{t} development control record contains a private path",
    "control_session": "{c} development session is not a reconciled us-ord resource session",
    "event": "{c} development terminal event does not match the selected control",
    "unsafe": "{t} development control label is unsafe",
}
# Historical P1/P2C messages (unchanged text), used wherever no pair is in scope.
_MISSING = P1_P2C_PAIR.message("missing")
_HISTORICAL = P1_P2C_PAIR.message("historical")
_RUN_TAG = P1_P2C_PAIR.message("run_tag")
_LABEL = P1_P2C_PAIR.message("label")
_COMMIT = P1_P2C_PAIR.message("commit")
_RESOURCE = P1_P2C_PAIR.message("resource")
_DIGEST = P1_P2C_PAIR.message("digest")
_FAILED = P1_P2C_PAIR.message("failed")
_STOPPED = P1_P2C_PAIR.message("stopped")
_PINS = P1_P2C_PAIR.message("pins")
_PROVENANCE = P1_P2C_PAIR.message("provenance")
_RELATION = P1_P2C_PAIR.message("relation")
_OWN_LABEL = P1_P2C_PAIR.message("own_label")
_MALFORMED = P1_P2C_PAIR.message("malformed")
_PRIVATE = P1_P2C_PAIR.message("private")
_PENDING = "development session has a pending lifecycle operation"
_P1_SESSION = P1_P2C_PAIR.message("control_session")
_RUN_TAG_UNSAFE = "qualification run tag is malformed"
_SESSION_CHANGED = "development session changed before the result was bound"
_EVENT = P1_P2C_PAIR.message("event")
_UNSAFE = P1_P2C_PAIR.message("unsafe")


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


def require_qualification_run_tag(run_tag: object) -> str:
    """Lifecycle run-tag contract, as a content-free qualification refusal.

    Returns only after the value is safe to embed in a path. Invalid values,
    including absolute paths, separators, traversal, and out-of-range lengths,
    raise before any path is constructed.
    """
    from blackwell_lab.cloud.lifecycle import LifecycleError, validate_run_tag

    try:
        if not isinstance(run_tag, str):
            raise LifecycleError("run_tag")
        return validate_run_tag(run_tag)
    except LifecycleError:
        _fail(_RUN_TAG_UNSAFE)


def ledger_path_for(results_dir: Path, run_tag: str) -> Path:
    """Ledger location. Does not create directories or read the filesystem."""
    checked = require_qualification_run_tag(run_tag)
    return results_dir / "infra-lifecycle" / checked / "ledger.json"


def control_record_path(
    results_dir: Path, p1_run_label: str, *, pair: ControlPair = P1_P2C_PAIR
) -> Path:
    from blackwell_lab.cloud.qualification import (
        QUALIFICATION_ARTIFACT_FAMILY,
        STAGE_DEVELOPMENT,
        output_label,
    )

    label = output_label(p1_run_label, STAGE_DEVELOPMENT, pair.control)
    return results_dir / QUALIFICATION_ARTIFACT_FAMILY / f"{label}-control.json"


def p1_receipt_path(
    results_dir: Path, p1_run_label: str, *, pair: ControlPair = P1_P2C_PAIR
) -> Path:
    from blackwell_lab.cloud.qualification import (
        QUALIFICATION_ARTIFACT_FAMILY,
        STAGE_DEVELOPMENT,
        output_label,
    )

    label = output_label(p1_run_label, STAGE_DEVELOPMENT, pair.control)
    return results_dir / QUALIFICATION_ARTIFACT_FAMILY / f"{label}-receipt.json"


def session_path_for(results_dir: Path, run_tag: str) -> Path:
    checked = require_qualification_run_tag(run_tag)
    return results_dir / "infra-lifecycle" / checked / "session.json"


@dataclass(frozen=True)
class DevelopmentSessionSnapshot:
    """Content-free binding of the ledger observed before live provenance."""

    run_tag: str
    ledger_sha256: str
    resource_identity_sha256: str
    region: str


def _production_resource_identity(
    resources: list, *, run_tag: str, pair: ControlPair = P1_P2C_PAIR
) -> str:
    """Exact instance and firewall identity used by the Terraform session."""
    from blackwell_lab.cloud.lifecycle import PROJECT_TAG
    from blackwell_lab.cloud.mvl import FROZEN_REGION
    from blackwell_lab.cloud.qualification import QualificationError

    session_message = pair.message("control_session")
    try:
        if session_region(resources) != FROZEN_REGION:
            _fail(_HISTORICAL)
        identity = resource_identity_sha256(resources)
    except QualificationError:
        _fail(session_message)
    expected_labels = {
        "linode_instance.gpu_baseline": f"bwlab-{run_tag}",
        "linode_firewall.gpu_baseline": f"bwlab-fw-{run_tag}",
    }
    required_tags = {PROJECT_TAG, f"run:{run_tag}", "ttl-hours:6", "phase:3"}
    by_address = {
        resource.get("address"): resource for resource in resources if isinstance(resource, dict)
    }
    for address, label in expected_labels.items():
        resource = by_address.get(address)
        if not isinstance(resource, dict) or resource.get("label") != label:
            _fail(session_message)
        tags = resource.get("tags")
        observed = (
            {tag for tag in tags if isinstance(tag, str)} if isinstance(tags, list) else set()
        )
        if not required_tags <= observed:
            _fail(session_message)
    return identity


def capture_development_session(
    results_dir: Path, *, run_tag: str, pair: ControlPair = P1_P2C_PAIR
) -> DevelopmentSessionSnapshot:
    """Read the ledger only after the run tag is a safe path component."""
    from blackwell_lab.cloud.mvl import FROZEN_REGION

    session_message = pair.message("control_session")
    checked = require_qualification_run_tag(run_tag)
    ledger_path = results_dir / "infra-lifecycle" / checked / "ledger.json"
    root = (results_dir / "infra-lifecycle").resolve()
    if ledger_path.resolve().parent.parent != root:
        _fail(_RUN_TAG_UNSAFE)
    if not ledger_path.is_file():
        _fail(session_message)
    if (ledger_path.parent / "pending.json").is_file():
        _fail(_PENDING)
    try:
        payload = ledger_path.read_bytes()
        ledger = json.loads(payload)
    except (OSError, json.JSONDecodeError):
        _fail(session_message)
    if not isinstance(ledger, dict) or ledger.get("run_tag") != checked:
        _fail(session_message)
    if ledger.get("reconciled") is not True:
        _fail(session_message)
    reconciliation = ledger.get("reconciliation")
    if not isinstance(reconciliation, dict) or reconciliation.get("provider_checked") is not True:
        _fail(session_message)
    resources = ledger.get("resources")
    if not isinstance(resources, list):
        _fail(session_message)
    return DevelopmentSessionSnapshot(
        run_tag=checked,
        ledger_sha256=_sha256_bytes(payload),
        resource_identity_sha256=_production_resource_identity(
            resources, run_tag=checked, pair=pair
        ),
        region=FROZEN_REGION,
    )


def revalidate_development_session(
    results_dir: Path, snapshot: DevelopmentSessionSnapshot, *, pair: ControlPair = P1_P2C_PAIR
) -> None:
    """Refuse when the ledger bytes or resource identity changed after capture."""
    current = capture_development_session(results_dir, run_tag=snapshot.run_tag, pair=pair)
    if current != snapshot:
        _fail(_SESSION_CHANGED)


def require_snapshot_matches_control(
    snapshot: DevelopmentSessionSnapshot,
    matched: dict | None,
    *,
    pair: ControlPair = P1_P2C_PAIR,
) -> None:
    """P2C may proceed only when the live snapshot is the authenticated control."""
    if not isinstance(matched, dict):
        _fail(pair.message("missing"))
    if (
        snapshot.ledger_sha256 != matched.get("ledger_sha256")
        or snapshot.resource_identity_sha256 != matched.get("resource_identity_sha256")
        or snapshot.region != matched.get("region")
    ):
        _fail(_SESSION_CHANGED)


def validate_p1_development_session(
    results_dir: Path, *, run_tag: str, pair: ControlPair = P1_P2C_PAIR
) -> str:
    """Read-only gate for a live P1 (or W1) development command.

    Fails before live provenance, endpoint contact, a model client, streaming,
    or any result write. Returns the resource-identity digest when the
    reconciled session matches the production resource contract.
    """
    return capture_development_session(
        results_dir, run_tag=run_tag, pair=pair
    ).resource_identity_sha256


def result_dir_for(
    results_dir: Path, p1_run_label: str, *, pair: ControlPair = P1_P2C_PAIR
) -> Path:
    from blackwell_lab.cloud.qualification import (
        QUALIFICATION_ARTIFACT_FAMILY,
        STAGE_DEVELOPMENT,
        output_label,
    )

    label = output_label(p1_run_label, STAGE_DEVELOPMENT, pair.control)
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


def _pin_record(pair: ControlPair = P1_P2C_PAIR) -> dict[str, Any]:
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
        FROZEN_MAX_TOKENS,
        P2C_EVALUATOR_VERSION,
        candidate_identity_digest,
        candidate_temperature,
        candidate_workload_version,
    )
    from blackwell_lab.workload.evaluator import EVALUATOR_VERSION
    from blackwell_lab.workload.scenarios import catalog_digest

    if EVALUATOR_VERSION != P2C_EVALUATOR_VERSION:
        _fail(pair.message("pins"))
    control = pair.control
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
        "workload_version": candidate_workload_version(control),
        "temperature": candidate_temperature(control),
        "top_p": FROZEN_TOP_P,
        "seed": FROZEN_SEED,
        "max_tokens": FROZEN_MAX_TOKENS,
        "reasoning_mode": True,
        "identity_sha256": candidate_identity_digest(control),
    }


def require_development_control_section(config: dict, *, candidate_id: str, stage: str) -> None:
    """Shape-check the binding. File authentication happens later."""
    from blackwell_lab.cloud.lifecycle import LifecycleError, validate_run_tag
    from blackwell_lab.cloud.mvl import FROZEN_REGION
    from blackwell_lab.cloud.qualification import (
        STAGE_DEVELOPMENT,
        require_safe_run_label,
    )

    section = config.get("development_control")
    pair = pair_for_treatment(candidate_id)
    if pair is None or stage != STAGE_DEVELOPMENT:
        if section is not None:
            if candidate_id in (W1_W2_PAIR.control, W1_W2_PAIR.treatment):
                raise ConfigError("development_control is valid only on W2 development")
            raise ConfigError("development_control is valid only on P2C development")
        return
    if not isinstance(section, dict):
        raise ConfigError(pair.message("missing"))
    if set(section) != set(pair.section_keys):
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
    label = section.get(pair.label_key)
    try:
        if not isinstance(label, str):
            raise ConfigError(f"development_control {pair.label_key} is malformed")
        require_safe_run_label(label)
    except ConfigError:
        raise ConfigError(f"development_control {pair.label_key} is malformed") from None
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


def _failure_markers(results_dir: Path, p1_run_label: str, *, pair: ControlPair) -> bool:
    from blackwell_lab.cloud.qualification import QUALIFICATION_ARTIFACT_FAMILY

    family = results_dir / QUALIFICATION_ARTIFACT_FAMILY
    cell = result_dir_for(results_dir, p1_run_label, pair=pair)
    names = (
        family / f"{p1_run_label}-failure.json",
        family / f"{cell.name}-failure.json",
    )
    if any(path.is_file() for path in names):
        return True
    return cell.is_dir() and any(cell.glob("*.failure.json"))


def _result_bytes(results_dir: Path, p1_run_label: str, *, pair: ControlPair) -> bytes:
    failed = pair.message("failed")
    if _failure_markers(results_dir, p1_run_label, pair=pair):
        _fail(failed)
    cell = result_dir_for(results_dir, p1_run_label, pair=pair)
    if not cell.is_dir():
        _fail(failed)
    results = sorted(path for path in cell.glob("*.result.json") if path.is_file())
    if len(results) != 1:
        _fail(failed)
    try:
        return results[0].read_bytes()
    except OSError:
        _fail(failed)


def _require_completed_receipt(
    receipt: dict,
    *,
    p1_run_label: str,
    config_sha256: str,
    identity_sha256: str,
    workload_version: str,
    pair: ControlPair,
) -> None:
    """The receipt's composed claims must match the selected control."""
    from blackwell_lab.cloud.qualification import (
        STAGE_DEVELOPMENT,
        candidate_controller,
        output_label,
    )

    control = pair.control
    if not isinstance(receipt, dict):
        _fail(pair.message("malformed"))
    if receipt.get("candidate_id") != control or receipt.get("stage") != STAGE_DEVELOPMENT:
        _fail(pair.message("failed"))
    if receipt.get("run_label") != output_label(p1_run_label, STAGE_DEVELOPMENT, control):
        _fail(pair.message("label"))
    if receipt.get("config_sha256") != config_sha256:
        _fail(pair.message("digest"))
    if receipt.get("candidate_identity_sha256") != identity_sha256:
        _fail(pair.message("digest"))
    if receipt.get("workload_version") != workload_version:
        _fail(pair.message("pins"))
    if receipt.get("controller") != candidate_controller(control):
        _fail(pair.message("pins"))
    gates = receipt.get("gates")
    if (
        receipt.get("stopped") is not False
        or not isinstance(gates, dict)
        or gates.get("stopped") is not False
        or gates.get("continue") is not True
    ):
        _fail(pair.message("stopped"))


def _require_terminal_event(
    session: dict,
    *,
    run_tag: str,
    p1_run_label: str,
    config_sha256: str,
    pair: ControlPair,
) -> str:
    """Digest of the single matching control development completion event."""
    event_message = pair.message("event")
    control = pair.control
    if not isinstance(session, dict) or session.get("run_tag") != run_tag:
        _fail(event_message)
    events = session.get("events")
    if not isinstance(events, list):
        _fail(event_message)
    completed: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        if detail.get("candidate_id") != control or detail.get("stage") != "development":
            continue
        name = event.get("event")
        if name not in {"qualification_completed", "qualification_stopped"}:
            continue
        if name == "qualification_stopped" or detail.get("stopped") is True:
            _fail(pair.message("stopped"))
        if name == "qualification_completed" and detail.get("stopped") is False:
            completed.append(event)
    if len(completed) != 1:
        _fail(event_message)
    detail = completed[0].get("detail")
    if not isinstance(detail, dict):
        _fail(event_message)
    if detail.get("run_label") != p1_run_label or detail.get("config_sha256") != config_sha256:
        _fail(event_message)
    if detail.get("candidate_id") != control or detail.get("stage") != "development":
        _fail(event_message)
    return _sha256_bytes(_canonical(completed[0]))


def _require_bound_completion(
    results_dir: Path, run_tag: str, record: dict, *, pair: ControlPair
) -> None:
    if (
        record.get("stopped") is not False
        or record.get("terminal_event") != "qualification_completed"
    ):
        _fail(pair.message("stopped"))
    label = record.get("run_label")
    if not isinstance(label, str):
        _fail(pair.message("malformed"))
    receipt_bytes = _read_bytes(
        p1_receipt_path(results_dir, label, pair=pair), missing=pair.message("failed")
    )
    if _sha256_bytes(receipt_bytes) != record.get("receipt_sha256"):
        _fail(pair.message("digest"))
    try:
        receipt = json.loads(receipt_bytes)
    except json.JSONDecodeError:
        _fail(pair.message("malformed"))
    _require_completed_receipt(
        receipt,
        p1_run_label=label,
        config_sha256=str(record.get("config_sha256", "")),
        identity_sha256=str(record.get("identity_sha256", "")),
        workload_version=str(record.get("workload_version", "")),
        pair=pair,
    )
    session_bytes = _read_bytes(
        session_path_for(results_dir, run_tag), missing=pair.message("failed")
    )
    try:
        session = json.loads(session_bytes)
    except json.JSONDecodeError:
        _fail(pair.message("malformed"))
    if _require_terminal_event(
        session,
        run_tag=run_tag,
        p1_run_label=label,
        config_sha256=str(record.get("config_sha256", "")),
        pair=pair,
    ) != record.get("terminal_event_sha256"):
        _fail(pair.message("digest"))


def _completed_receipt(
    *, p1_run_label: str, config_sha256: str, pair: ControlPair
) -> dict[str, Any]:
    from blackwell_lab.cloud.qualification import (
        STAGE_DEVELOPMENT,
        candidate_controller,
        candidate_identity_digest,
        candidate_workload_version,
        output_label,
    )

    control = pair.control
    return {
        "candidate_id": control,
        "stage": STAGE_DEVELOPMENT,
        "run_label": output_label(p1_run_label, STAGE_DEVELOPMENT, control),
        "config_sha256": config_sha256,
        "candidate_identity_sha256": candidate_identity_digest(control),
        "workload_version": candidate_workload_version(control),
        "controller": candidate_controller(control),
        "stopped": False,
        "gates": {"stopped": False, "continue": True},
    }


def _completed_session(
    *, run_tag: str, p1_run_label: str, config_sha256: str, pair: ControlPair
) -> dict[str, Any]:
    return {
        "run_tag": run_tag,
        "events": [
            {
                "event": "qualification_completed",
                "detail": {
                    "stage": "development",
                    "candidate_id": pair.control,
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
    pair: ControlPair,
) -> dict[str, Any]:
    pins = _pin_record(pair)
    if pins["evaluator_version"] != pins["expected_evaluator"]:
        _fail(pair.message("pins"))
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "kind": pair.kind,
        "run_tag": run_tag,
        "run_label": p1_run_label,
        "stage": "development",
        "candidate_id": pair.control,
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


def receipt_block(
    record: dict, control_record_sha256: str, *, pair: ControlPair = P1_P2C_PAIR
) -> dict[str, Any]:
    """Content-free provenance copied onto a treatment development receipt."""
    prefix = pair.prefix
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "kind": pair.kind,
        pair.label_key: record["run_label"],
        "control_record_sha256": control_record_sha256,
        f"{prefix}_config_sha256": record["config_sha256"],
        f"{prefix}_result_sha256": record["result_sha256"],
        f"{prefix}_identity_sha256": record["identity_sha256"],
        "resource_identity_sha256": record["resource_identity_sha256"],
        "ledger_sha256": record["ledger_sha256"],
        "catalog_digest": record["catalog_digest"],
        "schedule_digest": record["schedule_digest"],
        "precision": record["precision"],
        "region": record["region"],
        "canonical_commit": record["canonical_commit"],
        f"{prefix}_receipt_sha256": record["receipt_sha256"],
        "terminal_event_sha256": record["terminal_event_sha256"],
        "stopped": record["stopped"],
        "terminal_event": record["terminal_event"],
    }


def section_from_record(
    record: dict, control_record_sha256: str, *, pair: ControlPair = P1_P2C_PAIR
) -> dict[str, Any]:
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "run_tag": record["run_tag"],
        pair.label_key: record["run_label"],
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
    receipt_sha256: str,
    session_path: Path,
    snapshot: DevelopmentSessionSnapshot,
    pair: ControlPair = P1_P2C_PAIR,
) -> None:
    """Write the control only for the session captured before inference.

    A ledger replaced during measurement is refused. The control is never
    bound to the replacement resource identity.
    """
    if snapshot.run_tag != run_tag:
        _fail(_SESSION_CHANGED)
    revalidate_development_session(results_dir, snapshot, pair=pair)
    receipt_bytes = _read_bytes(
        p1_receipt_path(results_dir, p1_run_label, pair=pair), missing=pair.message("failed")
    )
    if _sha256_bytes(receipt_bytes) != receipt_sha256:
        _fail(pair.message("digest"))
    try:
        receipt = json.loads(receipt_bytes)
    except json.JSONDecodeError:
        _fail(pair.message("malformed"))
    pins = _pin_record(pair)
    _require_completed_receipt(
        receipt,
        p1_run_label=p1_run_label,
        config_sha256=config_sha256,
        identity_sha256=pins["identity_sha256"],
        workload_version=pins["workload_version"],
        pair=pair,
    )
    session_bytes = _read_bytes(session_path, missing=pair.message("failed"))
    try:
        session = json.loads(session_bytes)
    except json.JSONDecodeError:
        _fail(pair.message("malformed"))
    terminal_digest = _require_terminal_event(
        session,
        run_tag=run_tag,
        p1_run_label=p1_run_label,
        config_sha256=config_sha256,
        pair=pair,
    )
    result = _result_bytes(results_dir, p1_run_label, pair=pair)
    record = _compose_record(
        run_tag=run_tag,
        p1_run_label=p1_run_label,
        canonical_commit=canonical_commit,
        config_sha256=config_sha256,
        result_sha256=_sha256_bytes(result),
        ledger_sha256=snapshot.ledger_sha256,
        resource_identity=snapshot.resource_identity_sha256,
        receipt_sha256=receipt_sha256,
        terminal_event_sha256=terminal_digest,
        pair=pair,
    )
    _validate_record(record, pair=pair)
    write_private_json(control_record_path(results_dir, p1_run_label, pair=pair), record)


def install_verified_p1_development_control(
    results_dir: Path,
    config: dict,
    *,
    run_tag: str,
    p1_run_label: str,
    canonical_commit: str,
    pair: ControlPair = P1_P2C_PAIR,
) -> dict[str, Any]:
    """Test helper path and offline fixture writer. Reads an existing ledger.

    Writes one synthetic result, a non-stopped control receipt, a terminal
    qualification_completed event, and the control record, then returns the
    config section. It does not contact a provider or construct a client.
    """
    snapshot = capture_development_session(results_dir, run_tag=run_tag, pair=pair)
    result_body = {
        "schema_version": "1.0.0",
        "kind": "synthetic-matched-control-result",
        "completed": True,
    }
    result_digest = write_private_json(
        result_dir_for(results_dir, p1_run_label, pair=pair) / f"{pair.prefix}.result.json",
        result_body,
    )
    config_digest = _sha256_bytes(
        _canonical(
            {
                "candidate_id": pair.control,
                "stage": "development",
                "canonical_commit": canonical_commit,
            }
        )
    )
    receipt_digest = write_private_json(
        p1_receipt_path(results_dir, p1_run_label, pair=pair),
        _completed_receipt(p1_run_label=p1_run_label, config_sha256=config_digest, pair=pair),
    )
    session = _completed_session(
        run_tag=run_tag,
        p1_run_label=p1_run_label,
        config_sha256=config_digest,
        pair=pair,
    )
    write_private_json(session_path_for(results_dir, run_tag), session)
    record = _compose_record(
        run_tag=run_tag,
        p1_run_label=p1_run_label,
        canonical_commit=canonical_commit,
        config_sha256=config_digest,
        result_sha256=result_digest,
        ledger_sha256=snapshot.ledger_sha256,
        resource_identity=snapshot.resource_identity_sha256,
        receipt_sha256=receipt_digest,
        terminal_event_sha256=_require_terminal_event(
            session,
            run_tag=run_tag,
            p1_run_label=p1_run_label,
            config_sha256=config_digest,
            pair=pair,
        ),
        pair=pair,
    )
    control_digest = write_private_json(
        control_record_path(results_dir, p1_run_label, pair=pair), record
    )
    section = section_from_record(record, control_digest, pair=pair)
    config["development_control"] = section
    return section


def _validate_record(record: dict, *, pair: ControlPair = P1_P2C_PAIR) -> None:
    try:
        jsonschema.validate(record, _load_schema())
    except jsonschema.ValidationError:
        _fail(pair.message("malformed"))
    # The schema admits both pair kinds; the pair in scope admits only its own.
    if record.get("kind") != pair.kind or record.get("candidate_id") != pair.control:
        _fail(pair.message("malformed"))
    _reject_private_strings(record)


def _require_pins(record: dict, *, pair: ControlPair = P1_P2C_PAIR) -> None:
    pins = _pin_record(pair)
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
                _fail(pair.message("historical"))
            _fail(pair.message("pins"))


def authenticate_matched_development_control(
    config: dict,
    *,
    results_dir: Path,
    run_tag: str,
    p2c_run_label: str,
    pair: ControlPair = P1_P2C_PAIR,
) -> dict[str, Any]:
    """Read-only authentication. Raises before any client or result write."""
    from blackwell_lab.cloud.mvl import FROZEN_REGION

    msg = pair.message
    require_development_control_section(config, candidate_id=pair.treatment, stage="development")
    section = config["development_control"]
    label_key = pair.label_key
    if section["run_tag"] != run_tag:
        _fail(msg("run_tag"))
    if section[label_key] == p2c_run_label:
        _fail(msg("own_label"))
    if section["canonical_commit"] != config.get("canonical_commit"):
        _fail(msg("commit"))
    cloud = config.get("cloud") if isinstance(config.get("cloud"), dict) else {}
    if section["region"] != FROZEN_REGION or cloud.get("region") != FROZEN_REGION:
        _fail(msg("historical"))
    require_qualification_run_tag(run_tag)
    snapshot = capture_development_session(results_dir, run_tag=run_tag, pair=pair)
    if snapshot.ledger_sha256 != section["ledger_sha256"]:
        _fail(msg("digest"))
    if snapshot.resource_identity_sha256 != section["resource_identity_sha256"]:
        _fail(msg("resource"))
    if snapshot.region != section["region"]:
        _fail(msg("historical"))
    control_path = control_record_path(results_dir, section[label_key], pair=pair)
    control_bytes = _read_bytes(control_path, missing=msg("missing"))
    if _sha256_bytes(control_bytes) != section["control_record_sha256"]:
        _fail(msg("digest"))
    try:
        record = json.loads(control_bytes)
    except json.JSONDecodeError:
        _fail(msg("malformed"))
    if not isinstance(record, dict):
        _fail(msg("malformed"))
    _validate_record(record, pair=pair)
    if (
        record.get("completed") is not True
        or record.get("verified") is not True
        or record.get("failure_record") is not False
    ):
        _fail(msg("failed"))
    if record.get("run_tag") != run_tag:
        _fail(msg("run_tag"))
    if record.get("run_label") != section[label_key]:
        _fail(msg("label"))
    if record.get("canonical_commit") != section["canonical_commit"]:
        _fail(msg("commit"))
    if record.get("region") != FROZEN_REGION:
        _fail(msg("historical"))
    if record.get("config_sha256") != section["config_sha256"]:
        _fail(msg("digest"))
    if record.get("result_sha256") != section["result_sha256"]:
        _fail(msg("digest"))
    if record.get("ledger_sha256") != section["ledger_sha256"]:
        _fail(msg("digest"))
    if record.get("resource_identity_sha256") != section["resource_identity_sha256"]:
        _fail(msg("resource"))
    _require_pins(record, pair=pair)
    result = _result_bytes(results_dir, section[label_key], pair=pair)
    if _sha256_bytes(result) != record["result_sha256"]:
        _fail(msg("digest"))
    _require_bound_completion(results_dir, run_tag, record, pair=pair)
    return receipt_block(record, section["control_record_sha256"], pair=pair)


def _treatment_development_pair(name: str, receipt: object) -> ControlPair | None:
    """The pair whose treatment development receipt this is, if any."""
    for pair in CONTROL_PAIRS:
        if name.endswith(f"{pair.treatment_suffix}-receipt.json"):
            return pair
    if isinstance(receipt, dict) and receipt.get("stage") == "development":
        return pair_for_treatment(receipt.get("candidate_id"))
    return None


def _safe_control_label(label: object) -> str | None:
    """Existing safe-label contract. Returns None before any path is built."""
    from blackwell_lab.cloud.qualification import require_safe_run_label

    if not isinstance(label, str):
        return None
    try:
        return require_safe_run_label(label)
    except ConfigError:
        return None


def _ledger_resource_identity(
    results_dir: Path, run_tag: str, *, pair: ControlPair = P1_P2C_PAIR
) -> str:
    """Parse the ledger and recompute its production resource identity."""
    return capture_development_session(
        results_dir, run_tag=run_tag, pair=pair
    ).resource_identity_sha256


def _control_pair_of_record(record: dict) -> ControlPair | None:
    """Pair a control record claims to belong to, from its kind and candidate."""
    pair = pair_for_kind(record.get("kind"))
    if pair is None or record.get("candidate_id") != pair.control:
        return None
    return pair


def _audit_p2c_relationship(
    family: Path, matched: object, *, pair: ControlPair = P1_P2C_PAIR
) -> str | None:
    """Return a content-free error, or None when the control relationship holds."""
    from blackwell_lab.cloud.qualification import QualificationError

    msg = pair.message
    if not isinstance(matched, dict):
        return msg("provenance")
    label = _safe_control_label(matched.get(pair.label_key))
    if label is None:
        return msg("unsafe")
    control_path = family / f"{label}{pair.control_suffix}-control.json"
    try:
        payload = control_path.read_bytes()
        record = json.loads(payload)
    except (OSError, json.JSONDecodeError):
        return msg("relation")
    if not isinstance(record, dict):
        return msg("malformed")
    try:
        _validate_record(record, pair=pair)
        _require_pins(record, pair=pair)
        if (
            record.get("completed") is not True
            or record.get("verified") is not True
            or record.get("failure_record") is not False
        ):
            return msg("failed")
        run_tag = record.get("run_tag")
        if not isinstance(run_tag, str):
            return msg("malformed")
        _require_bound_completion(family.parent, run_tag, record, pair=pair)
        identity = _ledger_resource_identity(family.parent, run_tag, pair=pair)
        if identity != record.get("resource_identity_sha256") or identity != matched.get(
            "resource_identity_sha256"
        ):
            return msg("resource")
        expected = receipt_block(record, _sha256_bytes(payload), pair=pair)
    except QualificationError as exc:
        return str(exc)
    if matched != expected:
        return msg("relation")
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
    for pair in CONTROL_PAIRS:
        for result_dir in sorted(family.glob(f"*{pair.treatment_suffix}")):
            if not result_dir.is_dir() or not any(result_dir.glob("*.result.json")):
                continue
            receipt_name = f"{result_dir.name}-receipt.json"
            if not (family / receipt_name).is_file():
                failures.append({"file": receipt_name, "error": pair.message("provenance")})
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
        pair = _control_pair_of_record(record) or P1_P2C_PAIR
        msg = pair.message
        try:
            _validate_record(record, pair=pair)
            _require_pins(record, pair=pair)
        except QualificationError as exc:
            failures.append({"file": name, "error": str(exc)})
            continue
        if (
            record.get("completed") is not True
            or record.get("verified") is not True
            or record.get("failure_record") is not False
        ):
            failures.append({"file": name, "error": msg("failed")})
            continue
        label = _safe_control_label(record.get("run_label"))
        if label is None:
            failures.append({"file": name, "error": msg("unsafe")})
            continue
        result_directory = family / f"{label}{pair.control_suffix}"
        results = (
            sorted(result_directory.glob("*.result.json")) if result_directory.is_dir() else []
        )
        if len(results) != 1:
            failures.append({"file": name, "error": msg("failed")})
            continue
        try:
            result_hash = _sha256_bytes(results[0].read_bytes())
        except OSError:
            failures.append({"file": name, "error": msg("failed")})
            continue
        if result_hash != record.get("result_sha256"):
            failures.append({"file": name, "error": msg("digest")})
        run_tag = record.get("run_tag")
        if not isinstance(run_tag, str):
            failures.append({"file": name, "error": msg("malformed")})
            continue
        ledger = ledger_path_for(family.parent, run_tag)
        try:
            ledger_hash = _sha256_bytes(ledger.read_bytes()) if ledger.is_file() else ""
        except OSError:
            ledger_hash = ""
        if ledger_hash != record.get("ledger_sha256"):
            failures.append({"file": name, "error": msg("digest")})
            continue
        try:
            identity = _ledger_resource_identity(family.parent, run_tag, pair=pair)
            if identity != record.get("resource_identity_sha256"):
                raise QualificationError(msg("resource"))
            _require_bound_completion(family.parent, run_tag, record, pair=pair)
        except QualificationError as exc:
            failures.append({"file": name, "error": str(exc)})
    for receipt_path in sorted(family.glob("*-receipt.json")):
        name = receipt_path.name
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pair = _treatment_development_pair(name, None)
            if pair is not None:
                failures.append({"file": name, "error": pair.message("provenance")})
            continue
        pair = _treatment_development_pair(name, receipt)
        if pair is None:
            continue
        matched = receipt.get("matched_control") if isinstance(receipt, dict) else None
        error = _audit_p2c_relationship(family, matched, pair=pair)
        if error is not None:
            failures.append({"file": name, "error": error})
    return failures
