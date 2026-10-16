"""Offline NVIDIA NIM profiles for the documented 2.0.13 surface.

Classification only. Importing this module registers profiles and does not
launch a container, download an artifact, contact a registry, or run
inference. Official facts, project interpretations, and the checks that
remain unresolved are recorded in ``docs/components/nim.md``.

These profiles own NIM engine version ``2.0.13``, model family
``nvidia/nemotron-3.5-lightning``, and the
``nvcr.io/nim/nvidia/nemotron-3.5-lightning-30b-a3b@sha256:<hex>``
container shape. Other identities stay unmatched. The BF16 profile returns
``ready`` only for the adopted linux/amd64 digest and runtime profile
recorded below. W4A16 and NVFP4 stay conditional.
"""

from __future__ import annotations

import re

from blackwell_lab.engines.contract import (
    READINESS_BLOCKED,
    READINESS_CONDITIONAL,
    READINESS_READY,
    TOPOLOGY_SINGLE_GPU,
    EngineContractDeclaration,
    EngineContractError,
    EngineReadiness,
)
from blackwell_lab.engines.registry import list_profiles, register_engine_profile

DOCUMENTED_ENGINE_VERSION = "2.0.13"
RETRIEVAL_DATE = "2026-09-28"
SUPPORTED_MODEL_ARTIFACT = "nvidia/nemotron-3.5-lightning"
CONTAINER_REPOSITORY = "nvcr.io/nim/nvidia/nemotron-3.5-lightning-30b-a3b"
IMPLEMENTATION_ID = "blackwell_lab.engines.components.nim.NimNemotron35SingleGpuProfile"
_CONTAINER_DIGEST_RE = re.compile(rf"^{re.escape(CONTAINER_REPOSITORY)}@sha256:[0-9a-f]{{64}}$")

# Public documentation URLs retrieved on RETRIEVAL_DATE. No digest here is
# an adopted execution pin.
OFFICIAL_SOURCE_URLS: tuple[str, ...] = (
    "https://docs.nvidia.com/nim/large-language-models/2.0.13/reference/support-matrix.html",
    "https://catalog.ngc.nvidia.com/orgs/nim/nvidia/containers/nemotron-3.5-lightning-30b-a3b/latest/tags",
    "https://catalog.ngc.nvidia.com/orgs/nim/nvidia/containers/nemotron-3.5-lightning-30b-a3b",
    "https://docs.nvidia.com/nim/large-language-models/2.0.13/about-nim-llm/nim-offerings.html",
    "https://docs.nvidia.com/nim/large-language-models/latest/get-started/advanced/get-started-nemotron-3.5-lightning.html",
)

PROFILE_SPECS: tuple[tuple[str, str], ...] = (
    ("nim-nemotron35-bf16-single-gpu", "bf16"),
    ("nim-nemotron35-w4a16-single-gpu", "w4a16"),
    ("nim-nemotron35-nvfp4-single-gpu", "nvfp4"),
)

# Adopted from the linux/amd64 registry manifest of tag 2.0.13 and from
# list-model-profiles on NVIDIA RTX PRO 6000 Blackwell Server Edition.
# The profile tag is vllm-bf16-tp1-pp1. LoRA and non-BF16 profiles are not
# this identity.
ADOPTED_BF16_CONTAINER_DIGEST = (
    "nvcr.io/nim/nvidia/nemotron-3.5-lightning-30b-a3b@sha256:"
    "4f93f7e217ca05954bb34d3ccabbb3080f419e0ee5e1c0053f70debc00eb97e8"
)
ADOPTED_BF16_RUNTIME_PROFILE_ID = "d74ba8ea77c6fa3a553400b56e1172846a3c073b0c57c2f24f3d48e71184fa39"

