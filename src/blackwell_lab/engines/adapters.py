"""Adapters from existing shared objects onto the engine contract."""

from __future__ import annotations

from typing import Any

from blackwell_lab.engines.contract import (
    TOPOLOGY_MULTI_GPU,
    TOPOLOGY_MULTI_NODE,
    TOPOLOGY_SINGLE_GPU,
    EngineContractDeclaration,
    EngineContractError,
    ImmutableIdentity,
    TopologyDeclaration,
    require_supported_contract,
)
from blackwell_lab.engines.registry import evaluate_engine_contract


def topology_from_counts(*, gpu_count: int, node_count: int = 1) -> TopologyDeclaration:
    """Infer topology kind from declared GPU and node counts."""
    if node_count >= 2:
        kind = TOPOLOGY_MULTI_NODE
    elif gpu_count >= 2:
        kind = TOPOLOGY_MULTI_GPU
    else:
        kind = TOPOLOGY_SINGLE_GPU
    return TopologyDeclaration(kind=kind, gpu_count=gpu_count, node_count=node_count)


def declaration_from_real_spec(spec: Any) -> EngineContractDeclaration:
    """Map a ``RealRunSpec`` onto the contract. Missing identity fails closed."""
    model = spec.model if isinstance(getattr(spec, "model", None), dict) else {}
    gpu_count = int(getattr(spec, "gpu_count", 1) or 1)
    node_count = int(getattr(spec, "node_count", 1) or 1)
    kind = getattr(spec, "topology_kind", None)
    topology = (
        TopologyDeclaration(kind=kind, gpu_count=gpu_count, node_count=node_count)
        if kind
        else topology_from_counts(gpu_count=gpu_count, node_count=node_count)
    )
    return EngineContractDeclaration(
        engine=getattr(spec, "engine", "") or "",
        precision=str(model.get("precision") or ""),
        topology=topology,
        identity=ImmutableIdentity(
            model_artifact=str(model.get("artifact") or ""),
            model_revision=str(model.get("revision") or ""),
            model_artifact_hash=str(model.get("artifact_hash") or ""),
            container_digest=str(getattr(spec, "container_digest", "") or ""),
            engine_version=str(getattr(spec, "engine_version", "") or ""),
        ),
    )


def declaration_from_approved(
    approved: dict[str, Any],
    *,
    gpu_count: int | None = None,
    node_count: int = 1,
    observed_engine_version: str | None = None,
    observed_container_digest: str | None = None,
    observed_model_hash: str | None = None,
) -> EngineContractDeclaration:
    """Map an approved config plus optional observations onto the contract."""
    if not isinstance(approved, dict):
        raise EngineContractError("approved configuration is required")
    serving = approved.get("serving")
    model = approved.get("model")
    host = approved.get("host") if isinstance(approved.get("host"), dict) else {}
    if not isinstance(serving, dict):
        raise EngineContractError("engine is required")
    if not isinstance(model, dict):
        raise EngineContractError("immutable identity is required")
    if gpu_count is None:
        raw = host.get("gpu_count")
        gpu_count = int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else 1
    return EngineContractDeclaration(
        engine=str(serving.get("engine") or ""),
        precision=str(model.get("precision") or ""),
        topology=topology_from_counts(gpu_count=gpu_count, node_count=node_count),
        identity=ImmutableIdentity(
            model_artifact=str(model.get("artifact") or ""),
            model_revision=str(model.get("revision") or ""),
            model_artifact_hash=str(observed_model_hash or model.get("artifact_hash") or ""),
            container_digest=str(
                observed_container_digest or serving.get("container_digest") or ""
            ),
            engine_version=str(observed_engine_version or serving.get("engine_version") or ""),
        ),
    )


def require_real_spec_contract(spec: Any):
    """Fail closed before a genuine cell if the spec is unsupported."""
    return require_supported_contract(evaluate_engine_contract(declaration_from_real_spec(spec)))


def require_approved_contract(
    approved: dict[str, Any],
    *,
    gpu_count: int | None = None,
    observed_engine_version: str | None = None,
    observed_container_digest: str | None = None,
    observed_model_hash: str | None = None,
):
    """Fail closed during provenance if the observed combination is blocked."""
    declaration = declaration_from_approved(
        approved,
        gpu_count=gpu_count,
        observed_engine_version=observed_engine_version,
        observed_container_digest=observed_container_digest,
        observed_model_hash=observed_model_hash,
    )
    return require_supported_contract(evaluate_engine_contract(declaration))
