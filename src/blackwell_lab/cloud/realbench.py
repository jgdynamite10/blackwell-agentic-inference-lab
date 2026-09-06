"""Real-benchmark (gpu-mode) assembly, preserving every Phase 2 contract.

This module extends the Phase 2 benchmark path to a genuine serving endpoint
without changing any measurement semantics: the same bounded closed-loop
scheduler (``_run_pass``), the same agent loop and claim-time submission
stamping, the same gate-based evaluator, the same task accounting, and the
same raw-observation retention. Only the client (an
:class:`~blackwell_lab.workload.openai_client.OpenAICompatibleClient`) and
the manifest/result assembly differ.

Truthfulness rules for genuine runs:

- **Genuine output requires** ``RunMode.REAL`` **and an external
  LAB_RESULTS_DIR.** The privacy guard is resolved before any work; an unset
  or unsafe location fails closed. Nothing is ever written inside the
  repository.
- **Transport chunks are never tokens** — the client emits no token events,
  so inter-token latency is recorded as unavailable rather than derived from
  chunk timing.
- **Required measurements fail visibly.** A genuine run that cannot produce
  its required measurements raises :class:`RequiredMeasurementError` instead
  of silently writing "unavailable": end-to-end task latency, authoritative
  usage-based token counts (every completed task), GPU telemetry, and — for
  the interactive profile — TTFT are required. Serving-engine queue
  telemetry and true per-token ITL remain optional and are recorded as
  explicitly unavailable when the engine does not provide them.
- **No fabricated or placeholder values** anywhere: every number in a
  genuine-run document is measured, derived from measurements, or absent
  with a reason.
"""

from __future__ import annotations

import contextlib
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from blackwell_lab.paths import RunMode, resolve_results_dir
from blackwell_lab.schemas import (
    validate_benchmark_result,
    validate_run_manifest,
    validate_task_observations,
)
from blackwell_lab.workload.clock import SYSTEM_CLOCK, Clock
from blackwell_lab.workload.evaluator import EVALUATOR_VERSION, QUALITY_THRESHOLD
from blackwell_lab.workload.model_client import GenerationSettings, ModelClient
from blackwell_lab.workload.runner import (
    DEFAULT_REPETITIONS,
    DEFAULT_TASKS_PER_REPETITION,
    DEFAULT_WARMUP_PASSES,
    MANIFEST_SCHEMA_VERSION,
    PROFILES,
    RESULT_SCHEMA_VERSION,
    SCHEDULER_DESCRIPTION,
    Profile,
    RepetitionData,
    RepetitionRecord,
    TaskOutcome,
    _git_provenance,
    _observation_document,
    _run_pass,
    _write_private_json,
)
from blackwell_lab.workload.sampling import generate_task_instances, sample_design_summary
from blackwell_lab.workload.scenarios import (
    WORKLOAD_NAME,
    WORKLOAD_VERSION,
    catalog,
    catalog_digest,
)
from blackwell_lab.workload.stats import measure_from_values, unavailable_measure
from blackwell_lab.workload.validation import (
    ConfigError,
    validate_result_semantics,
    validate_runner_config,
)

_CONTAINER_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_ARTIFACT_HASH_RE = re.compile(r"^(sha256:[0-9a-f]{64}|sha512:[0-9a-f]{128}|blake3:[0-9a-f]{64})$")
_RUN_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

ITL_UNAVAILABLE_REASON = (
    "the OpenAI-compatible stream carries no true per-token timing; transport "
    "chunks are never tokens, so inter-token latency is not reported"
)
QUEUE_UNAVAILABLE_REASON = (
    "the serving engine reported no per-request queue telemetry in the "
    "stream; engine-level queue metrics are collected from the metrics "
    "endpoint only when genuinely available"
)


class RequiredMeasurementError(RuntimeError):
    """A measurement required for genuine runs is unavailable (fail visibly)."""


#: Controlled-resource mode requires the joint 14-vCPU/100-GiB
#: serving-plus-benchmark cgroup envelope to be genuinely implemented,
#: enforced, and observed. Until that exists, any run labeled
#: "controlled-resource" would be labeled by configuration alone — a
#: fabrication — so the mode is rejected outright. Enabling it is a reviewed
#: code change gated on verified enforcement, never a runtime flag
#: (decision D-0013).
CONTROLLED_RESOURCE_ENFORCEMENT_IMPLEMENTED = False


