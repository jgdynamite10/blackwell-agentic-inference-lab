"""Bounded agent-quality qualification (decision D-0019).

Reuses the existing pilot/MVL runner, provenance, external-results, and
verification paths. This is not a new orchestration framework and it
never reuses or overwrites MVL-F identities or paths.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blackwell_lab.cloud.mvl import (
    FROZEN_COMPARISON_MODE,
    FROZEN_CONTAINER_DIGEST,
    FROZEN_ENGINE,
    FROZEN_ENGINE_VERSION,
    FROZEN_GPU,
    FROZEN_HOURLY_PRICE_USD,
    FROZEN_INSTANCE_TYPE,
    FROZEN_MODEL_ARTIFACT,
    FROZEN_MODEL_ARTIFACT_HASH,
    FROZEN_MODEL_REVISION,
    FROZEN_PRECISION,
    FROZEN_PROVIDER,
    FROZEN_REASONING_MODE,
    FROZEN_REGION,
    FROZEN_SEED,
    FROZEN_TOP_P,
    FROZEN_VLLM_IMAGE_DIGEST,
)
from blackwell_lab.workload.native_tools import (
    REASONING_PARSER,
    TOOL_CALL_PARSER,
    TOOL_CALL_TRANSPORT,
    TOOL_DESCRIPTIONS,
)
from blackwell_lab.workload.scenarios import WORKLOAD_VERSION, catalog
from blackwell_lab.workload.stats import percentile_nearest_rank
from blackwell_lab.workload.validation import ConfigError

QUALIFICATION_APPROVAL_TEMPLATE = (
    "I approve the Akamai agent qualification for run {run_tag} "
    "({run_label}) using candidate {candidate_id} config sha256:{config_sha256}"
)

QUALIFICATION_WORKLOAD_VERSION = "2.4.0"
CATALOG_WORKLOAD_VERSION = "2.3.0"
QUALIFICATION_WORKFLOW = "qualify-agent"
QUALIFICATION_ARTIFACT_FAMILY = "qualification-runs"
QUALIFICATION_LABEL = "agent-quality-qualification"

SPLIT_SEED = "blackwell-lab-agent-qualification-v1"
SPLIT_RULE = "sha256-hex-sort"

CANDIDATE_C1 = "C1"
CANDIDATE_C2 = "C2"
AUTHORIZED_CANDIDATES = (CANDIDATE_C1, CANDIDATE_C2)
C1_TEMPERATURE = 1.0
C2_TEMPERATURE = 0.2
FROZEN_MAX_TOKENS = 1024

STAGE_DEVELOPMENT = "development"
STAGE_HOLDOUT = "holdout"
STAGE_FREEZE = "freeze"
AUTHORIZED_STAGES = (STAGE_DEVELOPMENT, STAGE_HOLDOUT, STAGE_FREEZE)

DEVELOPMENT_TASKS = 20
HOLDOUT_TASKS = 20
FREEZE_TASKS = 200
SCREEN_WARMUP_PASSES = 0
FREEZE_WARMUP_PASSES = 1
STAGE_REPETITIONS = 1
STAGE_PROFILE = "interactive"
STAGE_CONCURRENCY = 1

DEVELOPMENT_QUALITY_FLOOR = 0.40
HOLDOUT_QUALITY_FLOOR = 0.50

STUDY_ENTRY_MIN_AGGREGATE = 0.70
STUDY_ENTRY_MIN_SCENARIO = 0.40
PRODUCTION_LIKE_MIN_AGGREGATE = 0.90
PRODUCTION_LIKE_MIN_SCENARIO = 0.80
MIN_VALID_NATIVE_TOOL_CALL_RATE = 0.99
MAX_INVALID_TOOL_NAME_RATE = 0.0
MAX_INVALID_ARGUMENT_RATE = 0.01
MAX_REQUEST_INFERENCE_ERROR_RATE = 0.01
MAX_TIMEOUTS = 0
MAX_INTERACTIVE_TTFT_P95_MS = 2_500.0
MAX_INTERACTIVE_E2E_P95_MS = 60_000.0

STRUCTURAL_TOOL_FAILURES = frozenset(
    {"malformed_tool_call", "invalid_tool_name", "invalid_tool_arguments"}
)
REQUEST_INFERENCE_ERRORS = frozenset({"endpoint_error"})
TIMEOUT_CATEGORIES = frozenset({"task_timeout"})

FORBIDDEN_IDENTITY_MARKERS = ("mvl-f", "mvlf", "mvl-baseline", "p3-mvl", "mvl")
FORBIDDEN_PATH_MARKERS = ("mvl-f", "mvlf", "mvl-baseline", "real-runs")

_RUN_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

HOLDOUT_MUST_NOT_REVISE_WORDING = "Holdout results must never be used to revise candidate wording."


class QualificationError(RuntimeError):
    """Fail-closed qualification error (sanitized; no private paths)."""


def _catalog_template_ids() -> tuple[str, ...]:
    return tuple(catalog())


def freeze_template_split(
    template_ids: Sequence[str] | None = None,
    *,
    seed: str = SPLIT_SEED,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Deterministic 6/4 split: sha256(seed:id) hex-sort, then template id."""
    ids = tuple(template_ids) if template_ids is not None else _catalog_template_ids()
    scored = sorted(
        (hashlib.sha256(f"{seed}:{template_id}".encode()).hexdigest(), template_id)
        for template_id in ids
    )
    development = tuple(template_id for _digest, template_id in scored[:6])
    holdout = tuple(template_id for _digest, template_id in scored[6:])
    return development, holdout


