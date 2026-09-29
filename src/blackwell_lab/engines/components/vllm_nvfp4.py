"""Offline classification for one vLLM NVFP4 single-GPU profile.

The profile is discovered by package import and registers itself. It does
not launch a server, download weights or images, call a provider, or read
credentials. It never reports ``ready``.

Official evidence retrieved 2026-09-28 is recorded below. A contract field
with no published immutable value stays unresolved and is not invented.
Generic Blackwell or SM120 coverage is not an RTX PRO 6000 NVFP4 recipe.
"""

from __future__ import annotations

from blackwell_lab.engines.contract import (
    READINESS_BLOCKED,
    TOPOLOGY_SINGLE_GPU,
    EngineContractDeclaration,
    EngineReadiness,
    entitlement_blockers,
)
from blackwell_lab.engines.registry import list_profiles, register_engine_profile

PROFILE_ID = "vllm-nvfp4-single-gpu"
ENGINE = "vllm"
PRECISION = "nvfp4"
GPU_COUNT = 1
NODE_COUNT = 1
RETRIEVAL_DATE = "2026-09-28"

# Official NVFP4 checkpoint. Not the BF16 repository.
MODEL_ARTIFACT = "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4"
# Hugging Face model API ``sha`` for that repository on the retrieval date.
MODEL_REVISION = "bee7596271d1495f6992ae224aefde4410e816b8"
# No official single artifact digest is published. Do not synthesize one.
MODEL_ARTIFACT_HASH: str | None = None

# Model card names this image tag. vLLM published release v0.27.1 the same day.
ENGINE_VERSION = "0.27.1"
# Docker Hub linux/amd64 image digest for that tag. Not the multi-arch index.
CONTAINER_DIGEST = (
    "docker.io/vllm/vllm-openai@sha256:"
    "c2f3b1b964e47809b722b5e75b61b1e7b39a50f70388cf2bf2418f16a9f31da2"
)

# vLLM v0.27.1 docker/Dockerfile default. Not an RTX PRO 6000 validation.
DOCUMENTED_CUDA_VERSION = "13.0.3"
# NVIDIA CUDA 13.0 toolkit notes: corresponding driver branch, and 13.x
# minor-version compatibility floor. Not an RTX PRO 6000 NVFP4 driver pin.
DOCUMENTED_CUDA_DRIVER_BRANCH = "R580"
DOCUMENTED_CUDA_MINIMUM_DRIVER = ">=580"

# Model-card hardware list. RTX PRO 6000 is absent. RTX 5090 has no recipe.
DOCUMENTED_ARCHITECTURES = (
    "DGX Spark / GB10",
    "GB200",
    "GeForce RTX 5090",
    "H100",
    "H200",
    "Ampere via W4A16",
)
# Compile list in the v0.27.1 Dockerfile. 12.0 is generic, not this GPU.
DOCKERFILE_CUDA_ARCHITECTURES = ("7.5", "8.0", "8.6", "8.9", "9.0", "10.0", "11.0", "12.0")

NATIVE_OPENAI_TOOL_CALL_SUPPORT = "not-validated-for-rtx-pro-6000-nvfp4"
LIVE_EXECUTION_AUTHORIZED = False

MODEL_CARD_URL = "https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4"
VLLM_RELEASE_URL = "https://github.com/vllm-project/vllm/releases/tag/v0.27.1"
VLLM_DOCKERFILE_URL = "https://github.com/vllm-project/vllm/blob/v0.27.1/docker/Dockerfile"
CONTAINER_REGISTRY_URL = "https://hub.docker.com/r/vllm/vllm-openai/tags"
CUDA_DRIVER_URL = "https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html"

_RTX_PRO_6000_BLOCK = (
    "live execution remains blocked until the exact RTX PRO 6000 NVFP4 path is validated"
)
_ARTIFACT_DIGEST_BLOCK = (
    "unsupported model artifact digest: no official immutable NVFP4 artifact digest is published"
)
_SM120_IS_NOT_VALIDATION = (
    "generic Blackwell or SM120 support is not an RTX PRO 6000 NVFP4 validation"
)
_TOOL_CALL_UNVALIDATED = (
    "native OpenAI tool-call support is not validated for the RTX PRO 6000 NVFP4 path"
)


class VllmNvfp4SingleGpuProfile:
    """vLLM + NVFP4 + one GPU. Classification only; never a launch command."""

    profile_id = PROFILE_ID
    engine = ENGINE
    precision = PRECISION
    topologies = frozenset({TOPOLOGY_SINGLE_GPU})

    def matches(self, declaration: EngineContractDeclaration) -> bool:
        return (
            declaration.engine == self.engine
            and declaration.precision == self.precision
            and declaration.topology.kind in self.topologies
            and declaration.topology.gpu_count == GPU_COUNT
            and declaration.topology.node_count == NODE_COUNT
        )

    def evaluate(self, declaration: EngineContractDeclaration) -> EngineReadiness:
        if not self.matches(declaration):
            return EngineReadiness(
                status=READINESS_BLOCKED,
                profile_id=self.profile_id,
                reasons=("declaration is not vLLM NVFP4 single-GPU",),
                declaration=declaration,
            )
        identity = declaration.identity
        reasons: list[str] = list(entitlement_blockers(declaration.entitlements))
        if identity.model_artifact != MODEL_ARTIFACT:
            reasons.append("unsupported model artifact for vllm-nvfp4-single-gpu")
        if identity.model_revision != MODEL_REVISION:
            reasons.append("unsupported model revision for vllm-nvfp4-single-gpu")
        if identity.engine_version != ENGINE_VERSION:
            reasons.append("unsupported engine version for vllm-nvfp4-single-gpu")
        if identity.container_digest != CONTAINER_DIGEST:
            reasons.append("unsupported container digest for vllm-nvfp4-single-gpu")
        # Every supplied digest is unsupported until an official one exists.
        reasons.append(_ARTIFACT_DIGEST_BLOCK)
        resolved_pins_match = (
            identity.model_artifact == MODEL_ARTIFACT
            and identity.model_revision == MODEL_REVISION
            and identity.engine_version == ENGINE_VERSION
            and identity.container_digest == CONTAINER_DIGEST
        )
        if resolved_pins_match:
            reasons.append(_RTX_PRO_6000_BLOCK)
            reasons.append(_SM120_IS_NOT_VALIDATION)
            reasons.append(_TOOL_CALL_UNVALIDATED)
        return EngineReadiness(
            status=READINESS_BLOCKED,
            profile_id=self.profile_id,
            reasons=tuple(reasons),
            declaration=declaration,
        )


def _register_profile() -> None:
    """Register once per process.

    Discovery reloads this module when the registry is reset. Reloading an
    already-registered instance must keep that instance; the registry rejects
    a second object with the same id.
    """
    if any(profile.profile_id == PROFILE_ID for profile in list_profiles()):
        return
    register_engine_profile(VllmNvfp4SingleGpuProfile())


_register_profile()