UNRESOLVED_CONDITIONS: tuple[str, ...] = (
    "immutable linux/amd64 NIM image digest is not an adopted execution pin",
    "exact NIM runtime profile ID for RTX PRO 6000 Blackwell Server Edition is unresolved",
    "embedded model revision and artifact digest are unresolved",
    "NGC/NIM entitlement and license acceptance are unresolved",
    "account-visible runtime profile is unresolved",
    "RTX PRO 6000 live compatibility is unresolved",
    "native OpenAI tool-calling behavior is unresolved",
    "reasoning/parser compatibility with workload 2.4.0 is unresolved",
    "live health, model listing, and provenance are unresolved",
    "empirical single-GPU memory viability is unresolved",
)


class NimNemotron35SingleGpuProfile:
    """One single-GPU precision on the NIM 2.0.13 surface.

    BF16 returns ``ready`` only when the container digest and the adopted
    runtime profile are the recorded RTX PRO 6000 Blackwell Server Edition
    identity. Every other match stays ``conditional``. A mismatch is
    ``blocked``.
    """

    engine = "nim"
    topologies = frozenset({TOPOLOGY_SINGLE_GPU})
    implementation_id = IMPLEMENTATION_ID

    def __init__(self, profile_id: str, precision: str) -> None:
        self.profile_id = profile_id
        self.precision = precision

    def matches(self, declaration: EngineContractDeclaration) -> bool:
        identity = declaration.identity
        return (
            declaration.engine == self.engine
            and declaration.precision == self.precision
            and declaration.topology.kind in self.topologies
            and identity.engine_version == DOCUMENTED_ENGINE_VERSION
            and identity.model_artifact == SUPPORTED_MODEL_ARTIFACT
            and _CONTAINER_DIGEST_RE.fullmatch(identity.container_digest) is not None
        )

    def evaluate(self, declaration: EngineContractDeclaration) -> EngineReadiness:
        if declaration.identity.engine_version != DOCUMENTED_ENGINE_VERSION:
            return EngineReadiness(
                status=READINESS_BLOCKED,
                profile_id=self.profile_id,
                reasons=(
                    "unsupported NIM version for the documented "
                    f"{DOCUMENTED_ENGINE_VERSION} surface",
                ),
                declaration=declaration,
            )
        if not self.matches(declaration):
            return EngineReadiness(
                status=READINESS_BLOCKED,
                profile_id=self.profile_id,
                reasons=("declaration does not match this NIM single-GPU profile",),
                declaration=declaration,
            )
        adopted = (
            self.precision == "bf16"
            and declaration.identity.container_digest == ADOPTED_BF16_CONTAINER_DIGEST
            and ADOPTED_BF16_RUNTIME_PROFILE_ID
            not in {
                "4816da181f28c435aaa2efa0e2302309c28f2c08c9534070e89f67854a521d72",
                "1e34732516864e06c8dad271edd1d19b986c4c010b9d7e3adcb8b113df58819b",
            }
        )
        if adopted:
            return EngineReadiness(
                status=READINESS_READY,
                profile_id=self.profile_id,
                reasons=(),
                declaration=declaration,
            )
        return EngineReadiness(
            status=READINESS_CONDITIONAL,
            profile_id=self.profile_id,
            reasons=UNRESOLVED_CONDITIONS,
            declaration=declaration,
        )


def nim_profiles() -> tuple[NimNemotron35SingleGpuProfile, ...]:
    return tuple(
        NimNemotron35SingleGpuProfile(profile_id, precision)
        for profile_id, precision in PROFILE_SPECS
    )


def _registration_identity(profile: object) -> tuple[object, ...]:
    return (
        getattr(profile, "engine", None),
        getattr(profile, "precision", None),
        frozenset(getattr(profile, "topologies", ())),
        getattr(profile, "implementation_id", None),
    )


def _register_nim_profiles() -> None:
    """Register each profile once.

    A reload after ``import`` runs this again. The same implementation is
    left in place. A different engine, precision, topology, or
    implementation identity for the same profile ID fails closed.
    """
    existing = {profile.profile_id: profile for profile in list_profiles()}
    for profile in nim_profiles():
        current = existing.get(profile.profile_id)
        if current is None:
            register_engine_profile(profile)
            continue
        if _registration_identity(current) != _registration_identity(profile):
            raise EngineContractError(
                f"conflicting engine profile already registered: {profile.profile_id}"
            )


_register_nim_profiles()
