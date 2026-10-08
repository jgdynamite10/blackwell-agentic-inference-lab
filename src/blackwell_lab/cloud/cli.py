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
- ``recover-empty-apply``
                      clear a pending apply only after a provider-verified
                      empty ledger, empty Terraform state, and clean orphan
                      report; never marks the run reconciled.
- ``pilot``           the short, owner-approved compatibility/headroom pilot
                      (RunMode.REAL): blocked until reconciliation is clean,
                      provider-native only, live provenance verified first.
- ``mvl-baseline``    owner-approved D-0017 Akamai minimum valuable lab
                      (provider-native, three cells; fail-closed).
- ``qualify-agent``   owner-approved agent-quality qualification
                      (C1/C2 on workload 2.4.0, P1 on workload 2.4.1,
                      P2 and P2C on workload 2.5.0 with the
                      evidence-grounding-v1 controller; frozen
                      dev/holdout/freeze; fail-closed; no infrastructure
                      changes). P2 development/holdout cells bind to a
                      D-0023 sealed custody stage through ``--custody-dir``
                      (external, never printed); P2C is the controlled
                      public-catalog version of P2 and refuses custody
                      input at every stage (D-0026). ``--validate-only``
                      checks the binding offline.
- ``full-baseline``   DISABLED: the research-grade 12-cell baseline is not
                      part of the MVL.
- ``verify-results``  external verification of persisted genuine results;
                      an empty directory is a FAILURE unless --allow-empty.
- ``teardown-plan``   identity-verified, saved destroy plan from the ledger.
- ``destroy``         destroy exactly the verified saved destroy plan —
                      separate approval phrase naming the destroy-plan
                      digest; success only after read-only deletion
                      confirmation.