DEVELOPMENT_TEMPLATE_IDS, HOLDOUT_TEMPLATE_IDS = freeze_template_split()


def require_frozen_split() -> None:
    development, holdout = freeze_template_split()
    if development != DEVELOPMENT_TEMPLATE_IDS or holdout != HOLDOUT_TEMPLATE_IDS:
        raise QualificationError("the frozen 6/4 template split drifted")
    if set(development) & set(holdout):
        raise QualificationError("development and holdout templates must be disjoint")
    if set(development).union(holdout) != set(_catalog_template_ids()):
        raise QualificationError("the frozen split must cover every catalog template")
    if len(development) != 6 or len(holdout) != 4:
        raise QualificationError(
            "the frozen split must be exactly six development and four holdout"
        )


def stage_spec(stage: str) -> dict[str, Any]:
    if stage == STAGE_DEVELOPMENT:
        return {
            "stage": stage,
            "template_ids": DEVELOPMENT_TEMPLATE_IDS,
            "tasks": DEVELOPMENT_TASKS,
            "warmup_passes": SCREEN_WARMUP_PASSES,
            "repetitions": STAGE_REPETITIONS,
            "profile": STAGE_PROFILE,
            "concurrency": STAGE_CONCURRENCY,
            "quality_floor": DEVELOPMENT_QUALITY_FLOOR,
            "apply_study_entry_gate": False,
        }
    if stage == STAGE_HOLDOUT:
        return {
            "stage": stage,
            "template_ids": HOLDOUT_TEMPLATE_IDS,
            "tasks": HOLDOUT_TASKS,
            "warmup_passes": SCREEN_WARMUP_PASSES,
            "repetitions": STAGE_REPETITIONS,
            "profile": STAGE_PROFILE,
            "concurrency": STAGE_CONCURRENCY,
            "quality_floor": HOLDOUT_QUALITY_FLOOR,
            "apply_study_entry_gate": False,
        }
    if stage == STAGE_FREEZE:
        return {
            "stage": stage,
            "template_ids": tuple(_catalog_template_ids()),
            "tasks": FREEZE_TASKS,
            "warmup_passes": FREEZE_WARMUP_PASSES,
            "repetitions": STAGE_REPETITIONS,
            "profile": STAGE_PROFILE,
            "concurrency": STAGE_CONCURRENCY,
            "quality_floor": None,
            "apply_study_entry_gate": True,
        }
    raise ConfigError("qualification stage must be development, holdout, or freeze")


