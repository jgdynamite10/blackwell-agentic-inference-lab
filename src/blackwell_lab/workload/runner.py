"""Local benchmark runner for the synthetic Cloud Operations Agent (Phase 2).

CLI-first and fully offline: no GPU, no model download, no external API, no
network. The runner executes cells of (profile x concurrency), separates
warm-up from measurement, performs five measured repetitions by default, and
emits one schema-valid run manifest and one benchmark result per repetition.

Results privacy: persistence goes through the existing ``LAB_RESULTS_DIR``
guard in **explicit synthetic mode** — an unset directory means *no
persistence at all* (never a fallback into the repository), and a configured
directory must be an absolute external path (``blackwell_lab.paths``).

Usage::

    blackwell-bench --profile interactive --concurrency 1
    python -m blackwell_lab.workload.runner --profile batch-heavy --concurrency 8
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from blackwell_lab.paths import ResultsLocationError, RunMode, resolve_results_dir
from blackwell_lab.schemas import validate_benchmark_result, validate_run_manifest
from blackwell_lab.workload.agent import ERROR_TAXONOMY, TaskExecution, run_task
from blackwell_lab.workload.evaluator import (
    EVALUATOR_VERSION,
    PROPOSED_QUALITY_THRESHOLD,
    Evaluation,
    evaluate,
)
from blackwell_lab.workload.model_client import (
    MOCK_CLIENT_VERSION,
    DeterministicMockClient,
    GenerationSettings,
    ModelClient,
)
from blackwell_lab.workload.scenarios import (
    WORKLOAD_NAME,
    WORKLOAD_VERSION,
    Scenario,
    catalog,
    catalog_digest,
)
from blackwell_lab.workload.stats import summarize_latencies
from blackwell_lab.workload.tools import SimulatedToolbox

MANIFEST_SCHEMA_VERSION = "1.1.0"
RESULT_SCHEMA_VERSION = "1.1.0"

#: Measurement defaults from the contract (§5).
DEFAULT_REPETITIONS = 5
DEFAULT_WARMUP_PASSES = 1
CONCURRENCY_LEVELS = (1, 4, 8)


@dataclass(frozen=True)
class Profile:
    """Exact parameterization of one workload profile (decision D-0009).

    SLO targets are **proposals requiring owner approval** before Phase 3
    measurement; they are recorded per result so attainment is recomputable.
    """

    name: str
    arrival: str
    log_limit: int
    metric_window_s: int
    max_tokens: int
    task_timeout_ms: float
    slo_task_ms: float
    slo_ttft_ms: float | None


PROFILES: dict[str, Profile] = {
    "interactive": Profile(
        name="interactive",
        arrival="closed-loop: each concurrency slot issues its next task only after "
        "its previous task finishes",
        log_limit=10,
        metric_window_s=900,
        max_tokens=1024,
        task_timeout_ms=120_000.0,
        slo_task_ms=60_000.0,
        slo_ttft_ms=2_500.0,
    ),
    "batch-heavy": Profile(
        name="batch-heavy",
        arrival="queue-full: the task queue is kept full for every concurrency slot "
        "for the whole measurement window",
        log_limit=50,
        metric_window_s=3600,
        max_tokens=4096,
        task_timeout_ms=600_000.0,
        slo_task_ms=300_000.0,
        slo_ttft_ms=None,
    ),
}


@dataclass(frozen=True)
class RepetitionRecord:
    """One measured repetition: its manifest, result, and raw executions."""

    run_id: str
    manifest: dict
    result: dict
    executions: tuple[TaskExecution, ...]
    evaluations: tuple[Evaluation, ...]


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


def build_manifest(
    *,
    run_id: str,
    profile: Profile,
    concurrency: int,
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
        "model": {
            # Mock mode serves no model; the "artifact" is the deterministic
            # mock client, content-addressed by the scenario-catalog digest so
            # results are attributable to an exact workload definition.
            "artifact": "synthetic/deterministic-mock-client",
            "revision": MOCK_CLIENT_VERSION,
            "artifact_hash": catalog_digest(),
            "precision": "other",
        },
        "serving": {
            "engine": "mock",
            "engine_version": client.version,
        },
        "host": _host_description(),
        "cloud": {
            "provider": "other",
            "instance_type": "local-development-host",
            "region": "local",
            "comparison_mode": "provider-native",
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
            "profile": profile.name,
            "concurrency": concurrency,
            "scenario_count": scenario_count,
            "evaluator_version": EVALUATOR_VERSION,
        },
        "timing_conditions": {
            "started_at_utc": started_at_utc,
            "ended_at_utc": ended_at_utc,
            "warmup_description": (
                f"{warmup_passes} full workload pass(es) at target concurrency before "
                "the first measured repetition; warm-up data excluded from results"
            ),
            "repetition_index": repetition_index,
            "repetitions_planned": repetitions_planned,
            "clock_source": (
                "time.monotonic() for all durations; datetime.now(UTC) wall-clock "
                "timestamps for correlation only"
            ),
            "notes": (
                "Offline mock execution: no GPU, no model download, no network. "
                "Tool latencies are fixed simulated values (recorded, not slept)."
            ),
        },
    }


def build_result(
    *,
    run_id: str,
    manifest_ref: str,
    profile: Profile,
    executions: list[TaskExecution],
    evaluations: list[Evaluation],
    measured_wall_s: float,
) -> dict:
    """A schema-valid benchmark result for one mock-mode repetition."""
    attempted = len(executions)
    succeeded = sum(1 for e in evaluations if e.success)
    failed = sum(1 for x in executions if x.status == "error")
    timed_out = sum(1 for x in executions if x.status == "timeout")

    taxonomy: dict[str, int] = {}
    for x in executions:
        if x.error_category is not None:
            taxonomy[x.error_category] = taxonomy.get(x.error_category, 0) + 1

    task_completion = [x.e2e_ms for x in executions]
    ttft = [t.ttft_ms for x in executions for t in x.turn_metrics]
    inter_token = [g for x in executions for t in x.turn_metrics for g in t.inter_token_gaps_ms]
    serving = [t.serving_time_ms for x in executions for t in x.turn_metrics]
    tool_exec = [ms for x in executions for ms in x.tool_latencies_ms]

    total_tokens = sum(t.output_tokens for x in executions for t in x.turn_metrics)

    def _attains_slo(execution: TaskExecution, evaluation: Evaluation) -> bool:
        if not evaluation.success or execution.e2e_ms > profile.slo_task_ms:
            return False
        if profile.slo_ttft_ms is not None:
            return all(t.ttft_ms <= profile.slo_ttft_ms for t in execution.turn_metrics)
        return True

    slo_attaining = sum(
        1 for x, ev in zip(executions, evaluations, strict=True) if _attains_slo(x, ev)
    )
    scores = [ev.score for ev in evaluations]

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "run_id": run_id,
        "is_synthetic_example": False,
        "manifest_ref": manifest_ref,
        "tasks": {
            "attempted": attempted,
            "succeeded": succeeded,
            "failed": failed,
            "timed_out": timed_out,
            "success_rate": round(succeeded / attempted, 6) if attempted else 0.0,
            "mean_quality_score": round(sum(scores) / len(scores), 6) if scores else None,
        },
        "latency_ms": {
            "task_completion": summarize_latencies(task_completion),
            "time_to_first_token": summarize_latencies(ttft),
            "inter_token": summarize_latencies(inter_token),
            # Mock mode has no serving endpoint, so queue time is not
            # observable: an explicitly empty series (count 0), never zeros.
            "queue_time": summarize_latencies([]),
            "model_serving_time": summarize_latencies(serving),
            "tool_execution_time": summarize_latencies(tool_exec),
        },
        "throughput": {
            "output_tokens_per_second": (
                round(total_tokens / measured_wall_s, 3) if measured_wall_s > 0 else 0.0
            ),
            "total_output_tokens": total_tokens,
            # No GPU-hours were consumed in mock mode: represented as null,
            # never a fabricated number.
            "successful_tasks_per_gpu_hour": None,
        },
        "gpu": {
            "telemetry_available": False,
            "unavailable_reason": (
                "mock execution mode: no GPU present and no DCGM telemetry "
                "collected; GPU metrics are not fabricated"
            ),
        },
        "errors": {
            "error_rate": round(failed / attempted, 6) if attempted else 0.0,
            "timeout_rate": round(timed_out / attempted, 6) if attempted else 0.0,
            "taxonomy": taxonomy,
        },
        "slo": {
            "latency_target_ms": profile.slo_task_ms,
            "ttft_target_ms": profile.slo_ttft_ms,
            "quality_threshold": PROPOSED_QUALITY_THRESHOLD,
            "slo_attaining_tasks": slo_attaining,
            "slo_attaining_throughput_per_gpu_hour": None,
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
            "Offline mock-mode repetition. SLO targets and the quality threshold "
            "are proposals pending owner approval (decision D-0009)."
        ),
    }


def _execute_pass(
    scenarios: list[Scenario],
    client: ModelClient,
    profile: Profile,
    concurrency: int,
    settings: GenerationSettings,
    timeout_ms: float,
) -> list[TaskExecution]:
    """Runs every scenario once at the given concurrency level.

    The driver enforces the concurrency level exactly: at most ``concurrency``
    tasks are in flight at any time. Interactive is closed-loop per slot;
    batch-heavy keeps the queue full — with a thread pool of exactly
    ``concurrency`` workers, both submit-all semantics apply and each slot
    pulls its next task the moment it frees up.
    """

    def _one(scenario: Scenario) -> TaskExecution:
        toolbox = SimulatedToolbox(
            scenario,
            log_limit=profile.log_limit,
            metric_window_s=profile.metric_window_s,
        )
        return run_task(
            scenario,
            client,
            toolbox,
            settings,
            timeout_s=timeout_ms / 1000.0,
        )

    if concurrency == 1:
        return [_one(s) for s in scenarios]
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(_one, scenarios))


def run_cell(
    *,
    profile_name: str,
    concurrency: int,
    repetitions: int = DEFAULT_REPETITIONS,
    warmup_passes: int = DEFAULT_WARMUP_PASSES,
    scenario_ids: list[str] | None = None,
    client: ModelClient | None = None,
    seed: int = 20260906,
    timeout_ms: float | None = None,
) -> list[RepetitionRecord]:
    """Executes one experimental cell: warm-up, then measured repetitions."""
    if profile_name not in PROFILES:
        raise ValueError(f"unknown profile: {profile_name!r}")
    if concurrency not in CONCURRENCY_LEVELS:
        raise ValueError(f"concurrency must be one of {CONCURRENCY_LEVELS}")
    profile = PROFILES[profile_name]
    full_catalog = catalog()
    if scenario_ids is None:
        scenarios = list(full_catalog.values())
    else:
        missing = [s for s in scenario_ids if s not in full_catalog]
        if missing:
            raise ValueError(f"unknown scenario ids: {missing}")
        scenarios = [full_catalog[s] for s in scenario_ids]

    client = client or DeterministicMockClient()
    settings = GenerationSettings(max_tokens=profile.max_tokens, seed=seed)
    effective_timeout_ms = timeout_ms if timeout_ms is not None else profile.task_timeout_ms

    # Warm-up: full passes excluded from measurement (contract §5). Warm-up
    # data is discarded here; nothing from warm-up enters any result.
    for _ in range(warmup_passes):
        _execute_pass(scenarios, client, profile, concurrency, settings, effective_timeout_ms)

    records: list[RepetitionRecord] = []
    for repetition_index in range(1, repetitions + 1):
        run_id = str(uuid.uuid4())
        started_at_utc = datetime.now(timezone.utc).isoformat()
        pass_start = time.monotonic()
        executions = _execute_pass(
            scenarios, client, profile, concurrency, settings, effective_timeout_ms
        )
        measured_wall_s = time.monotonic() - pass_start
        ended_at_utc = datetime.now(timezone.utc).isoformat()

        evaluations = [evaluate(full_catalog[x.scenario_id], x) for x in executions]
        manifest = build_manifest(
            run_id=run_id,
            profile=profile,
            concurrency=concurrency,
            scenario_count=len(scenarios),
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
            executions=executions,
            evaluations=evaluations,
            measured_wall_s=measured_wall_s,
        )
        # Both documents must validate before they are reported or persisted.
        validate_run_manifest(manifest)
        validate_benchmark_result(result)
        records.append(
            RepetitionRecord(
                run_id=run_id,
                manifest=manifest,
                result=result,
                executions=tuple(executions),
                evaluations=tuple(evaluations),
            )
        )
    return records


def persist_records(records: list[RepetitionRecord]) -> list[Path] | None:
    """Persists repetition records through the ``LAB_RESULTS_DIR`` guard.

    Uses **explicit synthetic mode**: an unset ``LAB_RESULTS_DIR`` yields no
    persistence (returns ``None``); a set value must resolve to an absolute
    path outside the repository or the guard raises. There is never a
    fallback into the repository.
    """
    results_dir = resolve_results_dir(mode=RunMode.SYNTHETIC)
    if results_dir is None:
        return None
    # Creating the external directory is the runner's explicit, logged action.
    target = results_dir / "synthetic-mock-runs"
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for record in records:
        manifest_path = target / f"{record.run_id}.manifest.json"
        result_path = target / f"{record.run_id}.result.json"
        manifest_path.write_text(json.dumps(record.manifest, indent=2) + "\n", encoding="utf-8")
        result_path.write_text(json.dumps(record.result, indent=2) + "\n", encoding="utf-8")
        written.extend([manifest_path, result_path])
    return written


def _summary_line(record: RepetitionRecord) -> dict:
    tasks = record.result["tasks"]
    return {
        "run_id": record.run_id,
        "repetition": record.manifest["timing_conditions"]["repetition_index"],
        "attempted": tasks["attempted"],
        "succeeded": tasks["succeeded"],
        "failed": tasks["failed"],
        "timed_out": tasks["timed_out"],
        "mean_quality_score": tasks["mean_quality_score"],
        "task_completion_ms": record.result["latency_ms"]["task_completion"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blackwell-bench",
        description=(
            "Offline synthetic benchmark runner (Phase 2). Runs the Cloud "
            "Operations Agent against the deterministic mock client: no GPU, no "
            "model download, no network, no credentials. Output persists only "
            "when LAB_RESULTS_DIR points to an absolute directory outside the "
            "repository; unset means no persistence."
        ),
    )
    parser.add_argument("--profile", choices=sorted(PROFILES), help="Workload profile.")
    parser.add_argument(
        "--concurrency",
        type=int,
        choices=CONCURRENCY_LEVELS,
        default=1,
        help="Simultaneous in-flight agent tasks (default 1).",
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
        help=f"Warm-up passes excluded from measurement (default {DEFAULT_WARMUP_PASSES}).",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        metavar="SCENARIO_ID",
        help="Limit to specific scenario ids (repeatable; default: all ten).",
    )
    parser.add_argument("--seed", type=int, default=20260906, help="Recorded generation seed.")
    parser.add_argument("--list-scenarios", action="store_true", help="List scenario ids and exit.")
    args = parser.parse_args(argv)

    if args.list_scenarios:
        for scenario_id, scenario in catalog().items():
            print(f"{scenario_id}\t{scenario.incident_class}\t{scenario.title}")
        return 0
    if args.profile is None:
        parser.error("--profile is required unless --list-scenarios is given")

    try:
        records = run_cell(
            profile_name=args.profile,
            concurrency=args.concurrency,
            repetitions=args.repetitions,
            warmup_passes=args.warmup_passes,
            scenario_ids=args.scenarios,
            seed=args.seed,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        written = persist_records(records)
    except ResultsLocationError as exc:
        print(f"error: refusing to persist results: {exc}", file=sys.stderr)
        return 3

    summary = {
        "workload": WORKLOAD_NAME,
        "workload_version": WORKLOAD_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "profile": args.profile,
        "concurrency": args.concurrency,
        "repetitions": [_summary_line(r) for r in records],
        "error_taxonomy_reference": list(ERROR_TAXONOMY),
        "persistence": (
            [str(p) for p in written]
            if written is not None
            else "disabled (LAB_RESULTS_DIR unset; nothing was written anywhere)"
        ),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
