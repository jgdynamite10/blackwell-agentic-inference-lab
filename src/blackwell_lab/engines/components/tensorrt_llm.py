"""Offline TensorRT-LLM single-GPU profiles.

These profiles classify declarations. They do not build or convert an
engine, pull a container, download weights, call a registry, or run
inference. Neither profile can return ``ready``.
"""

from __future__ import annotations

import re

from blackwell_lab.engines.contract import (
    READINESS_BLOCKED,
    READINESS_CONDITIONAL,
    TOPOLOGY_SINGLE_GPU,
    EngineContractDeclaration,
    EngineReadiness,
)
from blackwell_lab.engines.registry import register_engine_profile

ENGINE = "tensorrt-llm"
PROFILE_BF16 = "tensorrt-llm-bf16-single-gpu"
PROFILE_NVFP4 = "tensorrt-llm-nvfp4-single-gpu"

#: Versioned TensorRT-LLM 1.1.0 docs name RTX Pro 6000 SE FP4 support.
#: Retrieved 2026-09-28. This is not a validated Nemotron pin.
DOCUMENTED_ENGINE_VERSION = "1.1.0"

#: Documented runtime repository. The immutable digest is not published here.
RELEASE_REPOSITORY = "nvcr.io/nvidia/tensorrt-llm/release"

NEMOTRON_BF16_ARTIFACT = "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16"
NEMOTRON_NVFP4_ARTIFACT = "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4"

_RELEASE_DIGEST_RE = re.compile(r"^nvcr\.io/nvidia/tensorrt-llm/release@sha256:[0-9a-f]{64}$")

_UNRESOLVED_COMMON = (
    "exact supported Nemotron 3.5 Lightning TensorRT-LLM profile is unresolved",
    "exact conversion/build artifact and immutable digest are unresolved",
    "exact container/runtime identity is unresolved",
    "RTX PRO 6000 live behavior is unresolved",
)
_UNRESOLVED_TAIL = (
    "native OpenAI tool-calling compatibility is unresolved",
    "reasoning/parser compatibility is unresolved",
    "entitlement or registry-access requirements are unresolved",
    "SM120 support alone does not establish a functioning Nemotron profile",
)
BF16_VIABILITY = "BF16 end-to-end viability on RTX PRO 6000 is unresolved"
NVFP4_VIABILITY = "NVFP4 end-to-end viability on RTX PRO 6000 is unresolved"


def _conditional_reasons(viability: str) -> tuple[str, ...]:
    return (*_UNRESOLVED_COMMON, viability, *_UNRESOLVED_TAIL)


class TensorRtLlmSingleGpuProfile:
    """One precision of the offline TensorRT-LLM single-GPU classification."""

    engine = ENGINE
    topologies = frozenset({TOPOLOGY_SINGLE_GPU})

    def __init__(self, *, profile_id: str, precision: str, model_artifact: str, viability: str):
        self.profile_id = profile_id
        self.precision = precision
        self._model_artifact = model_artifact
        self._reasons = _conditional_reasons(viability)

    def matches(self, declaration: EngineContractDeclaration) -> bool:
        return (
            declaration.engine == self.engine
            and declaration.precision == self.precision
            and declaration.topology.kind in self.topologies
            and declaration.topology.gpu_count == 1
            and declaration.topology.node_count == 1
        )

    def evaluate(self, declaration: EngineContractDeclaration) -> EngineReadiness:
        if not self.matches(declaration):
            return EngineReadiness(
                status=READINESS_BLOCKED,
                profile_id=self.profile_id,
                reasons=(f"declaration is outside {self.profile_id}",),
                declaration=declaration,
            )
        blockers: list[str] = []
        identity = declaration.identity
        if identity.model_artifact != self._model_artifact:
            blockers.append(
                "model artifact is not the Nemotron 3.5 Lightning checkpoint for this precision"
            )
        if identity.engine_version != DOCUMENTED_ENGINE_VERSION:
            blockers.append(
                "engine_version is not the documented TensorRT-LLM 1.1.0 release "
                "recorded for this offline profile"
            )
        if _RELEASE_DIGEST_RE.fullmatch(identity.container_digest) is None:
            blockers.append(
                "container identity is not an immutable "
                "nvcr.io/nvidia/tensorrt-llm/release@sha256 digest"
            )
        if blockers:
            return EngineReadiness(
                status=READINESS_BLOCKED,
                profile_id=self.profile_id,
                reasons=tuple(blockers),
                declaration=declaration,
            )
        return EngineReadiness(
            status=READINESS_CONDITIONAL,
            profile_id=self.profile_id,
            reasons=self._reasons,
            declaration=declaration,
        )


def _register() -> None:
    register_engine_profile(
        TensorRtLlmSingleGpuProfile(
            profile_id=PROFILE_BF16,
            precision="bf16",
            model_artifact=NEMOTRON_BF16_ARTIFACT,
            viability=BF16_VIABILITY,
        )
    )
    register_engine_profile(
        TensorRtLlmSingleGpuProfile(
            profile_id=PROFILE_NVFP4,
            precision="nvfp4",
            model_artifact=NEMOTRON_NVFP4_ARTIFACT,
            viability=NVFP4_VIABILITY,
        )
    )


_register()