def validate_stage_request(stage: str, *, template_ids: Sequence[str], tasks: int) -> None:
    spec = stage_spec(stage)
    if tuple(template_ids) != spec["template_ids"]:
        raise ConfigError(f"qualification {stage} templates must equal the frozen split")
    if tasks != spec["tasks"]:
        raise ConfigError(f"qualification {stage} must use exactly {spec['tasks']} tasks")


def candidate_temperature(candidate_id: str) -> float:
    if candidate_id == CANDIDATE_C1:
        return C1_TEMPERATURE
    if candidate_id == CANDIDATE_C2:
        return C2_TEMPERATURE
    raise ConfigError("qualification candidate must be C1 or C2")


def frozen_candidate_fields(candidate_id: str) -> dict[str, Any]:
    if candidate_id not in AUTHORIZED_CANDIDATES:
        raise ConfigError("qualification candidate must be C1 or C2")
    return {
        "candidate_id": candidate_id,
        "workflow": QUALIFICATION_WORKFLOW,
        "workload_version": QUALIFICATION_WORKLOAD_VERSION,
        "catalog_workload_version": CATALOG_WORKLOAD_VERSION,
        "provider": FROZEN_PROVIDER,
        "region": FROZEN_REGION,
        "instance_type": FROZEN_INSTANCE_TYPE,
        "gpu": FROZEN_GPU,
        "comparison_mode": FROZEN_COMPARISON_MODE,
        "model": {
            "artifact": FROZEN_MODEL_ARTIFACT,
            "revision": FROZEN_MODEL_REVISION,
            "artifact_hash": FROZEN_MODEL_ARTIFACT_HASH,
            "precision": FROZEN_PRECISION,
        },
        "serving": {
            "engine": FROZEN_ENGINE,
            "engine_version": FROZEN_ENGINE_VERSION,
            "container_digest": FROZEN_CONTAINER_DIGEST,
            "tool_call_transport": TOOL_CALL_TRANSPORT,
            "tool_call_parser": TOOL_CALL_PARSER,
            "reasoning_parser": REASONING_PARSER,
        },
        "generation": {
            "temperature": candidate_temperature(candidate_id),
            "top_p": FROZEN_TOP_P,
            "max_tokens": FROZEN_MAX_TOKENS,
            "seed": FROZEN_SEED,
            "reasoning_mode": FROZEN_REASONING_MODE,
        },
    }


