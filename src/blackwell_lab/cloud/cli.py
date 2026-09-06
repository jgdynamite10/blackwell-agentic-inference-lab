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
- ``full-baseline``   DISABLED: Phase 3B measurement is not authorized.
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

#: Phase 3B full-baseline measurement is NOT authorized. Enabling this
#: constant requires a separate owner authorization recorded in the decision
#: log and reviewed in a pull request — never a runtime flag or environment
#: variable.
FULL_BASELINE_AUTHORIZED = False

PILOT_APPROVAL_TEMPLATE = "I approve the short Akamai pilot for run {run_label}"


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
            [terraform, "init", "-backend=false", "-input=false"],
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
    for required in ("bootstrap.sh", "fetch-model.sh", "watchdog.sh"):
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
    required_keys = (
        "VLLM_IMAGE",
        "VLLM_IMAGE_DIGEST",
        "MODEL_ARTIFACT",
        "MODEL_REVISION",
        "MODEL_DIGEST_MANIFEST",
        "NVIDIA_DRIVER_PACKAGE",
        "MIN_DRIVER_BRANCH",
        "DRIVER_MAX_CUDA_MAJOR",
        "REQUIRED_CONTAINER_CUDA_VERSION",
        "GPU_PROBE_IMAGE",
        "GPU_PROBE_IMAGE_DIGEST",
    )
    missing = [key for key in required_keys if f"{key}=" not in content]
    if missing:
        return {"status": "failed", "detail": f"bootstrap.env.example lacks pins: {missing}"}
    return {"status": "ok", "detail": "bootstrap pin template declares every required pin"}


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
                    "Identity verification passed (ledger vs state"
                    + (" vs provider API" if token else "; provider API not checked — no token")
                    + "). The saved destroy plan targets ONLY the ledger's "
                    "recorded resources. Review the redacted plan, then "
                    "destroy with the exact approval phrase above."
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


def _load_pilot_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
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
    for key in ("artifact_dir", "digest_manifest"):
        if not config["model_verification"].get(key):
            raise ConfigError(f"pilot config is missing model_verification.{key}")
    return config


def cmd_pilot(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle, provenance, realbench, telemetry
    from blackwell_lab.workload.openai_client import OpenAICompatibleClient

    lifecycle.refuse_hosted_execution()
    run_label = args.run_label
    expected = PILOT_APPROVAL_TEMPLATE.format(run_label=run_label)
    if (args.approve or "") != expected:
        print(
            "BLOCKED: the pilot requires the exact owner approval phrase "
            f"(expected verbatim: {expected!r}); nothing was executed.",
            file=sys.stderr,
        )
        return 1

    results_dir = _resolve_real_results_dir()
    config = _load_pilot_config(Path(args.config))

    # Comparison mode: provider-native ONLY until the joint cgroup envelope
    # is genuinely implemented, enforced, and observed. A configuration-based
    # controlled-resource label is a fabrication and is rejected.
    if config["comparison_mode"] != "provider-native":
        print(
            "BLOCKED: the pilot runs only in provider-native mode. "
            "Controlled-resource labeling requires the joint 14-vCPU/100-GiB "
            "serving-plus-benchmark limit to be genuinely enforced and "
            "observed, which is not yet implemented (decision D-0013).",
            file=sys.stderr,
        )
        return 1

    # Lifecycle gate: the pilot is blocked until reconciliation is clean.
    paths = lifecycle.lifecycle_paths(results_dir, args.run_tag)
    if lifecycle.has_pending(paths):
        print(
            "BLOCKED: a pending lifecycle operation exists for this run. "
            "Run 'blackwell-cloud reconcile' until it reports clean before "
            "any pilot execution.",
            file=sys.stderr,
        )
        return 1
    ledger = lifecycle.load_ledger(paths.ledger_path)
    if not ledger.get("reconciled"):
        print(
            "BLOCKED: the run's ledger is not cleanly reconciled. Run "
            "'blackwell-cloud reconcile' and resolve any untracked or "
            "possibly created resources first.",
            file=sys.stderr,
        )
        return 1

    # Live provenance: observed immediately before the cells; configuration
    # values are expectations, never manifest facts.
    endpoint = config["endpoint"]
    observed = provenance.verify_live_provenance(
        run_tag=args.run_tag,
        approved=config,
        ledger=ledger,
        artifact_dir=Path(config["model_verification"]["artifact_dir"]),
        digest_manifest=Path(config["model_verification"]["digest_manifest"]),
        serving_base_url=endpoint["base_url"],
    )

    client = OpenAICompatibleClient(
        endpoint["base_url"],
        endpoint["model"],
        api_key_env=endpoint.get("api_key_env"),
    )
    host = {**observed.host_facts, **observed.gpu_facts}
    model = dict(config["model"])
    model["artifact_hash"] = observed.model_artifact_hash

    lifecycle.record_session_event(paths, "pilot_started", {"run_label": run_label})
    cells = config.get("cells") or [
        {"profile": "interactive", "concurrency": 1},
        {"profile": "batch-heavy", "concurrency": 4},
    ]
    summaries = []
    for index, cell in enumerate(cells, start=1):
        spec = realbench.RealRunSpec(
            profile_name=cell["profile"],
            concurrency=cell["concurrency"],
            comparison_mode="provider-native",
            instance_type=observed.instance["instance_type"],
            region=observed.instance["region"],
            list_price_usd_per_hour=config["cloud"]["list_price_usd_per_hour"],
            price_source_date=config["cloud"]["price_source_date"],
            model=model,
            engine=config["serving"]["engine"],
            engine_version=observed.engine_version,
            container_digest=observed.container_digest,
            container_cuda_runtime_version=observed.container_cuda_runtime_version,
            repetitions=int(config.get("repetitions", 1)),
            warmup_passes=int(config.get("warmup_passes", 1)),
            tasks_per_repetition=int(config.get("tasks_per_repetition", 20)),
            run_label=f"{run_label}-cell{index}",
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
            "DISABLED: the full Akamai baseline (Phase 3B provisioning and "
            "measurement) is not authorized. It remains disabled until the "
            "owner grants separate explicit authorization, recorded in "
            "docs/decision-log.md and enabled through a reviewed change to "
            "FULL_BASELINE_AUTHORIZED. The short pilot must complete first, "
            "and full-run settings are frozen only after the pilot "
            "(docs/roadmap.md, Phase 3B).",
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
            "the external private LAB_RESULTS_DIR. The full baseline is "
            "disabled until Phase 3B is authorized."
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
        "--config", required=True, help="Pilot config JSON (kept outside Git)."
    )
    pilot_parser.add_argument("--approve", help="The exact pilot approval phrase.")

    sub.add_parser(
        "full-baseline",
        help="DISABLED until Phase 3B is separately authorized by the owner.",
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
