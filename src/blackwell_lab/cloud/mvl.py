"""Akamai minimum valuable lab (decision D-0017).

Provider-native three-cell baseline built on the existing pilot and
``run_real_cell`` paths. This is not a research-grade orchestration
framework: no controlled-resource mode, no 12-cell matrix, and no resume.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from blackwell_lab.workload.scenarios import WORKLOAD_VERSION, catalog
from blackwell_lab.workload.validation import ConfigError

MVL_APPROVAL_TEMPLATE = (
    "I approve the Akamai minimum valuable baseline for run {run_tag} "
    "({run_label}) using config sha256:{config_sha256}"
)

FROZEN_PROVIDER = "akamai"
FROZEN_REGION = "us-sea"
FROZEN_INSTANCE_TYPE = "g3-gpu-rtxpro6000-blackwell-1"
FROZEN_GPU = "RTX PRO 6000 Blackwell"
FROZEN_COMPARISON_MODE = "provider-native"
FROZEN_MODEL_ARTIFACT = "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16"
FROZEN_MODEL_REVISION = "a9904d24bcc1d289a1950fa9d2b978c47cf903b9"
FROZEN_MODEL_ARTIFACT_HASH = (
    "sha256:5d6a435f3e0faf95dd4a610c66fefd9c4ccbe350af950216dab9fec6f3d3da0d"
)
FROZEN_PRECISION = "bf16"
FROZEN_ENGINE = "vllm"
FROZEN_ENGINE_VERSION = "0.27.1"
FROZEN_VLLM_IMAGE = "docker.io/vllm/vllm-openai:v0.27.1"
FROZEN_VLLM_IMAGE_DIGEST = "sha256:c2f3b1b964e47809b722b5e75b61b1e7b39a50f70388cf2bf2418f16a9f31da2"
FROZEN_CONTAINER_DIGEST = f"{FROZEN_VLLM_IMAGE}@{FROZEN_VLLM_IMAGE_DIGEST}"
FROZEN_TEMPERATURE = 1.0
FROZEN_TOP_P = 0.95
FROZEN_SEED = 20260906
FROZEN_REASONING_MODE = True
FROZEN_WORKLOAD_VERSION = "2.3.0"
FROZEN_WARMUP_PASSES = 1
FROZEN_REPETITIONS = 3
FROZEN_TASKS_PER_REPETITION = 200
FROZEN_CANARY_TASKS = 10
FROZEN_CANARY_CONCURRENCY = 1
FROZEN_SESSION_HOURS = 6
FROZEN_HOURLY_PRICE_USD = 3.0

AUTHORIZED_MVL_CELLS: tuple[tuple[str, int], ...] = (
    ("interactive", 1),
    ("batch-heavy", 4),
    ("batch-heavy", 8),
)

_RUN_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

STRUCTURAL_FAILURE_CATEGORIES = frozenset(
    {
        "endpoint_error",
        "malformed_tool_call",
        "invalid_tool_name",
        "invalid_tool_arguments",
        "task_timeout",
        "agent_runtime_error",
    }
)

TEARDOWN_FROM_LAPTOP_NOTE = (
    "Inference has stopped. Run 'blackwell-cloud teardown-plan' from the "
    "owner's laptop. This command never generates a Terraform destroy plan "
    "on the GPU host and never deletes provider resources."
)


class MvlError(RuntimeError):
    """Fail-closed MVL error (sanitized; no private paths or credentials)."""


def measured_counts() -> dict:
    cells = len(AUTHORIZED_MVL_CELLS)
    repetitions = cells * FROZEN_REPETITIONS
    return {
        "cells": cells,
        "measured_repetitions": repetitions,
        "measured_task_observations": repetitions * FROZEN_TASKS_PER_REPETITION,
    }


def require_safe_run_label(run_label: str) -> str:
    if not _RUN_LABEL_RE.match(run_label):
        raise ConfigError("run_label must be short lowercase letters/digits/hyphens")
    return run_label


def output_label(run_label: str, suffix: str) -> str:
    label = f"{run_label}-{suffix}"
    if not _RUN_LABEL_RE.match(label):
        raise ConfigError("composed MVL output label is unsafe")
    return label


def cell_output_suffix(profile: str, concurrency: int) -> str:
    return f"{profile}-{concurrency}"


def require_external_config(path_argument: str, repo: Path) -> Path:
    path = Path(path_argument)
    if not path.is_absolute() or not path.is_file():
        raise ConfigError(
            "mvl-baseline config must be an existing absolute path outside the repository"
        )
    resolved = path.resolve()
    for candidate in (path, resolved):
        try:
            candidate.relative_to(repo)
        except ValueError:
            continue
        raise ConfigError(
            "mvl-baseline config must be an existing absolute path outside the repository"
        )
    return resolved


def validate_authorized_mvl_config(config: dict) -> None:
    if not isinstance(config, dict):
        raise ConfigError("mvl-baseline config must be a JSON object")
    required = (
        "endpoint",
        "cloud",
        "model",
        "serving",
        "host",
        "model_verification",
        "canonical_commit",
        "cells",
    )
    for key in required:
        if key not in config:
            raise ConfigError(f"mvl-baseline config is missing required section: {key}")

    commit = config.get("canonical_commit")
    if not isinstance(commit, str) or len(commit) != 40:
        raise ConfigError("mvl-baseline config canonical_commit must be the 40-character SHA")

    verification = config["model_verification"]
    if not isinstance(verification, dict):
        raise ConfigError("mvl-baseline config is missing required section: model_verification")
    for key in ("artifact_dir", "digest_manifest"):
        if not verification.get(key):
            raise ConfigError(f"mvl-baseline config is missing model_verification.{key}")

    if config.get("comparison_mode") not in (None, FROZEN_COMPARISON_MODE):
        raise ConfigError("mvl-baseline comparison_mode must equal provider-native")

    cloud = config["cloud"]
    if not isinstance(cloud, dict):
        raise ConfigError("mvl-baseline config is missing required section: cloud")
    if cloud.get("instance_type") != FROZEN_INSTANCE_TYPE:
        raise ConfigError(f"mvl-baseline cloud.instance_type must equal {FROZEN_INSTANCE_TYPE}")
    if cloud.get("region") != FROZEN_REGION:
        raise ConfigError(f"mvl-baseline cloud.region must equal {FROZEN_REGION}")
    if cloud.get("list_price_usd_per_hour") not in (None, FROZEN_HOURLY_PRICE_USD):
        raise ConfigError("mvl-baseline cloud.list_price_usd_per_hour must equal 3.0")

    model = config["model"]
    if not isinstance(model, dict):
        raise ConfigError("mvl-baseline config is missing required section: model")
    if model.get("artifact") != FROZEN_MODEL_ARTIFACT:
        raise ConfigError("mvl-baseline model.artifact does not match the frozen identity")
    if model.get("revision") != FROZEN_MODEL_REVISION:
        raise ConfigError("mvl-baseline model.revision does not match the frozen identity")
    if model.get("artifact_hash") != FROZEN_MODEL_ARTIFACT_HASH:
        raise ConfigError("mvl-baseline model.artifact_hash does not match the frozen aggregate")
    if model.get("precision") != FROZEN_PRECISION:
        raise ConfigError("mvl-baseline model.precision must equal bf16")

    serving = config["serving"]
    if not isinstance(serving, dict):
        raise ConfigError("mvl-baseline config is missing required section: serving")
    if serving.get("engine") != FROZEN_ENGINE:
        raise ConfigError("mvl-baseline serving.engine must equal vllm")
    if serving.get("engine_version") != FROZEN_ENGINE_VERSION:
        raise ConfigError("mvl-baseline serving.engine_version must equal 0.27.1")
    digest = serving.get("container_digest") or serving.get("image_digest")
    if digest not in {FROZEN_CONTAINER_DIGEST, FROZEN_VLLM_IMAGE_DIGEST}:
        raise ConfigError("mvl-baseline serving.container_digest does not match the frozen image")

    if config.get("expected_gpu_model") != FROZEN_GPU:
        raise ConfigError(f"mvl-baseline expected_gpu_model must equal {FROZEN_GPU}")
    if config.get("warmup_passes") not in (None, FROZEN_WARMUP_PASSES):
        raise ConfigError("mvl-baseline warmup_passes must equal 1")
    if config.get("repetitions") not in (None, FROZEN_REPETITIONS):
        raise ConfigError("mvl-baseline repetitions must equal 3")
    if config.get("tasks_per_repetition") not in (None, FROZEN_TASKS_PER_REPETITION):
        raise ConfigError("mvl-baseline tasks_per_repetition must equal 200")
    if config.get("workload_version") not in (None, FROZEN_WORKLOAD_VERSION, WORKLOAD_VERSION):
        raise ConfigError("mvl-baseline workload_version must equal 2.3.0")
    if WORKLOAD_VERSION != FROZEN_WORKLOAD_VERSION:
        raise ConfigError("the running workload version is not the frozen 2.3.0 identity")

    generation = config.get("generation") or {}
    if generation:
        if generation.get("temperature") != FROZEN_TEMPERATURE:
            raise ConfigError("mvl-baseline generation.temperature must equal 1.0")
        if generation.get("top_p") != FROZEN_TOP_P:
            raise ConfigError("mvl-baseline generation.top_p must equal 0.95")
        if generation.get("reasoning_mode") is not True:
            raise ConfigError("mvl-baseline generation.reasoning_mode must be true")
        if generation.get("seed") not in (None, FROZEN_SEED):
            raise ConfigError("mvl-baseline generation.seed must equal the frozen seed")

    raw_cells = config.get("cells")
    if not isinstance(raw_cells, list):
        raise ConfigError("mvl-baseline config must declare exactly the frozen three-cell matrix")
    normalized: list[tuple[object, object]] = []
    for cell in raw_cells:
        if not isinstance(cell, dict):
            raise ConfigError(
                "mvl-baseline config must declare exactly the frozen three-cell matrix"
            )
        if cell.get("comparison_mode") not in (None, FROZEN_COMPARISON_MODE):
            raise ConfigError("mvl-baseline cells must be provider-native")
        normalized.append((cell.get("profile"), cell.get("concurrency")))
    if tuple(normalized) != AUTHORIZED_MVL_CELLS:
        raise ConfigError(
            "mvl-baseline cells must be exactly interactive/1, batch-heavy/4, "
            "and batch-heavy/8 with no missing, additional, or duplicate cells"
        )


def load_mvl_config(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("mvl-baseline config is not valid JSON") from exc
    if not isinstance(config, dict):
        raise ConfigError("mvl-baseline config must be a JSON object")
    validate_authorized_mvl_config(config)
    return config, digest


def require_clean_canonical_commit(config: dict, *, git_head: str, tree_clean: bool) -> str:
    expected = config["canonical_commit"]
    if git_head != expected:
        raise MvlError(
            "the working tree commit does not match the frozen canonical_commit "
            "in the approved config; nothing was executed"
        )
    if not tree_clean:
        raise MvlError(
            "the working tree is dirty; the MVL requires an exact canonical clean commit"
        )
    return git_head


def _field(outcome: object, name: str) -> object:
    if isinstance(outcome, dict):
        return outcome.get(name)
    execution = getattr(outcome, "execution", None)
    if execution is not None and hasattr(execution, name):
        return getattr(execution, name)
    if name == "template_id":
        if execution is not None and getattr(execution, "scenario_id", None):
            return execution.scenario_id
    return getattr(outcome, name, None)


def canary_structural_failures(outcomes: Iterable[object]) -> list[str]:
    failures: list[str] = []
    for outcome in outcomes:
        category = _field(outcome, "error_category")
        if category in STRUCTURAL_FAILURE_CATEGORIES:
            failures.append(str(category))
    return failures


def canary_used_native_tools(outcomes: Iterable[object]) -> bool:
    for outcome in outcomes:
        if _field(outcome, "tool_trace"):
            return True
    return False


def evaluate_canary(outcomes: Sequence[object]) -> None:
    expected = list(catalog())
    if len(outcomes) != FROZEN_CANARY_TASKS:
        raise MvlError(f"canary must cover all {FROZEN_CANARY_TASKS} scenarios exactly once")
    templates = [_field(outcome, "template_id") for outcome in outcomes]
    templates = [item for item in templates if item is not None]
    if len(set(templates)) != len(expected):
        raise MvlError("canary must cover all ten scenarios once")
    failures = canary_structural_failures(outcomes)
    if failures:
        raise MvlError("canary failed structurally: " + ", ".join(sorted(set(failures))))
    if not canary_used_native_tools(outcomes):
        raise MvlError("canary did not exercise native tool_calls and role=tool round trips")


def project_measured_seconds(canary_wall_s: float) -> float:
    if canary_wall_s <= 0:
        raise MvlError("canary projection requires a positive observed duration")
    mean_task_s = canary_wall_s * FROZEN_CANARY_CONCURRENCY / FROZEN_CANARY_TASKS
    measured_s = 0.0
    for _profile, concurrency in AUTHORIZED_MVL_CELLS:
        tasks = FROZEN_TASKS_PER_REPETITION * (FROZEN_REPETITIONS + FROZEN_WARMUP_PASSES)
        measured_s += tasks * mean_task_s / concurrency
    return measured_s


def remaining_session_seconds(
    events: Sequence[dict],
    *,
    now: datetime | None = None,
    ttl_hours: float = FROZEN_SESSION_HOURS,
) -> float:
    current = now or datetime.now(timezone.utc)
    provisioned = None
    for entry in events:
        if entry.get("event") == "provisioned":
            try:
                provisioned = datetime.fromisoformat(str(entry["at_utc"]))
            except (KeyError, TypeError, ValueError):
                provisioned = None
            break
    budget = ttl_hours * 3600.0
    if provisioned is None:
        return budget
    elapsed = (current - provisioned).total_seconds()
    return max(0.0, budget - elapsed)


def refuse_if_session_exceeded(projected_s: float, remaining_s: float) -> None:
    if projected_s <= remaining_s:
        return
    raise MvlError(
        "projected MVL duration exceeds the remaining six-hour session envelope; "
        "measured work will not start"
    )


def failure_record(message: str) -> dict:
    return {
        "failure_record": True,
        "is_valid_result": False,
        "workflow": "mvl-baseline",
        "diagnostic_only": True,
        "error_type": "MvlError",
        "note": message,
    }
