"""Local benchmark runner for the synthetic Cloud Operations Agent (Phase 2).

CLI-first and fully offline: no GPU, no model download, no external API, no
network. The runner executes cells of (profile x concurrency) with a single
truthful **bounded closed-loop scheduler**: at most ``concurrency`` slots
exist, a worker claims the next task and stamps its submission only when a
slot becomes available (no pre-submitted unbounded backlog; unslotted tasks
consume no timeout budget), and requested/achieved-max/mean in-flight
concurrency are all recorded. Profiles are differentiated by context/input
size, output budget, timeout, and SLO — not by unimplemented arrival
algorithms.

Each repetition runs ``tasks_per_repetition`` seeded task instances
(measurement default 200) balanced across the incident templates (decision
D-0010), and emits one schema-valid run manifest, one benchmark result, and
raw task-observation files per repetition. Warm-up observations are retained
separately (labeled ``warmup``) and excluded from measured summaries.

Mock validity: mock execution is **functional-only**. Host-clock replay
latency, throughput, and production SLO attainment are never presented as
benchmark performance — the primary measures are explicitly unavailable with
reasons, and host-clock timings live only in the clearly separated
``mock_diagnostics`` namespace. Mock Python replay speed is never model
tokens/sec.

Results privacy: persistence goes through the existing ``LAB_RESULTS_DIR``
guard in **explicit synthetic mode** — an unset directory means *no
persistence at all* (never a fallback into the repository), and a configured
directory must be an absolute external path (``blackwell_lab.paths``). The
guard is validated **before any work** when persistence is requested; every
repetition is persisted promptly (atomic temp-file-plus-rename, private
permissions) rather than after the whole cell; and absolute private paths are
never printed — CLI output reports safe relative filenames and counts only.

Usage::

    blackwell-bench --profile interactive --concurrency 1
    python -m blackwell_lab.workload.runner --profile batch-heavy --concurrency 8
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from blackwell_lab.paths import ResultsLocationError, RunMode, resolve_results_dir
from blackwell_lab.schemas import (
    validate_benchmark_result,
    validate_run_manifest,
    validate_task_observations,
)
from blackwell_lab.workload.agent import (
    DEFAULT_MAX_TURNS,
    ERROR_TAXONOMY,
    TaskExecution,
    run_task,
)
from blackwell_lab.workload.clock import SYSTEM_CLOCK, Clock
from blackwell_lab.workload.evaluator import (
    EVALUATOR_VERSION,
    QUALITY_THRESHOLD,
    Evaluation,
    evaluate,
)
from blackwell_lab.workload.model_client import (
    DeterministicMockClient,
    GenerationSettings,
    ModelClient,
)
from blackwell_lab.workload.sampling import (
    TaskInstance,
    generate_task_instances,
    sample_design_summary,
)
from blackwell_lab.workload.scenarios import (
    WORKLOAD_NAME,
    WORKLOAD_VERSION,
    catalog,
    catalog_digest,
)
from blackwell_lab.workload.stats import measure_from_values, unavailable_measure
from blackwell_lab.workload.tools import SimulatedToolbox
from blackwell_lab.workload.validation import (
    ConfigError,
    validate_result_semantics,
    validate_runner_config,
)

MANIFEST_SCHEMA_VERSION = "2.1.0"
RESULT_SCHEMA_VERSION = "2.0.0"
OBSERVATION_SCHEMA_VERSION = "1.0.0"

#: Measurement defaults (measurement contract §5; decision D-0010).
DEFAULT_REPETITIONS = 5
DEFAULT_WARMUP_PASSES = 1
DEFAULT_TASKS_PER_REPETITION = 200
CONCURRENCY_LEVELS = (1, 4, 8)

_MOCK_REASONS = {
    "task_completion": (
        "mock execution is functional-only: host-clock replay latency is not "
        "benchmark performance (host-clock diagnostics are in mock_diagnostics)"
    ),
    "time_to_first_token": (
        "mock execution has no serving endpoint: mock replay TTFT is not benchmark performance"
    ),
    "inter_token": (
        "no true per-token timing: the mock client emits multi-word transport "
        "chunks, which are never tokens"
    ),
    "queue_time": ("no serving-engine queue telemetry exists in mock execution"),
    "model_serving_time": (
        "mock execution serves no model: host-clock turn time is not model serving time"
    ),
    "throughput": (
        "no authoritative usage data or tokenizer: mock Python replay speed is "
        "never reported as model tokens/sec"
    ),
    "slo_attainment": (
        "mock execution cannot attain a production SLO: host-clock replay "
        "latency is not production latency, so attainment is not measured"
    ),
    "gpu": (
        "mock execution mode: no GPU present and no DCGM telemetry collected; "
        "GPU metrics are not fabricated"
    ),
}


@dataclass(frozen=True)
class Profile:
    """Exact parameterization of one workload profile.

    SLO targets and timeouts are **owner-approved** (decision D-0010):
    interactive T_task 60,000 ms with 2,500 ms per-turn TTFT and 120,000 ms
    timeout; batch-heavy T_task 300,000 ms with no TTFT target and 600,000 ms
    timeout. Both profiles use the same bounded closed-loop scheduler and are
    differentiated by context/input size, output budget, timeout, and SLO.
    """

    name: str
    log_limit: int
    metric_window_s: int
    max_tokens: int
    task_timeout_ms: float
    slo_task_ms: float
    slo_ttft_ms: float | None


PROFILES: dict[str, Profile] = {
    "interactive": Profile(
        name="interactive",
        log_limit=10,
        metric_window_s=900,
        max_tokens=1024,
        task_timeout_ms=120_000.0,
        slo_task_ms=60_000.0,
        slo_ttft_ms=2_500.0,
    ),
    "batch-heavy": Profile(
        name="batch-heavy",
        log_limit=50,
        metric_window_s=3600,
        max_tokens=4096,
        task_timeout_ms=600_000.0,
        slo_task_ms=300_000.0,
        slo_ttft_ms=None,
    ),
}

#: One truthful scheduler for Phase 2 (recorded in manifests).
SCHEDULER_DESCRIPTION = (
    "bounded closed-loop: at most `concurrency` slots exist, and a worker "
    "claims the next task and stamps its submission only when a slot becomes "
    "available (a task that has not entered a slot consumes no timeout "
    "budget); in-flight work is bounded above by the requested concurrency "
    "but is not claimed to be exactly enforced during ramp-up and drain"
)


@dataclass(frozen=True)
class TaskOutcome:
    """One scheduled task: its instance, execution record, and evaluation."""

    task_index: int
    instance: TaskInstance
    execution: TaskExecution
    evaluation: Evaluation
    submitted_offset_ms: float


@dataclass(frozen=True)
class RepetitionData:
    """Raw outcomes plus scheduler accounting for one repetition/pass."""

    outcomes: tuple[TaskOutcome, ...]
    wall_time_s: float
    achieved_max_concurrency: int
    mean_in_flight: float


@dataclass(frozen=True)
class RepetitionRecord:
    """One measured repetition: manifest, result, raw docs, written files."""

    run_id: str
    manifest: dict
    result: dict
    measured_observations: dict
    warmup_observations: dict | None
    written_files: tuple[str, ...]  # safe relative filenames only


def _git_provenance() -> dict:
    """Best-effort git commit provenance for the manifest (no secrets)."""
    git = shutil.which("git")
    repo_url = "https://github.com/jgdynamite10/blackwell-agentic-inference-lab"
    if git is None:
        return {"commit": "0" * 40, "repository": repo_url, "dirty": True}
    try:
        root = Path(__file__).resolve()
        commit = subprocess.run(
            [git, "-C", str(root.parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            [git, "-C", str(root.parent), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
        return {"commit": commit, "repository": repo_url, "dirty": bool(status.strip())}
    except (subprocess.SubprocessError, OSError):
        return {"commit": "0" * 40, "repository": repo_url, "dirty": True}


def _host_description() -> dict:
    """Non-sensitive description of the local development host (mock mode)."""
    try:
        memory_gib = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024**3), 2)
    except (ValueError, OSError, AttributeError):
        memory_gib = 1.0
    return {
        "operating_system": platform.platform(),
        "cpu_model": platform.processor() or platform.machine() or "unknown",
        "vcpu_count": os.cpu_count() or 1,
        "system_memory_gib": max(memory_gib, 0.01),
        "storage_description": "local development storage (mock execution; not measured)",
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_pass(
    *,
    instances: list[TaskInstance],
    scenarios_by_id: dict,
    client: ModelClient,
    profile: Profile,
    concurrency: int,
    settings: GenerationSettings,
    timeout_ms: float,
    max_turns: int,
    clock: Clock,
) -> RepetitionData:
    """One bounded closed-loop pass over the instance schedule.

    At most ``concurrency`` slots exist. A worker **claims** the next task
    and stamps its submission only when its slot becomes available — no
    unbounded backlog is ever pre-submitted, and a task that has not entered
    a slot consumes none of its timeout budget. Each terminal record is
    handed to the evaluator immediately in the same worker.
    """
    pass_start = clock.monotonic()
    outcomes: list[TaskOutcome | None] = [None] * len(instances)
    lock = threading.Lock()
    state = {"next_index": 0, "in_flight": 0, "achieved_max": 0, "busy_s": 0.0}

    def _worker() -> None:
        while True:
            with lock:
                task_index = state["next_index"]
                if task_index >= len(instances):
                    return
                state["next_index"] += 1
                state["in_flight"] += 1
                state["achieved_max"] = max(state["achieved_max"], state["in_flight"])
            instance = instances[task_index]
            # Actual driver submission instant: the slot was just claimed, so
            # task timing — and the timeout budget — starts here.
            submitted_at = clock.monotonic()
            submitted_utc = _utc_now()
            scenario = scenarios_by_id[instance.template_id]
            toolbox = SimulatedToolbox(
                scenario,
                log_limit=profile.log_limit,
                metric_window_s=profile.metric_window_s,
                clock=clock,
            )
            execution = run_task(
                scenario,
                client,
                toolbox,
                settings,
                timeout_s=timeout_ms / 1000.0,
                max_turns=max_turns,
                clock=clock,
                instance=instance,
                submitted_at=submitted_at,
            )
            execution.submitted_at_utc = submitted_utc
            # End boundary: the terminal record is handed to the evaluator
            # immediately, in the same worker, with no buffering.
            evaluation = evaluate(scenario, execution)
            ended = clock.monotonic()
            with lock:
                state["in_flight"] -= 1
                state["busy_s"] += max(0.0, ended - submitted_at)
            outcomes[task_index] = TaskOutcome(
                task_index=task_index,
                instance=instance,
                execution=execution,
                evaluation=evaluation,
                submitted_offset_ms=max(0.0, (submitted_at - pass_start) * 1000.0),
            )

    if concurrency == 1:
        _worker()
    else:
        threads = [threading.Thread(target=_worker, daemon=True) for _ in range(concurrency)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    wall_s = max(clock.monotonic() - pass_start, 0.0)
    mean_in_flight = round(state["busy_s"] / wall_s, 3) if wall_s > 0 else 0.0
    return RepetitionData(
        outcomes=tuple(o for o in outcomes if o is not None),
        wall_time_s=wall_s,
        achieved_max_concurrency=state["achieved_max"],
        mean_in_flight=min(mean_in_flight, float(concurrency)),
    )


def _observation(outcome: TaskOutcome) -> dict:
    execution = outcome.execution
    terminal = None
    if execution.status == "completed":
        terminal = {
            "diagnosis_id": execution.diagnosis_id,
            "rationale": execution.rationale,
            "remediation_id": execution.remediation_id,
        }
    return {
        "task_index": outcome.task_index,
        "template_id": execution.scenario_id,
        "instance_id": execution.instance_id,
        "instance_seed": execution.instance_seed,
        "status": execution.status,
        "error_category": execution.error_category,
        "submitted_at_utc": execution.submitted_at_utc,
        "submitted_offset_ms": round(outcome.submitted_offset_ms, 3),
        "started_offset_ms": round(outcome.submitted_offset_ms + execution.queue_wait_ms, 3),
        "ended_offset_ms": round(outcome.submitted_offset_ms + execution.e2e_ms, 3),
        "e2e_ms": round(execution.e2e_ms, 3),
        "queue_wait_ms": round(execution.queue_wait_ms, 3),
        "turns": [
            {
                "ttft_ms": round(t.ttft_ms, 3) if t.ttft_ms is not None else None,
                "serving_time_ms": round(t.serving_time_ms, 3),
                "chunk_count": t.chunk_count,
                "content_chars": t.content_chars,
                "itl_available": t.itl_available,
                "itl_unavailable_reason": t.itl_unavailable_reason,
                "inter_token_gaps_ms": [round(g, 3) for g in t.inter_token_gaps_ms],
                "output_tokens": t.output_tokens,
                "tokens_unavailable_reason": t.tokens_unavailable_reason,
                "engine_queue_time_ms": t.engine_queue_time_ms,
            }
            for t in execution.turns
        ],
        "tool_trace": [
            {
                "tool": trace.tool,
                "arguments": trace.arguments,
                "result": trace.result,
                "simulated_latency_ms": trace.simulated_latency_ms,
            }
            for trace in execution.tool_trace
        ],
        "terminal": terminal,
        "evaluation": {
            "evaluator_version": outcome.evaluation.evaluator_version,
            "success": outcome.evaluation.success,
            "score": outcome.evaluation.score,
            "gates": [
                {"gate_id": g.gate_id, "passed": g.passed, "detail": g.detail}
                for g in outcome.evaluation.gates
            ],
            "diagnosis_component": outcome.evaluation.diagnosis_component,
            "remediation_component": outcome.evaluation.remediation_component,
            "evidence_component": outcome.evaluation.evidence_component,
            "reason": outcome.evaluation.reason,
        },
    }


def _observation_document(
    *, run_id: str, repetition_index: int, phase: str, outcomes: tuple[TaskOutcome, ...]
) -> dict:
    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "run_id": run_id,
        "repetition_index": repetition_index,
        "phase": phase,
        "observations": [_observation(o) for o in outcomes],
    }


def build_manifest(
    *,
    run_id: str,
    profile: Profile,
    concurrency: int,
    tasks_per_repetition: int,
    scenario_count: int,
    repetition_index: int,
    repetitions_planned: int,
    warmup_passes: int,
    settings: GenerationSettings,
    client: ModelClient,
    started_at_utc: str,
    ended_at_utc: str,
) -> dict:
    """A schema-valid run manifest for one mock-mode repetition."""
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "is_synthetic_example": False,
        "execution_mode": "mock",
        "created_at_utc": started_at_utc,
        "git": _git_provenance(),
        # No "model" block: mock execution serves no model, and the workload
        # catalog digest is recorded in workload.catalog_digest (never
        # misrepresented as a model artifact hash).
        "serving": {
            "engine": "mock",
            "engine_version": client.version,
        },
        "host": _host_description(),
        "cloud": {
            "provider": "other",
            "instance_type": "local-development-host",
            "region": "local",
            "comparison_mode": "not-applicable",
            "list_price_usd_per_hour": 0.0,
        },
        "generation": {
            "temperature": settings.temperature,
            "top_p": settings.top_p,
            "max_tokens": settings.max_tokens,
            "seed": settings.seed,
            "reasoning_mode": settings.reasoning_mode,
        },
        "workload": {
            "name": WORKLOAD_NAME,
            "version": WORKLOAD_VERSION,
            "catalog_digest": catalog_digest(),
            "profile": profile.name,
            "concurrency": concurrency,
            "tasks_per_repetition": tasks_per_repetition,
            "scenario_count": scenario_count,
            "evaluator_version": EVALUATOR_VERSION,
        },
        "timing_conditions": {
            "started_at_utc": started_at_utc,
            "ended_at_utc": ended_at_utc,
            "warmup_description": (
                f"{warmup_passes} full workload pass(es) at target concurrency before "
                "the first measured repetition; warm-up observations are retained "
                "separately (phase=warmup) and excluded from measured summaries"
            ),
            "repetition_index": repetition_index,
            "repetitions_planned": repetitions_planned,
            "clock_source": (
                "injectable monotonic clock (time.monotonic in production) for all "
                "durations; datetime.now(UTC) wall-clock timestamps for correlation "
                "only; task timing starts at actual driver submission"
            ),
            "notes": (
                "Offline mock execution: no GPU, no model download, no network. "
                "Simulated tool latencies are consumed through the clock (they "
                f"occupy task duration and timeout budget). Scheduler: {SCHEDULER_DESCRIPTION}."
            ),
        },
    }


def build_result(
    *,
    run_id: str,
    manifest_ref: str,
    profile: Profile,
    requested_concurrency: int,
    data: RepetitionData,
    sample_design: dict,
    observations_block: dict,
) -> dict:
    """A schema-valid benchmark result for one mock-mode repetition.

    Mock execution is functional-only: primary latency/throughput/SLO
    measures are explicitly unavailable with reasons; host-clock replay
    timings appear only under ``mock_diagnostics``.
    """
    outcomes = data.outcomes
    attempted = len(outcomes)
    succeeded = sum(
        1 for o in outcomes if o.execution.status == "completed" and o.evaluation.success
    )
    quality_failed = sum(
        1 for o in outcomes if o.execution.status == "completed" and not o.evaluation.success
    )
    errored = sum(1 for o in outcomes if o.execution.status == "error")
    timed_out = sum(1 for o in outcomes if o.execution.status == "timeout")

    taxonomy: dict[str, int] = {}
    for o in outcomes:
        if o.execution.error_category is not None:
            category = o.execution.error_category
            taxonomy[category] = taxonomy.get(category, 0) + 1

    tool_exec = [ms for o in outcomes for ms in o.execution.tool_latencies_ms]
    host_clock_e2e = [o.execution.e2e_ms for o in outcomes]
    scores = [o.evaluation.score for o in outcomes]

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "run_id": run_id,
        "is_synthetic_example": False,
        "execution_mode": "mock",
        "manifest_ref": manifest_ref,
        "tasks": {
            "attempted": attempted,
            "succeeded": succeeded,
            "quality_failed": quality_failed,
            "errored": errored,
            "timed_out": timed_out,
            "success_rate": round(succeeded / attempted, 6) if attempted else 0.0,
            "mean_quality_score": round(sum(scores) / len(scores), 6) if scores else None,
        },
        "sample_design": sample_design,
        "concurrency": {
            "requested": requested_concurrency,
            "achieved_max": data.achieved_max_concurrency,
            "mean_in_flight": data.mean_in_flight,
        },
        "latency_ms": {
            "task_completion": unavailable_measure(_MOCK_REASONS["task_completion"]),
            "time_to_first_token": unavailable_measure(_MOCK_REASONS["time_to_first_token"]),
            "inter_token": unavailable_measure(_MOCK_REASONS["inter_token"]),
            "queue_time": unavailable_measure(_MOCK_REASONS["queue_time"]),
            "model_serving_time": unavailable_measure(_MOCK_REASONS["model_serving_time"]),
            # Simulated tool latencies are defined, documented values — the
            # one latency series that is truthful in mock execution.
            "tool_execution_time": measure_from_values(
                tool_exec, empty_reason="no tool invocations occurred"
            ),
        },
        "throughput": unavailable_measure(_MOCK_REASONS["throughput"]),
        "gpu": {
            "telemetry_available": False,
            "unavailable_reason": _MOCK_REASONS["gpu"],
        },
        "errors": {
            "error_rate": round(errored / attempted, 6) if attempted else 0.0,
            "timeout_rate": round(timed_out / attempted, 6) if attempted else 0.0,
            "taxonomy": taxonomy,
        },
        "slo": {
            "latency_target_ms": profile.slo_task_ms,
            "ttft_target_ms": profile.slo_ttft_ms,
            "quality_threshold": QUALITY_THRESHOLD,
            "slo_attaining_tasks": None,
            "attainment_unavailable_reason": _MOCK_REASONS["slo_attainment"],
            "slo_attaining_throughput_per_gpu_hour": None,
        },
        "observations": observations_block,
        "mock_diagnostics": {
            "note": (
                "Functional diagnostics only: host-clock replay timings of the "
                "offline mock pipeline (includes consumed simulated tool delays). "
                "NOT benchmark performance, NOT model latency, NOT tokens/sec."
            ),
            "host_clock_task_completion_ms": {
                "mean": round(sum(host_clock_e2e) / len(host_clock_e2e), 3),
                "min": round(min(host_clock_e2e), 3),
                "max": round(max(host_clock_e2e), 3),
                "count": len(host_clock_e2e),
            },
            "wall_time_s": round(data.wall_time_s, 3),
        },
        "economics": {
            "gpu_hours": 0.0,
            "list_price_usd_per_hour": 0.0,
            "cost_per_successful_task_usd": None,
            "cost_basis_note": (
                "Mock execution on a local development host: zero GPU-hours, no "
                "billable resources; cost measures are not applicable and are "
                "represented as null."
            ),
        },
        "notes": (
            "Offline mock-mode repetition (functional validation only). SLO "
            "targets, timeouts, and the S_min = 1.0 gate-based quality threshold "
            "are owner-approved (decision D-0010)."
        ),
    }


def _write_private_json(directory: Path, filename: str, document: dict) -> str:
    """Atomically writes a private JSON file; returns its SHA-256 hex digest.

    Uses a temporary file in the same directory plus ``os.replace`` so a
    crash never leaves a partial artifact, and private (0600) permissions
    where the platform supports them.
    """
    payload = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{filename}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, directory / filename)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return hashlib.sha256(payload).hexdigest()


def run_cell(
    *,
    profile_name: str,
    concurrency: int,
    repetitions: int = DEFAULT_REPETITIONS,
    warmup_passes: int = DEFAULT_WARMUP_PASSES,
    tasks_per_repetition: int = DEFAULT_TASKS_PER_REPETITION,
    scenario_ids: list[str] | None = None,
    client: ModelClient | None = None,
    seed: int = 20260906,
    timeout_ms: float | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    clock: Clock = SYSTEM_CLOCK,
    results_dir: Path | None = None,
) -> list[RepetitionRecord]:
    """Executes one experimental cell: warm-up, then measured repetitions.

    When ``results_dir`` is provided (already validated by the
    ``LAB_RESULTS_DIR`` guard), every repetition is persisted promptly and
    atomically as it completes; ``None`` means no persistence anywhere.
    """
    validate_runner_config(
        profile_name=profile_name,
        concurrency=concurrency,
        repetitions=repetitions,
        warmup_passes=warmup_passes,
        tasks_per_repetition=tasks_per_repetition,
        timeout_ms=timeout_ms,
        max_turns=max_turns,
        seed=seed,
        allowed_profiles=tuple(sorted(PROFILES)),
        allowed_concurrency=CONCURRENCY_LEVELS,
    )
    profile = PROFILES[profile_name]
    full_catalog = catalog()
    if scenario_ids is None:
        template_ids = list(full_catalog)
    else:
        missing = [s for s in scenario_ids if s not in full_catalog]
        if missing:
            raise ConfigError(f"unknown scenario ids: {missing}")
        template_ids = list(scenario_ids)

    client = client or DeterministicMockClient()
    settings = GenerationSettings(max_tokens=profile.max_tokens, seed=seed)
    effective_timeout_ms = timeout_ms if timeout_ms is not None else profile.task_timeout_ms

    target_dir: Path | None = None
    if results_dir is not None:
        target_dir = results_dir / "synthetic-mock-runs"
        # Creating the external directory is the runner's explicit action;
        # private permissions where supported.
        target_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(target_dir, 0o700)

    # Run ids are fixed up front so warm-up observations (which precede the
    # first measured repetition) attach to repetition 1's run.
    run_ids = [str(uuid.uuid4()) for _ in range(repetitions)]

    def _pass(instances: list[TaskInstance]) -> RepetitionData:
        return _run_pass(
            instances=instances,
            scenarios_by_id=full_catalog,
            client=client,
            profile=profile,
            concurrency=concurrency,
            settings=settings,
            timeout_ms=effective_timeout_ms,
            max_turns=max_turns,
            clock=clock,
        )

    # Warm-up: labeled, retained separately, excluded from measured summaries.
    warmup_outcomes: list[TaskOutcome] = []
    for warmup_index in range(warmup_passes):
        warmup_seed = seed - warmup_index - 1
        warmup_instances = generate_task_instances(template_ids, tasks_per_repetition, warmup_seed)
        warmup_outcomes.extend(_pass(warmup_instances).outcomes)
    warmup_document = None
    if warmup_passes > 0:
        warmup_document = _observation_document(
            run_id=run_ids[0],
            repetition_index=1,
            phase="warmup",
            outcomes=tuple(warmup_outcomes),
        )
        validate_task_observations(warmup_document)

    records: list[RepetitionRecord] = []
    for repetition_index in range(1, repetitions + 1):
        run_id = run_ids[repetition_index - 1]
        # A distinct seed per repetition: byte-identical repetitions are never
        # represented as independent quality cases (decision D-0010).
        repetition_seed = seed + repetition_index
        instances = generate_task_instances(template_ids, tasks_per_repetition, repetition_seed)
        started_at_utc = _utc_now()
        data = _pass(instances)
        ended_at_utc = _utc_now()

        measured_document = _observation_document(
            run_id=run_id,
            repetition_index=repetition_index,
            phase="measured",
            outcomes=data.outcomes,
        )
        validate_task_observations(measured_document)

        written: list[str] = []
        is_first_repetition = repetition_index == 1
        repetition_warmup_doc = warmup_document if is_first_repetition else None
        warmup_count = len(warmup_outcomes) if is_first_repetition else 0
        observations_block: dict = {
            "persisted": target_dir is not None,
            "measured_count": len(data.outcomes),
            "warmup_count": warmup_count,
        }
        if target_dir is not None:
            # Prompt, atomic, private persistence of raw observations —
            # validated above, hashed here, referenced from the result below.
            measured_name = f"{run_id}.observations.json"
            measured_sha = _write_private_json(target_dir, measured_name, measured_document)
            observations_block["measured_file"] = measured_name
            observations_block["measured_sha256"] = measured_sha
            written.append(measured_name)
            if repetition_warmup_doc is not None:
                warmup_name = f"{run_id}.warmup-observations.json"
                warmup_sha = _write_private_json(target_dir, warmup_name, repetition_warmup_doc)
                observations_block["warmup_file"] = warmup_name
                observations_block["warmup_sha256"] = warmup_sha
                written.append(warmup_name)

        manifest = build_manifest(
            run_id=run_id,
            profile=profile,
            concurrency=concurrency,
            tasks_per_repetition=tasks_per_repetition,
            scenario_count=len(template_ids),
            repetition_index=repetition_index,
            repetitions_planned=repetitions,
            warmup_passes=warmup_passes,
            settings=settings,
            client=client,
            started_at_utc=started_at_utc,
            ended_at_utc=ended_at_utc,
        )
        result = build_result(
            run_id=run_id,
            manifest_ref=f"{run_id}.manifest.json",
            profile=profile,
            requested_concurrency=concurrency,
            data=data,
            sample_design=sample_design_summary(instances, repetition_seed),
            observations_block=observations_block,
        )
        # Every document must validate — schema AND semantics — before it is
        # reported or persisted.
        validate_run_manifest(manifest)
        validate_benchmark_result(result)
        validate_result_semantics(
            manifest,
            result,
            measured_observations=measured_document,
            warmup_observations=repetition_warmup_doc,
        )
        if target_dir is not None:
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


def _summary_line(record: RepetitionRecord) -> dict:
    tasks = record.result["tasks"]
    return {
        "run_id": record.run_id,
        "repetition": record.manifest["timing_conditions"]["repetition_index"],
        "attempted": tasks["attempted"],
        "succeeded": tasks["succeeded"],
        "quality_failed": tasks["quality_failed"],
        "errored": tasks["errored"],
        "timed_out": tasks["timed_out"],
        "success_rate": tasks["success_rate"],
        "concurrency": record.result["concurrency"],
        "sample_design": {
            "tasks_per_repetition": record.result["sample_design"]["tasks_per_repetition"],
            "unique_template_count": record.result["sample_design"]["unique_template_count"],
            "unique_instance_count": record.result["sample_design"]["unique_instance_count"],
        },
        "observations": {
            "persisted": record.result["observations"]["persisted"],
            "measured_count": record.result["observations"]["measured_count"],
            "warmup_count": record.result["observations"]["warmup_count"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blackwell-bench",
        description=(
            "Offline synthetic benchmark runner (Phase 2, functional validation "
            "only). Runs the Cloud Operations Agent against the deterministic "
            "mock client: no GPU, no model download, no network, no credentials. "
            "Output persists only when LAB_RESULTS_DIR points to an absolute "
            "directory outside the repository; unset means no persistence. "
            "Absolute private paths are never printed."
        ),
    )
    parser.add_argument("--profile", choices=sorted(PROFILES), help="Workload profile.")
    parser.add_argument(
        "--concurrency",
        type=int,
        choices=CONCURRENCY_LEVELS,
        default=1,
        help="Requested concurrency: bounded closed-loop scheduler slots (default 1).",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=DEFAULT_REPETITIONS,
        help=f"Measured repetitions per cell (default {DEFAULT_REPETITIONS}).",
    )
    parser.add_argument(
        "--warmup-passes",
        type=int,
        default=DEFAULT_WARMUP_PASSES,
        help=(
            "Warm-up passes retained separately and excluded from measured "
            f"summaries (default {DEFAULT_WARMUP_PASSES})."
        ),
    )
    parser.add_argument(
        "--tasks-per-repetition",
        type=int,
        default=DEFAULT_TASKS_PER_REPETITION,
        help=(
            "Seeded task instances per measured repetition, balanced across "
            f"incident templates (measurement default {DEFAULT_TASKS_PER_REPETITION}; "
            "must be >= concurrency)."
        ),
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help=f"Maximum model turns per task (default {DEFAULT_MAX_TURNS}).",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        metavar="SCENARIO_ID",
        help="Limit to specific scenario ids (repeatable; default: all ten).",
    )
    parser.add_argument("--seed", type=int, default=20260906, help="Deterministic sampling seed.")
    parser.add_argument("--list-scenarios", action="store_true", help="List scenario ids and exit.")
    args = parser.parse_args(argv)

    if args.list_scenarios:
        for scenario_id, scenario in catalog().items():
            print(f"{scenario_id}\t{scenario.incident_class}\t{scenario.title}")
        return 0
    if args.profile is None:
        parser.error("--profile is required unless --list-scenarios is given")

    # Persistence guard runs BEFORE any work: when LAB_RESULTS_DIR is set it
    # must be a safe external absolute path, or the run refuses to start.
    try:
        results_dir = resolve_results_dir(mode=RunMode.SYNTHETIC)
    except ResultsLocationError as exc:
        print(f"error: refusing to persist results: {exc}", file=sys.stderr)
        return 3

    try:
        records = run_cell(
            profile_name=args.profile,
            concurrency=args.concurrency,
            repetitions=args.repetitions,
            warmup_passes=args.warmup_passes,
            tasks_per_repetition=args.tasks_per_repetition,
            scenario_ids=args.scenarios,
            seed=args.seed,
            max_turns=args.max_turns,
            results_dir=results_dir,
        )
    except (ConfigError, ValueError) as exc:
        # Sanitized by construction: configuration messages carry no paths or
        # environment values.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    written_files = [name for record in records for name in record.written_files]
    summary = {
        "workload": WORKLOAD_NAME,
        "workload_version": WORKLOAD_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "profile": args.profile,
        "concurrency_requested": args.concurrency,
        "repetitions": [_summary_line(r) for r in records],
        "error_taxonomy_reference": list(ERROR_TAXONOMY),
        "persistence": (
            {
                "enabled": True,
                "location": "LAB_RESULTS_DIR (external private directory; path not printed)",
                "files": written_files,
                "file_count": len(written_files),
            }
            if results_dir is not None
            else {
                "enabled": False,
                "location": "LAB_RESULTS_DIR unset; nothing was written anywhere",
                "files": [],
                "file_count": 0,
            }
        ),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