- ``orphan-report``   read-only sweep of project-tagged resources vs ledger.
- ``session-summary`` observed billable duration and estimated session cost.
- ``engine-contract``  offline engine/precision/topology contract check
                      (no launch, no credentials, no provider access).

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
from blackwell_lab.cloud.mvl import MVL_APPROVAL_TEMPLATE
from blackwell_lab.paths import ResultsLocationError, RunMode, resolve_results_dir
from blackwell_lab.schemas import (
    validate_benchmark_result,
    validate_engine_contract,
    validate_run_manifest,
    validate_task_observations,
)
from blackwell_lab.workload.validation import (
    ConfigError,
    SemanticValidationError,
    validate_result_semantics,
)

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
# D-0030 fixed qualification region. The D-0029 us-sea lock and the
# D-0027 us-iad-2 lock are historical.
AUTHORIZED_PILOT_REGION = "us-ord"
AUTHORIZED_PILOT_INSTANCE_TYPE = "g3-gpu-rtxpro6000-blackwell-1"
AUTHORIZED_WARMUP_PASSES = 1
AUTHORIZED_REPETITIONS = 1
AUTHORIZED_TASKS_PER_REPETITION = 20
FULL_BASELINE_AUTHORIZED = False


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
        import blackwell_lab.cloud.mvl
        import blackwell_lab.cloud.preflight
        import blackwell_lab.cloud.provenance
        import blackwell_lab.cloud.realbench
        import blackwell_lab.cloud.telemetry
        import blackwell_lab.engines
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
        validate_engine_contract(
            json.loads((examples / "example-engine-contract.json").read_text(encoding="utf-8"))
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


def cmd_engine_contract(args: argparse.Namespace) -> int:
    """Offline engine/precision contract inspection. No launch, no credentials."""
    from blackwell_lab.engines import (
        declaration_from_mapping,
        evaluate_engine_contract,
        list_profiles,
        load_registered_components,
        require_ready_contract,
    )

    load_registered_components()
    profiles = [
        {
            "profile_id": profile.profile_id,
            "engine": profile.engine,
            "precision": profile.precision,
            "topologies": sorted(profile.topologies),
        }
        for profile in list_profiles()
    ]
    if getattr(args, "list_profiles", False) and not getattr(args, "config", None):
        print(json.dumps({"profiles": profiles, "credentials_required": False}, indent=2))
        return 0
    if not getattr(args, "config", None):
        raise ConfigError("engine-contract --config is required unless --list is set")
    path = Path(args.config)
    if not path.is_file():
        raise ConfigError("engine-contract config must be an existing file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    validate_engine_contract(payload)
    declaration = declaration_from_mapping(payload)
    readiness = evaluate_engine_contract(declaration)
    print(json.dumps({"readiness": readiness.as_dict(), "profiles": profiles}, indent=2))
    require_ready_contract(readiness)
    return 0


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


def cmd_recover_empty_apply(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import lifecycle

    paths = _paths_for(args.run_tag)
    fetch, token = _read_only_provider_access()
    report = lifecycle.recover_verified_empty_apply(
        args.run_tag, paths=paths, fetch=fetch, token=token
    )
    print(json.dumps(report, indent=2))
    return 0


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


def _git_head() -> str:
    git = shutil.which("git")
    if git is None:
        raise ConfigError("the current git commit could not be determined")
    result = subprocess.run(
        [git, "rev-parse", "HEAD"],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ConfigError("the current git commit could not be determined")
    return result.stdout.strip()


def _tree_clean() -> bool:
    git = shutil.which("git")
    if git is None:
        return False
    result = subprocess.run(
        [git, "status", "--porcelain"],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return result.returncode == 0 and not result.stdout.strip()


def cmd_full_baseline(_args: argparse.Namespace) -> int:
    if not FULL_BASELINE_AUTHORIZED:
        print(
            "DISABLED: the full 12-cell Akamai baseline is not authorized. "
            "Decision D-0017 authorizes the provider-native minimum valuable "
            "lab (blackwell-cloud mvl-baseline). The research-grade 12-cell "
            "baseline remains disabled until the owner grants separate "
            "explicit authorization, recorded in docs/decision-log.md and "
            "enabled through a reviewed change to FULL_BASELINE_AUTHORIZED.",
            file=sys.stderr,
        )
        return 3
    raise NotImplementedError  # pragma: no cover - unreachable while disabled


def _mvl_stop(message: str, *, results_dir: Path | None, run_label: str) -> int:
    from blackwell_lab.cloud.artifacts import write_private_json
    from blackwell_lab.cloud.mvl import TEARDOWN_FROM_LAPTOP_NOTE, failure_record

    if results_dir is not None:
        write_private_json(
            results_dir / "real-runs" / f"{run_label}-failure.json",
            failure_record(message),
        )
    print(f"BLOCKED: {message}", file=sys.stderr)
    print(TEARDOWN_FROM_LAPTOP_NOTE, file=sys.stderr)
    return 1


def cmd_mvl_baseline(args: argparse.Namespace) -> int:
    import time

    from blackwell_lab.cloud import lifecycle, mvl, provenance, realbench, telemetry
    from blackwell_lab.workload.model_client import GenerationSettings
    from blackwell_lab.workload.openai_client import OpenAICompatibleClient

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
        config_sha256=config_sha256,
    )
    if (args.approve or "") != expected:
        print(
            "BLOCKED: the MVL requires the exact owner approval phrase "
            f"(expected verbatim: {expected!r}); nothing was executed.",
            file=sys.stderr,
        )
        return 1

    try:
        mvl.require_clean_canonical_commit(config, git_head=_git_head(), tree_clean=_tree_clean())
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

    endpoint = config["endpoint"]
    lifecycle.record_session_event(
        paths,
        "mvl_started",
        {"run_label": run_label, "config_sha256": config_sha256},
    )

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
                workload_version=mvl.FROZEN_WORKLOAD_VERSION,
            ),
            workload_version=mvl.FROZEN_WORKLOAD_VERSION,
        )
        client = OpenAICompatibleClient(
            endpoint["base_url"],
            endpoint["model"],
            api_key_env=endpoint.get("api_key_env"),
        )
        return realbench.run_real_cell(
            spec,
            client,
            host=host,
            sampler_factory=telemetry.GpuSamplerThread,
            results_dir=results_dir,
        )

    try:
        canary_label = mvl.output_label(run_label, "canary")
        started = time.monotonic()
        canary_records = _run_cell(
            profile="interactive",
            concurrency=mvl.FROZEN_CANARY_CONCURRENCY,
            repetitions=1,
            warmup=0,
            tasks=mvl.FROZEN_CANARY_TASKS,
            label=canary_label,
        )
        wall_s = max(time.monotonic() - started, 0.001)
        outcomes: list[object] = []
        for record in canary_records:
            outcomes.extend(getattr(record, "outcomes", ()) or ())
            measured = getattr(record, "measured_observations", None) or {}
            if isinstance(measured, dict):
                outcomes.extend(measured.get("observations") or [])
            else:
                outcomes.extend(getattr(measured, "get", lambda *_: [])("observations") or [])
        if not outcomes:
            for record in canary_records:
                document = getattr(record, "measured_observations", None)
                if isinstance(document, dict):
                    outcomes.extend(document.get("observations") or [])
        mvl.evaluate_canary(outcomes)
        remaining = mvl.remaining_session_seconds(
            (json.loads(paths.session_path.read_text(encoding="utf-8")).get("events") or [])
            if paths.session_path.is_file()
            else []
        )
        mvl.refuse_if_session_exceeded(mvl.project_measured_seconds(wall_s), remaining)
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, (mvl.MvlError, ConfigError))
            else f"canary aborted: {type(exc).__name__}"
        )
        return _mvl_stop(message, results_dir=results_dir, run_label=run_label)

    summaries = [
        {
            "kind": "canary",
            "diagnostic_only": True,
            "run_label": canary_label,
            "note": "Canary observations are diagnostic and excluded from MVL summaries.",
        }
    ]
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

    lifecycle.record_session_event(paths, "mvl_completed", {"cells": len(mvl.AUTHORIZED_MVL_CELLS)})
    print(
        json.dumps(
            {
                "workflow": "mvl-baseline",
                "run_label": run_label,
                "comparison_mode": "provider-native",
                "counts": mvl.measured_counts(),
                "cells": summaries,
                "note": mvl.TEARDOWN_FROM_LAPTOP_NOTE,
            },
            indent=2,
        )
    )
    return 0


def _qualify_stop(message: str, *, results_dir: Path | None, run_label: str) -> int:
    from blackwell_lab.cloud.artifacts import write_private_json
    from blackwell_lab.cloud.qualification import failure_record

    if results_dir is not None:
        write_private_json(
            results_dir / "qualification-runs" / f"{run_label}-failure.json",
            failure_record(message),
        )
    print(f"BLOCKED: {message}", file=sys.stderr)
    return 1


def _sealed_validation_report(
    *,
    candidate_id: str,
    stage: str,
    config_sha256: str,
    sealed: object,
    matched_control: dict | None = None,
    qual_environment: dict | None = None,
) -> dict:
    """Content-free ``--validate-only`` report. No paths, ids, or payload content."""
    from blackwell_lab.cloud import qualification

    binding = getattr(sealed, "binding", None)
    return {
        "workflow": qualification.QUALIFICATION_WORKFLOW,
        "mode": "validate-only",
        "executed": False,
        "approval_checked": False,
        "model_client_constructed": False,
        "endpoint_contacted": False,
        "candidate_id": candidate_id,
        "stage": stage,
        "config_sha256": config_sha256,
        "candidate_identity_sha256": qualification.candidate_identity_digest(candidate_id),
        "workload_version": qualification.candidate_workload_version(candidate_id),
        "controller": qualification.candidate_controller(candidate_id),
        "sealed_input": binding is not None,
        **({"sealed_set": binding.provenance()} if binding is not None else {}),
        **(
            {"controlled_experiment": qualification.p2c_experiment_record()}
            if candidate_id == qualification.CANDIDATE_P2C
            else {}
        ),
        **(
            {"controlled_experiment": qualification.w_pair_experiment_record(candidate_id)}
            if candidate_id in qualification.WORKFLOW_CANDIDATES
            else {}
        ),
        **({"qualification_environment": qual_environment} if qual_environment is not None else {}),
        **({"matched_control": matched_control} if matched_control is not None else {}),
        **(
            {
                "sealed_tasks_loaded": len(sealed.tasks),
                "sealed_scenario_count": len(sealed.scenario_ids),
                "custody_access": "stage-specific",
                "other_stage_observed": False,
            }
            if binding is not None
            else {}
        ),
        "note": (
            "Offline validation only: the config, candidate binding, and (for "
            "sealed cells) the selected custody stage were verified. Nothing "
            "was executed, no model client was constructed, no endpoint was "
            "contacted, and no credentials were used."
        ),
    }