#: A sampler factory returns an object with start()/stop()/summary(...) —
#: production uses telemetry.GpuSamplerThread; tests inject fakes.
SamplerFactory = Callable[[], object]


@dataclass(frozen=True)
class RealRunSpec:
    """Complete parameterization of one genuine experimental cell."""

    profile_name: str
    concurrency: int
    comparison_mode: str  # "controlled-resource" | "provider-native"
    instance_type: str
    region: str
    list_price_usd_per_hour: float
    price_source_date: str
    model: dict  # artifact, revision, artifact_hash, precision[, license]
    engine: str
    engine_version: str
    container_digest: str
    repetitions: int = DEFAULT_REPETITIONS
    warmup_passes: int = DEFAULT_WARMUP_PASSES
    tasks_per_repetition: int = DEFAULT_TASKS_PER_REPETITION
    seed: int = 20260906
    resource_limits: dict | None = None  # required for controlled-resource
    container_cuda_runtime_version: str | None = None  # observed, never configured
    run_label: str = "real"
    generation: GenerationSettings = field(
        # Model-card recommended sampling (feasibility report §5); frozen
        # for the full baseline only after the pilot (decision D-0012).
        default_factory=lambda: GenerationSettings(temperature=1.0, top_p=0.95)
    )


def _validate_spec(spec: RealRunSpec) -> Profile:
    validate_runner_config(
        profile_name=spec.profile_name,
        concurrency=spec.concurrency,
        repetitions=spec.repetitions,
        warmup_passes=spec.warmup_passes,
        tasks_per_repetition=spec.tasks_per_repetition,
        timeout_ms=None,
        max_turns=12,
        seed=spec.seed,
        allowed_profiles=tuple(sorted(PROFILES)),
        allowed_concurrency=(1, 4, 8),
    )
    if spec.comparison_mode not in ("controlled-resource", "provider-native"):
        raise ConfigError(
            "comparison_mode must be 'controlled-resource' or 'provider-native' "
            "for genuine runs (never 'not-applicable')"
        )
    if spec.comparison_mode == "controlled-resource":
        if not CONTROLLED_RESOURCE_ENFORCEMENT_IMPLEMENTED:
            raise ConfigError(
                "controlled-resource runs are rejected: the joint "
                "14-vCPU/100-GiB serving-plus-benchmark cgroup envelope is not "
                "yet implemented and verified, and a comparison-mode label "
                "supplied merely in configuration is a fabrication. Run "
                "provider-native (decision D-0013)."
            )
        if not spec.resource_limits:  # pragma: no cover - unreachable while gated
            raise ConfigError(
                "controlled-resource runs must declare the enforced joint "
                "resource_limits (vcpu_limit, memory_limit_gib)"
            )
    for key in ("artifact", "revision", "artifact_hash", "precision"):
        if not spec.model.get(key):
            raise ConfigError(f"model.{key} is required for genuine runs")
    if not _ARTIFACT_HASH_RE.match(spec.model["artifact_hash"]):
        raise ConfigError("model.artifact_hash must be a pinned algorithm:hex digest")
    if not _CONTAINER_DIGEST_RE.match(spec.container_digest):
        raise ConfigError("container_digest must be an immutable repo@sha256:... digest")
    if not spec.engine or spec.engine == "mock":
        raise ConfigError("genuine runs require a real serving engine (never 'mock')")
    if not spec.engine_version:
        raise ConfigError("engine_version is required for genuine runs")
    if not _RUN_LABEL_RE.match(spec.run_label):
        raise ConfigError("run_label must be short lowercase letters/digits/hyphens")
    if spec.list_price_usd_per_hour < 0:
        raise ConfigError("list_price_usd_per_hour must be >= 0")
    if not spec.instance_type or not spec.region:
        raise ConfigError("instance_type and region are required for genuine runs")
    return PROFILES[spec.profile_name]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_real_manifest(
    *,
    spec: RealRunSpec,
    profile: Profile,
    run_id: str,
    host: dict,
    scenario_count: int,
    repetition_index: int,
    started_at_utc: str,
    ended_at_utc: str,
) -> dict:
    """A schema-valid gpu-mode run manifest (no fabricated values)."""
    cloud: dict = {
        "provider": "akamai",
        "instance_type": spec.instance_type,
        "region": spec.region,
        "comparison_mode": spec.comparison_mode,
        "list_price_usd_per_hour": spec.list_price_usd_per_hour,
        "price_source_date": spec.price_source_date,
    }
    if spec.comparison_mode == "controlled-resource":
        cloud["resource_limits"] = spec.resource_limits
    serving: dict = {
        "engine": spec.engine,
        "engine_version": spec.engine_version,
        "container_digest": spec.container_digest,
    }
    if spec.container_cuda_runtime_version:
        serving["container_cuda_runtime_version"] = spec.container_cuda_runtime_version
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "is_synthetic_example": False,
        "execution_mode": "gpu",
        "created_at_utc": started_at_utc,
        "git": _git_provenance(),
        "model": dict(spec.model),
        "serving": serving,
        "host": host,
        "cloud": cloud,
        "generation": {
            "temperature": spec.generation.temperature,
            "top_p": spec.generation.top_p,
            "max_tokens": profile.max_tokens,
            "seed": spec.generation.seed,
            "reasoning_mode": spec.generation.reasoning_mode,
        },
        "workload": {
            "name": WORKLOAD_NAME,
            "version": WORKLOAD_VERSION,
            "catalog_digest": catalog_digest(),
            "profile": profile.name,
            "concurrency": spec.concurrency,
            "tasks_per_repetition": spec.tasks_per_repetition,
            "scenario_count": scenario_count,
            "evaluator_version": EVALUATOR_VERSION,
        },
        "timing_conditions": {
            "started_at_utc": started_at_utc,
            "ended_at_utc": ended_at_utc,
            "warmup_description": (
                f"{spec.warmup_passes} full workload pass(es) at target concurrency "
                "before the first measured repetition; warm-up observations are "
                "retained separately (phase=warmup) and excluded from measured "
                "summaries"
            ),
            "repetition_index": repetition_index,
            "repetitions_planned": spec.repetitions,
            "clock_source": (
                "injectable monotonic clock (time.monotonic in production) for "
                "all durations; datetime.now(UTC) wall-clock timestamps for "
                "correlation only; task timing starts at actual driver submission"
            ),
            "notes": (
                "Genuine gpu-mode run against a local OpenAI-compatible serving "
                f"endpoint. Scheduler: {SCHEDULER_DESCRIPTION}."
            ),
        },
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RequiredMeasurementError(message)