def serialize_candidate(candidate_id: str) -> bytes:
    return json.dumps(
        frozen_candidate_fields(candidate_id),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def candidate_identity_digest(candidate_id: str) -> str:
    return hashlib.sha256(serialize_candidate(candidate_id)).hexdigest()


def require_safe_run_label(run_label: str) -> str:
    if not _RUN_LABEL_RE.match(run_label):
        raise ConfigError("run_label must be short lowercase letters/digits/hyphens")
    return run_label


def output_label(run_label: str, stage: str, candidate_id: str) -> str:
    label = f"{run_label}-{candidate_id.lower()}-{stage}"
    if not _RUN_LABEL_RE.match(label):
        raise ConfigError("composed qualification output label is unsafe")
    refuse_mvl_identities(
        run_tag="", run_label=label, config={}, artifact_family=QUALIFICATION_ARTIFACT_FAMILY
    )
    return label


def _identity_blob(*parts: object) -> str:
    pieces: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            pieces.append(json.dumps(part, sort_keys=True, default=str))
        else:
            pieces.append(str(part))
    return " ".join(pieces).casefold()


def refuse_mvl_identities(
    *,
    run_tag: str,
    run_label: str,
    config: dict,
    artifact_family: str,
    extra: str = "",
) -> None:
    blob = _identity_blob(run_tag, run_label, config, artifact_family, extra)
    for marker in FORBIDDEN_IDENTITY_MARKERS:
        if marker in blob:
            raise QualificationError(
                "MVL-F identities and output paths are refused for qualification"
            )
    for marker in FORBIDDEN_PATH_MARKERS:
        if marker in blob:
            raise QualificationError(
                "MVL-F identities and output paths are refused for qualification"
            )
    if artifact_family != QUALIFICATION_ARTIFACT_FAMILY:
        raise QualificationError("qualification artifacts must use qualification-runs")
    workflow = config.get("workflow")
    if workflow in {"mvl-baseline", "mvl", "pilot"}:
        raise QualificationError("MVL-F identities and output paths are refused for qualification")


def require_external_config(path_argument: str, repo: Path) -> Path:
    path = Path(path_argument)
    if not path.is_absolute() or not path.is_file():
        raise ConfigError(
            "qualify-agent config must be an existing absolute path outside the repository"
        )
    resolved = path.resolve()
    for candidate in (path, resolved):
        try:
            candidate.relative_to(repo)
        except ValueError:
            continue
        raise ConfigError(
            "qualify-agent config must be an existing absolute path outside the repository"
        )
    return resolved


def validate_authorized_qualification_config(
    config: dict, *, candidate_id: str, stage: str
) -> None:
    if not isinstance(config, dict):
        raise ConfigError("qualify-agent config must be a JSON object")
    required = (
        "endpoint",
        "cloud",
        "model",
        "serving",
        "host",
        "model_verification",
        "canonical_commit",
        "candidate_id",
    )
    for key in required:
        if key not in config:
            raise ConfigError(f"qualify-agent config is missing required section: {key}")

    if config.get("workflow") not in (None, QUALIFICATION_WORKFLOW):
        raise ConfigError("qualify-agent workflow must equal qualify-agent")
    if config.get("candidate_id") != candidate_id:
        raise ConfigError("qualify-agent config candidate_id must match --candidate")
    if candidate_id not in AUTHORIZED_CANDIDATES:
        raise ConfigError("qualification candidate must be C1 or C2")
    if config.get("workload_version") not in (None, QUALIFICATION_WORKLOAD_VERSION):
        raise ConfigError("qualify-agent workload_version must equal 2.4.0")
    if WORKLOAD_VERSION != CATALOG_WORKLOAD_VERSION:
        raise ConfigError("the scenario catalog identity must remain 2.3.0")

    commit = config.get("canonical_commit")
    if not isinstance(commit, str) or len(commit) != 40:
        raise ConfigError("qualify-agent config canonical_commit must be the 40-character SHA")

    verification = config["model_verification"]
    if not isinstance(verification, dict):
        raise ConfigError("qualify-agent config is missing required section: model_verification")
    for key in ("artifact_dir", "digest_manifest"):
        if not verification.get(key):
            raise ConfigError(f"qualify-agent config is missing model_verification.{key}")

    if config.get("comparison_mode") not in (None, FROZEN_COMPARISON_MODE):
        raise ConfigError("qualify-agent comparison_mode must equal provider-native")

    cloud = config["cloud"]
    if not isinstance(cloud, dict):
        raise ConfigError("qualify-agent config is missing required section: cloud")
    if cloud.get("instance_type") != FROZEN_INSTANCE_TYPE:
        raise ConfigError(f"qualify-agent cloud.instance_type must equal {FROZEN_INSTANCE_TYPE}")
    if cloud.get("region") != FROZEN_REGION:
        raise ConfigError(f"qualify-agent cloud.region must equal {FROZEN_REGION}")
    if cloud.get("list_price_usd_per_hour") not in (None, FROZEN_HOURLY_PRICE_USD):
        raise ConfigError("qualify-agent cloud.list_price_usd_per_hour must equal 3.0")

    model = config["model"]
    if not isinstance(model, dict):
        raise ConfigError("qualify-agent config is missing required section: model")
    if model.get("artifact") != FROZEN_MODEL_ARTIFACT:
        raise ConfigError("qualify-agent model.artifact does not match the frozen identity")
    if model.get("revision") != FROZEN_MODEL_REVISION:
        raise ConfigError("qualify-agent model.revision does not match the frozen identity")
    if model.get("artifact_hash") != FROZEN_MODEL_ARTIFACT_HASH:
        raise ConfigError("qualify-agent model.artifact_hash does not match the frozen aggregate")
    if model.get("precision") != FROZEN_PRECISION:
        raise ConfigError("qualify-agent model.precision must equal bf16")

    serving = config["serving"]
    if not isinstance(serving, dict):
        raise ConfigError("qualify-agent config is missing required section: serving")
    if serving.get("engine") != FROZEN_ENGINE:
        raise ConfigError("qualify-agent serving.engine must equal vllm")
    if serving.get("engine_version") != FROZEN_ENGINE_VERSION:
        raise ConfigError("qualify-agent serving.engine_version must equal 0.27.1")
    digest = serving.get("container_digest") or serving.get("image_digest")
    if digest not in {FROZEN_CONTAINER_DIGEST, FROZEN_VLLM_IMAGE_DIGEST}:
        raise ConfigError("qualify-agent serving.container_digest does not match the frozen image")

    if config.get("expected_gpu_model") not in (None, FROZEN_GPU):
        raise ConfigError(f"qualify-agent expected_gpu_model must equal {FROZEN_GPU}")

    spec = stage_spec(stage)
    if config.get("warmup_passes") not in (None, spec["warmup_passes"]):
        raise ConfigError(f"qualify-agent {stage} warmup_passes must equal {spec['warmup_passes']}")
    if config.get("repetitions") not in (None, spec["repetitions"]):
        raise ConfigError("qualify-agent repetitions must equal 1")
    if config.get("tasks_per_repetition") not in (None, spec["tasks"]):
        raise ConfigError(f"qualify-agent {stage} tasks_per_repetition must equal {spec['tasks']}")

    generation = config.get("generation") or {}
    if generation:
        expected_temperature = candidate_temperature(candidate_id)
        if generation.get("temperature") != expected_temperature:
            raise ConfigError(
                f"qualify-agent {candidate_id} generation.temperature "
                f"must equal {expected_temperature}"
            )
        if generation.get("top_p") != FROZEN_TOP_P:
            raise ConfigError("qualify-agent generation.top_p must equal 0.95")
        if generation.get("reasoning_mode") is not True:
            raise ConfigError("qualify-agent generation.reasoning_mode must be true")
        if generation.get("seed") not in (None, FROZEN_SEED):
            raise ConfigError("qualify-agent generation.seed must equal the frozen seed")
        if generation.get("max_tokens") not in (None, FROZEN_MAX_TOKENS):
            raise ConfigError("qualify-agent generation.max_tokens must equal 1024")

    raw_cells = config.get("cells")
    if raw_cells is not None:
        if not isinstance(raw_cells, list) or len(raw_cells) != 1:
            raise ConfigError("qualify-agent cells must be exactly interactive/1")
        cell = raw_cells[0]
        if not isinstance(cell, dict):
            raise ConfigError("qualify-agent cells must be exactly interactive/1")
        if cell.get("profile") != STAGE_PROFILE or cell.get("concurrency") != STAGE_CONCURRENCY:
            raise ConfigError("qualify-agent cells must be exactly interactive/1")
        if cell.get("comparison_mode") not in (None, FROZEN_COMPARISON_MODE):
            raise ConfigError("qualify-agent cells must be provider-native")

    refuse_mvl_identities(
        run_tag="",
        run_label="",
        config=config,
        artifact_family=QUALIFICATION_ARTIFACT_FAMILY,
    )


def load_qualification_config(path: Path, *, candidate_id: str, stage: str) -> tuple[dict, str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("qualify-agent config is not valid JSON") from exc
    if not isinstance(config, dict):
        raise ConfigError("qualify-agent config must be a JSON object")
    validate_authorized_qualification_config(config, candidate_id=candidate_id, stage=stage)
    return config, digest


def require_clean_canonical_commit(config: dict, *, git_head: str, tree_clean: bool) -> str:
    expected = config["canonical_commit"]
    if git_head != expected:
        raise QualificationError(
            "the working tree commit does not match the frozen canonical_commit "
            "in the approved config; nothing was executed"
        )
    if not tree_clean:
        raise QualificationError(
            "the working tree is dirty; qualification requires an exact canonical clean commit"
        )
    return git_head


def approval_phrase(run_tag: str, run_label: str, candidate_id: str, config_sha256: str) -> str:
    return QUALIFICATION_APPROVAL_TEMPLATE.format(
        run_tag=run_tag,
        run_label=run_label,
        candidate_id=candidate_id,
        config_sha256=config_sha256,
    )


def require_approval(
    provided: str | None,
    *,
    run_tag: str,
    run_label: str,
    candidate_id: str,
    config_sha256: str,
) -> None:
    expected = approval_phrase(run_tag, run_label, candidate_id, config_sha256)
    if (provided or "") != expected:
        raise QualificationError(
            "the qualification requires the exact owner approval phrase "
            f"(expected verbatim: {expected!r}); nothing was executed."
        )


def _field(outcome: object, name: str) -> object:
    if isinstance(outcome, dict):
        if name == "success":
            evaluation = outcome.get("evaluation")
            if isinstance(evaluation, dict) and "success" in evaluation:
                return evaluation.get("success")
        return outcome.get(name)
    execution = getattr(outcome, "execution", None)
    if name == "template_id":
        if execution is not None and getattr(execution, "scenario_id", None):
            return execution.scenario_id
        instance = getattr(outcome, "instance", None)
        if instance is not None:
            return getattr(instance, "template_id", None)
    if name == "success":
        evaluation = getattr(outcome, "evaluation", None)
        if evaluation is not None:
            return getattr(evaluation, "success", None)
    if name == "error_category" and execution is not None:
        return getattr(execution, "error_category", None)
    if name == "e2e_ms" and execution is not None:
        return getattr(execution, "e2e_ms", None)
    if name == "ttft_ms":
        if execution is not None:
            turns = getattr(execution, "turns", ())
            values = [getattr(turn, "ttft_ms", None) for turn in turns]
            values = [value for value in values if value is not None]
            if values:
                return min(values)
        if isinstance(outcome, dict):
            turns = outcome.get("turns") or []
            values = [
                turn.get("ttft_ms")
                for turn in turns
                if isinstance(turn, dict) and turn.get("ttft_ms") is not None
            ]
            if values:
                return min(values)
        return None
    return getattr(outcome, name, None)


@dataclass(frozen=True)
class QualificationMetrics:
    attempted: int
    succeeded: int
    aggregate_quality: float
    scenario_quality: dict[str, float]
    scenario_attempted: dict[str, int]
    valid_native_tool_call_rate: float
    invalid_tool_name_rate: float
    invalid_argument_rate: float
    request_inference_error_rate: float
    timeouts: int
    interactive_ttft_p95_ms: float | None
    interactive_e2e_p95_ms: float | None
    provenance_ok: bool
    verification_ok: bool


def compute_qualification_metrics(
    outcomes: Sequence[object],
    *,
    provenance_ok: bool,
    verification_ok: bool,
) -> QualificationMetrics:
    attempted = len(outcomes)
    if attempted < 1:
        raise QualificationError("qualification produced no task observations")
    succeeded = 0
    scenario_success: Counter[str] = Counter()
    scenario_attempted: Counter[str] = Counter()
    invalid_names = 0
    invalid_args = 0
    inference_errors = 0
    timeouts = 0
    structural = 0
    e2e_values: list[float] = []
    ttft_values: list[float] = []
    for outcome in outcomes:
        template_id = str(_field(outcome, "template_id") or "")
        scenario_attempted[template_id] += 1
        if _field(outcome, "success") is True:
            succeeded += 1
            scenario_success[template_id] += 1
        category = _field(outcome, "error_category")
        if category == "invalid_tool_name":
            invalid_names += 1
        if category == "invalid_tool_arguments":
            invalid_args += 1
        if category in REQUEST_INFERENCE_ERRORS:
            inference_errors += 1
        if category in TIMEOUT_CATEGORIES:
            timeouts += 1
        if category in STRUCTURAL_TOOL_FAILURES:
            structural += 1
        e2e = _field(outcome, "e2e_ms")
        if isinstance(e2e, (int, float)):
            e2e_values.append(float(e2e))
        ttft = _field(outcome, "ttft_ms")
        if isinstance(ttft, (int, float)):
            ttft_values.append(float(ttft))
    scenario_quality = {
        template_id: (scenario_success[template_id] / count if count else 0.0)
        for template_id, count in scenario_attempted.items()
    }
    return QualificationMetrics(
        attempted=attempted,
        succeeded=succeeded,
        aggregate_quality=succeeded / attempted,
        scenario_quality=scenario_quality,
        scenario_attempted=dict(scenario_attempted),
        valid_native_tool_call_rate=(attempted - structural) / attempted,
        invalid_tool_name_rate=invalid_names / attempted,
        invalid_argument_rate=invalid_args / attempted,
        request_inference_error_rate=inference_errors / attempted,
        timeouts=timeouts,
        interactive_ttft_p95_ms=(
            percentile_nearest_rank(sorted(ttft_values), 0.95) if ttft_values else None
        ),
        interactive_e2e_p95_ms=(
            percentile_nearest_rank(sorted(e2e_values), 0.95) if e2e_values else None
        ),
        provenance_ok=provenance_ok,
        verification_ok=verification_ok,
    )


def _rate_pass(value: float, minimum: float | None = None, maximum: float | None = None) -> bool:
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def evaluate_common_structural_requirements(metrics: QualificationMetrics) -> dict[str, bool]:
    ttft = metrics.interactive_ttft_p95_ms
    e2e = metrics.interactive_e2e_p95_ms
    return {
        "valid_native_tool_call_rate": _rate_pass(
            metrics.valid_native_tool_call_rate, minimum=MIN_VALID_NATIVE_TOOL_CALL_RATE
        ),
        "invalid_tool_name_rate": _rate_pass(
            metrics.invalid_tool_name_rate, maximum=MAX_INVALID_TOOL_NAME_RATE
        ),
        "invalid_argument_rate": _rate_pass(
            metrics.invalid_argument_rate, maximum=MAX_INVALID_ARGUMENT_RATE
        ),
        "request_inference_error_rate": _rate_pass(
            metrics.request_inference_error_rate, maximum=MAX_REQUEST_INFERENCE_ERROR_RATE
        ),
        "timeouts": metrics.timeouts <= MAX_TIMEOUTS,
        "interactive_ttft_p95": ttft is not None and ttft <= MAX_INTERACTIVE_TTFT_P95_MS,
        "interactive_e2e_p95": e2e is not None and e2e <= MAX_INTERACTIVE_E2E_P95_MS,
        "provenance": metrics.provenance_ok,
        "verification": metrics.verification_ok,
    }


def evaluate_quality_level(
    metrics: QualificationMetrics,
    *,
    min_aggregate: float,
    min_scenario: float,
    require_structural: bool,
) -> dict[str, Any]:
    scenario_failures = {
        template_id: rate
        for template_id, rate in metrics.scenario_quality.items()
        if rate < min_scenario
    }
    checks = {
        "aggregate_quality": metrics.aggregate_quality >= min_aggregate,
        "every_scenario": not scenario_failures,
    }
    structural = evaluate_common_structural_requirements(metrics) if require_structural else {}
    checks.update(structural)
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "scenario_failures": scenario_failures,
        "aggregate_quality": metrics.aggregate_quality,
        "min_aggregate": min_aggregate,
        "min_scenario": min_scenario,
    }


def evaluate_study_entry_gate(metrics: QualificationMetrics) -> dict[str, Any]:
    result = evaluate_quality_level(
        metrics,
        min_aggregate=STUDY_ENTRY_MIN_AGGREGATE,
        min_scenario=STUDY_ENTRY_MIN_SCENARIO,
        require_structural=True,
    )
    result["level"] = "study-entry"
    result["production_grade"] = False
    result["note"] = (
        "Passing this gate only permits a configuration to enter comparative "
        "measurement. It is not called production-grade. These are "
        "project-defined targets, not industry standards."
    )
    return result


def evaluate_production_like_target(metrics: QualificationMetrics) -> dict[str, Any]:
    result = evaluate_quality_level(
        metrics,
        min_aggregate=PRODUCTION_LIKE_MIN_AGGREGATE,
        min_scenario=PRODUCTION_LIKE_MIN_SCENARIO,
        require_structural=True,
    )
    result["level"] = "production-like"
    result["invalidates_measurement"] = False
    result["note"] = (
        "A measured configuration may fail this project-defined production-like "
        "target without invalidating its measurement. The study must report "
        "that failure. These are project-defined targets, not industry standards."
    )
    return result


def evaluate_stage_thresholds(stage: str, metrics: QualificationMetrics) -> dict[str, Any]:
    spec = stage_spec(stage)
    if spec["apply_study_entry_gate"]:
        study_entry = evaluate_study_entry_gate(metrics)
        production_like = evaluate_production_like_target(metrics)
        return {
            "stage": stage,
            "continue": study_entry["passed"],
            "stopped": not study_entry["passed"],
            "study_entry": study_entry,
            "production_like": production_like,
        }
    floor = float(spec["quality_floor"])
    passed = metrics.aggregate_quality >= floor
    return {
        "stage": stage,
        "continue": passed,
        "stopped": not passed,
        "quality_floor": floor,
        "aggregate_quality": metrics.aggregate_quality,
        "note": HOLDOUT_MUST_NOT_REVISE_WORDING if stage == STAGE_HOLDOUT else None,
    }


def outcomes_from_records(records: Iterable[object]) -> list[object]:
    outcomes: list[object] = []
    for record in records:
        direct = getattr(record, "outcomes", None)
        if direct:
            outcomes.extend(direct)
            continue
        measured = getattr(record, "measured_observations", None)
        if isinstance(measured, dict):
            outcomes.extend(measured.get("observations") or [])
        result = getattr(record, "result", None)
        if isinstance(result, dict):
            observations = result.get("observations")
            if isinstance(observations, dict) and observations.get("observations"):
                outcomes.extend(observations["observations"])
    return outcomes


def sanitized_receipt(
    *,
    run_label: str,
    candidate_id: str,
    stage: str,
    config_sha256: str,
    identity_digest: str,
    gates: dict[str, Any],
    files: Sequence[str],
    stopped: bool,
    message: str | None = None,
) -> dict[str, Any]:
    return {
        "workflow": QUALIFICATION_WORKFLOW,
        "artifact_family": QUALIFICATION_ARTIFACT_FAMILY,
        "diagnostic_label": QUALIFICATION_LABEL,
        "not_mvl": True,
        "not_comparative": True,
        "candidate_id": candidate_id,
        "stage": stage,
        "run_label": run_label,
        "config_sha256": config_sha256,
        "candidate_identity_sha256": identity_digest,
        "workload_version": QUALIFICATION_WORKLOAD_VERSION,
        "catalog_workload_version": CATALOG_WORKLOAD_VERSION,
        "gates": gates,
        "stopped": stopped,
        "files": list(files),
        "holdout_wording_rule": HOLDOUT_MUST_NOT_REVISE_WORDING,
        "note": (
            message
            or (
                "Qualification artifacts are labeled separately from MVL and "
                "comparative results. No infrastructure changes were made and "
                "no automatic teardown was performed."
            )
        ),
    }


def failure_record(message: str) -> dict[str, Any]:
    return {
        "failure_record": True,
        "is_valid_result": False,
        "workflow": QUALIFICATION_WORKFLOW,
        "artifact_family": QUALIFICATION_ARTIFACT_FAMILY,
        "diagnostic_label": QUALIFICATION_LABEL,
        "diagnostic_only": True,
        "error_type": "QualificationError",
        "note": message,
    }


def tool_contract_mentions_required_workflow() -> bool:
    system = TOOL_DESCRIPTIONS["retrieve_runbook"] + TOOL_DESCRIPTIONS["recommend_remediation"]
    return (
        "service or system" in TOOL_DESCRIPTIONS["retrieve_runbook"]
        and "remediation_ids" in TOOL_DESCRIPTIONS["retrieve_runbook"]
        and "found=false" in TOOL_DESCRIPTIONS["retrieve_runbook"]
        and "retrieve_runbook" in TOOL_DESCRIPTIONS["recommend_remediation"]
        and "log-dependent" in TOOL_DESCRIPTIONS["search_logs"]
        and "service or system" in system
    )