def cmd_qualify_agent(args: argparse.Namespace) -> int:
    from blackwell_lab.cloud import (
        lifecycle,
        provenance,
        qual_env,
        qualification,
        realbench,
        telemetry,
    )
    from blackwell_lab.cloud.sealed_binding import (
        require_manifest_provenance,
        require_sealed_stage_evidence,
        resolve_sealed_stage,
    )
    from blackwell_lab.workload.evidence import require_controller_binding
    from blackwell_lab.workload.model_client import GenerationSettings
    from blackwell_lab.workload.openai_client import OpenAICompatibleClient

    validate_only = bool(getattr(args, "validate_only", False))
    custody_dir = getattr(args, "custody_dir", None)
    if not validate_only:
        lifecycle.refuse_hosted_execution()
    try:
        from blackwell_lab.cloud.matched_control import require_qualification_run_tag

        require_qualification_run_tag(args.run_tag)
        run_label = qualification.require_safe_run_label(args.run_label)
        candidate_id = args.candidate
        stage = args.stage
        if candidate_id not in qualification.ALL_AUTHORIZED_CANDIDATES:
            raise ConfigError(qualification.UNKNOWN_CANDIDATE_MESSAGE)
        if stage not in qualification.AUTHORIZED_STAGES:
            raise ConfigError("qualification stage must be development, holdout, or freeze")
        # P2C (D-0026) and W1/W2 (D-0031) are catalog cells at every stage: a
        # custody directory is refused before the config is read.
        qualification.require_p2c_runtime(candidate_id, custody_dir=custody_dir)
        qualification.require_w_runtime(candidate_id, custody_dir=custody_dir)
        # The isolated qualification environment (D-0031) is verified before
        # any config, ledger, provenance, or client work. Read-only.
        qual_environment = qual_env.require_qualification_environment(validate_only=validate_only)
        qualification.require_frozen_split()
        qualification.refuse_mvl_identities(
            run_tag=args.run_tag,
            run_label=run_label,
            config={"candidate_id": candidate_id, "stage": stage},
            artifact_family=qualification.QUALIFICATION_ARTIFACT_FAMILY,
        )
        config_path = qualification.require_external_config(args.config, _repo_root())
        config, config_sha256 = qualification.load_qualification_config(
            config_path, candidate_id=candidate_id, stage=stage
        )
        qualification.refuse_mvl_identities(
            run_tag=args.run_tag,
            run_label=run_label,
            config=config,
            artifact_family=qualification.QUALIFICATION_ARTIFACT_FAMILY,
        )
        if not validate_only:
            qualification.require_approval(
                args.approve,
                run_tag=args.run_tag,
                run_label=run_label,
                candidate_id=candidate_id,
                config_sha256=config_sha256,
            )
            qualification.require_clean_canonical_commit(
                config, git_head=_git_head(), tree_clean=_tree_clean()
            )
        # Sealed input (D-0024): P2 development/holdout load exactly the bound
        # custody stage here — before the results directory, ledger, live
        # provenance, or any model client exists. Every custody failure
        # (path, modes, manifest, receipt, digests, identity, stage, count,
        # payload) stops the command at this point with a content-free
        # reason. Catalog cells refuse --custody-dir and a sealed_set section.
        sealed = resolve_sealed_stage(
            config,
            candidate_id=candidate_id,
            stage=stage,
            custody_dir=custody_dir,
            repo=_repo_root(),
        )
        require_controller_binding(
            qualification.candidate_workload_version(candidate_id),
            qualification.candidate_controller(candidate_id),
        )
        # Same-session control (D-0027 for P1->P2C, D-0031 for W1->W2).
        # Read-only, before any client, endpoint contact, provider mutation,
        # or result write. Holdout and freeze do not carry this binding.
        from blackwell_lab.cloud.matched_control import pair_for_control, pair_for_treatment

        treatment_pair = pair_for_treatment(candidate_id)
        control_pair = pair_for_control(candidate_id)
        matched_control = None
        if treatment_pair is not None and stage == qualification.STAGE_DEVELOPMENT:
            from blackwell_lab.cloud.matched_control import (
                authenticate_matched_development_control,
            )

            matched_control = authenticate_matched_development_control(
                config,
                results_dir=_resolve_real_results_dir(),
                run_tag=args.run_tag,
                p2c_run_label=run_label,
                pair=treatment_pair,
            )
        if (
            not validate_only
            and control_pair is not None
            and stage == qualification.STAGE_DEVELOPMENT
        ):
            from blackwell_lab.cloud.matched_control import validate_p1_development_session

            validate_p1_development_session(
                _resolve_real_results_dir(), run_tag=args.run_tag, pair=control_pair
            )
    except (ConfigError, qualification.QualificationError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1

    if validate_only:
        print(
            json.dumps(
                _sealed_validation_report(
                    candidate_id=candidate_id,
                    stage=stage,
                    config_sha256=config_sha256,
                    sealed=sealed,
                    matched_control=matched_control,
                    qual_environment=qual_environment,
                ),
                indent=2,
            )
        )
        return 0

    results_dir = _resolve_real_results_dir()
    paths = lifecycle.lifecycle_paths(results_dir, args.run_tag)
    if not paths.ledger_path.is_file():
        return _qualify_stop(
            "the resource ledger is missing for this run. Run "
            "'blackwell-cloud init', apply, and 'blackwell-cloud reconcile' "
            "before any qualification execution.",
            results_dir=results_dir,
            run_label=run_label,
        )
    ledger = lifecycle.load_ledger(paths.ledger_path)
    blockers = lifecycle.pilot_blockers(ledger, pending=lifecycle.has_pending(paths))
    if blockers:
        return _qualify_stop(
            "qualification cannot run until every lifecycle gate passes. " + "; ".join(blockers),
            results_dir=results_dir,
            run_label=run_label,
        )

    spec = qualification.stage_spec(stage)
    cell_label = qualification.output_label(run_label, stage, candidate_id)
    qualification.refuse_mvl_identities(
        run_tag=args.run_tag,
        run_label=cell_label,
        config=config,
        artifact_family=qualification.QUALIFICATION_ARTIFACT_FAMILY,
    )
    endpoint = config["endpoint"]
    snapshot = None
    session_pair = treatment_pair or control_pair
    if stage == qualification.STAGE_DEVELOPMENT and session_pair is not None:
        from blackwell_lab.cloud.matched_control import (
            capture_development_session,
            require_snapshot_matches_control,
        )

        try:
            snapshot = capture_development_session(
                results_dir, run_tag=args.run_tag, pair=session_pair
            )
            if treatment_pair is not None:
                require_snapshot_matches_control(snapshot, matched_control, pair=treatment_pair)
        except qualification.QualificationError as exc:
            print(f"BLOCKED: {exc}", file=sys.stderr)
            return 1
    lifecycle.record_session_event(
        paths,
        "qualification_started",
        {
            "run_label": run_label,
            "candidate_id": candidate_id,
            "stage": stage,
            "config_sha256": config_sha256,
        },
    )

    try:
        observed = provenance.verify_live_provenance(
            run_tag=args.run_tag,
            approved=config,
            ledger=ledger,
            artifact_dir=Path(config["model_verification"]["artifact_dir"]),
            digest_manifest=Path(config["model_verification"]["digest_manifest"]),
            serving_base_url=endpoint["base_url"],
        )
        if observed.model_artifact_hash != qualification.FROZEN_MODEL_ARTIFACT_HASH:
            raise qualification.QualificationError(
                "live model digest drifted from the frozen aggregate"
            )
        digest = str(observed.container_digest)
        if digest not in {
            qualification.FROZEN_CONTAINER_DIGEST,
            qualification.FROZEN_VLLM_IMAGE_DIGEST,
        } and not digest.endswith(qualification.FROZEN_VLLM_IMAGE_DIGEST):
            raise qualification.QualificationError(
                "live container digest drifted from the frozen image"
            )
        host = {**observed.host_facts, **observed.gpu_facts}
        model = dict(config["model"])
        model["artifact_hash"] = observed.model_artifact_hash
        run_spec = realbench.RealRunSpec(
            profile_name=spec["profile"],
            concurrency=spec["concurrency"],
            comparison_mode=qualification.FROZEN_COMPARISON_MODE,
            instance_type=observed.instance["instance_type"],
            region=observed.instance["region"],
            list_price_usd_per_hour=config["cloud"]["list_price_usd_per_hour"],
            price_source_date=config["cloud"]["price_source_date"],
            model=model,
            engine=config["serving"]["engine"],
            engine_version=observed.engine_version,
            container_digest=observed.container_digest,
            container_cuda_runtime_version=observed.container_cuda_runtime_version,
            repetitions=spec["repetitions"],
            warmup_passes=spec["warmup_passes"],
            tasks_per_repetition=spec["tasks"],
            seed=qualification.FROZEN_SEED,
            run_label=cell_label,
            generation=GenerationSettings(
                temperature=qualification.candidate_temperature(candidate_id),
                top_p=qualification.FROZEN_TOP_P,
                seed=qualification.FROZEN_SEED,
                reasoning_mode=qualification.FROZEN_REASONING_MODE,
                workload_version=qualification.candidate_workload_version(candidate_id),
            ),
            template_ids=None if sealed is not None else spec["template_ids"],
            artifact_family=qualification.QUALIFICATION_ARTIFACT_FAMILY,
            workload_version=qualification.candidate_workload_version(candidate_id),
            controller=qualification.candidate_controller(candidate_id),
            sealed_set=sealed.binding if sealed is not None else None,
        )
        # The workload/controller binding is re-checked against the
        # assembled spec before any client exists (fail closed, no inference).
        require_controller_binding(run_spec.workload_version, run_spec.controller)
        qualification.require_p2c_catalog_execution(
            candidate_id,
            stage=stage,
            template_ids=run_spec.template_ids,
            sealed_set=run_spec.sealed_set,
            sealed_tasks=sealed,
        )
        qualification.require_w_catalog_execution(
            candidate_id,
            stage=stage,
            template_ids=run_spec.template_ids,
            sealed_set=run_spec.sealed_set,
            sealed_tasks=sealed,
        )
        client = OpenAICompatibleClient(
            endpoint["base_url"],
            endpoint["model"],
            api_key_env=endpoint.get("api_key_env"),
        )
        records = realbench.run_real_cell(
            run_spec,
            client,
            host=host,
            sampler_factory=telemetry.GpuSamplerThread,
            results_dir=results_dir,
            sealed_tasks=sealed,
        )
        if len(records) != spec["repetitions"]:
            raise qualification.QualificationError(
                "qualification must persist exactly one measured repetition"
            )
        outcomes = qualification.outcomes_from_records(records)
        if sealed is not None:
            require_sealed_stage_evidence(sealed, outcomes)
            expected_template_ids = sealed.scenario_ids
        else:
            qualification.require_complete_stage_evidence(stage, outcomes)
            expected_template_ids = spec["template_ids"]
        verification_ok = True
        for record in records:
            result = getattr(record, "result", None)
            manifest = getattr(record, "manifest", None)
            measured = getattr(record, "measured_observations", None)
            if result is None or manifest is None:
                if sealed is not None:
                    raise qualification.QualificationError(
                        "sealed qualification must persist a manifest and result"
                    )
                continue
            validate_benchmark_result(result)
            validate_run_manifest(manifest)
            if isinstance(measured, dict):
                validate_task_observations(measured)
            validate_result_semantics(
                manifest,
                result,
                measured_observations=measured if isinstance(measured, dict) else None,
                warmup_observations=getattr(record, "warmup_observations", None),
            )
            if sealed is not None:
                require_manifest_provenance(manifest, sealed.binding)
        metrics = qualification.compute_qualification_metrics(
            outcomes,
            provenance_ok=True,
            verification_ok=verification_ok,
            expected_template_ids=expected_template_ids,
        )
        gates = qualification.evaluate_stage_thresholds(stage, metrics)
        files = [name for record in records for name in getattr(record, "written_files", ())]
        if snapshot is not None:
            from blackwell_lab.cloud.matched_control import revalidate_development_session

            revalidate_development_session(results_dir, snapshot, pair=session_pair)
        receipt = qualification.sanitized_receipt(
            run_label=cell_label,
            candidate_id=candidate_id,
            stage=stage,
            config_sha256=config_sha256,
            identity_digest=qualification.candidate_identity_digest(candidate_id),
            gates=gates,
            files=files,
            stopped=bool(gates["stopped"]),
            sealed_set=sealed.binding if sealed is not None else None,
            matched_control=matched_control,
        )
        from blackwell_lab.cloud.artifacts import write_private_json

        receipt_sha256 = write_private_json(
            results_dir
            / qualification.QUALIFICATION_ARTIFACT_FAMILY
            / f"{cell_label}-receipt.json",
            receipt,
        )
        terminal_event = (
            "qualification_completed" if not gates["stopped"] else "qualification_stopped"
        )
        lifecycle.record_session_event(
            paths,
            terminal_event,
            {
                "stage": stage,
                "candidate_id": candidate_id,
                "run_label": run_label,
                "config_sha256": config_sha256,
                "stopped": bool(gates["stopped"]),
            },
        )
        if (
            control_pair is not None
            and stage == qualification.STAGE_DEVELOPMENT
            and not gates["stopped"]
        ):
            from blackwell_lab.cloud.matched_control import (
                persist_completed_p1_development_control,
            )

            if snapshot is None:
                raise qualification.QualificationError(
                    "development session changed before the result was bound"
                )
            persist_completed_p1_development_control(
                results_dir=results_dir,
                run_tag=args.run_tag,
                p1_run_label=run_label,
                canonical_commit=str(config["canonical_commit"]),
                config_sha256=config_sha256,
                receipt_sha256=receipt_sha256,
                session_path=paths.session_path,
                snapshot=snapshot,
                pair=control_pair,
            )
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, (qualification.QualificationError, ConfigError))
            else f"qualification aborted: {type(exc).__name__}"
        )
        return _qualify_stop(message, results_dir=results_dir, run_label=run_label)

    print(json.dumps(receipt, indent=2))
    return 1 if gates["stopped"] else 0


