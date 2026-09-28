"""Builtin authorized profile: existing vLLM BF16 single-GPU behavior.

This is not a launch command. It only recognizes the already-authorized
vLLM + BF16 + single-GPU combination so current MVL and qualification
paths remain valid. Other engines, precisions, and topologies stay
blocked until a component module registers them.
"""

from __future__ import annotations

from blackwell_lab.engines.contract import (
    BUILTIN_VLLM_BF16_SINGLE_GPU,
    READINESS_READY,
    TOPOLOGY_SINGLE_GPU,
    EngineContractDeclaration,
    EngineReadiness,
)
from blackwell_lab.engines.registry import EngineProfile


class VllmBf16SingleGpuProfile:
    """Existing authorized serving path (vLLM, BF16, one GPU)."""

    profile_id = BUILTIN_VLLM_BF16_SINGLE_GPU
    engine = "vllm"
    precision = "bf16"
    topologies = frozenset({TOPOLOGY_SINGLE_GPU})

    def matches(self, declaration: EngineContractDeclaration) -> bool:
        return (
            declaration.engine == self.engine
            and declaration.precision == self.precision
            and declaration.topology.kind in self.topologies
        )

    def evaluate(self, declaration: EngineContractDeclaration) -> EngineReadiness:
        if not self.matches(declaration):
            return EngineReadiness(
                status="blocked",
                profile_id=self.profile_id,
                reasons=("declaration is not vLLM BF16 single-GPU",),
                declaration=declaration,
            )
        return EngineReadiness(
            status=READINESS_READY,
            profile_id=self.profile_id,
            reasons=("authorized existing vLLM BF16 single-GPU contract",),
            declaration=declaration,
        )


def builtin_profiles() -> tuple[EngineProfile, ...]:
    return (VllmBf16SingleGpuProfile(),)
