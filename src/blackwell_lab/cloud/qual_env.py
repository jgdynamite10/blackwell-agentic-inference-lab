"""Pinned, isolated qualification Python environment (decision D-0031).

The qual-p1-b development run aborted before any task because the
interpreter that executed ``qualify-agent`` lacked the JSON Schema format
extras, so :func:`blackwell_lab.schemas._format_checker` refused to run.
Every qualification command therefore verifies, before the config, ledger,
provenance, approval, or any model client is touched, that:

* the interpreter runs inside a virtual environment (never system Python);
* that environment carries the bootstrap marker written by
  ``scripts/bootstrap_qual_env.sh``, and the marker's recorded versions still
  match what is installed;
* every distribution the schema format checks depend on is installed at the
  version pinned in ``constraints.txt``;
* the ``date`` and ``date-time`` format checkers are registered.

The check is read-only. Its report is content-free: it records versions,
booleans, and problem codes, never paths. ``--validate-only`` reports the
state; live execution fails closed.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
import sys
from pathlib import Path
from typing import Any

from blackwell_lab.workload.validation import ConfigError

QUAL_ENV_SCHEMA_VERSION = "1.0.0"
MARKER_NAME = "bwlab-qual-env.json"
MARKER_KIND = "blackwell-lab-qualification-environment"
#: Distributions the schema format checks (and the package itself) require.
REQUIRED_DISTRIBUTIONS: tuple[str, ...] = (
    "jsonschema",
    "jsonschema-specifications",
    "referencing",
    "rpds-py",
    "rfc3339-validator",
    "attrs",
    "six",
)
REQUIRED_FORMATS: tuple[str, ...] = ("date", "date-time")
#: Project interpreter floor (pyproject ``requires-python``). The bootstrap
#: script pins the exact interpreter; the marker records it.
MINIMUM_PYTHON = (3, 10)

_CONSTRAINT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^;\s]+)\s*(;\s*(?P<marker>.+))?$"
)
_MARKER_RE = re.compile(
    r'^python_version\s*(?P<op>>=|<=|==|<|>)\s*"(?P<major>\d+)\.(?P<minor>\d+)"$'
)


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def constraints_path(repo_root: Path | None = None) -> Path:
    root = repo_root or Path(__file__).resolve().parents[3]
    return root / "constraints.txt"


def _marker_applies(marker: str | None, python_version: tuple[int, int]) -> bool:
    if marker is None:
        return True
    match = _MARKER_RE.match(marker.strip())
    if match is None:
        raise ConfigError("constraints.txt contains an unsupported environment marker")
    target = (int(match.group("major")), int(match.group("minor")))
    op = match.group("op")
    return {
        ">=": python_version >= target,
        "<=": python_version <= target,
        "==": python_version == target,
        "<": python_version < target,
        ">": python_version > target,
    }[op]


def pinned_versions(
    path: Path | None = None, *, python_version: tuple[int, int] | None = None
) -> dict[str, str]:
    """Exact pins from constraints.txt for the running (or given) interpreter."""
    path = path or constraints_path()
    version = python_version or sys.version_info[:2]
    pins: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigError("constraints.txt is unreadable") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _CONSTRAINT_RE.match(line)
        if match is None:
            raise ConfigError("constraints.txt contains a non-exact pin")
        if _marker_applies(match.group("marker"), version):
            pins[normalize(match.group("name"))] = match.group("version")
    return pins


def installed_versions(names: tuple[str, ...] = REQUIRED_DISTRIBUTIONS) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for name in names:
        try:
            found[normalize(name)] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            found[normalize(name)] = None
    return found


def format_checker_report() -> dict[str, bool]:
    """Which required format checkers the installed jsonschema registers."""
    import jsonschema

    checkers = jsonschema.FormatChecker().checkers
    return {name: name in checkers for name in REQUIRED_FORMATS}


def in_virtual_environment(prefix: str | None = None, base_prefix: str | None = None) -> bool:
    return (prefix or sys.prefix) != (base_prefix or sys.base_prefix)


def marker_path(prefix: str | None = None) -> Path:
    """Marker location inside the environment prefix. Tests may monkeypatch."""
    return Path(prefix or sys.prefix) / MARKER_NAME


def marker_document(*, pins: dict[str, str], installed: dict[str, str | None]) -> dict[str, Any]:
    """Content-free record of the environment at bootstrap time."""
    return {
        "schema_version": QUAL_ENV_SCHEMA_VERSION,
        "kind": MARKER_KIND,
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "required_distributions": list(REQUIRED_DISTRIBUTIONS),
        "pinned_versions": {name: pins[name] for name in sorted(pins)},
        "installed_versions": {name: installed[name] for name in sorted(installed)},
        "format_checkers": format_checker_report(),
    }


def write_marker(path: Path, *, pins: dict[str, str] | None = None) -> dict[str, Any]:
    """Write the bootstrap marker for the running interpreter (idempotent)."""
    pins = pins if pins is not None else pinned_versions()
    document = marker_document(pins=pins, installed=installed_versions())
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return document


def _read_marker(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def inspect_environment(
    *,
    constraints: Path | None = None,
    prefix: str | None = None,
    base_prefix: str | None = None,
    marker: Path | None = None,
) -> dict[str, Any]:
    """Read-only environment report. Never raises for an incomplete environment."""
    problems: list[str] = []
    python_version = sys.version_info[:2]
    if python_version < MINIMUM_PYTHON:
        problems.append("unsupported_python")
    isolated = in_virtual_environment(prefix, base_prefix)
    if not isolated:
        problems.append("not_a_virtual_environment")
    try:
        pins = pinned_versions(constraints)
    except ConfigError:
        pins = {}
        problems.append("constraints_unreadable")
    installed = installed_versions()
    mismatches: dict[str, dict[str, str | None]] = {}
    for name in map(normalize, REQUIRED_DISTRIBUTIONS):
        expected = pins.get(name)
        actual = installed.get(name)
        if actual is None:
            problems.append("missing_distribution")
            mismatches[name] = {"pinned": expected, "installed": None}
        elif expected is None:
            problems.append("unpinned_distribution")
            mismatches[name] = {"pinned": None, "installed": actual}
        elif expected != actual:
            problems.append("version_drift")
            mismatches[name] = {"pinned": expected, "installed": actual}
    try:
        formats = format_checker_report()
    except Exception:  # jsonschema itself may be missing
        formats = {name: False for name in REQUIRED_FORMATS}
    if not all(formats.values()):
        problems.append("format_checker_missing")
    marker_file = marker if marker is not None else marker_path(prefix)
    document = _read_marker(marker_file)
    marker_present = document is not None
    marker_valid = False
    if document is None:
        problems.append("marker_missing")
    else:
        recorded = document.get("installed_versions")
        marker_valid = (
            document.get("kind") == MARKER_KIND
            and document.get("schema_version") == QUAL_ENV_SCHEMA_VERSION
            and document.get("python_version") == ".".join(str(p) for p in sys.version_info[:3])
            and isinstance(recorded, dict)
            and all(recorded.get(name) == installed.get(name) for name in installed)
            and document.get("pinned_versions") == {name: pins[name] for name in sorted(pins)}
        )
        if not marker_valid:
            problems.append("marker_stale")
    ordered = list(dict.fromkeys(problems))
    return {
        "schema_version": QUAL_ENV_SCHEMA_VERSION,
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "isolated": isolated,
        "marker_present": marker_present,
        "marker_valid": marker_valid,
        "pinned_versions": {
            name: pins.get(name) for name in map(normalize, REQUIRED_DISTRIBUTIONS)
        },
        "installed_versions": installed,
        "mismatches": mismatches,
        "format_checkers": formats,
        "problems": ordered,
        "ok": not ordered,
    }


def require_qualification_environment(*, validate_only: bool = False) -> dict[str, Any]:
    """Gate every qualification command on the isolated environment.

    ``--validate-only`` returns the report regardless of its verdict so the
    operator can see what is missing. Live execution raises before any
    config, ledger, approval, provenance, or client work.
    """
    report = inspect_environment()
    if validate_only or report["ok"]:
        return report
    raise ConfigError(
        "qualification environment is incomplete ("
        + ", ".join(report["problems"])
        + "); run scripts/bootstrap_qual_env.sh and re-invoke through its interpreter. "
        "Nothing was executed."
    )
