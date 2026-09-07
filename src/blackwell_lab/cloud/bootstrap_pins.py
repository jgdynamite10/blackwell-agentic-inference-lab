"""Fail-closed validation for public bootstrap candidate pins.

The example file is the only pin document that lives in Git. It must name
immutable, non-secret candidate values. It is not a claim that those values
have been validated on the target GPU, and it must never contain tokens or
a private results path.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

#: Installation pins that must be exact package or repository material
#: before bootstrap mutates a host. Empty or floating values fail closed.
INSTALLATION_PINS = (
    "NVIDIA_DRIVER_PACKAGE",
    "NVIDIA_DRIVER_PACKAGE_VERSION",
    "NVIDIA_CTK_PACKAGE_VERSION",
    "NVIDIA_REPO_KEY_URL",
    "NVIDIA_REPO_LIST",
    "DOCKER_PACKAGE",
    "DOCKER_PACKAGE_VERSION",
)

#: Runtime pins that identify the candidate serving and probe artifacts.
IDENTITY_PINS = (
    "VLLM_IMAGE",
    "VLLM_IMAGE_DIGEST",
    "VLLM_IMAGE_INDEX_DIGEST",
    "MODEL_ARTIFACT",
    "MODEL_REVISION",
    "MODEL_DIR",
    "MODEL_DIGEST_MANIFEST",
    "MIN_DRIVER_BRANCH",
    "DRIVER_MAX_CUDA_MAJOR",
    "REQUIRED_CONTAINER_CUDA_VERSION",
    "GPU_PROBE_IMAGE",
    "GPU_PROBE_IMAGE_DIGEST",
    "GPU_PROBE_IMAGE_INDEX_DIGEST",
    "GPU_PROBE_EXPECTED_GPU",
)

ALL_REQUIRED_PINS = INSTALLATION_PINS + IDENTITY_PINS

OCI_DIGEST_PINS = (
    "VLLM_IMAGE_DIGEST",
    "VLLM_IMAGE_INDEX_DIGEST",
    "GPU_PROBE_IMAGE_DIGEST",
    "GPU_PROBE_IMAGE_INDEX_DIGEST",
)

_ASSIGNMENT_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
_DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
_REVISION_RE = re.compile(r"^[a-f0-9]{40}$")
_FLOATING_VALUES = frozenset(
    {
        "latest",
        "nightly",
        "stable",
        "current",
        "main",
        "master",
        "head",
    }
)
_FORBIDDEN_KEY_FRAGMENTS = (
    "TOKEN",
    "PASSWORD",
    "SECRET",
    "CREDENTIAL",
    "API_KEY",
    "LAB_RESULTS_DIR",
)
_FORBIDDEN_VALUE_FRAGMENTS = (
    "HF_TOKEN=",
    "LINODE_TOKEN=",
    "AWS_SECRET",
    "BEGIN PRIVATE KEY",
)


def parse_env_assignments(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` assignments, ignoring comments and blank lines."""
    assignments: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ASSIGNMENT_RE.match(line)
        if match is None:
            continue
        key, raw_value = match.group(1), match.group(2)
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        assignments[key] = value
    return assignments


def _looks_floating(value: str) -> bool:
    lowered = value.strip().casefold()
    if lowered in _FLOATING_VALUES:
        return True
    if lowered.endswith(":latest") or lowered.endswith("@latest"):
        return True
    if "/latest" in lowered:
        return True
    return False


def validate_candidate_pins(text: str) -> list[str]:
    """Return human-readable problems; an empty list means the pins are usable."""
    assignments = parse_env_assignments(text)
    problems: list[str] = []

    for key in assignments:
        if any(fragment in key for fragment in _FORBIDDEN_KEY_FRAGMENTS):
            problems.append(f"{key} is a forbidden credential or private-path name")
    for raw_line in text.splitlines():
        upper = raw_line.upper()
        for fragment in _FORBIDDEN_VALUE_FRAGMENTS:
            if fragment in upper:
                problems.append("bootstrap pin text contains a credential-shaped assignment")
                break

    for key in ALL_REQUIRED_PINS:
        if key not in assignments:
            problems.append(f"missing required pin {key}")
            continue
        value = assignments[key]
        if value == "":
            problems.append(f"required pin {key} is empty")
            continue
        if _looks_floating(value):
            problems.append(f"required pin {key} uses a floating value")

    for key in OCI_DIGEST_PINS:
        value = assignments.get(key, "")
        if value and not _DIGEST_RE.fullmatch(value):
            problems.append(f"{key} must be an immutable sha256:<64-hex> digest")

    revision = assignments.get("MODEL_REVISION", "")
    if revision and not _REVISION_RE.fullmatch(revision):
        problems.append("MODEL_REVISION must be the exact 40-character Hugging Face commit")

    image = assignments.get("VLLM_IMAGE", "")
    if image.endswith(":latest") or (image and _looks_floating(image)):
        problems.append("VLLM_IMAGE uses a floating tag")

    probe = assignments.get("GPU_PROBE_IMAGE", "")
    if probe.endswith(":latest"):
        problems.append("GPU_PROBE_IMAGE uses the floating tag latest")

    return problems


def candidate_pins_are_valid(text: str) -> bool:
    return not validate_candidate_pins(text)


def require_nonempty_installation_pins(assignments: Mapping[str, str]) -> list[str]:
    """Installation-only check used by fail-closed mutation tests."""
    problems = []
    for key in INSTALLATION_PINS:
        value = assignments.get(key, "")
        if not value or _looks_floating(value):
            problems.append(key)
    return problems
