"""Terraform lifecycle wrapper for the Akamai Phase 3 baseline instance.

Safety model (cost guardrails; AGENTS.md section 1; decision D-0012):

- **Plan/dry-run is the default.** ``plan`` renders the exact changes and
  costs nothing. It is the only lifecycle verb the readiness workflow ever
  needs.
- **Apply and destroy each require a separate explicit local owner
  approval**: the exact approval phrase — which names the run tag — must be
  supplied verbatim for each invocation. No approval, no terraform
  execution. Approval phrases are never read from the environment or a file:
  the owner types them.
- **Hosted execution is refused.** Billable lifecycle verbs refuse to run
  when CI/hosted-agent environment markers are present; they belong in the
  owner's local, authenticated environment only.
- **An exact resource ledger** is recorded from ``terraform show -json``
  after apply, stored outside Git (under ``LAB_RESULTS_DIR``), and is the
  ONLY source of teardown targets: teardown operates strictly on the
  recorded run's resources, never on filters or sweeps. There is no broad
  cleanup command anywhere in this project, by design.
- **The orphan report is read-only**: it lists project-tagged resources via
  GET requests and compares them against the ledger; deletion always remains
  a separate, owner-approved action.

Terraform state and ``*.tfvars`` stay outside Git (``infra/akamai/.gitignore``);
the ledger and orphan reports are written only under the external private
``LAB_RESULTS_DIR``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from blackwell_lab.cloud.preflight import Fetch

#: Every resource this project creates carries this tag.
PROJECT_TAG = "blackwell-lab"

#: Approval phrases (typed verbatim by the local owner; one per verb).
APPLY_APPROVAL_TEMPLATE = "I approve creating billable Akamai resources for run {run_tag}"
DESTROY_APPROVAL_TEMPLATE = "I approve deleting the exact recorded resources for run {run_tag}"

#: Environment markers that identify hosted (non-local-owner) execution.
_HOSTED_ENV_MARKERS = ("CI", "GITHUB_ACTIONS", "CURSOR_AGENT", "CLOUD_AGENT")

_RUN_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{3,40}$")
_LEDGER_SCHEMA_VERSION = "1.0.0"


class LifecycleError(RuntimeError):
    """A lifecycle operation is invalid or unsafe in this context."""


class ApprovalError(LifecycleError):
    """The required explicit owner approval phrase was not supplied."""


@dataclass(frozen=True)
class CommandResult:
    """Captured outcome of one terraform invocation."""

    returncode: int
    stdout: str
    stderr: str = ""


#: Injectable executor: (argv, cwd) -> CommandResult.
CommandRunner = Callable[[list[str], Path], CommandResult]


def default_runner(argv: list[str], cwd: Path) -> CommandResult:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=3600,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def default_terraform_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "infra" / "akamai"


def validate_run_tag(run_tag: str) -> str:
    """Run tags are short, lowercase, and safe to embed in labels and tags."""
    if not isinstance(run_tag, str) or not _RUN_TAG_RE.match(run_tag):
        raise LifecycleError(
            "run_tag must be 4-41 chars of lowercase letters, digits, and "
            "hyphens, starting with a letter or digit"
        )
    return run_tag


def refuse_hosted_execution(environ: dict | None = None) -> None:
    """Billable verbs run only in the owner's local environment."""
    env = os.environ if environ is None else environ
    markers = [m for m in _HOSTED_ENV_MARKERS if env.get(m)]
    if markers:
        raise LifecycleError(
            "billable lifecycle operations (apply/destroy) are refused in "
            f"CI/hosted environments (detected: {', '.join(markers)}). Run "
            "them locally with explicit owner approval."
        )


def _tf(run_tag: str, verb: str, extra: list[str]) -> list[str]:
    return [
        "terraform",
        verb,
        "-input=false",
        f"-var=run_tag={run_tag}",
        *extra,
    ]


