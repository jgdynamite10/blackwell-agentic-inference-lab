"""``blackwell-cloud`` — Phase 3 readiness and lifecycle workflows (CLI-first).

Subcommands map one-to-one to the separated workflows required by Phase 3A:

- ``readiness``       offline validation only: no cloud access, no credentials.
- ``init``            configure the run's EXTERNAL terraform backend/state
                      and TF_DATA_DIR under LAB_RESULTS_DIR.
- ``plan``            create the reviewed SAVED plan (binary + redacted text
                      + hashed metadata), all outside Git.
- ``apply``           apply exactly the reviewed saved plan — requires the
                      approval phrase naming the run tag AND plan digest;
                      refused in hosted/CI environments; reconciles after.
- ``reconcile``       inspect external state vs provider, update the ledger.
- ``pilot``           the short, owner-approved compatibility/headroom pilot
                      (RunMode.REAL): blocked until reconciliation is clean,
                      provider-native only, live provenance verified first.
- ``full-baseline``   DISABLED: the full 12-cell baseline remains unauthorized
                      (only the bounded D-0014 pilot is authorized).
- ``verify-results``  external verification of persisted genuine results;
                      an empty directory is a FAILURE unless --allow-empty.
- ``teardown-plan``   identity-verified, saved destroy plan from the ledger.
- ``destroy``         destroy exactly the verified saved destroy plan —
                      separate approval phrase naming the destroy-plan
                      digest; success only after read-only deletion
                      confirmation.
- ``orphan-report``   read-only sweep of project-tagged resources vs ledger.
- ``session-summary`` observed billable duration and estimated session cost.

Privacy rules: absolute private paths (``LAB_RESULTS_DIR``, ledger locations)
are never printed — output references safe relative filenames only. Tokens
are read from the environment at call time and never echoed. Unexpected
exceptions never leak arbitrary text: only sanitized messages from this
project's own error types are printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from blackwell_lab.cloud.bootstrap_pins import validate_candidate_pins
from blackwell_lab.cloud.lifecycle import TERRAFORM_LOCKFILE_READONLY
from blackwell_lab.paths import ResultsLocationError, RunMode, resolve_results_dir
from blackwell_lab.schemas import (
    validate_benchmark_result,
    validate_run_manifest,
    validate_task_observations,
)
from blackwell_lab.workload.validation import (
    ConfigError,
    SemanticValidationError,
    validate_result_semantics,
)

#: The full 12-cell Phase 3 baseline is NOT authorized (D-0014 authorizes
#: only the bounded compatibility/headroom pilot). Enabling this constant
#: requires a separate owner authorization recorded in the decision log and
#: reviewed in a pull request — never a runtime flag or environment variable.
FULL_BASELINE_AUTHORIZED = False

PILOT_APPROVAL_TEMPLATE = (
    "I approve the short Akamai pilot for run {run_tag} ({run_label}) "
    "using config sha256:{config_sha256}"
)

AUTHORIZED_PILOT_CELLS = (
    ("interactive", 1),
    ("batch-heavy", 4),
    ("batch-heavy", 8),
)
AUTHORIZED_PILOT_PRECISION = "bf16"
AUTHORIZED_PILOT_SERVING_MODE = "provider-native"
AUTHORIZED_PILOT_GPU = "RTX PRO 6000 Blackwell"
AUTHORIZED_PILOT_REGION = "us-sea"
AUTHORIZED_PILOT_INSTANCE_TYPE = "g3-gpu-rtxpro6000-blackwell-1"
AUTHORIZED_WARMUP_PASSES = 1
AUTHORIZED_REPETITIONS = 1
AUTHORIZED_TASKS_PER_REPETITION = 20


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _terraform_dir() -> Path:
    return _repo_root() / "infra" / "akamai"


def _bootstrap_dir() -> Path:
    return _terraform_dir() / "bootstrap"


# -- readiness ---------------------------------------------------------------


def _check_terraform_files() -> dict:
    directory = _terraform_dir()
    required = [
        "versions.tf",
        "variables.tf",
        "main.tf",
        "outputs.tf",
        ".gitignore",
        ".terraform.lock.hcl",
    ]
    missing = [name for name in required if not (directory / name).is_file()]
    if missing:
        return {"status": "failed", "detail": f"missing terraform files: {missing}"}
    versions = (directory / "versions.tf").read_text(encoding="utf-8")
    if 'required_version = "= 1.9.8"' not in versions:
        return {
            "status": "failed",
            "detail": "versions.tf must pin Terraform CLI exactly 1.9.8",
        }
    gitignore = (directory / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("*.tfstate", "*.tfvars", ".terraform"):
        if pattern not in gitignore:
            return {
                "status": "failed",
                "detail": f"infra/akamai/.gitignore must exclude {pattern}",
            }
    return {"status": "ok", "detail": "terraform configuration and state exclusions present"}


def _check_terraform_static() -> dict:
    terraform = shutil.which("terraform")
    if terraform is None:
        return {
            "status": "skipped",
            "detail": (
                "terraform binary not installed locally; the dedicated "
                "terraform-validation CI check runs fmt/init/validate on "
                "every pull request, so validation cannot silently disappear"
            ),
        }
    directory = _terraform_dir()
    fmt = subprocess.run(
        [terraform, "fmt", "-check", "-recursive"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if fmt.returncode != 0:
        return {"status": "failed", "detail": "terraform fmt -check reported formatting drift"}
    with tempfile.TemporaryDirectory(prefix="bwlab-tf-validate-") as tf_data:
        env = {**os.environ, "TF_DATA_DIR": tf_data, "TF_IN_AUTOMATION": "1"}
        init = subprocess.run(
            [
                terraform,
                "init",
                "-backend=false",
                "-input=false",
                TERRAFORM_LOCKFILE_READONLY,
            ],
            cwd=directory,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if init.returncode != 0:
            return {
                "status": "failed",
                "detail": (
                    "terraform init -backend=false failed (provider download "
                    "or configuration error)"
                ),
            }
        validate = subprocess.run(
            [terraform, "validate", "-no-color"],
            cwd=directory,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    if validate.returncode != 0:
        return {"status": "failed", "detail": "terraform validate failed"}
    return {"status": "ok", "detail": "terraform fmt -check, init -backend=false, validate passed"}


def _check_bootstrap_scripts() -> dict:
    directory = _bootstrap_dir()
    scripts = sorted(directory.glob("*.sh")) if directory.is_dir() else []
    names = {s.name for s in scripts}
    for required in ("bootstrap.sh", "fetch-model.sh", "watchdog.sh", "pins.sh"):
        if required not in names:
            return {"status": "failed", "detail": f"bootstrap script missing: {required}"}
    bash = shutil.which("bash")
    if bash is None:
        return {"status": "skipped", "detail": "bash not available for syntax checks"}
    for script in scripts:
        check = subprocess.run(
            [bash, "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if check.returncode != 0:
            return {"status": "failed", "detail": f"bash -n failed for {script.name}"}
    return {"status": "ok", "detail": f"{len(scripts)} bootstrap script(s) pass bash -n"}


def _check_bootstrap_pins() -> dict:
    env_example = _bootstrap_dir() / "bootstrap.env.example"
    if not env_example.is_file():
        return {"status": "failed", "detail": "bootstrap.env.example is missing"}
    content = env_example.read_text(encoding="utf-8")
    problems = validate_candidate_pins(content)
    if problems:
        return {"status": "failed", "detail": f"bootstrap.env.example pin errors: {problems}"}
    return {"status": "ok", "detail": "bootstrap pin template declares verified candidate pins"}


def _check_python_modules() -> dict:
    try:
        import blackwell_lab.cloud.lifecycle
        import blackwell_lab.cloud.preflight
        import blackwell_lab.cloud.provenance
        import blackwell_lab.cloud.realbench
        import blackwell_lab.cloud.telemetry
        import blackwell_lab.workload.openai_client  # noqa: F401
    except Exception as exc:  # pragma: no cover - import failure is the finding
        return {"status": "failed", "detail": f"module import failed: {type(exc).__name__}"}
    return {"status": "ok", "detail": "cloud/workload modules import cleanly"}


def _check_examples_validate() -> dict:
    examples = _repo_root() / "examples"
    try:
        validate_run_manifest(
            json.loads((examples / "example-run-manifest.json").read_text(encoding="utf-8"))
        )
        validate_benchmark_result(
            json.loads((examples / "example-benchmark-result.json").read_text(encoding="utf-8"))
        )
        validate_task_observations(
            json.loads((examples / "example-task-observations.json").read_text(encoding="utf-8"))
        )
    except Exception as exc:
        return {"status": "failed", "detail": f"example validation failed: {type(exc).__name__}"}
    return {"status": "ok", "detail": "schemas load and synthetic examples validate"}


def cmd_readiness(_args: argparse.Namespace) -> int:
    """Offline readiness validation: no cloud access, no credentials."""
    checks = {
        "terraform_files": _check_terraform_files(),
        "terraform_static": _check_terraform_static(),
        "bootstrap_scripts": _check_bootstrap_scripts(),
        "bootstrap_pins": _check_bootstrap_pins(),
        "python_modules": _check_python_modules(),
        "schemas_and_examples": _check_examples_validate(),
    }
    failed = sorted(name for name, check in checks.items() if check["status"] == "failed")
    report = {
        "workflow": "readiness",
        "cloud_access": "none (offline validation only)",
        "credentials_required": False,
        "checks": checks,
        "failed": failed,
        "ok": not failed,
    }
    print(json.dumps(report, indent=2))
    return 0 if not failed else 1


# -- lifecycle wrappers -------------------------------------------------------


def _resolve_real_results_dir() -> Path:
    resolved = resolve_results_dir(mode=RunMode.REAL)
    if resolved is None:  # pragma: no cover - REAL mode never returns None
        raise ResultsLocationError("LAB_RESULTS_DIR did not resolve")
    return resolved


def _paths_for(run_tag: str):
    from blackwell_lab.cloud import lifecycle

    return lifecycle.lifecycle_paths(_resolve_real_results_dir(), run_tag)


def _read_only_provider_access() -> tuple:
    """(fetch, token) when a read-only token is locally available, else (None, None)."""
    token = os.environ.get("LINODE_TOKEN")
    if not token:
        return None, None
    from blackwell_lab.cloud.preflight import get_json

    return get_json, token


def cmd_init(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle

    paths = _paths_for(args.run_tag)
    report = lifecycle.init_backend(args.run_tag, paths=paths)
    print(json.dumps(report, indent=2))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle

    paths = _paths_for(args.run_tag)
    meta = lifecycle.save_plan(args.run_tag, stage="apply", paths=paths)
    approval = lifecycle.APPLY_APPROVAL_TEMPLATE.format(
        run_tag=args.run_tag, plan_sha256=meta["plan_sha256"]
    )
    print(
        json.dumps(
            {
                "workflow": "plan",
                "run_tag": args.run_tag,
                "plan_sha256": meta["plan_sha256"],
                "terraform_version": meta["terraform_version"],
                "created_at_utc": meta["created_at_utc"],
                "actions": meta["actions"],
                "redacted_plan_file": paths.plan_text_path("apply").name,
                "apply_approval_phrase": approval,
                "note": (
                    "The saved binary plan, redacted review text, and hashed "
                    "metadata were written to the run's external lifecycle "
                    "directory (paths not printed). Review the redacted plan, "
                    "then apply with the exact approval phrase above."
                ),
            },
            indent=2,
        )
    )
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle

    paths = _paths_for(args.run_tag)
    fetch, token = _read_only_provider_access()
    report = lifecycle.apply(
        args.run_tag,
        args.approve or "",
        paths=paths,
        fetch=fetch,
        token=token,
    )
    print(
        json.dumps(
            {
                "applied": True,
                **report,
                "note": (
                    "Exactly the reviewed saved plan was applied and "
                    "reconciliation is clean. Billing has started: schedule "
                    "the teardown before leaving the session."
                ),
            },
            indent=2,
        )
    )
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle

    paths = _paths_for(args.run_tag)
    fetch, token = _read_only_provider_access()
    report = lifecycle.reconcile(args.run_tag, paths=paths, fetch=fetch, token=token)
    print(json.dumps(report, indent=2))
    return 0 if report["reconciled"] else 1


def cmd_teardown_plan(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle

    paths = _paths_for(args.run_tag)
    ledger = lifecycle.load_ledger(paths.ledger_path)
    fetch, token = _read_only_provider_access()
    meta = lifecycle.plan_destroy(args.run_tag, ledger, paths=paths, fetch=fetch, token=token)
    approval = lifecycle.DESTROY_APPROVAL_TEMPLATE.format(
        run_tag=args.run_tag, plan_sha256=meta["plan_sha256"]
    )
    print(
        json.dumps(
            {
                "workflow": "teardown-plan",
                "run_tag": args.run_tag,
                "destroy_plan_sha256": meta["plan_sha256"],
                "targets": meta["resource_addresses"],
                "redacted_plan_file": paths.plan_text_path("destroy").name,
                "destroy_approval_phrase": approval,
                "note": (
                    "Identity verification passed (ledger vs state vs provider API). "
                    "The saved destroy plan targets ONLY the ledger's recorded "
                    "resources. Review the redacted plan, then destroy with the "
                    "exact approval phrase above."
                ),
            },
            indent=2,
        )
    )
    return 0


def cmd_destroy(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle

    paths = _paths_for(args.run_tag)
    ledger = lifecycle.load_ledger(paths.ledger_path)
    fetch, token = _read_only_provider_access()
    report = lifecycle.destroy(
        args.run_tag,
        args.approve or "",
        ledger,
        paths=paths,
        fetch=fetch,
        token=token,
    )
    print(
        json.dumps(
            {
                **report,
                "run_tag": args.run_tag,
                "next": "Run 'blackwell-cloud orphan-report' now.",
            },
            indent=2,
        )
    )
    return 0


def cmd_orphan_report(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle
    from blackwell_lab.cloud.artifacts import write_private_json
    from blackwell_lab.cloud.preflight import get_json

    token = os.environ.get("LINODE_TOKEN")
    if not token:
        print(
            "BLOCKED: LINODE_TOKEN is not set. The orphan report is a "
            "read-only, locally run check; set a read-only token and retry.",
            file=sys.stderr,
        )
        return 1
    ledger = None
    if args.run_tag:
        paths = _paths_for(args.run_tag)
        ledger = lifecycle.load_ledger(paths.ledger_path)
    report = lifecycle.orphan_report(token, fetch=get_json, ledger=ledger)
    # Sanitized terminal output: labels/kinds/regions only. The complete
    # report (with resource ids) is written atomically (0700/0600) to the
    # external private dir.
    reports_dir = _resolve_real_results_dir() / "orphan-reports"
    stamp = report["generated_at_utc"].replace(":", "").replace("+", "Z")
    report_name = f"orphan-report-{stamp}.json"
    write_private_json(reports_dir / report_name, report)
    print(
        json.dumps(
            {
                "clean": report["clean"],
                "finding_count": len(report["findings"]),
                "findings": [
                    {
                        "kind": f["kind"],
                        "label": f["label"],
                        "region": f["region"],
                        "in_ledger": f["in_ledger"],
                        "suspicious_untagged_gpu": f["suspicious_untagged_gpu"],
                    }
                    for f in report["findings"]
                ],
                "full_report_file": report_name,
                "note": report["note"],
            },
            indent=2,
        )
    )
    return 0 if report["clean"] else 1


def cmd_session_summary(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle

    paths = _paths_for(args.run_tag)
    summary = lifecycle.session_summary(paths, hourly_price_usd=args.hourly_price)
    print(json.dumps(summary, indent=2))
    return 0


# -- pilot / full baseline ----------------------------------------------------


def _path_is_inside_repo(path: Path, repo: Path) -> bool:
    try:
        path.relative_to(repo)
        return True
    except ValueError:
        return False


def require_external_pilot_config(path_argument: str) -> Path:
    """Pilot config must be an existing absolute path outside this repository."""
    path = Path(path_argument)
    if not path.is_absolute() or not path.is_file():
        raise ConfigError("pilot config must be an existing absolute path outside the repository")
    repo = _repo_root()
    resolved = path.resolve()
    if _path_is_inside_repo(path, repo) or _path_is_inside_repo(resolved, repo):
        raise ConfigError("pilot config must be an existing absolute path outside the repository")
    return resolved


def validate_authorized_pilot_config(config: dict) -> None:
    """Rejects any config that is not the locked D-0014 diagnostic envelope."""
    required = (
        "endpoint",
        "cloud",
        "model",
        "serving",
        "host",
        "comparison_mode",
        "model_verification",
    )
    for key in required:
        if key not in config:
            raise ConfigError(f"pilot config is missing required section: {key}")
    verification = config.get("model_verification")
    if not isinstance(verification, dict):
        raise ConfigError("pilot config is missing required section: model_verification")
    for key in ("artifact_dir", "digest_manifest"):
        if not verification.get(key):
            raise ConfigError(f"pilot config is missing model_verification.{key}")

    if config.get("comparison_mode") != AUTHORIZED_PILOT_SERVING_MODE:
        raise ConfigError(
            "the pilot runs only in provider-native mode. "
            "Controlled-resource labeling requires the joint 14-vCPU/100-GiB "
            "serving-plus-benchmark limit to be genuinely enforced and "
            "observed, which is not yet implemented (decision D-0013)."
        )

    cloud = config.get("cloud")
    if not isinstance(cloud, dict):
        raise ConfigError("pilot config is missing required section: cloud")
    if cloud.get("instance_type") != AUTHORIZED_PILOT_INSTANCE_TYPE:
        raise ConfigError(
            f"pilot config cloud.instance_type must equal {AUTHORIZED_PILOT_INSTANCE_TYPE}"
        )
    if cloud.get("region") != AUTHORIZED_PILOT_REGION:
        raise ConfigError(f"pilot config cloud.region must equal {AUTHORIZED_PILOT_REGION}")

    model = config.get("model")
    if not isinstance(model, dict):
        raise ConfigError("pilot config is missing required section: model")
    if model.get("precision") != AUTHORIZED_PILOT_PRECISION:
        raise ConfigError(f"pilot config model.precision must equal {AUTHORIZED_PILOT_PRECISION}")

    if config.get("expected_gpu_model") != AUTHORIZED_PILOT_GPU:
        raise ConfigError(f"pilot config expected_gpu_model must equal {AUTHORIZED_PILOT_GPU}")

    if config.get("warmup_passes") != AUTHORIZED_WARMUP_PASSES:
        raise ConfigError("pilot config warmup_passes must equal 1")
    if config.get("repetitions") != AUTHORIZED_REPETITIONS:
        raise ConfigError("pilot config repetitions must equal 1")
    if config.get("tasks_per_repetition") != AUTHORIZED_TASKS_PER_REPETITION:
        raise ConfigError("pilot config tasks_per_repetition must equal 20")

    raw_cells = config.get("cells")
    if not isinstance(raw_cells, list):
        raise ConfigError("pilot config must declare exactly the three authorized D-0014 cells")
    normalized: list[tuple[object, object]] = []
    for cell in raw_cells:
        if not isinstance(cell, dict):
            raise ConfigError("pilot config must declare exactly the three authorized D-0014 cells")
        normalized.append((cell.get("profile"), cell.get("concurrency")))
    if tuple(normalized) != AUTHORIZED_PILOT_CELLS:
        raise ConfigError(
            "pilot config cells must be exactly interactive/1, batch-heavy/4, "
            "and batch-heavy/8 with no missing, additional, or duplicate cells"
        )


def _load_pilot_config_bytes(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("pilot config is not valid JSON") from exc
    if not isinstance(config, dict):
        raise ConfigError("pilot config must be a JSON object")
    validate_authorized_pilot_config(config)
    return config, digest


def cmd_pilot(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle, provenance, realbench, telemetry
    from blackwell_lab.workload.openai_client import OpenAICompatibleClient

    lifecycle.refuse_hosted_execution()
    run_label = args.run_label
    try:
        config_path = require_external_pilot_config(args.config)
        config, config_sha256 = _load_pilot_config_bytes(config_path)
    except ConfigError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    expected = PILOT_APPROVAL_TEMPLATE.format(
        run_tag=args.run_tag,
        run_label=run_label,
        config_sha256=config_sha256,
    )
    if (args.approve or "") != expected:
        print(
            "BLOCKED: the pilot requires the exact owner approval phrase "
            f"(expected verbatim: {expected!r}); nothing was executed.",
            file=sys.stderr,
        )
        return 1

    results_dir = _resolve_real_results_dir()

    # Lifecycle gate: the pilot is blocked until reconciliation is fully clean.
    paths = lifecycle.lifecycle_paths(results_dir, args.run_tag)
    if not paths.ledger_path.is_file():
        print(
            "BLOCKED: the resource ledger is missing for this run. Run "
            "'blackwell-cloud init', apply, and 'blackwell-cloud reconcile' "
            "before any pilot execution.",
            file=sys.stderr,
        )
        return 1
    ledger = lifecycle.load_ledger(paths.ledger_path)
    blockers = lifecycle.pilot_blockers(ledger, pending=lifecycle.has_pending(paths))
    if blockers:
        print(
            "BLOCKED: the pilot cannot run until every lifecycle gate passes. "
            + "; ".join(blockers)
            + ". Run 'blackwell-cloud reconcile' with a read-only LINODE_TOKEN "
            "until reconciliation reports clean.",
            file=sys.stderr,
        )
        return 1

    endpoint = config["endpoint"]
    lifecycle.record_session_event(
        paths,
        "pilot_started",
        {"run_label": run_label, "config_sha256": config_sha256},
    )
    cells = [
        {"profile": profile, "concurrency": concurrency}
        for profile, concurrency in AUTHORIZED_PILOT_CELLS
    ]
    summaries = []
    for index, cell in enumerate(cells, start=1):
        # Live provenance: re-observed immediately before EVERY cell; the first
        # cell's observations are never reused for later cells.
        observed = provenance.verify_live_provenance(
            run_tag=args.run_tag,
            approved=config,
            ledger=ledger,
            artifact_dir=Path(config["model_verification"]["artifact_dir"]),
            digest_manifest=Path(config["model_verification"]["digest_manifest"]),
            serving_base_url=endpoint["base_url"],
        )
        host = {**observed.host_facts, **observed.gpu_facts}
        model = dict(config["model"])
        model["artifact_hash"] = observed.model_artifact_hash
        spec = realbench.RealRunSpec(
            profile_name=cell["profile"],
            concurrency=cell["concurrency"],
            comparison_mode=AUTHORIZED_PILOT_SERVING_MODE,
            instance_type=observed.instance["instance_type"],
            region=observed.instance["region"],
            list_price_usd_per_hour=config["cloud"]["list_price_usd_per_hour"],
            price_source_date=config["cloud"]["price_source_date"],
            model=model,
            engine=config["serving"]["engine"],
            engine_version=observed.engine_version,
            container_digest=observed.container_digest,
            container_cuda_runtime_version=observed.container_cuda_runtime_version,
            repetitions=AUTHORIZED_REPETITIONS,
            warmup_passes=AUTHORIZED_WARMUP_PASSES,
            tasks_per_repetition=AUTHORIZED_TASKS_PER_REPETITION,
            run_label=f"{run_label}-cell{index}",
        )
        # Construct the client only after config, approval, ledger, and
        # live-provenance checks for this cell have passed.
        client = OpenAICompatibleClient(
            endpoint["base_url"],
            endpoint["model"],
            api_key_env=endpoint.get("api_key_env"),
        )
        records = realbench.run_real_cell(
            spec,
            client,
            host=host,
            sampler_factory=telemetry.GpuSamplerThread,
            results_dir=results_dir,
        )
        for record in records:
            summaries.append(
                {
                    "run_id": record.run_id,
                    "profile": cell["profile"],
                    "concurrency": cell["concurrency"],
                    "tasks": record.result["tasks"],
                    "files": list(record.written_files),
                }
            )
    lifecycle.record_session_event(paths, "pilot_completed", {"cells": len(cells)})
    print(
        json.dumps(
            {
                "workflow": "pilot",
                "run_label": run_label,
                "comparison_mode": "provider-native",
                "provenance_verified": True,
                "cells": summaries,
                "note": (
                    "Pilot output calibrates realized task latency and validates "
                    "compatibility/headroom; it never freezes the baseline and is "
                    "never published. Results were persisted to the external "
                    "private results directory (path not printed)."
                ),
            },
            indent=2,
        )
    )
    return 0


def cmd_full_baseline(_args: argparse.Namespace) -> int:
    if not FULL_BASELINE_AUTHORIZED:
        print(
            "DISABLED: the full 12-cell Akamai baseline is not authorized. "
            "Decision D-0014 authorizes only the bounded compatibility/"
            "headroom pilot. The full baseline remains disabled until the "
            "owner grants separate explicit authorization, recorded in "
            "docs/decision-log.md and enabled through a reviewed change to "
            "FULL_BASELINE_AUTHORIZED. Full-run settings are frozen only "
            "after the pilot (docs/roadmap.md, Phase 3B).",
            file=sys.stderr,
        )
        return 3
    raise NotImplementedError  # pragma: no cover - unreachable while disabled


# -- external result verification ----------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def cmd_verify_results(args: argparse.Namespace) -> int:
    """Verifies persisted external results: schemas, semantics, hashes.

    An empty results directory is a FAILURE by default: when a pilot result
    is required, "nothing to verify" is never success. Pass --allow-empty
    only for explicitly exploratory checks.
    """
    base = _resolve_real_results_dir() / args.subdirectory
    result_paths = sorted(base.rglob("*.result.json")) if base.is_dir() else []
    if not result_paths:
        report = {
            "workflow": "verify-results",
            "verified": 0,
            "failed": [],
            "ok": bool(args.allow_empty),
            "note": (
                "no persisted results were found. This is a FAILURE unless "
                "--allow-empty was passed: an empty directory never verifies "
                "a required pilot result."
            ),
        }
        print(json.dumps(report, indent=2))
        return 0 if args.allow_empty else 1

    verified: list[str] = []
    failures: list[dict] = []
    for result_path in result_paths:
        run_dir = result_path.parent
        name = result_path.name
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            validate_benchmark_result(result)
            manifest_path = run_dir / result["manifest_ref"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            validate_run_manifest(manifest)
            observations = result["observations"]
            measured_doc = None
            warmup_doc = None
            if observations.get("persisted"):
                measured_path = run_dir / observations["measured_file"]
                if _sha256_file(measured_path) != observations["measured_sha256"]:
                    raise SemanticValidationError("measured observation hash mismatch")
                measured_doc = json.loads(measured_path.read_text(encoding="utf-8"))
                validate_task_observations(measured_doc)
                if observations.get("warmup_file"):
                    warmup_path = run_dir / observations["warmup_file"]
                    if _sha256_file(warmup_path) != observations["warmup_sha256"]:
                        raise SemanticValidationError("warmup observation hash mismatch")
                    warmup_doc = json.loads(warmup_path.read_text(encoding="utf-8"))
                    validate_task_observations(warmup_doc)
            validate_result_semantics(
                manifest,
                result,
                measured_observations=measured_doc,
                warmup_observations=warmup_doc,
            )
            verified.append(name)
        except Exception as exc:
            failures.append({"file": name, "error": f"{type(exc).__name__}: {exc}"})

    failure_records = sorted(p.name for p in base.rglob("*.failure.json"))
    print(
        json.dumps(
            {
                "workflow": "verify-results",
                "verified": len(verified),
                "verified_files": verified,
                "failed": failures,
                "failure_records": failure_records,
                "ok": not failures,
                "note": (
                    "file names are relative to the external private "
                    "directory. failure_records are explicitly marked "
                    "incomplete attempts: they are never valid results and "
                    "are listed only for auditability."
                ),
            },
            indent=2,
        )
    )
    return 0 if not failures else 1


# -- entry point ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blackwell-cloud",
        description=(
            "Phase 3 readiness and lifecycle workflows. Readiness validation "
            "is fully offline; billable verbs (apply/destroy) and the pilot "
            "require separate explicit owner approval phrases (naming the "
            "run tag and the reviewed plan digest) and run only in the "
            "owner's local environment. Every lifecycle artifact lives in "
            "the external private LAB_RESULTS_DIR. The full 12-cell baseline "
            "remains disabled; only the bounded D-0014 pilot is authorized."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("readiness", help="Offline readiness validation (no cloud access).")

    init_parser = sub.add_parser(
        "init", help="Configure the run's EXTERNAL terraform backend/state and TF_DATA_DIR."
    )
    init_parser.add_argument("--run-tag", required=True)

    plan_parser = sub.add_parser(
        "plan", help="Create the reviewed saved plan (binary + redacted text + metadata)."
    )
    plan_parser.add_argument("--run-tag", required=True)

    apply_parser = sub.add_parser(
        "apply",
        help=(
            "Apply exactly the reviewed saved plan (approval phrase names "
            "the run tag and plan digest)."
        ),
    )
    apply_parser.add_argument("--run-tag", required=True)
    apply_parser.add_argument(
        "--approve",
        help="The exact apply approval phrase printed by 'plan'.",
    )

    reconcile_parser = sub.add_parser(
        "reconcile", help="Inspect external state vs provider and update the ledger."
    )
    reconcile_parser.add_argument("--run-tag", required=True)

    pilot_parser = sub.add_parser(
        "pilot", help="Short owner-approved compatibility/headroom pilot (provider-native only)."
    )
    pilot_parser.add_argument("--run-tag", required=True)
    pilot_parser.add_argument("--run-label", required=True)
    pilot_parser.add_argument(
        "--config",
        required=True,
        help=(
            "Existing absolute path to the approved D-0014 pilot config JSON; "
            "must live outside this repository."
        ),
    )
    pilot_parser.add_argument("--approve", help="The exact pilot approval phrase.")

    sub.add_parser(
        "full-baseline",
        help="DISABLED: the full 12-cell baseline remains unauthorized (D-0014).",
    )

    verify_parser = sub.add_parser(
        "verify-results", help="Verify persisted external results (schemas, hashes)."
    )
    verify_parser.add_argument(
        "--subdirectory",
        default="real-runs",
        help="Subdirectory of LAB_RESULTS_DIR to verify (default: real-runs).",
    )
    verify_parser.add_argument(
        "--allow-empty",
        action="store_true",
        help=(
            "Treat an empty results directory as success (exploratory only; "
            "never when a pilot result is required)."
        ),
    )

    teardown_parser = sub.add_parser(
        "teardown-plan",
        help="Identity-verified saved destroy plan from the recorded ledger.",
    )
    teardown_parser.add_argument("--run-tag", required=True)

    destroy_parser = sub.add_parser(
        "destroy",
        help=(
            "Destroy exactly the verified saved destroy plan (approval "
            "phrase names the run tag and destroy-plan digest); success only "
            "after read-only deletion confirmation."
        ),
    )
    destroy_parser.add_argument("--run-tag", required=True)
    destroy_parser.add_argument(
        "--approve",
        help="The exact destroy approval phrase printed by 'teardown-plan'.",
    )

    orphan_parser = sub.add_parser(
        "orphan-report", help="Read-only sweep of project-tagged resources vs the ledger."
    )
    orphan_parser.add_argument("--run-tag", help="Compare against this run's ledger.")

    session_parser = sub.add_parser(
        "session-summary",
        help="Observed billable duration and estimated session cost (external record).",
    )
    session_parser.add_argument("--run-tag", required=True)
    session_parser.add_argument(
        "--hourly-price",
        type=float,
        help="Applicable hourly price (USD) for the cost estimate.",
    )

    return parser


_HANDLERS = {
    "readiness": cmd_readiness,
    "init": cmd_init,
    "plan": cmd_plan,
    "apply": cmd_apply,
    "reconcile": cmd_reconcile,
    "pilot": cmd_pilot,
    "full-baseline": cmd_full_baseline,
    "verify-results": cmd_verify_results,
    "teardown-plan": cmd_teardown_plan,
    "destroy": cmd_destroy,
    "orphan-report": cmd_orphan_report,
    "session-summary": cmd_session_summary,
}


def _sanitized_error_types() -> tuple[type[BaseException], ...]:
    """Error types whose messages are sanitized by construction (this
    project's own errors). Anything else prints ONLY its type name —
    arbitrary exception text could carry provider responses, ids, tokens, or
    private paths."""
    from blackwell_lab.cloud.lifecycle import LifecycleError
    from blackwell_lab.cloud.provenance import ProvenanceError
    from blackwell_lab.cloud.realbench import RequiredMeasurementError
    from blackwell_lab.cloud.telemetry import ArtifactVerificationError, TelemetryUnavailable

    return (
        LifecycleError,
        ProvenanceError,
        RequiredMeasurementError,
        TelemetryUnavailable,
        ArtifactVerificationError,
        SemanticValidationError,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = _HANDLERS[args.command]
    try:
        return handler(args)
    except ResultsLocationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        if isinstance(exc, _sanitized_error_types()):
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        else:
            # Arbitrary exception text is never printed: it could contain raw
            # provider responses, resource ids, tokens, or private paths.
            print(
                f"error: {type(exc).__name__} (details suppressed: arbitrary "
                "exception text is never printed; inspect local logs)",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