# -- ten-task diagnostic canary (D-0031) ---------------------------------------


def _canary_stop(message: str, *, results_dir: Path | None, run_label: str) -> int:
    from blackwell_lab.cloud.artifacts import write_private_json
    from blackwell_lab.cloud.canary import CANARY_ARTIFACT_FAMILY, CANARY_WORKFLOW
    from blackwell_lab.cloud.qualification import failure_record

    if results_dir is not None:
        record = failure_record(message)
        record["workflow"] = CANARY_WORKFLOW
        record["artifact_family"] = CANARY_ARTIFACT_FAMILY
        write_private_json(
            results_dir / CANARY_ARTIFACT_FAMILY / f"{run_label}-failure.json",
            record,
        )
    print(f"BLOCKED: {message}", file=sys.stderr)
    return 1


def cmd_canary_agent(args: argparse.Namespace) -> int:
    """Ten-task diagnostic canary. Diagnostic only; authorizes nothing."""
    from blackwell_lab.cloud import (
        canary,
        lifecycle,
        provenance,
        qual_env,
        qualification,
        realbench,
        telemetry,
    )
    from blackwell_lab.workload.evidence import require_controller_binding
    from blackwell_lab.workload.model_client import GenerationSettings
    from blackwell_lab.workload.openai_client import OpenAICompatibleClient

    validate_only = bool(getattr(args, "validate_only", False))
    if not validate_only:
        lifecycle.refuse_hosted_execution()
    try:
        from blackwell_lab.cloud.matched_control import require_qualification_run_tag

        require_qualification_run_tag(args.run_tag)
        run_label = qualification.require_safe_run_label(args.run_label)
        candidate_id = args.candidate
        canary.require_canary_candidate(candidate_id)
        qual_environment = qual_env.require_qualification_environment(validate_only=validate_only)
        spec = canary.require_canary_contract()
        qualification.require_frozen_split()
        canary.refuse_identities(
            run_tag=args.run_tag,
            run_label=run_label,
            config={"candidate_id": candidate_id, "workflow": canary.CANARY_WORKFLOW},
        )
        config_path = qualification.require_external_config(args.config, _repo_root())
        config, config_sha256 = canary.load_canary_config(config_path, candidate_id=candidate_id)
        canary.refuse_identities(run_tag=args.run_tag, run_label=run_label, config=config)
        if not validate_only:
            canary.require_approval(
                args.approve,
                run_tag=args.run_tag,
                run_label=run_label,
                candidate_id=candidate_id,
                config_sha256=config_sha256,
            )
            qualification.require_clean_canonical_commit(
                config, git_head=_git_head(), tree_clean=_tree_clean()
            )
        require_controller_binding(
            qualification.candidate_workload_version(candidate_id),
            qualification.candidate_controller(candidate_id),
        )
        if candidate_id in qualification.WORKFLOW_CANDIDATES:
            qualification.require_w_pair_contract()
    except (ConfigError, qualification.QualificationError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1

    cell_label = canary.output_label(run_label, candidate_id)
    if validate_only:
        print(
            json.dumps(
                {
                    "workflow": canary.CANARY_WORKFLOW,
                    "mode": "validate-only",
                    "executed": False,
                    "approval_checked": False,
                    "model_client_constructed": False,
                    "endpoint_contacted": False,
                    "candidate_id": candidate_id,
                    "stage": canary.CANARY_STAGE,
                    "config_sha256": config_sha256,
                    "candidate_identity_sha256": qualification.candidate_identity_digest(
                        candidate_id
                    ),
                    "workload_version": qualification.candidate_workload_version(candidate_id),
                    "controller": qualification.candidate_controller(candidate_id),
                    "schedule": {
                        "tasks": spec["tasks"],
                        "seed": spec["seed"],
                        "templates": len(spec["template_ids"]),
                        "template_source": "development",
                        "holdout_excluded": True,
                        "allocation": canary.CANARY_ALLOCATION,
                        "quality_floor": spec["quality_floor"],
                        "min_passes": spec["min_passes"],
                    },
                    "diagnostic_only": True,
                    "authorizes_qualification": False,
                    "creates_development_control": False,
                    "qualification_environment": qual_environment,
                    "note": (
                        "Offline validation only: the canary config, candidate binding, "
                        "schedule balance, and schedule disjointness were verified. Nothing "
                        "was executed, no model client was constructed, no endpoint was "
                        "contacted, and no credentials were used."
                    ),
                },
                indent=2,
            )
        )
        return 0

    results_dir = _resolve_real_results_dir()
    paths = lifecycle.lifecycle_paths(results_dir, args.run_tag)
    if not paths.ledger_path.is_file():
        return _canary_stop(
            "the resource ledger is missing for this run. Run "
            "'blackwell-cloud init', apply, and 'blackwell-cloud reconcile' "
            "before any canary execution.",
            results_dir=results_dir,
            run_label=run_label,
        )
    ledger = lifecycle.load_ledger(paths.ledger_path)
    blockers = lifecycle.pilot_blockers(ledger, pending=lifecycle.has_pending(paths))
    if blockers:
        return _canary_stop(
            "the canary cannot run until every lifecycle gate passes. " + "; ".join(blockers),
            results_dir=results_dir,
            run_label=run_label,
        )
    endpoint = config["endpoint"]
    lifecycle.record_session_event(
        paths,
        "canary_started",
        {
            "run_label": run_label,
            "candidate_id": candidate_id,
            "stage": canary.CANARY_STAGE,
            "config_sha256": config_sha256,
            "diagnostic_only": True,
        },
    )
    try:
        observed = provenance.verify_live_provenance(
            run_tag=args.run_tag,
            approved=config,
            ledger=ledger,
            artifact_dir=Path(config["model_verification"]["artifact_dir"]),
            digest_manifest=Path(config["model_verification"]["digest_manifest"]),
            serving_base_url=endpoint["base_url"],
        )
        if observed.model_artifact_hash != qualification.FROZEN_MODEL_ARTIFACT_HASH:
            raise qualification.QualificationError(
                "live model digest drifted from the frozen aggregate"
            )
        digest = str(observed.container_digest)
        if digest not in {
            qualification.FROZEN_CONTAINER_DIGEST,
            qualification.FROZEN_VLLM_IMAGE_DIGEST,
        } and not digest.endswith(qualification.FROZEN_VLLM_IMAGE_DIGEST):
            raise qualification.QualificationError(
                "live container digest drifted from the frozen image"
            )
        host = {**observed.host_facts, **observed.gpu_facts}
        model = dict(config["model"])
        model["artifact_hash"] = observed.model_artifact_hash
        run_spec = realbench.RealRunSpec(
            profile_name=spec["profile"],
            concurrency=spec["concurrency"],
            comparison_mode=qualification.FROZEN_COMPARISON_MODE,
            instance_type=observed.instance["instance_type"],
            region=observed.instance["region"],
            list_price_usd_per_hour=config["cloud"]["list_price_usd_per_hour"],
            price_source_date=config["cloud"]["price_source_date"],
            model=model,
            engine=config["serving"]["engine"],
            engine_version=observed.engine_version,
            container_digest=observed.container_digest,
            container_cuda_runtime_version=observed.container_cuda_runtime_version,
            repetitions=spec["repetitions"],
            warmup_passes=spec["warmup_passes"],
            tasks_per_repetition=spec["tasks"],
            # Internal base seed. run_real_cell adds the 1-based repetition
            # index, so the measured schedule seed stays CANARY_MEASURED_SEED.
            seed=canary.CANARY_BASE_SEED,
            run_label=cell_label,
            generation=GenerationSettings(
                temperature=qualification.candidate_temperature(candidate_id),
                top_p=qualification.FROZEN_TOP_P,
                seed=qualification.FROZEN_SEED,
                reasoning_mode=qualification.FROZEN_REASONING_MODE,
                workload_version=qualification.candidate_workload_version(candidate_id),
            ),
            template_ids=spec["template_ids"],
            artifact_family=canary.CANARY_ARTIFACT_FAMILY,
            workload_version=qualification.candidate_workload_version(candidate_id),
            controller=qualification.candidate_controller(candidate_id),
            sealed_set=None,
        )
        require_controller_binding(run_spec.workload_version, run_spec.controller)
        if tuple(run_spec.template_ids or ()) != tuple(spec["template_ids"]):
            raise qualification.QualificationError(
                "the canary must schedule only the six development templates"
            )
        # Family allowlist and the measured schedule are checked before any
        # model client, stream, or inference. Provenance above verified the
        # already-running endpoint; it does not start a completion.
        realbench._validate_spec(run_spec)
        canary.require_measured_schedule(run_spec)
        client = OpenAICompatibleClient(
            endpoint["base_url"],
            endpoint["model"],
            api_key_env=endpoint.get("api_key_env"),
        )
        records = realbench.run_real_cell(
            run_spec,
            client,
            host=host,
            sampler_factory=telemetry.GpuSamplerThread,
            results_dir=results_dir,
            sealed_tasks=None,
        )
        if len(records) != spec["repetitions"]:
            raise qualification.QualificationError(
                "the canary must persist exactly one measured repetition"
            )
        outcomes = qualification.outcomes_from_records(records)
        canary.require_complete_canary_evidence(outcomes)
        for record in records:
            result = getattr(record, "result", None)
            manifest = getattr(record, "manifest", None)
            measured = getattr(record, "measured_observations", None)
            if result is None or manifest is None:
                continue
            validate_benchmark_result(result)
            validate_run_manifest(manifest)
            if isinstance(measured, dict):
                validate_task_observations(measured)
            validate_result_semantics(
                manifest,
                result,
                measured_observations=measured if isinstance(measured, dict) else None,
                warmup_observations=getattr(record, "warmup_observations", None),
            )
        metrics = qualification.compute_qualification_metrics(
            outcomes,
            provenance_ok=True,
            verification_ok=True,
            expected_template_ids=spec["template_ids"],
        )
        verdict = canary.evaluate_canary(metrics)
        files = [name for record in records for name in getattr(record, "written_files", ())]
        receipt = canary.sanitized_receipt(
            run_label=cell_label,
            candidate_id=candidate_id,
            config_sha256=config_sha256,
            identity_digest=qualification.candidate_identity_digest(candidate_id),
            verdict=verdict,
            files=files,
            qualification_environment=qual_environment,
        )
        from blackwell_lab.cloud.artifacts import write_private_json

        write_private_json(
            results_dir / canary.CANARY_ARTIFACT_FAMILY / f"{cell_label}-receipt.json",
            receipt,
        )
        lifecycle.record_session_event(
            paths,
            "canary_completed" if not verdict["stopped"] else "canary_stopped",
            {
                "stage": canary.CANARY_STAGE,
                "candidate_id": candidate_id,
                "run_label": run_label,
                "config_sha256": config_sha256,
                "stopped": bool(verdict["stopped"]),
                "diagnostic_only": True,
                "authorizes_qualification": False,
            },
        )
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, (qualification.QualificationError, ConfigError))
            else f"canary aborted: {type(exc).__name__}"
        )
        return _canary_stop(message, results_dir=results_dir, run_label=run_label)

    print(json.dumps(receipt, indent=2))
    return 1 if verdict["stopped"] else 0