def plan(
    run_tag: str,
    *,
    tf_dir: Path | None = None,
    runner: CommandRunner = default_runner,
) -> CommandResult:
    """Renders the terraform plan (the default, always-safe verb)."""
    validate_run_tag(run_tag)
    directory = tf_dir or default_terraform_dir()
    return runner(_tf(run_tag, "plan", ["-lock=false"]), directory)


def apply(
    run_tag: str,
    approval: str,
    *,
    tf_dir: Path | None = None,
    runner: CommandRunner = default_runner,
    ledger_dir: Path,
    environ: dict | None = None,
) -> Path:
    """Applies the plan after explicit approval; records the exact ledger.

    Returns the path of the written ledger file. Never runs without the
    verbatim approval phrase, and never in a hosted environment.
    """
    validate_run_tag(run_tag)
    refuse_hosted_execution(environ)
    expected = APPLY_APPROVAL_TEMPLATE.format(run_tag=run_tag)
    if approval != expected:
        raise ApprovalError(
            "apply requires the exact owner approval phrase "
            f"(expected verbatim: {expected!r}); nothing was executed"
        )
    directory = tf_dir or default_terraform_dir()
    result = runner(_tf(run_tag, "apply", ["-auto-approve"]), directory)
    if result.returncode != 0:
        raise LifecycleError(
            f"terraform apply exited with status {result.returncode}; inspect "
            "the local terraform output, then record/teardown any partially "
            "created resources via the ledger workflow"
        )
    show = runner(["terraform", "show", "-json"], directory)
    if show.returncode != 0:
        raise LifecycleError(
            "terraform show failed after apply: the resource ledger could not "
            "be recorded. Resolve locally before any further operation."
        )
    ledger = build_ledger(run_tag, json.loads(show.stdout))
    return write_ledger(ledger, ledger_dir)


def build_ledger(run_tag: str, show_json: dict) -> dict:
    """The exact-resource ledger for one run, from ``terraform show -json``.

    Records every resource in the root module with its address, type, name,
    provider id, and tags. This ledger is the ONLY authority for teardown
    targeting.
    """
    resources = []
    root_module = (show_json.get("values") or {}).get("root_module") or {}
    for resource in root_module.get("resources") or []:
        values = resource.get("values") or {}
        resources.append(
            {
                "address": resource.get("address"),
                "type": resource.get("type"),
                "name": resource.get("name"),
                "provider_id": str(values.get("id", "")),
                "label": values.get("label", ""),
                "region": values.get("region", ""),
                "tags": values.get("tags") or [],
            }
        )
    if not resources:
        raise LifecycleError(
            "terraform show reported no resources; refusing to write an empty ledger after apply"
        )
    return {
        "schema_version": _LEDGER_SCHEMA_VERSION,
        "run_tag": run_tag,
        "project_tag": PROJECT_TAG,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "resources": resources,
    }


