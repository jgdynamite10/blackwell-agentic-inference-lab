"""Provider-neutral engine/precision contract (Phase 4A).

Component modules register :class:`EngineProfile` implementations through
:func:`register_engine_profile`. The core contract never imports a component
by name and defines no launch commands.
"""

from blackwell_lab.engines.contract import (
    BUILTIN_VLLM_BF16_SINGLE_GPU,
    ENGINE_CONTRACT_VERSION,
    READINESS_BLOCKED,
    READINESS_CONDITIONAL,
    READINESS_READY,
    TOPOLOGY_MULTI_GPU,
    TOPOLOGY_MULTI_NODE,
    TOPOLOGY_SINGLE_GPU,
    EngineContractDeclaration,
    EngineContractError,
    EngineReadiness,
    EntitlementPrerequisite,
    ImmutableIdentity,
    TopologyDeclaration,
    declaration_from_mapping,
    require_supported_contract,
)
from blackwell_lab.engines.registry import (
    EngineProfile,
    evaluate_engine_contract,
    get_profile,
    list_profiles,
    load_registered_components,
    register_engine_profile,
    reset_registry,
)

__all__ = [
    "BUILTIN_VLLM_BF16_SINGLE_GPU",
    "ENGINE_CONTRACT_VERSION",
    "READINESS_BLOCKED",
    "READINESS_CONDITIONAL",
    "READINESS_READY",
    "TOPOLOGY_MULTI_GPU",
    "TOPOLOGY_MULTI_NODE",
    "TOPOLOGY_SINGLE_GPU",
    "EngineContractDeclaration",
    "EngineContractError",
    "EngineProfile",
    "EngineReadiness",
    "EntitlementPrerequisite",
    "ImmutableIdentity",
    "TopologyDeclaration",
    "declaration_from_mapping",
    "evaluate_engine_contract",
    "get_profile",
    "list_profiles",
    "load_registered_components",
    "register_engine_profile",
    "require_supported_contract",
    "reset_registry",
]