# -- read-only qualification analysis (D-0031) ---------------------------------


def cmd_analyze_qualification(args: argparse.Namespace) -> int:
    """Read-only analysis of an existing qualification result. No provider contact."""
    from blackwell_lab.cloud import analysis

    try:
        report = analysis.analyze(
            results_dir=_resolve_real_results_dir(),
            run_label=args.run_label,
            candidate_id=args.candidate,
            stage=args.stage,
            write_report=bool(getattr(args, "write_report", False)),
        )
    except analysis.AnalysisError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    print(analysis.render(report, fmt=getattr(args, "format", "text")))
    return 0


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
    from blackwell_lab.cloud.matched_control import audit_matched_controls

    base = _resolve_real_results_dir() / args.subdirectory
    control_failures = audit_matched_controls(base)
    result_paths = sorted(base.rglob("*.result.json")) if base.is_dir() else []
    if not result_paths:
        report = {
            "workflow": "verify-results",
            "verified": 0,
            "failed": control_failures,
            "ok": bool(args.allow_empty) and not control_failures,
            "note": (
                "no persisted results were found. This is a FAILURE unless "
                "--allow-empty was passed: an empty directory never verifies "
                "a required pilot result."
            ),
        }
        print(json.dumps(report, indent=2))
        return 0 if args.allow_empty and not control_failures else 1

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

    failures.extend(control_failures)
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
            "the external private LAB_RESULTS_DIR. Decision D-0017 authorizes "
            "the Akamai minimum valuable lab (mvl-baseline) and the D-0019 "
            "agent-quality qualification (qualify-agent). Decision D-0020 "
            "adds the offline engine-contract check. Live apply, MVL, "
            "qualification, and Phase 4 engine execution still require "
            "their separate digest-bearing phrases."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("readiness", help="Offline readiness validation (no cloud access).")
    engine_parser = sub.add_parser(
        "engine-contract",
        help="Offline engine/precision/topology contract check (no launch).",
    )
    engine_parser.add_argument(
        "--list",
        dest="list_profiles",
        action="store_true",
        help="List registered engine profiles.",
    )
    engine_parser.add_argument(
        "--config",
        help="Existing JSON file declaring an engine/precision/topology contract.",
    )

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

    recover_parser = sub.add_parser(
        "recover-empty-apply",
        help=(
            "Clear a pending apply only when Terraform state, the ledger, "
            "and a read-only provider check are all verified empty."
        ),
    )
    recover_parser.add_argument("--run-tag", required=True)

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

    mvl_parser = sub.add_parser(
        "mvl-baseline",
        help="Owner-approved D-0017 Akamai minimum valuable lab (provider-native, three cells).",
    )
    mvl_parser.add_argument("--run-tag", required=True)
    mvl_parser.add_argument("--run-label", required=True)
    mvl_parser.add_argument(
        "--config",
        required=True,
        help=(
            "Existing absolute path to the approved D-0017 MVL config JSON; "
            "must live outside this repository."
        ),
    )
    mvl_parser.add_argument("--approve", help="The exact MVL approval phrase.")

    qualify_parser = sub.add_parser(
        "qualify-agent",
        help=(
            "Owner-approved agent-quality qualification "
            "(C1/C2 on workload 2.4.0, P1 on workload 2.4.1, P2 and P2C on "
            "workload 2.5.0 with the evidence-grounding-v1 controller; "
            "frozen development/holdout/freeze stages)."
        ),
    )
    qualify_parser.add_argument("--run-tag", required=True)
    qualify_parser.add_argument("--run-label", required=True)
    qualify_parser.add_argument(
        "--candidate",
        required=True,
        choices=("C1", "C2", "P1", "P2", "P2C", "W1", "W2"),
        help=(
            "C1 (temperature 1.0, workload 2.4.0), "
            "C2 (temperature 0.2, workload 2.4.0), "
            "P2 (temperature 0.2, workload 2.5.0, evidence-grounding-v1 "
            "controller), "
            "P2C (the controlled public-catalog version of P2: same workload "
            "2.5.0 and controller, every stage on the D-0019 catalog schedule "
            "P1 uses; P1 is its control), "
            "P1, the workload 2.4.1 prompt-only variant at temperature 0.2, "
            "W1 (workload 2.6.0, workflow-controller-v1; the D-0031 control), or "
            "W2 (workload 2.6.1, workflow-controller-v1 with the evidence-refs "
            "treatment; W1 is its same-session control)."
        ),
    )
    qualify_parser.add_argument(
        "--stage",
        required=True,
        choices=("development", "holdout", "freeze"),
        help="Frozen qualification stage.",
    )
    qualify_parser.add_argument(
        "--config",
        required=True,
        help=(
            "Existing absolute path to the approved D-0019 qualification config "
            "JSON; must live outside this repository."
        ),
    )
    qualify_parser.add_argument(
        "--approve",
        help="The exact qualification approval phrase naming run, candidate, and config digest.",
    )
    qualify_parser.add_argument(
        "--custody-dir",
        help=(
            "Absolute path to the external D-0023 custody directory that holds "
            "the sealed stage bound by the config's sealed_set section "
            "(decision D-0024). Required for P2 development and holdout; "
            "refused for every other candidate/stage, including every P2C "
            "stage (decision D-0026). It must lie outside "
            "every Git repository, contain no symlink component, and keep "
            "0700/0600 modes. Only the requested stage is opened. The path "
            "is never printed and never persisted in receipts, manifests, or Git."
        ),
    )
    qualify_parser.add_argument(
        "--validate-only",
        action="store_true",
        help=(
            "Offline validation of the config, candidate binding, and (for "
            "P2 development/holdout) the selected custody stage. Constructs no "
            "model client, contacts no endpoint, touches no ledger, and uses "
            "no credentials. The approval phrase is not checked in this mode."
        ),
    )

    canary_parser = sub.add_parser(
        "canary-agent",
        help=(
            "Ten-task diagnostic canary (decision D-0031): ten predeclared tasks from "
            "the six development templates only, holdout excluded, measured seed "
            "20261007, disjoint from every official schedule, same evaluator and "
            "0.40 floor (four of ten). Diagnostic only: it never mints a development "
            "control and never authorizes qualification, comparative, or cross-cloud "
            "runs."
        ),
    )
    canary_parser.add_argument("--run-tag", required=True)
    canary_parser.add_argument("--run-label", required=True)
    canary_parser.add_argument(
        "--candidate",
        required=True,
        choices=("C1", "C2", "P1", "P2C", "W1", "W2"),
        help="Public-catalog candidate to canary (P2 is sealed and refused).",
    )
    canary_parser.add_argument(
        "--config",
        required=True,
        help=(
            "Existing absolute path to the canary config JSON (a qualification "
            "config with workflow canary-agent); must live outside this repository."
        ),
    )
    canary_parser.add_argument(
        "--approve",
        help="The exact canary approval phrase naming run, candidate, and config digest.",
    )
    canary_parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the config, schedule balance, and disjointness offline. Executes nothing.",
    )

    analyze_parser = sub.add_parser(
        "analyze-qualification",
        help=(
            "Read-only analysis of one existing qualification result under "
            "LAB_RESULTS_DIR: successful incidents, failed gates by task, aggregate "
            "gate-failure counts. No provider, endpoint, client, or stream activity."
        ),
    )
    analyze_parser.add_argument("--run-label", required=True)
    analyze_parser.add_argument(
        "--candidate", required=True, choices=("C1", "C2", "P1", "P2", "P2C", "W1", "W2")
    )
    analyze_parser.add_argument(
        "--stage", default="development", choices=("development", "holdout", "freeze")
    )
    analyze_parser.add_argument(
        "--format", default="text", choices=("text", "json"), help="Output format."
    )
    analyze_parser.add_argument(
        "--write-report",
        action="store_true",
        help=(
            "Also write the sanitized report under LAB_RESULTS_DIR/qualification-analysis/. "
            "Source artifacts are never modified."
        ),
    )

    sub.add_parser(
        "full-baseline",
        help="DISABLED: the research-grade 12-cell baseline is not part of the MVL.",
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
    "engine-contract": cmd_engine_contract,
    "init": cmd_init,
    "plan": cmd_plan,
    "apply": cmd_apply,
    "reconcile": cmd_reconcile,
    "recover-empty-apply": cmd_recover_empty_apply,
    "pilot": cmd_pilot,
    "mvl-baseline": cmd_mvl_baseline,
    "qualify-agent": cmd_qualify_agent,
    "canary-agent": cmd_canary_agent,
    "analyze-qualification": cmd_analyze_qualification,
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
    from blackwell_lab.cloud.mvl import MvlError
    from blackwell_lab.cloud.provenance import ProvenanceError
    from blackwell_lab.cloud.qualification import QualificationError
    from blackwell_lab.cloud.realbench import RequiredMeasurementError
    from blackwell_lab.cloud.telemetry import ArtifactVerificationError, TelemetryUnavailable

    return (
        MvlError,
        QualificationError,
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