def build_real_result(
    *,
    spec: RealRunSpec,
    profile: Profile,
    run_id: str,
    manifest_ref: str,
    data: RepetitionData,
    sample_design: dict,
    observations_block: dict,
    gpu_block: dict,
) -> dict:
    """A schema-valid gpu-mode benchmark result derived only from measurements.

    Raises :class:`RequiredMeasurementError` when a required measurement is
    missing — a genuine run never silently downgrades to "unavailable" for
    the measures the study depends on.
    """
    outcomes = data.outcomes
    attempted = len(outcomes)
    _require(attempted >= 1, "no tasks were attempted; nothing can be reported")
    succeeded_outcomes = [
        o for o in outcomes if o.execution.status == "completed" and o.evaluation.success
    ]
    succeeded = len(succeeded_outcomes)
    quality_failed = sum(
        1 for o in outcomes if o.execution.status == "completed" and not o.evaluation.success
    )
    errored = sum(1 for o in outcomes if o.execution.status == "error")
    timed_out = sum(1 for o in outcomes if o.execution.status == "timeout")

    taxonomy: dict[str, int] = {}
    for outcome in outcomes:
        if outcome.execution.error_category is not None:
            category = outcome.execution.error_category
            taxonomy[category] = taxonomy.get(category, 0) + 1

    # --- latency series (driver-measured, monotonic clock) ---------------
    e2e_values = [o.execution.e2e_ms for o in outcomes]
    ttft_values = [t.ttft_ms for o in outcomes for t in o.execution.turns if t.ttft_ms is not None]
    serving_values = [t.serving_time_ms for o in outcomes for t in o.execution.turns]
    tool_values = [ms for o in outcomes for ms in o.execution.tool_latencies_ms]
    itl_gaps = [
        gap
        for o in outcomes
        for t in o.execution.turns
        if t.itl_available
        for gap in t.inter_token_gaps_ms
    ]
    queue_values = [
        t.engine_queue_time_ms
        for o in outcomes
        for t in o.execution.turns
        if t.engine_queue_time_ms is not None
    ]

    if profile.slo_ttft_ms is not None:
        _require(
            bool(ttft_values),
            "TTFT is required for the interactive profile but no first-content "
            "timings were observed",
        )

    # --- authoritative token accounting (usage events only) --------------
    completed_outcomes = [o for o in outcomes if o.execution.status == "completed"]
    for outcome in completed_outcomes:
        for turn in outcome.execution.turns:
            _require(
                turn.output_tokens is not None,
                "authoritative usage token counts are required for every "
                "completed task's turns; the endpoint did not report usage "
                "(chunk counts are never substituted)",
            )
    counted_turns = [t for o in outcomes for t in o.execution.turns if t.output_tokens is not None]
    total_output_tokens = sum(t.output_tokens for t in counted_turns)  # type: ignore[misc]
    _require(data.wall_time_s > 0, "repetition wall time was not positive")
    per_turn_rates = [
        t.output_tokens / (t.serving_time_ms / 1000.0)
        for t in counted_turns
        if t.serving_time_ms > 0 and t.output_tokens is not None
    ]

    gpu_hours = data.wall_time_s / 3600.0
    _require(
        bool(gpu_block.get("telemetry_available")),
        "GPU telemetry is required for genuine runs and was not collected",
    )

    # --- SLO attainment (owner-approved targets, D-0010) ------------------
    def _attains(outcome: TaskOutcome) -> bool:
        if outcome.execution.e2e_ms > profile.slo_task_ms:
            return False
        if profile.slo_ttft_ms is not None:
            for turn in outcome.execution.turns:
                if turn.ttft_ms is None or turn.ttft_ms > profile.slo_ttft_ms:
                    return False
        return True

    slo_attaining = sum(1 for o in succeeded_outcomes if _attains(o))
    scores = [o.evaluation.score for o in outcomes]

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "run_id": run_id,
        "is_synthetic_example": False,
        "execution_mode": "gpu",
        "manifest_ref": manifest_ref,
        "tasks": {
            "attempted": attempted,
            "succeeded": succeeded,
            "quality_failed": quality_failed,
            "errored": errored,
            "timed_out": timed_out,
            "success_rate": round(succeeded / attempted, 6),
            "mean_quality_score": round(sum(scores) / len(scores), 6) if scores else None,
        },
        "sample_design": sample_design,
        "concurrency": {
            "requested": spec.concurrency,
            "achieved_max": data.achieved_max_concurrency,
            "mean_in_flight": data.mean_in_flight,
        },
        "latency_ms": {
            "task_completion": measure_from_values(e2e_values),
            "time_to_first_token": measure_from_values(
                ttft_values,
                empty_reason=(
                    "no first-content timings were observed (batch-heavy profile "
                    "has no TTFT target; interactive requires them)"
                ),
            ),
            "inter_token": (
                measure_from_values(itl_gaps)
                if itl_gaps
                else unavailable_measure(ITL_UNAVAILABLE_REASON)
            ),
            "queue_time": (
                measure_from_values(queue_values)
                if queue_values
                else unavailable_measure(QUEUE_UNAVAILABLE_REASON)
            ),
            "model_serving_time": measure_from_values(
                serving_values, empty_reason="no model turns completed"
            ),
            "tool_execution_time": measure_from_values(
                tool_values, empty_reason="no tool invocations occurred"
            ),
        },
        "throughput": {
            "available": True,
            "output_tokens_per_second": round(total_output_tokens / data.wall_time_s, 3),
            "total_output_tokens": total_output_tokens,
            "per_turn_output_tokens_per_second": measure_from_values(
                per_turn_rates, empty_reason="no turns carried usage token counts"
            ),
            "successful_tasks_per_gpu_hour": (
                round(succeeded / gpu_hours, 3) if gpu_hours > 0 else None
            ),
        },
        "gpu": gpu_block,
        "errors": {
            "error_rate": round(errored / attempted, 6),
            "timeout_rate": round(timed_out / attempted, 6),
            "taxonomy": taxonomy,
        },
        "slo": {
            "latency_target_ms": profile.slo_task_ms,
            "ttft_target_ms": profile.slo_ttft_ms,
            "quality_threshold": QUALITY_THRESHOLD,
            "slo_attaining_tasks": slo_attaining,
            "slo_attaining_throughput_per_gpu_hour": (
                round(slo_attaining / gpu_hours, 3) if gpu_hours > 0 else None
            ),
        },
        "observations": observations_block,
        "economics": {
            "gpu_hours": round(gpu_hours, 6),
            "list_price_usd_per_hour": spec.list_price_usd_per_hour,
            "cost_per_successful_task_usd": (
                round(spec.list_price_usd_per_hour * gpu_hours / succeeded, 6)
                if succeeded > 0
                else None
            ),
            "cost_basis_note": (
                "On-demand list price basis for the purchasable plan; excludes "
                "taxes, discounts, storage, and egress (reported separately at "
                "phase close). GPU-hours cover this repetition's measured wall "
                "time only; full-session hours (setup, warm-up, idle) are "
                "tracked in the session ledger."
            ),
        },
        "notes": (
            "Genuine gpu-mode repetition. GPU energy is integrated from "
            "periodic power samples (mean power x sampled wall time). "
            "Inter-token latency and per-request queue time are reported only "
            "when the serving path genuinely provides them."
        ),
    }


