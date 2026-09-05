"""Results-privacy path guard.

Genuine benchmark results must be written outside the public repository, to a
private location supplied via the ``LAB_RESULTS_DIR`` environment variable.
The benchmark runner (Phase 2+) must call :func:`resolve_results_dir` at
startup and refuse to run if the configured location is missing or resolves
inside the repository (including through symlinks or relative paths).
"""

from __future__ import annotations

import os
from pathlib import Path

RESULTS_DIR_ENV_VAR = "LAB_RESULTS_DIR"


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
) -> Path:
    """Validate and return the private results directory.

    Raises :class:`ResultsLocationError` if the variable is unset/empty or the
    path resolves inside the repository. Does not create the directory.
    """
    raw = value if value is not None else os.environ.get(RESULTS_DIR_ENV_VAR, "")
    if not raw.strip():
        raise ResultsLocationError(
            f"{RESULTS_DIR_ENV_VAR} is not set. Genuine results must be written to a "
            "private location outside the public repository (see docs/results-privacy.md)."
        )

    results_dir = Path(raw).expanduser().resolve()
    root = (repo_root or repository_root()).resolve()

    if results_dir == root or root in results_dir.parents:
        raise ResultsLocationError(
            f"{RESULTS_DIR_ENV_VAR}={raw!r} resolves inside the public repository "
            f"({root}). Refusing to run: genuine results must never be written into "
            "the repository (see docs/results-privacy.md)."
        )
    return results_dir
