"""Offline NVIDIA NIM profiles for the documented 2.0.13 surface.

Classification only. Importing this module registers profiles and does not
launch a container, download an artifact, contact a registry, or run
inference. Official facts, project interpretations, and the checks that
remain unresolved are recorded in ``docs/components/nim.md``.

These profiles own NIM engine version ``2.0.13`` only. Other versions stay
unmatched so they remain blocked and do not collide with unrelated
declarations.
"""

from __future__ import annotations

from blackwell_lab.engines.contract import (
    READINESS_BLOCKED,
    READINESS_CONDITIONAL,
    TOPOLOGY_SINGLE_GPU,
    EngineContractDeclaration,
    EngineReadiness,
)
from blackwell_lab.engines.registry import register_engine_profile

DOCUMENTED_ENGINE_VERSION = "2.0.13"
RETRIEVAL_DATE = "2026-09-28"

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
    """One conditional single-GPU precision on the NIM 2.0.13 surface.

    ``evaluate`` returns ``conditional`` or ``blocked``. It has no ``ready``
    result.
    """

    engine = "nim"
    topologies = frozenset({TOPOLOGY_SINGLE_GPU})

    def __init__(self, profile_id: str, precision: str) -> None:
        self.profile_id = profile_id
        self.precision = precision

    def matches(self, declaration: EngineContractDeclaration) -> bool:
        return (
            declaration.engine == self.engine
            and declaration.precision == self.precision
            and declaration.topology.kind in self.topologies
            and declaration.identity.engine_version == DOCUMENTED_ENGINE_VERSION
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


for _profile in nim_profiles():
    register_engine_profile(_profile)