def run_real_cell(
    spec: RealRunSpec,
    client: ModelClient,
    *,
    host: dict,
    sampler_factory: SamplerFactory,
    results_dir: Path | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> list[RepetitionRecord]:
    """Executes one genuine experimental cell and persists every repetition.

    ``host`` is the complete gpu-mode host block (telemetry facts, never
    fabricated). ``sampler_factory`` produces one GPU sampler per pass
    (``telemetry.GpuSamplerThread`` in production). The ``LAB_RESULTS_DIR``
    guard is enforced **independently by this entry point**: with no
    ``results_dir`` it resolves the guard in **RunMode.REAL** (unset fails
    closed), and an explicitly passed ``results_dir`` is re-validated
    through the same guard so no caller can steer genuine output into the
    repository.
    """
    profile = _validate_spec(spec)
    if results_dir is None:
        resolved = resolve_results_dir(mode=RunMode.REAL)
    else:
        # Independent enforcement: an explicitly supplied directory passes
        # through the same REAL-mode guard (absolute, external, symlink-safe).
        resolved = resolve_results_dir(str(results_dir), mode=RunMode.REAL)
    if resolved is None:  # pragma: no cover - RunMode.REAL never returns None
        raise RequiredMeasurementError("no external results directory was resolved")
    results_dir = resolved
    target_dir = results_dir / "real-runs" / spec.run_label
    target_dir.mkdir(parents=True, exist_ok=True)
    for directory in (results_dir / "real-runs", target_dir):
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)

    full_catalog = catalog()
    template_ids = list(full_catalog)
    settings = GenerationSettings(
        temperature=spec.generation.temperature,
        top_p=spec.generation.top_p,
        max_tokens=profile.max_tokens,
        seed=spec.generation.seed,
        reasoning_mode=spec.generation.reasoning_mode,
    )
    run_ids = [str(uuid.uuid4()) for _ in range(spec.repetitions)]

    def _pass(instances: list) -> RepetitionData:
        return _run_pass(
            instances=instances,
            scenarios_by_id=full_catalog,
            client=client,
            profile=profile,
            concurrency=spec.concurrency,
            settings=settings,
            timeout_ms=profile.task_timeout_ms,
            max_turns=12,
            clock=clock,
        )

    warmup_outcomes: list[TaskOutcome] = []
    for warmup_index in range(spec.warmup_passes):
        warmup_seed = spec.seed - warmup_index - 1
        instances = generate_task_instances(template_ids, spec.tasks_per_repetition, warmup_seed)
        warmup_outcomes.extend(_pass(instances).outcomes)
    warmup_document = None
    if spec.warmup_passes > 0:
        warmup_document = _observation_document(
            run_id=run_ids[0],
            repetition_index=1,
            phase="warmup",
            outcomes=tuple(warmup_outcomes),
        )
        validate_task_observations(warmup_document)

    records: list[RepetitionRecord] = []
    for repetition_index in range(1, spec.repetitions + 1):
        run_id = run_ids[repetition_index - 1]
        repetition_seed = spec.seed + repetition_index
        instances = generate_task_instances(
            template_ids, spec.tasks_per_repetition, repetition_seed
        )
        sampler = sampler_factory()
        started_at_utc = _utc_now()
        sampler.start()  # type: ignore[attr-defined]
        try:
            data = _pass(instances)
        finally:
            sampler.stop()  # type: ignore[attr-defined]
        ended_at_utc = _utc_now()
        succeeded = sum(
            1 for o in data.outcomes if o.execution.status == "completed" and o.evaluation.success
        )

        try:
            gpu_block = sampler.summary(successful_tasks=succeeded)  # type: ignore[attr-defined]

            measured_document = _observation_document(
                run_id=run_id,
                repetition_index=repetition_index,
                phase="measured",
                outcomes=data.outcomes,
            )
            validate_task_observations(measured_document)

            is_first = repetition_index == 1
            repetition_warmup_doc = warmup_document if is_first else None
            warmup_count = len(warmup_outcomes) if is_first else 0

            # Prompt, atomic, private persistence of raw observations.
            written: list[str] = []
            measured_name = f"{run_id}.observations.json"
            measured_sha = _write_private_json(target_dir, measured_name, measured_document)
            written.append(measured_name)
            observations_block: dict = {
                "persisted": True,
                "measured_count": len(data.outcomes),
                "warmup_count": warmup_count,
                "measured_file": measured_name,
                "measured_sha256": measured_sha,
            }
            if repetition_warmup_doc is not None:
                warmup_name = f"{run_id}.warmup-observations.json"
                warmup_sha = _write_private_json(target_dir, warmup_name, repetition_warmup_doc)
                observations_block["warmup_file"] = warmup_name
                observations_block["warmup_sha256"] = warmup_sha
                written.append(warmup_name)

            manifest = build_real_manifest(
                spec=spec,
                profile=profile,
                run_id=run_id,
                host=host,
                scenario_count=len(template_ids),
                repetition_index=repetition_index,
                started_at_utc=started_at_utc,
                ended_at_utc=ended_at_utc,
            )
            result = build_real_result(
                spec=spec,
                profile=profile,
                run_id=run_id,
                manifest_ref=f"{run_id}.manifest.json",
                data=data,
                sample_design=sample_design_summary(instances, repetition_seed),
                observations_block=observations_block,
                gpu_block=gpu_block,
            )
            validate_run_manifest(manifest)
            validate_benchmark_result(result)
            validate_result_semantics(
                manifest,
                result,
                measured_observations=measured_document,
                warmup_observations=repetition_warmup_doc,
            )
        except Exception as exc:
            # An incomplete attempt is retained externally as an explicitly
            # marked failure record — never presented as a valid result — and
            # the run still fails visibly.
            _write_private_json(
                target_dir,
                f"{run_id}.failure.json",
                {
                    "failure_record": True,
                    "is_valid_result": False,
                    "run_id": run_id,
                    "repetition_index": repetition_index,
                    "failed_at_utc": _utc_now(),
                    "error_type": type(exc).__name__,
                    "note": (
                        "This repetition did not produce a valid, complete "
                        "result. This record exists only for auditability; it "
                        "must never be analyzed or published as a result."
                    ),
                },
            )
            raise

        manifest_name = f"{run_id}.manifest.json"
        result_name = f"{run_id}.result.json"
        _write_private_json(target_dir, manifest_name, manifest)
        _write_private_json(target_dir, result_name, result)
        written.extend([manifest_name, result_name])

        records.append(
            RepetitionRecord(
                run_id=run_id,
                manifest=manifest,
                result=result,
                measured_observations=measured_document,
                warmup_observations=repetition_warmup_doc,
                written_files=tuple(written),
            )
        )
    return records
