"""Exact types and fail-closed validation for engine/precision declarations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from blackwell_lab.workload.validation import ConfigError

ENGINE_CONTRACT_VERSION = "1.0.0"
BUILTIN_VLLM_BF16_SINGLE_GPU = "vllm-bf16-single-gpu"

READINESS_READY = "ready"
READINESS_CONDITIONAL = "conditional"
READINESS_BLOCKED = "blocked"
READINESS_STATES = (READINESS_READY, READINESS_CONDITIONAL, READINESS_BLOCKED)

TOPOLOGY_SINGLE_GPU = "single-gpu"
TOPOLOGY_MULTI_GPU = "multi-gpu"
TOPOLOGY_MULTI_NODE = "multi-node"
TOPOLOGY_KINDS = (TOPOLOGY_SINGLE_GPU, TOPOLOGY_MULTI_GPU, TOPOLOGY_MULTI_NODE)

KNOWN_ENGINES = frozenset({"vllm", "tensorrt-llm", "nim"})
KNOWN_PRECISIONS = frozenset({"bf16", "nvfp4", "fp8", "w4a16"})

_FLOATING_VALUES = frozenset(
    {
        "latest",
        "nightly",
        "stable",
        "current",
        "main",
        "master",
        "head",
    }
)
_ARTIFACT_HASH_RE = re.compile(r"^(sha256:[0-9a-f]{64}|sha512:[0-9a-f]{128}|blake3:[0-9a-f]{64})$")
_CONTAINER_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


class EngineContractError(ConfigError):
    """A declared engine/precision contract is incomplete or unsupported."""


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EngineContractError(f"{name} is required")
    text = value.strip()
    if text.lower() in _FLOATING_VALUES:
        raise EngineContractError(f"{name} must be an exact pin (not {text})")
    return text


def _require_int(name: str, value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EngineContractError(f"{name} must be an integer")
    if value < minimum:
        raise EngineContractError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class TopologyDeclaration:
    """Declared accelerator topology. Counts must match the kind."""

    kind: str
    gpu_count: int
    node_count: int

    def __post_init__(self) -> None:
        kind = _require_text("topology.kind", self.kind)
        if kind not in TOPOLOGY_KINDS:
            raise EngineContractError(f"unknown topology kind: {kind}")
        gpu_count = _require_int("topology.gpu_count", self.gpu_count, minimum=1)
        node_count = _require_int("topology.node_count", self.node_count, minimum=1)
        if kind == TOPOLOGY_SINGLE_GPU and (gpu_count != 1 or node_count != 1):
            raise EngineContractError("single-gpu topology requires gpu_count=1 and node_count=1")
        if kind == TOPOLOGY_MULTI_GPU and (gpu_count < 2 or node_count != 1):
            raise EngineContractError("multi-gpu topology requires gpu_count>=2 and node_count=1")
        if kind == TOPOLOGY_MULTI_NODE and node_count < 2:
            raise EngineContractError("multi-node topology requires node_count>=2")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "gpu_count", gpu_count)
        object.__setattr__(self, "node_count", node_count)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "gpu_count": self.gpu_count, "node_count": self.node_count}


@dataclass(frozen=True)
class ImmutableIdentity:
    """Content-addressed model and container identity plus exact engine version."""

    model_artifact: str
    model_revision: str
    model_artifact_hash: str
    container_digest: str
    engine_version: str

    def __post_init__(self) -> None:
        artifact = _require_text("identity.model_artifact", self.model_artifact)
        revision = _require_text("identity.model_revision", self.model_revision)
        artifact_hash = _require_text("identity.model_artifact_hash", self.model_artifact_hash)
        digest = _require_text("identity.container_digest", self.container_digest)
        engine_version = _require_text("identity.engine_version", self.engine_version)
        if not _REVISION_RE.fullmatch(revision):
            raise EngineContractError("identity.model_revision must be a 40-character hex commit")
        if not _ARTIFACT_HASH_RE.fullmatch(artifact_hash):
            raise EngineContractError("identity.model_artifact_hash must be an immutable digest")
        if not _CONTAINER_DIGEST_RE.fullmatch(digest):
            raise EngineContractError("identity.container_digest must be repo@sha256:<hex>")
        object.__setattr__(self, "model_artifact", artifact)
        object.__setattr__(self, "model_revision", revision)
        object.__setattr__(self, "model_artifact_hash", artifact_hash)
        object.__setattr__(self, "container_digest", digest)
        object.__setattr__(self, "engine_version", engine_version)

    def as_dict(self) -> dict[str, str]:
        return {
            "model_artifact": self.model_artifact,
            "model_revision": self.model_revision,
            "model_artifact_hash": self.model_artifact_hash,
            "container_digest": self.container_digest,
            "engine_version": self.engine_version,
        }


def _require_bool(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise EngineContractError(f"{name} must be a boolean")
    return value


def _require_optional_bool(name: str, value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise EngineContractError(f"{name} must be true, false, or null")
    return value


@dataclass(frozen=True)
class EntitlementPrerequisite:
    """A capability or license gate. Required unknowns fail closed."""

    name: str
    required: bool
    satisfied: bool | None
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_text("entitlement.name", self.name))
        object.__setattr__(self, "required", _require_bool("entitlement.required", self.required))
        object.__setattr__(
            self, "satisfied", _require_optional_bool("entitlement.satisfied", self.satisfied)
        )
        if not isinstance(self.detail, str):
            raise EngineContractError("entitlement.detail must be a string")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "satisfied": self.satisfied,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class EngineContractDeclaration:
    """Provider-neutral request to use one engine/precision/topology identity."""

    engine: str
    precision: str
    topology: TopologyDeclaration
    identity: ImmutableIdentity
    entitlements: tuple[EntitlementPrerequisite, ...] = ()
    profile_id: str | None = None

    def __post_init__(self) -> None:
        engine = _require_text("engine", self.engine)
        precision = _require_text("precision", self.precision)
        if engine not in KNOWN_ENGINES:
            raise EngineContractError(f"unknown engine: {engine}")
        if precision not in KNOWN_PRECISIONS:
            raise EngineContractError(f"unknown precision: {precision}")
        if self.profile_id is not None:
            object.__setattr__(self, "profile_id", _require_text("profile_id", self.profile_id))
        object.__setattr__(self, "engine", engine)
        object.__setattr__(self, "precision", precision)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": ENGINE_CONTRACT_VERSION,
            "engine": self.engine,
            "precision": self.precision,
            "topology": self.topology.as_dict(),
            "identity": self.identity.as_dict(),
            "entitlements": [item.as_dict() for item in self.entitlements],
        }
        if self.profile_id is not None:
            payload["profile_id"] = self.profile_id
        return payload


@dataclass(frozen=True)
class EngineReadiness:
    """ready / conditional / blocked evaluation of one declaration."""

    status: str
    profile_id: str | None
    reasons: tuple[str, ...]
    declaration: EngineContractDeclaration

    def __post_init__(self) -> None:
        if self.status not in READINESS_STATES:
            raise EngineContractError(f"unknown readiness status: {self.status}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": ENGINE_CONTRACT_VERSION,
            "status": self.status,
            "profile_id": self.profile_id,
            "reasons": list(self.reasons),
            "declaration": self.declaration.as_dict(),
        }


def declaration_from_mapping(payload: MappingLike) -> EngineContractDeclaration:
    """Build a declaration from a mapping; missing fields fail closed."""
    if not isinstance(payload, dict):
        raise EngineContractError("engine contract must be a JSON object")
    topology_raw = payload.get("topology")
    if not isinstance(topology_raw, dict):
        raise EngineContractError("topology is required")
    identity_raw = payload.get("identity")
    if not isinstance(identity_raw, dict):
        raise EngineContractError("immutable identity is required")
    if "entitlements" not in payload:
        entitlements_raw: list[Any] = []
    else:
        entitlements_raw = payload["entitlements"]
        if not isinstance(entitlements_raw, list):
            raise EngineContractError("entitlements must be a list")
    entitlements = tuple(_entitlement_from_mapping(item) for item in entitlements_raw)
    return EngineContractDeclaration(
        engine=str(payload.get("engine") or ""),
        precision=str(payload.get("precision") or ""),
        topology=TopologyDeclaration(
            kind=str(topology_raw.get("kind") or ""),
            gpu_count=topology_raw.get("gpu_count"),
            node_count=topology_raw.get("node_count"),
        ),
        identity=ImmutableIdentity(
            model_artifact=str(identity_raw.get("model_artifact") or ""),
            model_revision=str(identity_raw.get("model_revision") or ""),
            model_artifact_hash=str(identity_raw.get("model_artifact_hash") or ""),
            container_digest=str(identity_raw.get("container_digest") or ""),
            engine_version=str(identity_raw.get("engine_version") or ""),
        ),
        entitlements=entitlements,
        profile_id=payload.get("profile_id"),
    )


def _entitlement_from_mapping(item: object) -> EntitlementPrerequisite:
    if not isinstance(item, dict):
        raise EngineContractError("entitlement must be an object")
    if "required" not in item:
        raise EngineContractError("entitlement.required is required")
    if "detail" in item and not isinstance(item["detail"], str):
        raise EngineContractError("entitlement.detail must be a string")
    return EntitlementPrerequisite(
        name=item.get("name"),
        required=item.get("required"),
        satisfied=item.get("satisfied"),
        detail=item["detail"] if "detail" in item else "",
    )


def entitlement_blockers(entitlements: tuple[EntitlementPrerequisite, ...]) -> tuple[str, ...]:
    """Required entitlements that are unsatisfied or unknown fail closed."""
    blockers: list[str] = []
    for item in entitlements:
        if not item.required:
            continue
        if item.satisfied is True:
            continue
        if item.satisfied is None:
            blockers.append(f"required entitlement {item.name} is unknown")
        else:
            blockers.append(f"required entitlement {item.name} is not satisfied")
    return tuple(blockers)


def require_ready_contract(readiness: EngineReadiness) -> EngineReadiness:
    """Genuine execution gate. Only ``ready`` may reach inference."""
    if readiness.status == READINESS_READY:
        return readiness
    if readiness.status == READINESS_CONDITIONAL:
        reason = readiness.reasons[0] if readiness.reasons else "unresolved conditions remain"
        raise EngineContractError(
            "conditional engine contract is not authorized for genuine inference: " + reason
        )
    reason = readiness.reasons[0] if readiness.reasons else "unsupported engine contract"
    raise EngineContractError(reason)


def require_supported_contract(readiness: EngineReadiness) -> EngineReadiness:
    """Fail closed unless the contract is ready for genuine execution."""
    return require_ready_contract(readiness)


MappingLike = dict[str, Any]
