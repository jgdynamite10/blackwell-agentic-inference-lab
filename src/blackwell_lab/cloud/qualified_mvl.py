"""Qualification-bound Akamai baseline; historical MVL-F is unchanged.

This command requires a verified W7, W8, W9, or W10 freeze result from the same resource
session before any client is constructed. The three measurement cells and
sample counts are D-0017's. Workload and generation follow the bound
qualified candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from blackwell_lab.cloud import mvl as historical
from blackwell_lab.cloud import qualification
from blackwell_lab.cloud.analysis import _execution, result_directory
from blackwell_lab.cloud.artifacts import write_private_json
from blackwell_lab.cloud.matched_control import resource_identity_sha256
from blackwell_lab.schemas import (
    validate_benchmark_result,
    validate_run_manifest,
    validate_task_observations,
)
from blackwell_lab.workload.clock import SYSTEM_CLOCK
from blackwell_lab.workload.evaluator import evaluate
from blackwell_lab.workload.model_client import ModelClientTimeout
from blackwell_lab.workload.public_metadata.catalog_v2 import evidence_contract
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.validation import ConfigError, validate_result_semantics

WORKFLOW = "qualified-baseline"
ARTIFACT_FAMILY = "qualified-baseline-runs"
MVL_APPROVAL_TEMPLATE = (
    "I approve the Akamai qualified baseline for run {run_tag} "
    "({run_label}) using candidate {candidate_id} config sha256:{config_sha256}"
)
FROZEN_WORKLOAD_VERSION = qualification.W7_WORKLOAD_VERSION
FROZEN_TEMPERATURE = qualification.W_TEMPERATURE
AUTHORIZED_MVL_CELLS = historical.AUTHORIZED_MVL_CELLS
FROZEN_REPETITIONS = historical.FROZEN_REPETITIONS
FROZEN_WARMUP_PASSES = historical.FROZEN_WARMUP_PASSES
FROZEN_TASKS_PER_REPETITION = historical.FROZEN_TASKS_PER_REPETITION
FROZEN_COMPARISON_MODE = historical.FROZEN_COMPARISON_MODE
FROZEN_MODEL_ARTIFACT_HASH = historical.FROZEN_MODEL_ARTIFACT_HASH
FROZEN_CONTAINER_DIGEST = historical.FROZEN_CONTAINER_DIGEST
FROZEN_VLLM_IMAGE_DIGEST = historical.FROZEN_VLLM_IMAGE_DIGEST
FROZEN_SEED = historical.FROZEN_SEED
FROZEN_TOP_P = historical.FROZEN_TOP_P
FROZEN_REASONING_MODE = historical.FROZEN_REASONING_MODE
TEARDOWN_FROM_LAPTOP_NOTE = historical.TEARDOWN_FROM_LAPTOP_NOTE
MvlError = historical.MvlError
require_safe_run_label = historical.require_safe_run_label
require_external_config = historical.require_external_config
require_clean_canonical_commit = historical.require_clean_canonical_commit
output_label = historical.output_label
cell_output_suffix = historical.cell_output_suffix
measured_counts = historical.measured_counts
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_study_entry_binding(binding: object) -> None:
    fields = {
        "candidate_id",
        "candidate_identity_sha256",
        "receipt_sha256",
        "result_sha256",
        "manifest_sha256",
        "study_entry_passed",
    }
    if not isinstance(binding, dict) or set(binding) != fields:
        raise ConfigError("qualified baseline requires the complete study-entry digest binding")
    candidate_id = binding["candidate_id"]
    passed = binding["study_entry_passed"] is True
    if candidate_id not in {"W7", "W8", "W9", "W10"} or not passed:
        raise ConfigError(
            "qualified baseline requires a verified W7, W8, W9, or W10 study-entry binding"
        )
    for field in fields - {"candidate_id", "study_entry_passed"}:
        if not isinstance(binding[field], str) or not _SHA256.fullmatch(binding[field]):
            raise ConfigError("qualified baseline study-entry digest is malformed")
    if binding["candidate_identity_sha256"] != qualification.candidate_identity_digest(
        binding["candidate_id"]
    ):
        raise ConfigError("qualified baseline study-entry candidate identity mismatch")


class SessionBudgetClient:
    """Clamp every model request to the remaining, verified six-hour session."""

    def __init__(self, client, deadline: float):
        self.client = client
        self.deadline = deadline

    def stream_turn(self, messages, settings, *, deadline=None, clock=SYSTEM_CLOCK):
        if clock.monotonic() >= self.deadline:
            raise ModelClientTimeout("the approved session budget has expired")
        bounded = min(deadline, self.deadline) if deadline is not None else self.deadline
        yield from self.client.stream_turn(messages, settings, deadline=bounded, clock=clock)


def load_mvl_config(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    try:
        config = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ConfigError("qualified baseline config is not valid JSON") from exc
    if not isinstance(config, dict):
        raise ConfigError("qualified baseline config must be an object")
    candidate_id = config.get("candidate_id")
    if config.get("workflow") != WORKFLOW or candidate_id not in {"W7", "W8", "W9", "W10"}:
        raise ConfigError(
            "qualified baseline requires workflow qualified-baseline "
            "and candidate W7, W8, W9, or W10"
        )
    expected_version = qualification.candidate_workload_version(str(candidate_id))
    if config.get("workload_version") != expected_version:
        raise ConfigError(f"qualified baseline requires workload {expected_version}")
    if config.get("candidate_identity_sha256") != qualification.candidate_identity_digest(
        str(candidate_id)
    ):
        raise ConfigError("qualified baseline candidate identity mismatch")
    generation = config.get("generation")
    expected = qualification.frozen_candidate_fields(str(candidate_id))["generation"]
    if generation != expected:
        raise ConfigError(
            "qualified baseline generation must equal the qualified candidate settings"
        )
    qualification.require_w_config(config, candidate_id=str(candidate_id))
    # Reuse the historical matrix/infra/serving validators. This is only a
    # validation view; the executed settings and manifests always bind W7.
    view = dict(
        config,
        workload_version=historical.FROZEN_WORKLOAD_VERSION,
        generation=dict(generation, temperature=historical.FROZEN_TEMPERATURE),
    )
    historical.validate_authorized_mvl_config(view)
    binding = config.get("qualification_binding")
    if not isinstance(binding, dict) or set(binding) != {"run_label", "receipt_sha256"}:
        raise ConfigError("qualified baseline requires an exact freeze receipt binding")
    if not isinstance(binding["run_label"], str):
        raise ConfigError("freeze run label is malformed")
    require_safe_run_label(binding["run_label"])
    if not isinstance(binding["receipt_sha256"], str) or not _SHA256.fullmatch(
        binding["receipt_sha256"]
    ):
        raise ConfigError("freeze receipt SHA-256 is malformed")
    return config, hashlib.sha256(raw).hexdigest()


def _read(path: Path) -> tuple[dict, str]:
    if path.is_symlink() or not path.is_file():
        raise MvlError("qualification artifact is missing or is a symlink")
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, ValueError, UnicodeError) as exc:
        raise MvlError("qualification artifact is unreadable") from exc
    if not isinstance(document, dict):
        raise MvlError("qualification artifact is not an object")
    return document, hashlib.sha256(raw).hexdigest()


def _inside(cell: Path, name: object) -> Path:
    if not isinstance(name, str) or Path(name).name != name or name in {"", ".", ".."}:
        raise MvlError("qualification artifact reference is malformed")
    return cell / name


def authenticate_freeze(config: dict, *, results_dir: Path, run_tag: str, ledger: dict) -> dict:
    """Verify the bound receipt, all persisted observations, gates and session."""
    binding = config["qualification_binding"]
    candidate_id = str(config["candidate_id"])
    label = qualification.output_label(binding["run_label"], "freeze", candidate_id)
    cell = result_directory(results_dir, label)
    receipt, digest = _read(cell.parent / f"{label}-receipt.json")
    if digest != binding["receipt_sha256"]:
        raise MvlError("freeze receipt digest mismatch")
    required = {
        "workflow": "qualify-agent",
        "candidate_id": candidate_id,
        "stage": "freeze",
        "run_label": label,
        "stopped": False,
        "candidate_identity_sha256": config["candidate_identity_sha256"],
        "workload_version": config["workload_version"],
        "controller": "workflow-controller-v3",
        "evidence_contract": evidence_contract(),
        "blind_generalization_evidence": False,
    }
    if (
        any(receipt.get(key) != value for key, value in required.items())
        or receipt.get("stopped") is not False
        or receipt.get("blind_generalization_evidence") is not False
    ):
        raise MvlError("freeze receipt does not bind this completed qualified candidate")
    hashes = receipt.get("artifact_sha256")
    if not isinstance(hashes, dict) or set(hashes) != set(receipt.get("files") or []):
        raise MvlError("freeze receipt does not bind its complete artifact set")
    for name, expected_hash in hashes.items():
        _, actual_hash = _read(_inside(cell, name))
        if actual_hash != expected_hash:
            raise MvlError("freeze artifact digest mismatch")
    context = receipt.get("execution_context") or {}
    if (
        context.get("run_tag") != run_tag
        or context.get("canonical_commit") != config["canonical_commit"]
        or context.get("resource_identity_sha256") != resource_identity_sha256(ledger["resources"])
    ):
        raise MvlError("freeze qualification belongs to another execution session")
    if list(cell.glob("*.failure.json")):
        raise MvlError("freeze qualification has a failure record")
    files = list(cell.glob("*.result.json"))
    if len(files) != 1:
        raise MvlError("freeze qualification must have exactly one result")
    result, result_sha = _read(files[0])
    if files[0].name not in hashes:
        raise MvlError("freeze result is absent from the receipt")
    if result.get("manifest_ref") not in hashes:
        raise MvlError("freeze manifest is absent from the receipt")
    manifest, manifest_sha = _read(_inside(cell, result.get("manifest_ref")))
    validate_benchmark_result(result)
    validate_run_manifest(manifest)
    if (
        manifest["is_synthetic_example"] is not False
        or manifest["execution_mode"] != "gpu"
        or manifest["git"]["commit"] != config["canonical_commit"]
        or manifest["git"]["dirty"]
        or manifest["workload"]["version"] != config["workload_version"]
        or manifest["workload"]["profile"] != "interactive"
        or manifest["workload"]["concurrency"] != 1
        or manifest["workload"]["tasks_per_repetition"] != 200
        or manifest["workload"]["scenario_count"] != 10
        or manifest["workload"]["evaluator_version"] != "3.1.0"
        or manifest["cloud"]["region"] != historical.FROZEN_REGION
        or manifest["cloud"]["instance_type"] != historical.FROZEN_INSTANCE_TYPE
        or manifest["cloud"]["comparison_mode"] != "provider-native"
        or manifest["host"]["gpu_model"]
        not in {historical.FROZEN_GPU, "NVIDIA RTX PRO 6000 Blackwell Server Edition"}
        or manifest["host"]["gpu_count"] != 1
        or manifest["timing_conditions"]["repetitions_planned"] != 1
        or manifest["timing_conditions"]["repetition_index"] != 1
        or manifest["generation"] != config["generation"]
    ):
        raise MvlError("freeze manifest provenance does not match the qualified baseline")
    for key in ("artifact", "revision", "artifact_hash", "precision"):
        if manifest["model"].get(key) != config["model"].get(key):
            raise MvlError("freeze model pins do not match the qualified baseline")
    for key in ("engine", "engine_version"):
        if manifest["serving"].get(key) != config["serving"].get(key):
            raise MvlError("freeze serving pins do not match the qualified baseline")
    # Docker reports repo@sha256, while configs may retain tag@sha256.
    # Compare the pinned digest, exactly as live provenance verification does.
    observed_digest = str(manifest["serving"].get("container_digest", "")).split("@")[-1]
    approved_digest = str(config["serving"].get("container_digest", "")).split("@")[-1]
    if observed_digest != approved_digest or observed_digest != historical.FROZEN_VLLM_IMAGE_DIGEST:
        raise MvlError("freeze serving pins do not match the qualified baseline")
    refs = result["observations"]
    if refs.get("warmup_count") != 200 or not refs.get("warmup_file"):
        raise MvlError("freeze qualification requires one complete 200-task warmup")
    if refs.get("measured_file") not in hashes:
        raise MvlError("freeze measured observations are absent from the receipt")
    measured, measured_sha = _read(_inside(cell, refs.get("measured_file")))
    if not refs.get("persisted") or measured_sha != refs.get("measured_sha256"):
        raise MvlError("freeze observation digest mismatch")
    validate_task_observations(measured)
    warmup = None
    if refs.get("warmup_file"):
        if refs["warmup_file"] not in hashes:
            raise MvlError("freeze warmup observations are absent from the receipt")
        warmup, warmup_sha = _read(_inside(cell, refs["warmup_file"]))
        if warmup_sha != refs.get("warmup_sha256"):
            raise MvlError("freeze warmup observation digest mismatch")
        validate_task_observations(warmup)
        qualification.require_complete_stage_evidence("freeze", warmup["observations"])
    validate_result_semantics(
        manifest, result, measured_observations=measured, warmup_observations=warmup
    )
    outcomes = []
    scenarios = catalog()
    for row in measured["observations"]:
        scenario = scenarios.get(row.get("template_id"))
        if scenario is None:
            raise MvlError("freeze observation names an unknown template")
        replay = evaluate(scenario, _execution(row, scenario))
        outcomes.append(dict(row, evaluation={"success": replay.success}))
    qualification.require_complete_stage_evidence("freeze", outcomes)
    metrics = qualification.compute_catalog_metrics(
        outcomes,
        provenance_ok=True,
        verification_ok=True,
        expected_template_ids=qualification.stage_spec("freeze")["template_ids"],
    )
    gates = qualification.evaluate_stage_thresholds("freeze", metrics)
    if gates["stopped"] or receipt.get("gates") != gates:
        raise MvlError("freeze qualification did not pass the study-entry gate")
    duration_s = max((row["ended_offset_ms"] for row in measured["observations"]), default=0) / 1000
    return {
        "candidate_id": candidate_id,
        "candidate_identity_sha256": config["candidate_identity_sha256"],
        "receipt_sha256": digest,
        "result_sha256": result_sha,
        "manifest_sha256": manifest_sha,
        "study_entry_passed": True,
        "measured_duration_s": duration_s,
    }


def _mvl_stop(message: str, *, results_dir: Path | None, run_label: str) -> int:
    if results_dir is not None:
        record = dict(
            historical.failure_record(message), workflow=WORKFLOW, artifact_family=ARTIFACT_FAMILY
        )
        write_private_json(results_dir / ARTIFACT_FAMILY / f"{run_label}-failure.json", record)
    print(f"BLOCKED: {message}", file=sys.stderr)
    return 1


def execute(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle, provenance, realbench, telemetry
    from blackwell_lab.cloud import qualified_mvl as mvl
    from blackwell_lab.cloud.cli import (
        _git_head,
        _repo_root,
        _resolve_real_results_dir,
        _tree_clean,
    )
    from blackwell_lab.workload.model_client import GenerationSettings
    from blackwell_lab.workload.openai_client import OpenAICompatibleClient

    validate_only = bool(getattr(args, "validate_only", False))
    if not validate_only:
        lifecycle.refuse_hosted_execution()
    try:
        run_label = mvl.require_safe_run_label(args.run_label)
        config_path = mvl.require_external_config(args.config, _repo_root())
        config, config_sha256 = mvl.load_mvl_config(config_path)
    except ConfigError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1

    expected = MVL_APPROVAL_TEMPLATE.format(
        run_tag=args.run_tag,
        run_label=run_label,
        candidate_id=config["candidate_id"],
        config_sha256=config_sha256,
    )
    if not validate_only and (args.approve or "") != expected:
        print(
            "BLOCKED: the MVL requires the exact owner approval phrase "
            f"(expected verbatim: {expected!r}); nothing was executed.",
            file=sys.stderr,
        )
        return 1

    try:
        if not validate_only:
            mvl.require_clean_canonical_commit(
                config, git_head=_git_head(), tree_clean=_tree_clean()
            )
    except mvl.MvlError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1

    results_dir = _resolve_real_results_dir()
    paths = lifecycle.lifecycle_paths(results_dir, args.run_tag)
    if not paths.ledger_path.is_file():
        print(
            "BLOCKED: the resource ledger is missing for this run. Run "
            "'blackwell-cloud init', apply, and 'blackwell-cloud reconcile' "
            "before any MVL execution.",
            file=sys.stderr,
        )
        return 1
    ledger = lifecycle.load_ledger(paths.ledger_path)
    blockers = lifecycle.pilot_blockers(ledger, pending=lifecycle.has_pending(paths))
    if blockers:
        print(
            "BLOCKED: the MVL cannot run until every lifecycle gate passes. " + "; ".join(blockers),
            file=sys.stderr,
        )
        return 1

    try:
        proof = mvl.authenticate_freeze(
            config, results_dir=results_dir, run_tag=args.run_tag, ledger=ledger
        )
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, (mvl.MvlError, ConfigError))
            else "freeze verification failed"
        )
        return _mvl_stop(message, results_dir=None, run_label=run_label)
    if validate_only:
        print(
            json.dumps(
                {
                    "workflow": WORKFLOW,
                    "mode": "validate-only",
                    "executed": False,
                    "model_client_constructed": False,
                    "endpoint_contacted": False,
                    "config_sha256": config_sha256,
                    "qualification_binding": proof,
                    "counts": mvl.measured_counts(),
                },
                indent=2,
            )
        )
        return 0

    endpoint = config["endpoint"]

    def _provenance() -> object:
        return provenance.verify_live_provenance(
            run_tag=args.run_tag,
            approved=config,
            ledger=ledger,
            artifact_dir=Path(config["model_verification"]["artifact_dir"]),
            digest_manifest=Path(config["model_verification"]["digest_manifest"]),
            serving_base_url=endpoint["base_url"],
        )

    def _run_cell(
        *,
        profile: str,
        concurrency: int,
        repetitions: int,
        warmup: int,
        tasks: int,
        label: str,
    ):
        if time.monotonic() >= session_deadline:
            raise MvlError("the approved six-hour session budget has expired")
        observed = _provenance()
        if observed.model_artifact_hash != mvl.FROZEN_MODEL_ARTIFACT_HASH:
            raise mvl.MvlError("live model digest drifted from the frozen aggregate")
        digest = str(observed.container_digest)
        if digest not in {
            mvl.FROZEN_CONTAINER_DIGEST,
            mvl.FROZEN_VLLM_IMAGE_DIGEST,
        } and not digest.endswith(mvl.FROZEN_VLLM_IMAGE_DIGEST):
            raise mvl.MvlError("live container digest drifted from the frozen image")
        host = {**observed.host_facts, **observed.gpu_facts}
        model = dict(config["model"])
        model["artifact_hash"] = observed.model_artifact_hash
        spec = realbench.RealRunSpec(
            profile_name=profile,
            concurrency=concurrency,
            comparison_mode=mvl.FROZEN_COMPARISON_MODE,
            instance_type=observed.instance["instance_type"],
            region=observed.instance["region"],
            list_price_usd_per_hour=config["cloud"]["list_price_usd_per_hour"],
            price_source_date=config["cloud"]["price_source_date"],
            model=model,
            engine=config["serving"]["engine"],
            engine_version=observed.engine_version,
            container_digest=observed.container_digest,
            container_cuda_runtime_version=observed.container_cuda_runtime_version,
            repetitions=repetitions,
            warmup_passes=warmup,
            tasks_per_repetition=tasks,
            seed=mvl.FROZEN_SEED,
            run_label=label,
            generation=GenerationSettings(
                temperature=mvl.FROZEN_TEMPERATURE,
                top_p=mvl.FROZEN_TOP_P,
                seed=mvl.FROZEN_SEED,
                reasoning_mode=mvl.FROZEN_REASONING_MODE,
                workload_version=config["workload_version"],
            ),
            workload_version=config["workload_version"],
            controller="workflow-controller-v3",
            artifact_family=mvl.ARTIFACT_FAMILY,
            study_entry_binding={
                key: value for key, value in proof.items() if key != "measured_duration_s"
            },
        )
        realbench._validate_spec(spec)
        client = OpenAICompatibleClient(
            endpoint["base_url"],
            endpoint["model"],
            api_key_env=endpoint.get("api_key_env"),
        )
        return realbench.run_real_cell(
            spec,
            SessionBudgetClient(client, session_deadline),
            host=host,
            sampler_factory=telemetry.GpuSamplerThread,
            results_dir=results_dir,
        )

    try:
        events = (
            (json.loads(paths.session_path.read_text(encoding="utf-8")).get("events") or [])
            if paths.session_path.is_file()
            else []
        )
        starts = [event.get("at_utc") for event in events if event.get("event") == "provisioned"]
        if len(starts) != 1:
            raise MvlError("the session requires a verified provisioning timestamp")
        provisioned = datetime.fromisoformat(str(starts[0]))
        if provisioned.tzinfo is None or provisioned > datetime.now(timezone.utc):
            raise MvlError("the session requires a verified provisioning timestamp")
        remaining = historical.remaining_session_seconds(events)
        session_deadline = time.monotonic() + remaining
        # The projection reuses the existing conservative MVL work estimate,
        # scaled from the verified 200-task freeze instead of another canary.
        projection = historical.project_measured_seconds(proof["measured_duration_s"] / 20)
        historical.refuse_if_session_exceeded(projection, remaining)
    except Exception:
        return _mvl_stop(
            "qualified baseline exceeds the remaining session budget",
            results_dir=results_dir,
            run_label=run_label,
        )
    summaries = []
    lifecycle.record_session_event(
        paths,
        "qualified_baseline_started",
        {"run_label": run_label, "config_sha256": config_sha256},
    )
    try:
        for profile, concurrency in mvl.AUTHORIZED_MVL_CELLS:
            label = mvl.output_label(run_label, mvl.cell_output_suffix(profile, concurrency))
            records = _run_cell(
                profile=profile,
                concurrency=concurrency,
                repetitions=mvl.FROZEN_REPETITIONS,
                warmup=mvl.FROZEN_WARMUP_PASSES,
                tasks=mvl.FROZEN_TASKS_PER_REPETITION,
                label=label,
            )
            if len(records) != mvl.FROZEN_REPETITIONS:
                raise mvl.MvlError("each measured cell must persist exactly three repetitions")
            summaries.append(
                {
                    "kind": "measured",
                    "profile": profile,
                    "concurrency": concurrency,
                    "repetitions": len(records),
                    "tasks_per_repetition": mvl.FROZEN_TASKS_PER_REPETITION,
                    "run_label": label,
                    "run_ids": [record.run_id for record in records],
                    "files": [name for record in records for name in record.written_files],
                }
            )
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, (mvl.MvlError, ConfigError))
            else f"measured cell aborted: {type(exc).__name__}"
        )
        return _mvl_stop(message, results_dir=results_dir, run_label=run_label)

    lifecycle.record_session_event(
        paths, "qualified_baseline_completed", {"cells": len(mvl.AUTHORIZED_MVL_CELLS)}
    )
    print(
        json.dumps(
            {
                "workflow": WORKFLOW,
                "run_label": run_label,
                "comparison_mode": "provider-native",
                "counts": mvl.measured_counts(),
                "cells": summaries,
                "qualification_binding": proof,
                "note": mvl.TEARDOWN_FROM_LAPTOP_NOTE,
            },
            indent=2,
        )
    )
    return 0
