"""``blackwell-cloud`` — Phase 3 readiness and lifecycle workflows (CLI-first).

Subcommands map one-to-one to the separated workflows required by Phase 3A:

- ``readiness``      offline validation only: no cloud access, no credentials.
- ``plan``           terraform plan (dry-run; the default lifecycle verb).
- ``apply``          terraform apply — requires the explicit owner approval
                     phrase; refused in hosted/CI environments.
- ``pilot``          the short, owner-approved compatibility/headroom pilot
                     against a local serving endpoint (RunMode.REAL).
- ``full-baseline``  DISABLED: Phase 3B measurement is not authorized; this
                     command refuses until a decision-log entry enables it.
- ``verify-results`` external verification of persisted genuine results
                     (schemas, semantics, observation hashes).
- ``teardown-plan``  exact-resource teardown plan from the recorded ledger.
- ``destroy``        terraform destroy of ledger-recorded resources only —
                     separate explicit approval phrase required.
- ``orphan-report``  read-only sweep of project-tagged resources vs ledger.

Privacy rules: absolute private paths (``LAB_RESULTS_DIR``, ledger locations)
are never printed — output references safe relative filenames only. Tokens
are read from the environment at call time and never echoed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
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

_LEDGER_SUBDIR = "infra-ledgers"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _terraform_dir() -> Path:
    return _repo_root() / "infra" / "akamai"


def _bootstrap_dir() -> Path:
    return _terraform_dir() / "bootstrap"


# -- readiness ---------------------------------------------------------------


def _check_terraform_files() -> dict:
    directory = _terraform_dir()
    required = ["versions.tf", "variables.tf", "main.tf", "outputs.tf", ".gitignore"]
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
            "detail": "terraform binary not installed; fmt/validate run in local environments",
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
    if not (directory / ".terraform").is_dir():
        return {
            "status": "ok",
            "detail": (
                "terraform fmt -check passed; validate skipped (providers not "
                "initialized — run terraform init locally for full validation)"
            ),
        }
    validate = subprocess.run(
        [terraform, "validate", "-no-color"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if validate.returncode != 0:
        return {"status": "failed", "detail": "terraform validate failed"}
    return {"status": "ok", "detail": "terraform fmt -check and validate passed"}


def _check_bootstrap_scripts() -> dict:
    directory = _bootstrap_dir()
    scripts = sorted(directory.glob("*.sh")) if directory.is_dir() else []
    if not scripts:
        return {"status": "failed", "detail": "no bootstrap scripts found"}
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
        "MODEL_DIGEST_MANIFEST",
        "MIN_DRIVER_BRANCH",
        "REQUIRED_CUDA_MAJOR",
    )
    missing = [key for key in required_keys if f"{key}=" not in content]
    if missing:
        return {"status": "failed", "detail": f"bootstrap.env.example lacks pins: {missing}"}
    return {"status": "ok", "detail": "bootstrap pin template declares every required pin"}


def _check_python_modules() -> dict:
    try:
        import blackwell_lab.cloud.lifecycle
        import blackwell_lab.cloud.preflight
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


def cmd_plan(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle

    result = lifecycle.plan(args.run_tag)
    sys.stdout.write(result.stdout)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
    return result.returncode


def cmd_apply(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle

    ledger_dir = _resolve_real_results_dir() / _LEDGER_SUBDIR
    ledger_path = lifecycle.apply(
        args.run_tag,
        args.approve or "",
        ledger_dir=ledger_dir,
    )
    print(
        json.dumps(
            {
                "applied": True,
                "run_tag": args.run_tag,
                "ledger_file": ledger_path.name,
                "note": (
                    "Exact resource ledger recorded outside Git. Billing has "
                    "started: schedule the teardown before leaving the session."
                ),
            },
            indent=2,
        )
    )
    return 0


def _ledger_path(run_tag: str) -> Path:
    return _resolve_real_results_dir() / _LEDGER_SUBDIR / f"{run_tag}.ledger.json"


def cmd_teardown_plan(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle

    ledger = lifecycle.load_ledger(_ledger_path(args.run_tag))
    print(json.dumps(lifecycle.teardown_plan(ledger), indent=2))
    return 0


def cmd_destroy(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle

    ledger = lifecycle.load_ledger(_ledger_path(args.run_tag))
    lifecycle.destroy(args.run_tag, args.approve or "", ledger)
    print(
        json.dumps(
            {
                "destroyed": True,
                "run_tag": args.run_tag,
                "note": (
                    "Only ledger-recorded resources were targeted. Run "
                    "'blackwell-cloud orphan-report' now to verify nothing "
                    "project-tagged remains."
                ),
            },
            indent=2,
        )
    )
    return 0


def cmd_orphan_report(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle
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
        ledger = lifecycle.load_ledger(_ledger_path(args.run_tag))
    report = lifecycle.orphan_report(token, fetch=get_json, ledger=ledger)
    # Sanitized terminal output: labels/kinds/regions only. The complete
    # report (with resource ids) is written to the external private dir.
    reports_dir = _resolve_real_results_dir() / "orphan-reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["generated_at_utc"].replace(":", "").replace("+", "Z")
    report_name = f"orphan-report-{stamp}.json"
    (reports_dir / report_name).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
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


# -- pilot / full baseline ----------------------------------------------------


def _load_pilot_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in ("endpoint", "cloud", "model", "serving", "host", "comparison_mode"):
        if key not in config:
            raise ConfigError(f"pilot config is missing required section: {key}")
    return config


def cmd_pilot(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle, realbench, telemetry
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
    endpoint = config["endpoint"]
    client = OpenAICompatibleClient(
        endpoint["base_url"],
        endpoint["model"],
        api_key_env=endpoint.get("api_key_env"),
    )
    host_config = config["host"]
    host = {
        **telemetry.collect_host_facts(
            storage_description=host_config["storage_description"],
            network_description=host_config["network_description"],
            virtualization=host_config.get("virtualization"),
        ),
        **telemetry.collect_gpu_facts(),
    }

    cells = config.get("cells") or [
        {"profile": "interactive", "concurrency": 1},
        {"profile": "batch-heavy", "concurrency": 4},
    ]
    summaries = []
    for index, cell in enumerate(cells, start=1):
        spec = realbench.RealRunSpec(
            profile_name=cell["profile"],
            concurrency=cell["concurrency"],
            comparison_mode=config["comparison_mode"],
            resource_limits=config.get("resource_limits"),
            instance_type=config["cloud"]["instance_type"],
            region=config["cloud"]["region"],
            list_price_usd_per_hour=config["cloud"]["list_price_usd_per_hour"],
            price_source_date=config["cloud"]["price_source_date"],
            model=config["model"],
            engine=config["serving"]["engine"],
            engine_version=config["serving"]["engine_version"],
            container_digest=config["serving"]["container_digest"],
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
    print(
        json.dumps(
            {
                "workflow": "pilot",
                "run_label": run_label,
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
    """Verifies persisted external results: schemas, semantics, hashes."""
    base = _resolve_real_results_dir() / args.subdirectory
    if not base.is_dir():
        print(
            json.dumps(
                {
                    "workflow": "verify-results",
                    "verified": 0,
                    "failed": [],
                    "note": "no results subdirectory exists yet (nothing to verify)",
                }
            )
        )
        return 0

    verified: list[str] = []
    failures: list[dict] = []
    for result_path in sorted(base.rglob("*.result.json")):
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

    print(
        json.dumps(
            {
                "workflow": "verify-results",
                "verified": len(verified),
                "verified_files": verified,
                "failed": failures,
                "ok": not failures,
                "note": "file names are relative to the external private directory",
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
            "require separate explicit owner approval phrases and run only in "
            "the owner's local environment. The full baseline is disabled "
            "until Phase 3B is authorized."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("readiness", help="Offline readiness validation (no cloud access).")

    plan_parser = sub.add_parser("plan", help="terraform plan (dry-run; default verb).")
    plan_parser.add_argument("--run-tag", required=True)

    apply_parser = sub.add_parser(
        "apply", help="terraform apply (requires the exact owner approval phrase)."
    )
    apply_parser.add_argument("--run-tag", required=True)
    apply_parser.add_argument(
        "--approve",
        help="The exact apply approval phrase (see infra/akamai/README.md).",
    )

    pilot_parser = sub.add_parser(
        "pilot", help="Short owner-approved compatibility/headroom pilot."
    )
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

    teardown_parser = sub.add_parser(
        "teardown-plan", help="Exact-resource teardown plan from the recorded ledger."
    )
    teardown_parser.add_argument("--run-tag", required=True)

    destroy_parser = sub.add_parser(
        "destroy",
        help="terraform destroy of ledger-recorded resources only (approval required).",
    )
    destroy_parser.add_argument("--run-tag", required=True)
    destroy_parser.add_argument(
        "--approve",
        help="The exact destroy approval phrase (see infra/akamai/README.md).",
    )

    orphan_parser = sub.add_parser(
        "orphan-report", help="Read-only sweep of project-tagged resources vs the ledger."
    )
    orphan_parser.add_argument("--run-tag", help="Compare against this run's ledger.")

    return parser


_HANDLERS = {
    "readiness": cmd_readiness,
    "plan": cmd_plan,
    "apply": cmd_apply,
    "pilot": cmd_pilot,
    "full-baseline": cmd_full_baseline,
    "verify-results": cmd_verify_results,
    "teardown-plan": cmd_teardown_plan,
    "destroy": cmd_destroy,
    "orphan-report": cmd_orphan_report,
}


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
        # Lifecycle/measurement errors carry sanitized messages by design.
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
