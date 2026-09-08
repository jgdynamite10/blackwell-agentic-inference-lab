"""Fail-closed Phase 3 full 12-cell Akamai baseline (decision D-0017).

This module is the only place the 12-cell matrix, canary gates, resume
ledger, and separately named full-baseline ceilings are defined. No
runtime flag or environment variable may expand the matrix, change pins,
or weaken approval. Live apply/destroy remain separately approved and
are never invoked from here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from blackwell_lab.cloud.artifacts import write_private_json
from blackwell_lab.cloud.bootstrap_pins import (
    APPROVED_REASONING_PARSER,
    APPROVED_TOOL_CALL_PARSER,
    APPROVED_VLLM_EXTRA_ARGS,
)
from blackwell_lab.cloud.controlled_resource import (
    JOINT_MEMORY_GIB,
    JOINT_VCPU_LIMIT,
    declared_resource_limits,
)
from blackwell_lab.workload.scenarios import WORKLOAD_VERSION, catalog
from blackwell_lab.workload.validation import ConfigError

# catalog() is the frozen ten-scenario identity used by the canary gate.

#: Implementation authorization (D-0017). Live execution still requires
#: the digest-bearing owner approval phrase at the CLI. Never a flag.
FULL_BASELINE_AUTHORIZED = True

FULL_BASELINE_APPROVAL_TEMPLATE = (
    "I approve the Phase 3 Akamai 12-cell baseline for run {run_tag} "
    "({run_label}) using config sha256:{config_sha256}"
)

# Frozen identity (D-0017). Aggregate hash is the SHA-256 of the sorted
# sha256sum-format per-file manifest lines (telemetry.verify_model_artifact).
# The complete per-file list remains host-resident at MODEL_DIGEST_MANIFEST
# and is re-verified live; it is never committed.
FROZEN_PROVIDER = "akamai"
FROZEN_REGION = "us-sea"
FROZEN_INSTANCE_TYPE = "g3-gpu-rtxpro6000-blackwell-1"
FROZEN_GPU = "RTX PRO 6000 Blackwell"
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
FROZEN_VLLM_IMAGE_INDEX_DIGEST = (
    "sha256:0a51ea5b4ae2dc5d81890e5173f54203d2a3ae0cfffe51b8fd2afd4391bfd967"
)
FROZEN_CONTAINER_DIGEST = f"{FROZEN_VLLM_IMAGE}@{FROZEN_VLLM_IMAGE_DIGEST}"
FROZEN_TEMPERATURE = 1.0
FROZEN_TOP_P = 0.95
FROZEN_MAX_TOKENS = 1024
FROZEN_SEED = 20260906
FROZEN_REASONING_MODE = True
FROZEN_WORKLOAD_VERSION = "2.3.0"
FROZEN_WARMUP_PASSES = 1
FROZEN_REPETITIONS = 5
FROZEN_TASKS_PER_REPETITION = 200
FROZEN_CANARY_TASKS = 10
FROZEN_CANARY_CONTROLLED_CONCURRENCY = 8
FROZEN_HOURLY_PRICE_USD = 3.0

#: Deterministic execution order used for Akamai and later AWS/GCP replicas.
COMPARISON_MODE_ORDER = ("controlled-resource", "provider-native")
PROFILE_ORDER = ("interactive", "batch-heavy")
CONCURRENCY_ORDER = (1, 4, 8)

AUTHORIZED_FULL_BASELINE_CELLS: tuple[tuple[str, str, int], ...] = tuple(
    (mode, profile, concurrency)
    for mode in COMPARISON_MODE_ORDER
    for profile in PROFILE_ORDER
    for concurrency in CONCURRENCY_ORDER
)

# Separately named from the D-0014 six-hour / $25 pilot envelope.
FULL_BASELINE_MEASURED_GPU_HOURS_MAX = 230.0
FULL_BASELINE_TOTAL_LIVE_HOURS_MAX = 240.0
FULL_BASELINE_NORMALIZED_COST_USD_MAX = 720.0
PILOT_TTL_HOURS = 6
PILOT_COST_USD_MAX = 25.0

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

PROGRESS_SCHEMA_VERSION = "1.0.0"
GitHead = Callable[[], str]
TreeClean = Callable[[], bool]


class BaselineError(RuntimeError):
    """Fail-closed baseline workflow error (sanitized; no private paths)."""


@dataclass(frozen=True)
class BaselineCell:
    comparison_mode: str
    profile: str
    concurrency: int

    @property
    def key(self) -> str:
        return f"{self.comparison_mode}:{self.profile}:{self.concurrency}"

    @property
    def run_label_suffix(self) -> str:
        mode = "cr" if self.comparison_mode == "controlled-resource" else "pn"
        return f"{mode}-{self.profile}-{self.concurrency}"


def full_baseline_cells() -> tuple[BaselineCell, ...]:
    return tuple(
        BaselineCell(mode, profile, concurrency)
        for mode, profile, concurrency in AUTHORIZED_FULL_BASELINE_CELLS
    )


def cell_execution_order() -> list[dict]:
    return [
        {
            "index": index,
            "comparison_mode": cell.comparison_mode,
            "profile": cell.profile,
            "concurrency": cell.concurrency,
            "key": cell.key,
        }
        for index, cell in enumerate(full_baseline_cells(), start=1)
    ]


def measured_counts() -> dict:
    cells = full_baseline_cells()
    return {
        "cells": len(cells),
        "measured_repetitions": len(cells) * FROZEN_REPETITIONS,
        "measured_task_observations": (
            len(cells) * FROZEN_REPETITIONS * FROZEN_TASKS_PER_REPETITION
        ),
    }


def require_external_config(path_argument: str, repo: Path) -> Path:
    path = Path(path_argument)
    if not path.is_absolute() or not path.is_file():
        raise ConfigError(
            "full-baseline config must be an existing absolute path outside the repository"
        )
    resolved = path.resolve()
    try:
        path.relative_to(repo)
        raise ConfigError(
            "full-baseline config must be an existing absolute path outside the repository"
        )
    except ValueError:
        pass
    try:
        resolved.relative_to(repo)
        raise ConfigError(
            "full-baseline config must be an existing absolute path outside the repository"
        )
    except ValueError:
        return resolved


def validate_authorized_full_baseline_config(config: dict) -> None:
    """Rejects any config that is not the locked D-0017 12-cell envelope."""
    if not isinstance(config, dict):
        raise ConfigError("full-baseline config must be a JSON object")
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
            raise ConfigError(f"full-baseline config is missing required section: {key}")

    commit = config.get("canonical_commit")
    if not isinstance(commit, str) or len(commit) != 40:
        raise ConfigError("full-baseline config canonical_commit must be the 40-character SHA")

    verification = config["model_verification"]
    if not isinstance(verification, dict):
        raise ConfigError("full-baseline config is missing required section: model_verification")
    for key in ("artifact_dir", "digest_manifest"):
        if not verification.get(key):
            raise ConfigError(f"full-baseline config is missing model_verification.{key}")

    cloud = config["cloud"]
    if not isinstance(cloud, dict):
        raise ConfigError("full-baseline config is missing required section: cloud")
    if cloud.get("instance_type") != FROZEN_INSTANCE_TYPE:
        raise ConfigError(f"full-baseline cloud.instance_type must equal {FROZEN_INSTANCE_TYPE}")
    if cloud.get("region") != FROZEN_REGION:
        raise ConfigError(f"full-baseline cloud.region must equal {FROZEN_REGION}")
    if cloud.get("list_price_usd_per_hour") != FROZEN_HOURLY_PRICE_USD:
        raise ConfigError("full-baseline cloud.list_price_usd_per_hour must equal 3.0")

    model = config["model"]
    if not isinstance(model, dict):
        raise ConfigError("full-baseline config is missing required section: model")
    if model.get("artifact") != FROZEN_MODEL_ARTIFACT:
        raise ConfigError("full-baseline model.artifact does not match the frozen identity")
    if model.get("revision") != FROZEN_MODEL_REVISION:
        raise ConfigError("full-baseline model.revision does not match the frozen identity")
    if model.get("artifact_hash") != FROZEN_MODEL_ARTIFACT_HASH:
        raise ConfigError("full-baseline model.artifact_hash does not match the frozen aggregate")
    if model.get("precision") != FROZEN_PRECISION:
        raise ConfigError("full-baseline model.precision must equal bf16")

    serving = config["serving"]
    if not isinstance(serving, dict):
        raise ConfigError("full-baseline config is missing required section: serving")
    if serving.get("engine") != FROZEN_ENGINE:
        raise ConfigError("full-baseline serving.engine must equal vllm")
    if serving.get("engine_version") != FROZEN_ENGINE_VERSION:
        raise ConfigError("full-baseline serving.engine_version must equal 0.27.1")
    digest = serving.get("container_digest") or serving.get("image_digest")
    if digest not in {FROZEN_CONTAINER_DIGEST, FROZEN_VLLM_IMAGE_DIGEST}:
        if serving.get("container_digest") != FROZEN_CONTAINER_DIGEST:
            raise ConfigError(
                "full-baseline serving.container_digest does not match the frozen image"
            )

    if config.get("expected_gpu_model") != FROZEN_GPU:
        raise ConfigError(f"full-baseline expected_gpu_model must equal {FROZEN_GPU}")
    if config.get("warmup_passes") != FROZEN_WARMUP_PASSES:
        raise ConfigError("full-baseline warmup_passes must equal 1")
    if config.get("repetitions") != FROZEN_REPETITIONS:
        raise ConfigError("full-baseline repetitions must equal 5")
    if config.get("tasks_per_repetition") != FROZEN_TASKS_PER_REPETITION:
        raise ConfigError("full-baseline tasks_per_repetition must equal 200")
    if config.get("workload_version") not in (None, FROZEN_WORKLOAD_VERSION, WORKLOAD_VERSION):
        raise ConfigError("full-baseline workload_version must equal 2.3.0")
    if WORKLOAD_VERSION != FROZEN_WORKLOAD_VERSION:
        raise ConfigError("the running workload version is not the frozen 2.3.0 identity")

    generation = config.get("generation") or {}
    if generation:
        if generation.get("temperature") != FROZEN_TEMPERATURE:
            raise ConfigError("full-baseline generation.temperature must equal 1.0")
        if generation.get("top_p") != FROZEN_TOP_P:
            raise ConfigError("full-baseline generation.top_p must equal 0.95")
        if generation.get("max_tokens") != FROZEN_MAX_TOKENS:
            raise ConfigError("full-baseline generation.max_tokens must equal 1024")
        if generation.get("reasoning_mode") is not True:
            raise ConfigError("full-baseline generation.reasoning_mode must be true")
        if generation.get("seed") not in (None, FROZEN_SEED):
            raise ConfigError("full-baseline generation.seed must equal the frozen seed")

    raw_cells = config.get("cells")
    if not isinstance(raw_cells, list):
        raise ConfigError("full-baseline config must declare exactly the frozen 12-cell matrix")
    normalized: list[tuple[object, object, object]] = []
    for cell in raw_cells:
        if not isinstance(cell, dict):
            raise ConfigError("full-baseline config must declare exactly the frozen 12-cell matrix")
        normalized.append(
            (cell.get("comparison_mode"), cell.get("profile"), cell.get("concurrency"))
        )
    if tuple(normalized) != AUTHORIZED_FULL_BASELINE_CELLS:
        raise ConfigError(
            "full-baseline cells must be the exact frozen 12-cell order "
            "(controlled-resource then provider-native; interactive then "
            "batch-heavy; concurrency 1, 4, 8) with no missing, additional, "
            "or duplicate cells"
        )


def load_full_baseline_config(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("full-baseline config is not valid JSON") from exc
    if not isinstance(config, dict):
        raise ConfigError("full-baseline config must be a JSON object")
    validate_authorized_full_baseline_config(config)
    return config, digest


def require_clean_canonical_commit(
    config: dict,
    *,
    git_head: GitHead,
    tree_clean: TreeClean,
) -> str:
    head = git_head()
    expected = config["canonical_commit"]
    if head != expected:
        raise BaselineError(
            "the working tree commit does not match the frozen canonical_commit "
            "in the approved config; nothing was executed"
        )
    if not tree_clean():
        raise BaselineError(
            "the working tree is dirty; the full baseline requires an exact canonical clean commit"
        )
    return head


def _outcome_field(outcome: object, name: str) -> object:
    if isinstance(outcome, dict):
        return outcome.get(name)
    execution = getattr(outcome, "execution", None)
    if execution is not None and hasattr(execution, name):
        return getattr(execution, name)
    instance = getattr(outcome, "instance", None)
    if instance is not None and hasattr(instance, name):
        return getattr(instance, name)
    return getattr(outcome, name, None)


def canary_structural_failures(outcomes: Iterable[object]) -> list[str]:
    """Return sanitized structural-failure categories. Quality is ignored."""
    failures: list[str] = []
    for outcome in outcomes:
        category = _outcome_field(outcome, "error_category")
        if category in STRUCTURAL_FAILURE_CATEGORIES:
            failures.append(str(category))
    return failures


def canary_used_native_tools(outcomes: Iterable[object]) -> bool:
    """True when at least one task produced a native tool round-trip."""
    for outcome in outcomes:
        if _outcome_field(outcome, "tool_trace"):
            return True
    return False


def evaluate_canary(
    outcomes: Sequence[object], *, scenario_ids: Sequence[str] | None = None
) -> None:
    expected_ids = list(scenario_ids) if scenario_ids is not None else list(catalog())
    if len(outcomes) != FROZEN_CANARY_TASKS:
        raise BaselineError(f"canary must cover all {FROZEN_CANARY_TASKS} scenarios exactly once")
    templates = []
    for outcome in outcomes:
        template = _outcome_field(outcome, "template_id")
        if template is not None:
            templates.append(template)
    if len(set(templates)) != len(expected_ids):
        raise BaselineError("canary must cover all ten scenarios once")
    failures = canary_structural_failures(outcomes)
    if failures:
        raise BaselineError("canary failed structurally: " + ", ".join(sorted(set(failures))))
    if not canary_used_native_tools(outcomes):
        raise BaselineError("canary did not exercise native tool_calls and role=tool round trips")


def project_duration_hours(
    *,
    canary_wall_s: float,
    canary_tasks: int,
    canary_concurrency: int,
    remaining_cells: Sequence[BaselineCell],
    setup_elapsed_s: float = 0.0,
    teardown_allowance_s: float = 2 * 3600.0,
) -> dict:
    """Sanitized projection used as a stop-gate, not a performance claim."""
    if canary_wall_s <= 0 or canary_tasks <= 0 or canary_concurrency <= 0:
        raise BaselineError("canary projection requires a positive observed duration")
    mean_task_s = canary_wall_s * canary_concurrency / canary_tasks
    measured_s = 0.0
    for cell in remaining_cells:
        tasks = FROZEN_TASKS_PER_REPETITION * (FROZEN_REPETITIONS + FROZEN_WARMUP_PASSES)
        measured_s += tasks * mean_task_s / cell.concurrency
    measured_h = measured_s / 3600.0
    total_h = (setup_elapsed_s + measured_s + teardown_allowance_s) / 3600.0
    cost = total_h * FROZEN_HOURLY_PRICE_USD
    return {
        "projected_measured_gpu_hours": round(measured_h, 3),
        "projected_total_live_hours": round(total_h, 3),
        "projected_normalized_cost_usd": round(cost, 2),
        "hourly_price_usd": FROZEN_HOURLY_PRICE_USD,
        "ceiling_measured_gpu_hours": FULL_BASELINE_MEASURED_GPU_HOURS_MAX,
        "ceiling_total_live_hours": FULL_BASELINE_TOTAL_LIVE_HOURS_MAX,
        "ceiling_normalized_cost_usd": FULL_BASELINE_NORMALIZED_COST_USD_MAX,
        "within_ceiling": (
            measured_h <= FULL_BASELINE_MEASURED_GPU_HOURS_MAX
            and total_h <= FULL_BASELINE_TOTAL_LIVE_HOURS_MAX
            and cost <= FULL_BASELINE_NORMALIZED_COST_USD_MAX
        ),
    }


def refuse_if_ceiling_exceeded(projection: dict) -> None:
    if projection.get("within_ceiling"):
        return
    raise BaselineError(
        "projected full-baseline duration exceeds the stated D-0017 ceiling; "
        "measured work will not start; a new owner decision is required"
    )


def progress_identity(
    *,
    run_tag: str,
    canonical_commit: str,
    config_sha256: str,
    model_artifact_hash: str,
    container_digest: str,
) -> dict:
    return {
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "kind": "full-baseline-progress",
        "run_tag": run_tag,
        "canonical_commit": canonical_commit,
        "config_sha256": config_sha256,
        "model_revision": FROZEN_MODEL_REVISION,
        "model_artifact_hash": model_artifact_hash,
        "container_digest": container_digest,
        "workload_version": FROZEN_WORKLOAD_VERSION,
        "vllm_extra_args": APPROVED_VLLM_EXTRA_ARGS,
        "reasoning_parser": APPROVED_REASONING_PARSER,
        "tool_call_parser": APPROVED_TOOL_CALL_PARSER,
        "matrix": cell_execution_order(),
        "counts": measured_counts(),
        "joint_envelope": declared_resource_limits()
        | {"vcpu_limit": JOINT_VCPU_LIMIT, "memory_limit_gib": JOINT_MEMORY_GIB},
    }


def empty_progress(identity: dict) -> dict:
    return {**identity, "completed": {}}


def load_progress(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError("the progress ledger is unreadable or corrupt") from exc
    if not isinstance(loaded, dict):
        raise BaselineError("the progress ledger is not a JSON object")
    return loaded


def write_progress(path: Path, document: dict) -> str:
    return write_private_json(path, document)


def assert_progress_identity(existing: dict, expected: dict) -> None:
    keys = (
        "run_tag",
        "canonical_commit",
        "config_sha256",
        "model_revision",
        "model_artifact_hash",
        "container_digest",
        "workload_version",
        "matrix",
        "counts",
    )
    for key in keys:
        if existing.get(key) != expected.get(key):
            raise BaselineError(
                f"progress ledger {key} does not match the frozen identity; resume is refused"
            )


def repetition_key(cell: BaselineCell, repetition_index: int) -> str:
    return f"cell:{cell.key}:rep:{repetition_index}"


def canary_key(mode: str) -> str:
    return f"canary:{mode}"


def refuse_overwrite(path: Path) -> None:
    if path.exists():
        raise BaselineError(
            "refusing to overwrite an existing genuine artifact; resume only "
            "skips fully verified repetitions"
        )


def artifact_triplet_ok(
    directory: Path,
    run_id: str,
    *,
    expected_sha256: dict[str, str],
    sha256_file: Callable[[Path], str],
) -> bool:
    """True only when manifest, result, and observations exist and match."""
    names = {
        "manifest": f"{run_id}.manifest.json",
        "result": f"{run_id}.result.json",
        "observations": f"{run_id}.observations.json",
    }
    for kind, name in names.items():
        path = directory / name
        if not path.is_file():
            return False
        digest = sha256_file(path)
        recorded = expected_sha256.get(kind)
        if recorded is None or digest != recorded:
            return False
    return True


@dataclass(frozen=True)
class FullBaselineDeps:
    """Injectable collaborators so the workflow is fully offline-testable."""

    git_head: GitHead
    tree_clean: TreeClean
    verify_live_provenance: Callable[..., object]
    observe_and_verify_mode: Callable[[str], dict]
    transition_serving: Callable[[str], None]
    run_real_cell: Callable[..., list]
    prepare_teardown_plan: Callable[[], dict]
    record_session_event: Callable[..., None]
    monotonic: Callable[[], float]
    sha256_file: Callable[[Path], str]
    client_factory: Callable[..., object]
    write_failure_receipt: Callable[[Path, dict], str]


def _cell_dir(results_dir: Path, run_label: str, suffix: str) -> Path:
    return results_dir / "real-runs" / f"{run_label}-{suffix}"


def execute_full_baseline(
    *,
    run_tag: str,
    run_label: str,
    config: dict,
    config_sha256: str,
    results_dir: Path,
    progress_path: Path,
    deps: FullBaselineDeps,
    measured_cells: Sequence[BaselineCell] | None = None,
) -> dict:
    """Run canaries then the 12-cell matrix. Never starts destroy."""
    if not FULL_BASELINE_AUTHORIZED:
        raise BaselineError("the full baseline is not authorized")
    commit = require_clean_canonical_commit(
        config, git_head=deps.git_head, tree_clean=deps.tree_clean
    )
    identity = progress_identity(
        run_tag=run_tag,
        canonical_commit=commit,
        config_sha256=config_sha256,
        model_artifact_hash=config["model"]["artifact_hash"],
        container_digest=config["serving"].get("container_digest", FROZEN_CONTAINER_DIGEST),
    )
    existing = load_progress(progress_path)
    if existing is None:
        progress = empty_progress(identity)
        write_progress(progress_path, progress)
    else:
        assert_progress_identity(existing, identity)
        progress = existing
    completed: dict = dict(progress.get("completed") or {})
    cells = list(measured_cells) if measured_cells is not None else list(full_baseline_cells())
    deps.record_session_event("baseline_started", {"config_sha256": config_sha256})

    def _fail(message: str, *, mode: str | None = None) -> None:
        receipt = {
            "failure_record": True,
            "is_valid_result": False,
            "workflow": "full-baseline",
            "diagnostic_only": True,
            "comparison_mode": mode,
            "error_type": "BaselineError",
            "note": message,
        }
        deps.write_failure_receipt(results_dir / "real-runs" / f"{run_label}-failure.json", receipt)
        deps.prepare_teardown_plan()
        deps.record_session_event("baseline_failed", {"reason": message})
        raise BaselineError(message)

    summaries: list[dict] = []
    measured_started = False
    for mode in COMPARISON_MODE_ORDER:
        mode_cells = [cell for cell in cells if cell.comparison_mode == mode]
        if not mode_cells:
            continue
        deps.transition_serving(mode)
        deps.verify_live_provenance(mode)
        enforcement = deps.observe_and_verify_mode(mode)
        canary = canary_key(mode)
        if canary in completed:
            recorded = completed[canary]
            if not recorded.get("verified"):
                _fail(
                    "a prior canary is recorded as unverified; measured work will not start",
                    mode=mode,
                )
        else:
            started = deps.monotonic()
            try:
                records = deps.run_real_cell(
                    {
                        "kind": "canary",
                        "comparison_mode": mode,
                        "concurrency": (
                            FROZEN_CANARY_CONTROLLED_CONCURRENCY
                            if mode == "controlled-resource"
                            else 1
                        ),
                        "tasks_per_repetition": FROZEN_CANARY_TASKS,
                        "warmup_passes": 0,
                        "repetitions": 1,
                        "diagnostic_only": True,
                        "observation_phase": "canary",
                        "resource_enforcement": enforcement,
                        "run_label": f"{run_label}-canary-{mode}",
                    }
                )
                wall_s = deps.monotonic() - started
                raw: list[object] = []
                observations: list[object] = []
                for record in records:
                    raw.extend(getattr(record, "outcomes", ()) or ())
                    measured = getattr(record, "measured_observations", None) or {}
                    if isinstance(measured, dict):
                        observations.extend(measured.get("observations") or [])
                evaluate_canary(raw or observations)
            except Exception as exc:
                message = (
                    str(exc)
                    if isinstance(exc, (BaselineError, ConfigError))
                    else f"canary aborted: {type(exc).__name__}"
                )
                _fail(message, mode=mode)
            remaining = [cell for cell in cells if cell.comparison_mode == mode]
            try:
                projection = project_duration_hours(
                    canary_wall_s=max(wall_s, 0.001),
                    canary_tasks=FROZEN_CANARY_TASKS,
                    canary_concurrency=(
                        FROZEN_CANARY_CONTROLLED_CONCURRENCY if mode == "controlled-resource" else 1
                    ),
                    remaining_cells=remaining,
                )
                refuse_if_ceiling_exceeded(projection)
            except BaselineError as exc:
                _fail(str(exc), mode=mode)
            completed[canary] = {
                "verified": True,
                "diagnostic_only": True,
                "projection": projection,
            }
            progress = {**identity, "completed": completed}
            write_progress(progress_path, progress)
            summaries.append(
                {
                    "kind": "canary",
                    "comparison_mode": mode,
                    "diagnostic_only": True,
                    "projection": projection,
                }
            )

        for cell in mode_cells:
            for repetition in range(1, FROZEN_REPETITIONS + 1):
                key = repetition_key(cell, repetition)
                if key in completed:
                    recorded = completed[key]
                    label_dir = _cell_dir(results_dir, run_label, cell.run_label_suffix)
                    if not artifact_triplet_ok(
                        label_dir,
                        recorded.get("run_id", ""),
                        expected_sha256=recorded.get("sha256") or {},
                        sha256_file=deps.sha256_file,
                    ):
                        raise BaselineError(
                            "resume rejected a partial, corrupt, mismatched, or foreign repetition"
                        )
                    continue
                deps.verify_live_provenance(mode)
                enforcement = deps.observe_and_verify_mode(mode)
                measured_started = True
                records = deps.run_real_cell(
                    {
                        "kind": "measured",
                        "comparison_mode": cell.comparison_mode,
                        "profile": cell.profile,
                        "concurrency": cell.concurrency,
                        "tasks_per_repetition": FROZEN_TASKS_PER_REPETITION,
                        "warmup_passes": FROZEN_WARMUP_PASSES if repetition == 1 else 0,
                        "repetitions": 1,
                        "repetition_index": repetition,
                        "diagnostic_only": False,
                        "observation_phase": "measured",
                        "resource_enforcement": enforcement,
                        "resource_limits": (
                            declared_resource_limits()
                            if cell.comparison_mode == "controlled-resource"
                            else None
                        ),
                        "run_label": f"{run_label}-{cell.run_label_suffix}",
                    }
                )
                record = records[0]
                label_dir = _cell_dir(results_dir, run_label, cell.run_label_suffix)
                digests = getattr(record, "digests", None)
                if not isinstance(digests, dict):
                    digests = {}
                    for kind, suffix in (
                        ("manifest", ".manifest.json"),
                        ("result", ".result.json"),
                        ("observations", ".observations.json"),
                    ):
                        path = label_dir / f"{record.run_id}{suffix}"
                        if path.is_file():
                            digests[kind] = deps.sha256_file(path)
                completed[key] = {
                    "verified": True,
                    "run_id": record.run_id,
                    "sha256": digests,
                    "files": list(record.written_files),
                }
                progress = {**identity, "completed": completed}
                write_progress(progress_path, progress)
                summaries.append(
                    {
                        "kind": "measured",
                        "key": cell.key,
                        "repetition": repetition,
                        "run_id": record.run_id,
                    }
                )

    deps.record_session_event("baseline_completed", {"cells": len(cells)})
    teardown = deps.prepare_teardown_plan()
    return {
        "workflow": "full-baseline",
        "run_label": run_label,
        "cells": summaries,
        "measured_started": measured_started,
        "teardown_plan_sha256": teardown.get("plan_sha256"),
        "note": (
            "Canary observations are diagnostic only and are excluded from "
            "baseline summaries. A fresh teardown plan was prepared; destroy "
            "requires the exact owner approval phrase. The watchdog is a "
            "workload safeguard and does not stop Akamai billing."
        ),
    }
