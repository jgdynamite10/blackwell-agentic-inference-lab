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
APPROVED_DRIVER_PACKAGE = "nvidia-driver-580-server-open"
PROPRIETARY_DRIVER_PACKAGE = "nvidia-driver-580-server"
APPROVED_DRIVER_PACKAGE_VERSION = "580.173.02-0ubuntu0.24.04.1"
APPROVED_OS_ID = "ubuntu"
APPROVED_OS_VERSION = "24.04"
APPROVED_SERVED_MODEL_NAME = "nemotron-3.5-lightning-30b-a3b-bf16"
APPROVED_SERVING_PORT = "8000"
APPROVED_VLLM_EXTRA_ARGS = (
    "--dtype bfloat16 --max-num-seqs 128 --enable-prefix-caching "
    "--async-scheduling --mamba-backend flashinfer "
    "--mamba-ssm-cache-dtype float16 --enable-mamba-cache-stochastic-rounding "
    "--mamba-cache-philox-rounds 5 "
    "--reasoning-parser nemotron_v3 --tool-call-parser qwen3_coder "
    "--enable-auto-tool-choice"
)
APPROVED_REASONING_PARSER = "nemotron_v3"
APPROVED_TOOL_CALL_PARSER = "qwen3_coder"
REQUIRED_VLLM_PARSER_FLAGS = (
    "--reasoning-parser",
    "--tool-call-parser",
    "--enable-auto-tool-choice",
)
APPROVED_WATCHDOG_IDLE_MINUTES = "45"
MAX_WATCHDOG_IDLE_MINUTES = 45

