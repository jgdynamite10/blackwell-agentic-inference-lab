"""Engine-profile registry. Components register; the core never names them."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from blackwell_lab.engines.contract import (
    READINESS_BLOCKED,
    EngineContractDeclaration,
    EngineContractError,
    EngineReadiness,
    entitlement_blockers,
)


@runtime_checkable
class EngineProfile(Protocol):
    """Extension point for a later component module.

    A profile matches a declaration and reports readiness. It must not
    launch inference, download artifacts, or call a provider API.
    """

    profile_id: str
    engine: str
    precision: str
    topologies: frozenset[str]

    def matches(self, declaration: EngineContractDeclaration) -> bool:
        """Return True when this profile owns the declared combination."""

    def evaluate(self, declaration: EngineContractDeclaration) -> EngineReadiness:
        """Return ready, conditional, or blocked for a matching declaration."""


class _Registry:
    def __init__(self) -> None:
        self._profiles: dict[str, EngineProfile] = {}

    def register(self, profile: EngineProfile) -> None:
        if not getattr(profile, "profile_id", "").strip():
            raise EngineContractError("engine profile_id is required")
        existing = self._profiles.get(profile.profile_id)
        if existing is not None and existing is not profile:
            raise EngineContractError(f"engine profile already registered: {profile.profile_id}")
        self._profiles[profile.profile_id] = profile

    def get(self, profile_id: str) -> EngineProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise EngineContractError(f"unknown engine profile: {profile_id}") from exc

    def all(self) -> tuple[EngineProfile, ...]:
        return tuple(self._profiles[key] for key in sorted(self._profiles))


_REGISTRY = _Registry()


def register_engine_profile(profile: EngineProfile) -> None:
    """Register a component profile. Core code never hard-codes component ids."""
    _REGISTRY.register(profile)


def get_profile(profile_id: str) -> EngineProfile:
    return _REGISTRY.get(profile_id)


def list_profiles() -> tuple[EngineProfile, ...]:
    return _REGISTRY.all()


def evaluate_engine_contract(declaration: EngineContractDeclaration) -> EngineReadiness:
    """Evaluate a declaration against registered profiles. No match is blocked."""
    if declaration.profile_id:
        profile = get_profile(declaration.profile_id)
        if not profile.matches(declaration):
            return EngineReadiness(
                status=READINESS_BLOCKED,
                profile_id=profile.profile_id,
                reasons=("declared profile does not match engine/precision/topology",),
                declaration=declaration,
            )
        return _finalize(profile.evaluate(declaration))
    matches = [profile for profile in list_profiles() if profile.matches(declaration)]
    if not matches:
        return EngineReadiness(
            status=READINESS_BLOCKED,
            profile_id=None,
            reasons=(
                "unsupported engine/precision/topology combination "
                f"({declaration.engine}/{declaration.precision}/{declaration.topology.kind})"
            ),
            declaration=declaration,
        )
    if len(matches) > 1:
        ids = ", ".join(profile.profile_id for profile in matches)
        return EngineReadiness(
            status=READINESS_BLOCKED,
            profile_id=None,
            reasons=(f"multiple engine profiles matched: {ids}",),
            declaration=declaration,
        )
    return _finalize(matches[0].evaluate(declaration))


def _finalize(readiness: EngineReadiness) -> EngineReadiness:
    blockers = entitlement_blockers(readiness.declaration.entitlements)
    if blockers and readiness.status != READINESS_BLOCKED:
        return EngineReadiness(
            status=READINESS_BLOCKED,
            profile_id=readiness.profile_id,
            reasons=blockers + readiness.reasons,
            declaration=readiness.declaration,
        )
    return readiness


def load_registered_components() -> tuple[str, ...]:
    """Import every module under ``engines.components`` without naming them.

    Component agents add a module in that package and call
    :func:`register_engine_profile` at import time. The core never lists
    those modules.
    """
    import importlib
    import pkgutil

    from blackwell_lab.engines import components

    loaded: list[str] = []
    for module_info in pkgutil.iter_modules(components.__path__, components.__name__ + "."):
        importlib.import_module(module_info.name)
        loaded.append(module_info.name)
    return tuple(loaded)


def reset_registry(profiles: Iterable[EngineProfile] | None = None) -> None:
    """Replace the registry. Tests restore the builtin profile after extras."""
    global _REGISTRY
    _REGISTRY = _Registry()
    if profiles is None:
        from blackwell_lab.engines.builtin import builtin_profiles

        profiles = builtin_profiles()
    for profile in profiles:
        _REGISTRY.register(profile)


def _ensure_builtin() -> None:
    from blackwell_lab.engines.builtin import builtin_profiles

    if not _REGISTRY.all():
        for profile in builtin_profiles():
            _REGISTRY.register(profile)


_ensure_builtin()