def write_ledger(ledger: dict, ledger_dir: Path) -> Path:
    """Atomically writes the ledger (private permissions) outside Git."""
    ledger_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(ledger_dir, 0o700)
    target = ledger_dir / f"{ledger['run_tag']}.ledger.json"
    payload = (json.dumps(ledger, indent=2) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=ledger_dir, prefix=".ledger.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return target


def load_ledger(ledger_path: Path) -> dict:
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError("the resource ledger is missing or unreadable") from exc
    if not isinstance(ledger, dict) or not ledger.get("resources"):
        raise LifecycleError("the resource ledger is empty or malformed")
    return ledger


def teardown_plan(ledger: dict) -> dict:
    """The exact-resource teardown plan derived strictly from the ledger.

    Produces ``-target`` addresses for every recorded resource — never a
    filter, never a sweep, never resources outside the ledger.
    """
    targets = []
    for resource in ledger["resources"]:
        address = resource.get("address")
        if not address:
            raise LifecycleError("ledger contains a resource without a terraform address")
        targets.append(address)
    return {
        "run_tag": ledger["run_tag"],
        "resource_count": len(targets),
        "targets": targets,
        "destroy_arguments": [f"-target={t}" for t in targets],
        "note": (
            "Teardown deletes ONLY the resources recorded in this ledger. "
            "Execution requires the separate destroy approval phrase. After "
            "destroy, run the orphan report to verify nothing project-tagged "
            "remains."
        ),
    }


def destroy(
    run_tag: str,
    approval: str,
    ledger: dict,
    *,
    tf_dir: Path | None = None,
    runner: CommandRunner = default_runner,
    environ: dict | None = None,
) -> CommandResult:
    """Destroys exactly the ledger-recorded resources after explicit approval."""
    validate_run_tag(run_tag)
    refuse_hosted_execution(environ)
    if ledger.get("run_tag") != run_tag:
        raise LifecycleError(
            "ledger run_tag does not match the requested run: refusing to "
            "target another run's resources"
        )
    expected = DESTROY_APPROVAL_TEMPLATE.format(run_tag=run_tag)
    if approval != expected:
        raise ApprovalError(
            "destroy requires the exact owner approval phrase "
            f"(expected verbatim: {expected!r}); nothing was executed"
        )
    targets = teardown_plan(ledger)["destroy_arguments"]
    directory = tf_dir or default_terraform_dir()
    result = runner(_tf(run_tag, "destroy", ["-auto-approve", *targets]), directory)
    if result.returncode != 0:
        raise LifecycleError(
            f"terraform destroy exited with status {result.returncode}; the "
            "run's resources may still exist and still bill. Re-run the "
            "teardown and the orphan report."
        )
    return result


def orphan_report(token: str, *, fetch: Fetch, ledger: dict | None = None) -> dict:
    """Read-only sweep of project-tagged billable resources vs the ledger.

    Lists instances and volumes via GET requests, keeps those carrying the
    project tag (or any untagged GPU instance, which is always suspicious for
    this account), and classifies them against the ledger. Returns a
    reporting dict; the caller decides what to print (labels and counts —
    resource ids belong in externally stored reports, not terminal scrollback
    that might be pasted into issues).
    """
    from blackwell_lab.cloud.preflight import _paginated  # read-only GET helper

    try:
        instances = _paginated(fetch, "/linode/instances", token)
        volumes = _paginated(fetch, "/volumes", token)
    except Exception as exc:
        raise LifecycleError(
            "the read-only resource sweep failed (token scope, expiry, or "
            "connectivity); no error payload is echoed"
        ) from exc

    ledger_ids = set()
    if ledger is not None:
        ledger_ids = {r.get("provider_id", "") for r in ledger.get("resources", [])}

    def _classify(kind: str, item: dict, *, suspicious: bool = False) -> dict:
        item_id = str(item.get("id", ""))
        return {
            "kind": kind,
            "id": item_id,
            "label": item.get("label", ""),
            "region": item.get("region", ""),
            "tags": item.get("tags") or [],
            "in_ledger": item_id in ledger_ids,
            "suspicious_untagged_gpu": suspicious,
        }

    findings = []
    for instance in instances:
        tags = instance.get("tags") or []
        is_project = PROJECT_TAG in tags
        is_untagged_gpu = not tags and str(instance.get("type", "")).startswith("g")
        if is_project or is_untagged_gpu:
            findings.append(_classify("instance", instance, suspicious=is_untagged_gpu))
    for volume in volumes:
        if PROJECT_TAG in (volume.get("tags") or []):
            findings.append(_classify("volume", volume))

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "project_tag": PROJECT_TAG,
        "ledger_run_tag": ledger.get("run_tag") if ledger else None,
        "findings": findings,
        "unrecorded_findings": [f for f in findings if not f["in_ledger"]],
        "clean": not findings,
        "note": (
            "Read-only report. Any finding is billable until deleted; deletion "
            "always requires separate explicit owner approval and targets only "
            "ledger-recorded resources."
        ),
    }