INSTALLATION_PINS = (
    "NVIDIA_DRIVER_PACKAGE",
    "NVIDIA_DRIVER_PACKAGE_VERSION",
    "NVIDIA_CTK_PACKAGE_VERSION",
    "NVIDIA_REPO_KEY_URL",
    "NVIDIA_REPO_KEY_SHA256",
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

#: Frozen D-0017 host/serving settings. Required and must match the reviewed
#: example. The D-0014 pilot continues to use the same pins.
PILOT_RUNTIME_PINS = (
    "REQUIRED_OS_ID",
    "REQUIRED_OS_VERSION",
    "SERVED_MODEL_NAME",
    "SERVING_PORT",
    "VLLM_EXTRA_ARGS",
    "WATCHDOG_IDLE_MINUTES",
)

ALL_REQUIRED_PINS = INSTALLATION_PINS + IDENTITY_PINS + PILOT_RUNTIME_PINS

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
    if any(token in value for token in ("*", "?")):
        return True
    if value.startswith((">=", "<=", ">", "<", "^")):
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

    key_digest = assignments.get("NVIDIA_REPO_KEY_SHA256", "")
    if key_digest and not (
        _DIGEST_RE.fullmatch(key_digest) or re.fullmatch(r"[a-fA-F0-9]{64}", key_digest)
    ):
        problems.append("NVIDIA_REPO_KEY_SHA256 must be the 64-hex SHA-256 of the official key")

    revision = assignments.get("MODEL_REVISION", "")
    if revision and not _REVISION_RE.fullmatch(revision):
        problems.append("MODEL_REVISION must be the exact 40-character Hugging Face commit")

    driver = assignments.get("NVIDIA_DRIVER_PACKAGE", "")
    if driver == PROPRIETARY_DRIVER_PACKAGE:
        problems.append(
            "proprietary nvidia-driver-580-server is rejected; Blackwell requires open modules"
        )
    elif driver and driver != APPROVED_DRIVER_PACKAGE:
        problems.append(f"NVIDIA_DRIVER_PACKAGE must be {APPROVED_DRIVER_PACKAGE}")
    version = assignments.get("NVIDIA_DRIVER_PACKAGE_VERSION", "")
    if version and version != APPROVED_DRIVER_PACKAGE_VERSION:
        problems.append(
            "NVIDIA_DRIVER_PACKAGE_VERSION must equal the reviewed open-driver candidate"
        )

    image = assignments.get("VLLM_IMAGE", "")
    if image.endswith(":latest") or (image and _looks_floating(image)):
        problems.append("VLLM_IMAGE uses a floating tag")

    probe = assignments.get("GPU_PROBE_IMAGE", "")
    if probe.endswith(":latest"):
        problems.append("GPU_PROBE_IMAGE uses the floating tag latest")

    os_id = assignments.get("REQUIRED_OS_ID", "")
    if os_id and os_id != APPROVED_OS_ID:
        problems.append("REQUIRED_OS_ID differs from the reviewed candidate baseline")
    os_version = assignments.get("REQUIRED_OS_VERSION", "")
    if os_version and os_version != APPROVED_OS_VERSION:
        problems.append("REQUIRED_OS_VERSION differs from the reviewed candidate baseline")
    served = assignments.get("SERVED_MODEL_NAME", "")
    if served and served != APPROVED_SERVED_MODEL_NAME:
        problems.append("SERVED_MODEL_NAME differs from the reviewed candidate baseline")
    extra_args = assignments.get("VLLM_EXTRA_ARGS", "")
    if extra_args:
        problems.extend(validate_vllm_parser_flags(extra_args))
        if extra_args != APPROVED_VLLM_EXTRA_ARGS:
            problems.append("VLLM_EXTRA_ARGS differs from the reviewed candidate baseline")

    port = assignments.get("SERVING_PORT", "")
    if port:
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            problems.append("SERVING_PORT must be an integer in 1-65535")
        elif port != APPROVED_SERVING_PORT:
            problems.append("SERVING_PORT differs from the reviewed candidate baseline")

    watchdog = assignments.get("WATCHDOG_IDLE_MINUTES", "")
    if watchdog:
        if not watchdog.isdigit() or not (1 <= int(watchdog) <= MAX_WATCHDOG_IDLE_MINUTES):
            problems.append(
                "WATCHDOG_IDLE_MINUTES must be a positive integer no greater than "
                f"{MAX_WATCHDOG_IDLE_MINUTES}"
            )
        elif watchdog != APPROVED_WATCHDOG_IDLE_MINUTES:
            problems.append("WATCHDOG_IDLE_MINUTES differs from the reviewed candidate baseline")

    return problems


def validate_vllm_parser_flags(extra_args: str) -> list[str]:
    """Require the NVIDIA Nemotron 3.5 Lightning / vLLM 0.27.1 parser trio.

    Official sources (NVIDIA model card for vLLM 0.27.1; NVIDIA NIM
    Nemotron 3.5 Lightning guide) name all three flags. Omission or a
    second incompatible parser value fails closed.
    """
    tokens = extra_args.split()
    problems: list[str] = []

    def values_for(flag: str) -> list[str]:
        found: list[str] = []
        index = 0
        while index < len(tokens):
            if tokens[index] == flag:
                if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                    found.append("")
                    index += 1
                    continue
                found.append(tokens[index + 1])
                index += 2
                continue
            index += 1
        return found

    reasoning = values_for("--reasoning-parser")
    tool_parser = values_for("--tool-call-parser")
    auto_choice = [flag for flag in tokens if flag == "--enable-auto-tool-choice"]

    if not reasoning:
        problems.append("VLLM_EXTRA_ARGS is missing --reasoning-parser nemotron_v3")
    elif len(reasoning) != 1:
        problems.append("VLLM_EXTRA_ARGS has incompatible duplicate --reasoning-parser values")
    elif reasoning[0] != APPROVED_REASONING_PARSER:
        problems.append("VLLM_EXTRA_ARGS --reasoning-parser must be nemotron_v3")

    if not tool_parser:
        problems.append("VLLM_EXTRA_ARGS is missing --tool-call-parser qwen3_coder")
    elif len(tool_parser) != 1:
        problems.append("VLLM_EXTRA_ARGS has incompatible duplicate --tool-call-parser values")
    elif tool_parser[0] != APPROVED_TOOL_CALL_PARSER:
        problems.append("VLLM_EXTRA_ARGS --tool-call-parser must be qwen3_coder")

    if not auto_choice:
        problems.append("VLLM_EXTRA_ARGS is missing --enable-auto-tool-choice")
    elif len(auto_choice) != 1:
        problems.append("VLLM_EXTRA_ARGS has a duplicate --enable-auto-tool-choice flag")

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
