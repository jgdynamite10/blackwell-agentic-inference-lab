"""Results-privacy path guard.

Genuine benchmark results must never be written inside the Git working tree.
They go to an external private location supplied via the ``LAB_RESULTS_DIR``
environment variable. The benchmark runner (Phase 2+) must call
:func:`resolve_results_dir` at startup, declare its run mode explicitly, and
refuse to run when this module raises.

Expected behavior (documented in ``docs/results-privacy.md``):

- **Real-run mode** (:attr:`RunMode.REAL`, the default): fails closed when the
  location is unset or blank; rejects every **relative path** before any
  resolution (the private results location must be an unambiguous absolute
  path); fails when the value — after resolving ``~`` and symlinks
  canonically — is the repository root or any directory beneath it. There is
  never a fallback to ``results/`` or any other repository directory.
- **Synthetic/test mode** (:attr:`RunMode.SYNTHETIC`): must be requested
  explicitly by the caller and is used only for synthetic examples and tests.
  An unset location yields ``None`` ("no persistence") rather than a silent
  fallback; a provided path is subject to the same relative-path and
  repository-interior rejections.
- The guard never creates a nonexistent directory merely while validating it.
  A nonexistent *external* absolute path is accepted; creating it is the
  runner's explicit, logged action.
"""

from __future__ import annotations

import enum
import os
from pathlib import Path

RESULTS_DIR_ENV_VAR = "LAB_RESULTS_DIR"


class RunMode(enum.Enum):
    """Explicit run mode; callers must never infer one from context."""

    REAL = "real"
    SYNTHETIC = "synthetic"


class ResultsLocationError(RuntimeError):
    """The private-results location is unsafe or not configured."""


def repository_root(start: Path | None = None) -> Path:
    """Locate the repository root by walking up to the directory holding ``.git``.

    Falls back to the package's grandparent directory when no ``.git`` is found
    (e.g. an exported source tree), which is still a safe boundary to guard.
    """
    probe = (start or Path(__file__)).resolve()
    for candidate in (probe, *probe.parents):
        if (candidate / ".git").exists():
            return candidate
    return Path(__file__).resolve().parents[2]


def resolve_results_dir(
    value: str | None = None,
    *,
    repo_root: Path | None = None,
    mode: RunMode = RunMode.REAL,
) -> Path | None:
    """Validate and return the private results directory for the given mode.

    ``value`` defaults to the ``LAB_RESULTS_DIR`` environment variable.

    Returns the canonical (symlink-resolved) external path, or ``None`` in
    :attr:`RunMode.SYNTHETIC` when no location is configured (meaning "persist
    nothing" — never a repository fallback).

    Raises :class:`ResultsLocationError` when the location is unset in
    :attr:`RunMode.REAL`, or when any provided location resolves to the
    repository root or a directory beneath it (in either mode).
    """
    raw = value if value is not None else os.environ.get(RESULTS_DIR_ENV_VAR, "")
    if not raw.strip():
        if mode is RunMode.SYNTHETIC:
            return None
        raise ResultsLocationError(
            f"{RESULTS_DIR_ENV_VAR} is not set. Real runs fail closed: genuine results "
            "must be written to a private location outside the repository "
            "(see docs/results-privacy.md). There is no fallback directory."
        )

    # Reject relative paths BEFORE any resolution: the private results
    # location must be unambiguous and independent of the current working
    # directory.
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        raise ResultsLocationError(
            f"{RESULTS_DIR_ENV_VAR}={raw!r} is a relative path. The private results "
            "location must be an absolute path outside the repository "
            "(see docs/results-privacy.md)."
        )

    # Path.resolve() canonicalizes and follows symlinks, so a symlink
    # pointing into the repository is caught.
    results_dir = candidate.resolve()
    root = (repo_root or repository_root()).resolve()

    if results_dir == root or root in results_dir.parents:
        raise ResultsLocationError(
            f"{RESULTS_DIR_ENV_VAR}={raw!r} resolves inside the repository ({root}). "
            "Refusing to run: genuine results must never be written into the Git "
            "working tree (see docs/results-privacy.md)."
        )
    return results_dir
