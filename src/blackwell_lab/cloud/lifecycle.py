"""Terraform lifecycle for the Akamai Phase 3 baseline — external state,
reviewed saved plans, partial-apply recovery, and identity-safe teardown.

Safety model (cost guardrails; AGENTS.md section 1; decisions D-0012/D-0013):

- **Everything Terraform writes lives OUTSIDE Git.** State, the Terraform
  data directory (``TF_DATA_DIR``), variable files, saved plans, plan
  metadata, the resource ledger, pending records, and session records are
  all stored under an absolute external ``LAB_RESULTS_DIR`` location with
  directory mode 0700 and file mode 0600. Nothing is ever written under the
  repository working tree.
- **Plan is saved, hashed, classified, and reviewed.** ``save_plan``
  produces the exact binary plan, a redacted human-readable rendering, the
  plan's SHA-256, and a metadata record (commit SHA, run tag, Terraform
  version, provider-lock digest, configuration digest, state digest,
  creation time, per-resource action classification). An apply-stage plan
  that contains delete, replace, or unrelated actions **fails closed**.
- **Apply executes exactly the reviewed saved plan.** The approval phrase
  names the run tag AND the saved plan's SHA-256; apply re-verifies the plan
  hash, configuration digest, lock digest, state digest, and commit SHA, and
  rejects stale plans. There is no path argument to smuggle in another plan
  and no fresh plan is ever generated at apply time. A bare
  ``terraform apply -auto-approve`` never occurs.
- **Partial applies are recoverable.** A pending record is written before
  every apply attempt; after every attempt — success or failure — the
  external state is inspected, optionally reconciled against read-only
  provider API results, and the ledger is atomically written or updated. A
  failed ``terraform show`` still leaves a recovery record. The pilot is
  blocked until reconciliation is clean.
- **Teardown is identity-safe.** Destroy requires that every target's
  Terraform address, resource type, provider id, project tag, run tag, and
  expected label match the ledger, the current state, and (when a read-only
  token is locally available) the provider API. The destroy plan is saved
  and reviewed like the apply plan, destruction executes only that saved
  plan after its own approval phrase, and success is reported only after
  read-only polling confirms every recorded provider id is gone —
  accounting for Akamai Blackwell deletions taking several minutes. A
  confirmation timeout reports that billing may continue.
- **Never broad cleanup.** Nothing here deletes by pattern, filter, or
  sweep. The read-only orphan report lists project-tagged resources for the
  owner; deletion always remains a separate, ledger-verified action.
- **Sanitized output.** No raw provider responses, tokens, absolute private
  paths, or arbitrary exception text are ever placed in error messages.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from blackwell_lab.cloud.artifacts import write_private_json, write_private_text
from blackwell_lab.cloud.preflight import Fetch

#: Exact Terraform CLI version required for every lifecycle operation.
REQUIRED_TERRAFORM_VERSION = "1.9.8"

#: Every resource this project creates carries this tag.
PROJECT_TAG = "blackwell-lab"

#: The complete set of resource addresses this configuration may manage.
#: Anything else in a plan is an "unrelated action" and fails closed.
EXPECTED_RESOURCE_ADDRESSES = frozenset(
    {"linode_instance.gpu_baseline", "linode_firewall.gpu_baseline"}
)
EXPECTED_RESOURCE_TYPES = {
    "linode_instance.gpu_baseline": "linode_instance",
    "linode_firewall.gpu_baseline": "linode_firewall",
}

#: Approval phrases (typed verbatim by the local owner; one per verb; each
#: names the run tag AND the saved plan digest it authorizes).
APPLY_APPROVAL_TEMPLATE = (
    "I approve creating billable Akamai resources for run {run_tag} using plan sha256:{plan_sha256}"
)
DESTROY_APPROVAL_TEMPLATE = (
    "I approve deleting the exact recorded resources for run {run_tag} "
    "using destroy plan sha256:{plan_sha256}"
)

#: Environment markers that identify hosted (non-local-owner) execution.
_HOSTED_ENV_MARKERS = ("CI", "GITHUB_ACTIONS", "CURSOR_AGENT", "CLOUD_AGENT")

_RUN_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{3,40}$")
_LEDGER_SCHEMA_VERSION = "2.0.0"
_PLAN_META_SCHEMA_VERSION = "1.0.0"

#: A saved plan older than this is stale and must be regenerated.
MAX_PLAN_AGE_S = 3600.0

#: Deletion confirmation polling: Akamai Blackwell instance deletion can take
#: several minutes; poll read-only until confirmed or the window closes.
DELETION_POLL_INTERVAL_S = 20.0
DELETION_CONFIRM_TIMEOUT_S = 1200.0


class LifecycleError(RuntimeError):
    """A lifecycle operation is invalid or unsafe in this context.

    Messages are sanitized by construction: no raw provider responses,
    tokens, absolute private paths, or foreign exception text.
    """


class ApprovalError(LifecycleError):
    """The required explicit owner approval phrase was not supplied."""


@dataclass(frozen=True)
class CommandResult:
    """Captured outcome of one subprocess invocation."""

    returncode: int
    stdout: str
    stderr: str = ""


#: Injectable executor: (argv, cwd, extra_env) -> CommandResult. The extra
#: environment (TF_DATA_DIR etc.) is merged over the process environment.
CommandRunner = Callable[[list[str], Path, dict[str, str]], CommandResult]

#: Read-only existence probe for one provider resource:
#: (api_path, token) -> "present" | "absent". Implementations must map an
#: HTTP 404 to "absent" and must never raise raw provider text.
ResourceProbe = Callable[[str, str], str]

#: Injectable monotonic clock + sleeper for deletion-confirmation polling.
MonotonicClock = Callable[[], float]
Sleeper = Callable[[float], None]


def default_runner(argv: list[str], cwd: Path, extra_env: dict[str, str]) -> CommandResult:
    env = {**os.environ, **extra_env}
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=3600,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def default_resource_probe(path: str, token: str) -> str:
    """Read-only GET probe mapping 404 to "absent". Never echoes payloads."""
    import urllib.error
    import urllib.request

    from blackwell_lab.cloud.preflight import API

    request = urllib.request.Request(f"{API}{path}")  # noqa: S310 - fixed https host
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30):  # noqa: S310 - fixed https host
            return "present"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "absent"
        raise LifecycleError(
            "a read-only provider probe failed (non-404 API error); no payload is echoed"
        ) from None
    except OSError:
        raise LifecycleError(
            "a read-only provider probe failed (connectivity); no payload is echoed"
        ) from None


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


# -- external private paths ----------------------------------------------------


@dataclass(frozen=True)
class LifecyclePaths:
    """Every lifecycle artifact for one run, all outside the Git tree."""

    run_dir: Path

    @property
    def tf_data_dir(self) -> Path:
        return self.run_dir / "tf-data"

    @property
    def state_path(self) -> Path:
        return self.run_dir / "terraform.tfstate"

    @property
    def var_file(self) -> Path:
        return self.run_dir / "terraform.tfvars"

    def plan_path(self, stage: str) -> Path:
        return self.run_dir / f"{stage}.tfplan"

    def plan_text_path(self, stage: str) -> Path:
        return self.run_dir / f"{stage}.tfplan.redacted.txt"

    def plan_meta_path(self, stage: str) -> Path:
        return self.run_dir / f"{stage}.plan-meta.json"

    @property
    def ledger_path(self) -> Path:
        return self.run_dir / "ledger.json"

    @property
    def pending_path(self) -> Path:
        return self.run_dir / "pending.json"

    @property
    def session_path(self) -> Path:
        return self.run_dir / "session.json"


def lifecycle_paths(results_dir: Path, run_tag: str) -> LifecyclePaths:
    """Resolves (and privately creates) the run's external lifecycle dir."""
    validate_run_tag(run_tag)
    if not results_dir.is_absolute():
        raise LifecycleError("the external results directory must be an absolute path")
    run_dir = results_dir / "infra-lifecycle" / run_tag
    run_dir.mkdir(parents=True, exist_ok=True)
    for directory in (results_dir / "infra-lifecycle", run_dir):
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
    return LifecyclePaths(run_dir=run_dir)


# -- digests and provenance -----------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def config_digest(tf_dir: Path) -> str:
    """SHA-256 over the sorted names and contents of every ``.tf`` file."""
    digest = hashlib.sha256()
    tf_files = sorted(tf_dir.glob("*.tf"))
    if not tf_files:
        raise LifecycleError("no terraform configuration files were found")
    for tf_file in tf_files:
        digest.update(tf_file.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(tf_file.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def lock_digest(tf_dir: Path) -> str:
    lock_file = tf_dir / ".terraform.lock.hcl"
    if not lock_file.is_file():
        raise LifecycleError("the provider lock file is missing from the configuration")
    return _sha256_file(lock_file)


def _state_digest(paths: LifecyclePaths) -> str | None:
    if paths.state_path.is_file():
        return _sha256_file(paths.state_path)
    return None


def _git_head(tf_dir: Path, runner: CommandRunner) -> str:
    result = runner(["git", "rev-parse", "HEAD"], tf_dir, {})
    if result.returncode != 0 or not result.stdout.strip():
        raise LifecycleError("the current git commit could not be determined")
    return result.stdout.strip()


def _terraform_version(tf_dir: Path, runner: CommandRunner, env: dict[str, str]) -> str:
    result = runner(["terraform", "version", "-json"], tf_dir, env)
    if result.returncode != 0:
        raise LifecycleError("terraform version could not be determined")
    try:
        version = str(json.loads(result.stdout)["terraform_version"])
    except (json.JSONDecodeError, KeyError) as exc:
        raise LifecycleError("terraform version output was unparseable") from exc
    if version != REQUIRED_TERRAFORM_VERSION:
        raise LifecycleError(
            f"terraform CLI version must be exactly {REQUIRED_TERRAFORM_VERSION}; "
            f"found {version}. Install the pinned release documented in "
            "infra/akamai/README.md"
        )
    return version


def _tf_env(paths: LifecyclePaths) -> dict[str, str]:
    return {"TF_DATA_DIR": str(paths.tf_data_dir), "TF_IN_AUTOMATION": "1"}


# -- init ------------------------------------------------------------------------


def init_backend(
    run_tag: str,
    *,
    paths: LifecyclePaths,
    tf_dir: Path | None = None,
    runner: CommandRunner = default_runner,
) -> dict:
    """Configures the external local backend and external ``TF_DATA_DIR``.

    State, plugin data, and every other Terraform artifact for this run live
    under the run's private external lifecycle directory — never under the
    repository.
    """
    validate_run_tag(run_tag)
    directory = tf_dir or default_terraform_dir()
    paths.tf_data_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(paths.tf_data_dir, 0o700)
    result = runner(
        [
            "terraform",
            "init",
            "-input=false",
            "-reconfigure",
            f"-backend-config=path={paths.state_path}",
        ],
        directory,
        _tf_env(paths),
    )
    if result.returncode != 0:
        raise LifecycleError(
            "terraform init failed (backend or provider installation); "
            "inspect the local terraform output"
        )
    version = _terraform_version(directory, runner, _tf_env(paths))
    return {
        "initialized": True,
        "run_tag": run_tag,
        "external_state": True,
        "terraform_version": version,
        "note": "state, plans, and TF_DATA_DIR live in the run's external lifecycle directory",
    }


# -- saved, reviewed plans -------------------------------------------------------

_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_SSH_KEY_RE = re.compile(r"(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa)\s+[A-Za-z0-9+/=]+")


def redact_plan_text(text: str) -> str:
    """Removes addresses and key material from the human-readable plan."""
    text = _SSH_KEY_RE.sub(r"\1 [REDACTED]", text)
    return _IPV4_RE.sub("[REDACTED-IPV4]", text)


def classify_plan_actions(plan_json: dict) -> list[dict]:
    """One classified entry per resource change in the saved plan."""
    classified = []
    for change in plan_json.get("resource_changes") or []:
        actions = list((change.get("change") or {}).get("actions") or [])
        if actions in (["delete", "create"], ["create", "delete"]):
            kind = "replace"
        elif actions == ["create"]:
            kind = "create"
        elif actions == ["delete"]:
            kind = "delete"
        elif actions == ["update"]:
            kind = "update"
        elif actions in (["no-op"], []):
            kind = "no-op"
        elif actions == ["read"]:
            kind = "read"
        else:
            kind = "unknown"
        classified.append(
            {
                "address": change.get("address", ""),
                "actions": actions,
                "classification": kind,
            }
        )
    return classified


def _enforce_stage_rules(stage: str, classified: list[dict]) -> None:
    if stage == "apply":
        blocked = [
            c
            for c in classified
            if c["classification"] in ("delete", "replace", "update", "unknown")
            or (
                c["classification"] not in ("no-op", "read")
                and c["address"] not in EXPECTED_RESOURCE_ADDRESSES
            )
        ]
        if blocked:
            addresses = ", ".join(sorted(c["address"] or "<unknown>" for c in blocked))
            raise LifecycleError(
                "the apply-stage plan contains delete, replace, update, or unrelated "
                f"actions ({addresses}); failing closed. Review the redacted "
                "plan text; maintenance/update workflows are not authorized and "
                "destructive changes require the separate destroy workflow."
            )
    elif stage == "destroy":
        wrong = [c for c in classified if c["classification"] not in ("delete", "no-op", "read")]
        if wrong:
            raise LifecycleError(
                "the destroy-stage plan contains non-delete actions; failing closed"
            )
        unexpected = [
            c
            for c in classified
            if c["classification"] == "delete" and c["address"] not in EXPECTED_RESOURCE_ADDRESSES
        ]
        if unexpected:
            raise LifecycleError(
                "the destroy-stage plan targets resources outside this "
                "project's configuration; failing closed"
            )
    else:  # pragma: no cover - internal misuse
        raise LifecycleError(f"unknown plan stage: {stage}")


def save_plan(
    run_tag: str,
    *,
    stage: str,
    paths: LifecyclePaths,
    tf_dir: Path | None = None,
    runner: CommandRunner = default_runner,
) -> dict:
    """Creates and records the reviewed saved plan for one stage.

    Writes, all under the external run directory: the exact binary plan, the
    redacted human-readable plan, and the plan metadata (SHA-256, commit
    SHA, run tag, Terraform version, provider-lock digest, configuration
    digest, state digest, creation time, action classification). Returns the
    metadata record.
    """
    validate_run_tag(run_tag)
    if stage not in ("apply", "destroy"):
        raise LifecycleError("plan stage must be 'apply' or 'destroy'")
    directory = tf_dir or default_terraform_dir()
    if not paths.var_file.is_file():
        raise LifecycleError(
            "the external terraform.tfvars for this run is missing; create it "
            "in the run's private lifecycle directory (variable files never "
            "live in the repository)"
        )
    env = _tf_env(paths)
    plan_path = paths.plan_path(stage)
    argv = [
        "terraform",
        "plan",
        "-input=false",
        f"-out={plan_path}",
        f"-var=run_tag={run_tag}",
        f"-var-file={paths.var_file}",
    ]
    if stage == "destroy":
        argv.insert(2, "-destroy")
    result = runner(argv, directory, env)
    if result.returncode != 0:
        raise LifecycleError(f"terraform plan ({stage}) failed; inspect the local terraform output")
    if not plan_path.is_file():
        raise LifecycleError("terraform reported success but the saved plan file is missing")
    with contextlib.suppress(OSError):
        os.chmod(plan_path, 0o600)

    show_json = runner(["terraform", "show", "-json", str(plan_path)], directory, env)
    if show_json.returncode != 0:
        raise LifecycleError("the saved plan could not be rendered for classification")
    try:
        plan_document = json.loads(show_json.stdout)
    except json.JSONDecodeError as exc:
        raise LifecycleError("the saved plan JSON rendering was unparseable") from exc
    classified = classify_plan_actions(plan_document)
    _enforce_stage_rules(stage, classified)

    show_text = runner(["terraform", "show", "-no-color", str(plan_path)], directory, env)
    if show_text.returncode != 0:
        raise LifecycleError("the saved plan could not be rendered for review")
    write_private_text(paths.plan_text_path(stage), redact_plan_text(show_text.stdout))

    meta = {
        "schema_version": _PLAN_META_SCHEMA_VERSION,
        "run_tag": run_tag,
        "stage": stage,
        "plan_sha256": _sha256_file(plan_path),
        "commit_sha": _git_head(directory, runner),
        "terraform_version": _terraform_version(directory, runner, env),
        "provider_lock_sha256": lock_digest(directory),
        "config_sha256": config_digest(directory),
        "state_sha256": _state_digest(paths),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "actions": classified,
        "resource_addresses": sorted(
            {c["address"] for c in classified if c["classification"] not in ("no-op", "read")}
        ),
    }
    write_private_json(paths.plan_meta_path(stage), meta)
    return meta


def verify_saved_plan(
    run_tag: str,
    *,
    stage: str,
    paths: LifecyclePaths,
    tf_dir: Path | None = None,
    runner: CommandRunner = default_runner,
    max_age_s: float = MAX_PLAN_AGE_S,
    now: datetime | None = None,
) -> dict:
    """Verifies the saved plan is exactly the reviewed one and still valid.

    Confirms the plan file's SHA-256 against the recorded digest and that
    the configuration, provider lock file, state, commit, and run tag have
    not changed since the plan was saved. Rejects stale plans. Only the
    canonical saved-plan path for this run is ever used — arbitrary plan
    paths are not accepted anywhere in this module.
    """
    validate_run_tag(run_tag)
    directory = tf_dir or default_terraform_dir()
    meta_path = paths.plan_meta_path(stage)
    if not meta_path.is_file():
        raise LifecycleError(
            f"no reviewed {stage} plan exists for this run; run the plan step first"
        )
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError("the saved plan metadata is unreadable") from exc

    if meta.get("run_tag") != run_tag or meta.get("stage") != stage:
        raise LifecycleError("the saved plan metadata does not belong to this run and stage")
    plan_path = paths.plan_path(stage)
    if not plan_path.is_file():
        raise LifecycleError("the saved binary plan file is missing")
    if _sha256_file(plan_path) != meta.get("plan_sha256"):
        raise LifecycleError(
            "the saved binary plan does not match its recorded SHA-256; it "
            "was modified or replaced after review — regenerate and re-review "
            "the plan"
        )
    if config_digest(directory) != meta.get("config_sha256"):
        raise LifecycleError(
            "the terraform configuration changed after the plan was saved; "
            "regenerate and re-review the plan"
        )
    if lock_digest(directory) != meta.get("provider_lock_sha256"):
        raise LifecycleError(
            "the provider lock file changed after the plan was saved; "
            "regenerate and re-review the plan"
        )
    if _state_digest(paths) != meta.get("state_sha256"):
        raise LifecycleError(
            "the terraform state changed after the plan was saved; "
            "regenerate and re-review the plan"
        )
    head = _git_head(directory, runner)
    if head != meta.get("commit_sha"):
        raise LifecycleError(
            "the git commit changed after the plan was saved; regenerate and "
            "re-review the plan from the current commit"
        )
    created_raw = meta.get("created_at_utc", "")
    try:
        created_at = datetime.fromisoformat(created_raw)
    except ValueError as exc:
        raise LifecycleError("the saved plan metadata has no valid creation time") from exc
    current = now or datetime.now(timezone.utc)
    age_s = (current - created_at).total_seconds()
    if age_s < 0 or age_s > max_age_s:
        raise LifecycleError(
            "the saved plan is stale (or its clock is inconsistent); "
            "regenerate and re-review the plan"
        )
    return meta


# -- pending records and reconciliation -------------------------------------------


def write_pending(paths: LifecyclePaths, *, run_tag: str, operation: str, plan_sha256: str) -> None:
    """The recovery record written BEFORE any apply/destroy attempt."""
    write_private_json(
        paths.pending_path,
        {
            "run_tag": run_tag,
            "operation": operation,
            "plan_sha256": plan_sha256,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "note": (
                "An infrastructure operation was attempted. Until "
                "reconciliation completes cleanly, resources may exist (and "
                "bill) without a complete ledger. Run 'blackwell-cloud "
                "reconcile' before any pilot or teardown."
            ),
        },
    )


def has_pending(paths: LifecyclePaths) -> bool:
    return paths.pending_path.is_file()


def _clear_pending(paths: LifecyclePaths) -> None:
    with contextlib.suppress(OSError):
        paths.pending_path.unlink()


def _state_resources(show_json: dict) -> list[dict]:
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
    return resources


def reconcile(
    run_tag: str,
    *,
    paths: LifecyclePaths,
    tf_dir: Path | None = None,
    runner: CommandRunner = default_runner,
    fetch: Fetch | None = None,
    token: str | None = None,
) -> dict:
    """Inspects external state, reconciles with the provider, updates the ledger.

    Runs after EVERY apply attempt (including failures) and on demand. When a
    read-only token is locally available, provider-visible run-tagged
    resources are compared against the state so possibly created, billable,
    or untracked resources are clearly reported. A failed ``terraform show``
    still produces a recovery ledger record. The returned report is
    sanitized (labels and counts, no provider ids).
    """
    validate_run_tag(run_tag)
    directory = tf_dir or default_terraform_dir()
    env = _tf_env(paths)
    recorded_at = datetime.now(timezone.utc).isoformat()

    show = runner(["terraform", "show", "-json"], directory, env)
    state_readable = show.returncode == 0
    resources: list[dict] = []
    if state_readable:
        try:
            resources = _state_resources(json.loads(show.stdout))
        except json.JSONDecodeError:
            state_readable = False

    reconciliation: dict = {
        "provider_checked": False,
        "untracked_billable": [],
        "missing_from_provider": [],
    }
    if fetch is None or not token:
        reconciliation["provider_note"] = (
            "the provider API was not checked (no read-only LINODE_TOKEN); "
            "reconciliation cannot be marked clean"
        )
    else:
        from blackwell_lab.cloud.preflight import _paginated

        try:
            instances = _paginated(fetch, "/linode/instances", token)
            firewalls = _paginated(fetch, "/networking/firewalls", token)
        except Exception:
            reconciliation["provider_lookup_failed"] = True
            reconciliation["provider_note"] = (
                "the provider reconciliation sweep failed (token scope, expiry, "
                "or connectivity); reconciliation cannot be marked clean"
            )
        else:
            reconciliation["provider_checked"] = True
            run_tag_label = f"run:{run_tag}"
            provider_ids = {
                str(item.get("id", ""))
                for item in [*instances, *firewalls]
                if run_tag_label in (item.get("tags") or [])
            }
            state_ids = {r["provider_id"] for r in resources if r["provider_id"]}
            reconciliation["untracked_billable"] = sorted(provider_ids - state_ids)
            reconciliation["missing_from_provider"] = sorted(state_ids - provider_ids)

    incomplete = [r["address"] for r in resources if not r["provider_id"]]
    clean = (
        state_readable
        and reconciliation["provider_checked"]
        and not incomplete
        and not reconciliation["untracked_billable"]
        and not reconciliation["missing_from_provider"]
    )

    ledger = {
        "schema_version": _LEDGER_SCHEMA_VERSION,
        "run_tag": run_tag,
        "project_tag": PROJECT_TAG,
        "recorded_at_utc": recorded_at,
        "state_readable": state_readable,
        "reconciled": clean,
        "resources": resources,
        "reconciliation": reconciliation,
    }
    if not state_readable:
        pending: dict = {}
        if paths.pending_path.is_file():
            with contextlib.suppress(OSError, json.JSONDecodeError):
                pending = json.loads(paths.pending_path.read_text(encoding="utf-8"))
        ledger["recovery"] = {
            "note": (
                "terraform show failed: the state could not be read. "
                "Resources MAY have been created and MAY be billing. Recover "
                "locally (terraform state inspection, provider console), then "
                "re-run reconciliation. This record preserves the pending "
                "operation context."
            ),
            "pending_operation": pending,
        }
    elif not reconciliation["provider_checked"]:
        pending: dict = {}
        if paths.pending_path.is_file():
            with contextlib.suppress(OSError, json.JSONDecodeError):
                pending = json.loads(paths.pending_path.read_text(encoding="utf-8"))
        if reconciliation.get("provider_lookup_failed"):
            recovery_note = (
                "the provider reconciliation sweep failed after Terraform state "
                "was read. Resources MAY have been created and MAY be billing. "
                "Fix token scope, expiry, or connectivity locally, then re-run "
                "reconciliation before any pilot execution."
            )
        else:
            recovery_note = (
                "the provider API was not successfully checked; reconciliation "
                "cannot be marked clean. Set LINODE_TOKEN locally and re-run "
                "reconciliation before any pilot execution."
            )
        ledger["recovery"] = {
            "note": recovery_note,
            "pending_operation": pending,
        }
    write_private_json(paths.ledger_path, ledger)
    if clean:
        _clear_pending(paths)

    return {
        "run_tag": run_tag,
        "state_readable": state_readable,
        "reconciled": clean,
        "resource_count": len(resources),
        "resource_labels": sorted(r["label"] for r in resources if r["label"]),
        "incomplete_resources": len(incomplete),
        "provider_checked": reconciliation["provider_checked"],
        "untracked_billable_count": len(reconciliation["untracked_billable"]),
        "missing_from_provider_count": len(reconciliation["missing_from_provider"]),
        "note": (
            "Reconciliation is clean: the ledger matches the observed state."
            if clean
            else "RECONCILIATION IS NOT CLEAN: possibly created, billable, or "
            "untracked resources exist. The pilot is blocked until this is "
            "resolved. Inspect the external ledger and recover locally."
        ),
    }


# -- apply -------------------------------------------------------------------------


def apply(
    run_tag: str,
    approval: str,
    *,
    paths: LifecyclePaths,
    tf_dir: Path | None = None,
    runner: CommandRunner = default_runner,
    environ: dict | None = None,
    fetch: Fetch | None = None,
    token: str | None = None,
) -> dict:
    """Applies exactly the reviewed saved plan after explicit approval.

    The approval phrase names the run tag and the saved plan's SHA-256. The
    plan, configuration, lock file, state, and commit are re-verified; a
    pending record is written before the attempt; and reconciliation runs
    after the attempt regardless of outcome.
    """
    validate_run_tag(run_tag)
    refuse_hosted_execution(environ)
    directory = tf_dir or default_terraform_dir()
    meta = verify_saved_plan(run_tag, stage="apply", paths=paths, tf_dir=directory, runner=runner)
    expected = APPLY_APPROVAL_TEMPLATE.format(run_tag=run_tag, plan_sha256=meta["plan_sha256"])
    if approval != expected:
        raise ApprovalError(
            "apply requires the exact owner approval phrase naming the run "
            f"tag and the reviewed plan digest (expected verbatim: {expected!r}); "
            "nothing was executed"
        )

    write_pending(paths, run_tag=run_tag, operation="apply", plan_sha256=meta["plan_sha256"])
    result = runner(
        ["terraform", "apply", "-input=false", str(paths.plan_path("apply"))],
        directory,
        _tf_env(paths),
    )
    report = reconcile(
        run_tag, paths=paths, tf_dir=directory, runner=runner, fetch=fetch, token=token
    )
    record_session_event(paths, "apply_attempted", {"exit_ok": result.returncode == 0})
    if result.returncode != 0:
        raise LifecycleError(
            "terraform apply failed. A recovery record and a reconciliation "
            "report were written to the run's external lifecycle directory; "
            "resources MAY have been created and MAY be billing. Resolve "
            "locally, re-run reconciliation, and tear down via the ledger "
            "workflow."
        )
    if not report["reconciled"]:
        raise LifecycleError(
            "terraform apply completed but reconciliation is NOT clean: "
            "possibly created, billable, or untracked resources exist. The "
            "pilot is blocked until reconciliation passes."
        )
    record_session_event(paths, "provisioned", {"resource_count": report["resource_count"]})
    return report


# -- identity-safe teardown --------------------------------------------------------


def load_ledger(ledger_path: Path) -> dict:
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError("the resource ledger is missing or unreadable") from exc
    if not isinstance(ledger, dict) or not ledger.get("resources"):
        raise LifecycleError("the resource ledger is empty or malformed")
    return ledger


def pilot_blockers(ledger: dict, *, pending: bool) -> list[str]:
    """Human-readable reasons the pilot must not run; empty when all gates pass."""
    blockers: list[str] = []
    if pending:
        blockers.append("a pending lifecycle operation exists")
    if not ledger.get("reconciled"):
        blockers.append("the ledger is not cleanly reconciled")
    reconciliation = ledger.get("reconciliation") or {}
    if not reconciliation.get("provider_checked"):
        blockers.append("the provider API has not been successfully checked")
    resources = ledger.get("resources") or []
    instances = [r for r in resources if r.get("type") == "linode_instance"]
    firewalls = [r for r in resources if r.get("type") == "linode_firewall"]
    if len(instances) != 1:
        blockers.append(f"expected exactly one instance in the ledger, found {len(instances)}")
    if len(firewalls) != 1:
        blockers.append(f"expected exactly one firewall in the ledger, found {len(firewalls)}")
    return blockers


def _verify_resource_identity(
    ledger_resource: dict,
    state_resource: dict | None,
    run_tag: str,
    *,
    provider_view: dict | None,
    provider_checked: bool,
) -> list[str]:
    """Every identity fact must match; returns human-readable mismatches."""
    mismatches: list[str] = []
    address = ledger_resource.get("address", "<unknown>")
    if state_resource is None:
        return [f"{address}: present in the ledger but absent from the current state"]
    expected_type = EXPECTED_RESOURCE_TYPES.get(address)
    if expected_type is None:
        return [f"{address}: not a resource this configuration is allowed to manage"]
    for field_name in ("type", "provider_id", "label"):
        if ledger_resource.get(field_name) != state_resource.get(field_name):
            mismatches.append(f"{address}: {field_name} differs between ledger and state")
    if state_resource.get("type") != expected_type:
        mismatches.append(f"{address}: unexpected resource type in state")
    expected_label_prefix = "bwlab-"
    if not str(state_resource.get("label", "")).startswith(expected_label_prefix):
        mismatches.append(f"{address}: label does not carry the expected bwlab- prefix")
    tags = set(state_resource.get("tags") or [])
    if PROJECT_TAG not in tags:
        mismatches.append(f"{address}: project tag missing")
    if f"run:{run_tag}" not in tags:
        mismatches.append(f"{address}: run tag missing")
    if provider_checked:
        if provider_view is None:
            mismatches.append(f"{address}: not visible via the read-only provider API")
        else:
            if str(provider_view.get("id", "")) != ledger_resource.get("provider_id"):
                mismatches.append(f"{address}: provider id differs from the API observation")
            if provider_view.get("label") != ledger_resource.get("label"):
                mismatches.append(f"{address}: label differs from the API observation")
            api_tags = set(provider_view.get("tags") or [])
            if PROJECT_TAG not in api_tags or f"run:{run_tag}" not in api_tags:
                mismatches.append(f"{address}: expected tags missing in the API observation")
            if ledger_resource.get("type") == "linode_instance":
                api_region = provider_view.get("region")
                if api_region and api_region != state_resource.get("region"):
                    mismatches.append(f"{address}: region differs from the API observation")
                if api_region and api_region != ledger_resource.get("region"):
                    mismatches.append(f"{address}: region differs between ledger and API")
    return mismatches


def _provider_view_path(resource: dict) -> str:
    if resource.get("type") == "linode_instance":
        return f"/linode/instances/{resource.get('provider_id')}"
    if resource.get("type") == "linode_firewall":
        return f"/networking/firewalls/{resource.get('provider_id')}"
    raise LifecycleError("the ledger contains a resource type this project never creates")


def verify_teardown_identity(
    run_tag: str,
    ledger: dict,
    *,
    paths: LifecyclePaths,
    tf_dir: Path | None = None,
    runner: CommandRunner = default_runner,
    fetch: Fetch | None = None,
    token: str | None = None,
) -> dict:
    """Fails closed unless every target's identity matches everywhere.

    Checks the Terraform address, resource type, provider id, project tag,
    run tag, and expected label of every ledger resource against the current
    state and — when a read-only token is locally available — against the
    provider API observation.
    """
    validate_run_tag(run_tag)
    if ledger.get("run_tag") != run_tag:
        raise LifecycleError(
            "ledger run_tag does not match the requested run: refusing to "
            "target another run's resources"
        )
    if not token or fetch is None:
        raise LifecycleError(
            "read-only provider verification is required before teardown; "
            "set a read-only LINODE_TOKEN locally and retry"
        )
    directory = tf_dir or default_terraform_dir()
    show = runner(["terraform", "show", "-json"], directory, _tf_env(paths))
    if show.returncode != 0:
        raise LifecycleError(
            "the current state could not be read for identity verification; failing closed"
        )
    try:
        state_by_address = {r["address"]: r for r in _state_resources(json.loads(show.stdout))}
    except json.JSONDecodeError as exc:
        raise LifecycleError("the current state rendering was unparseable") from exc

    provider_checked = True
    mismatches: list[str] = []
    for resource in ledger["resources"]:
        provider_view: dict | None = None
        try:
            provider_view = fetch(_provider_view_path(resource), token)
        except Exception:
            provider_view = None
        mismatches.extend(
            _verify_resource_identity(
                resource,
                state_by_address.get(resource.get("address")),
                run_tag,
                provider_view=provider_view,
                provider_checked=provider_checked,
            )
        )
    if mismatches:
        raise LifecycleError(
            "teardown identity verification FAILED; refusing to destroy. "
            "Mismatches: " + "; ".join(sorted(mismatches))
        )
    return {
        "verified": True,
        "provider_checked": provider_checked,
        "resource_count": len(ledger["resources"]),
    }


def plan_destroy(
    run_tag: str,
    ledger: dict,
    *,
    paths: LifecyclePaths,
    tf_dir: Path | None = None,
    runner: CommandRunner = default_runner,
    fetch: Fetch | None = None,
    token: str | None = None,
) -> dict:
    """Creates the reviewed, saved destroy plan after identity verification.

    The destroy plan must contain only delete actions, and its target
    address set must equal the ledger's address set exactly.
    """
    verify_teardown_identity(
        run_tag, ledger, paths=paths, tf_dir=tf_dir, runner=runner, fetch=fetch, token=token
    )
    meta = save_plan(run_tag, stage="destroy", paths=paths, tf_dir=tf_dir, runner=runner)
    ledger_addresses = sorted(r.get("address", "") for r in ledger["resources"])
    if sorted(meta["resource_addresses"]) != ledger_addresses:
        raise LifecycleError(
            "the destroy plan's targets do not exactly match the ledger's "
            "recorded resources; failing closed"
        )
    return meta


def confirm_deletion(
    run_tag: str,
    ledger: dict,
    *,
    token: str,
    probe: ResourceProbe = default_resource_probe,
    monotonic: MonotonicClock | None = None,
    sleeper: Sleeper | None = None,
    poll_interval_s: float = DELETION_POLL_INTERVAL_S,
    timeout_s: float = DELETION_CONFIRM_TIMEOUT_S,
) -> dict:
    """Polls the exact recorded provider ids until deletion is confirmed.

    Read-only. Success is reported only when every recorded provider id
    probes "absent". Akamai Blackwell deletions can take several minutes, so
    the default window is generous; on timeout this raises with an explicit
    billing-may-continue escalation message.
    """
    import time as _time

    validate_run_tag(run_tag)
    clock = monotonic or _time.monotonic
    wait = sleeper or _time.sleep
    remaining = {r.get("address", "<unknown>"): _provider_view_path(r) for r in ledger["resources"]}
    started = clock()
    while remaining:
        for address in sorted(remaining):
            if probe(remaining[address], token) == "absent":
                del remaining[address]
        if not remaining:
            break
        if clock() - started > timeout_s:
            raise LifecycleError(
                "deletion was NOT confirmed within the confirmation window "
                f"for: {', '.join(sorted(remaining))}. BILLING MAY CONTINUE. "
                "Escalate or retry: re-run destroy confirmation, check the "
                "provider console, and do not treat this teardown as complete."
            )
        wait(poll_interval_s)
    return {
        "deletion_confirmed": True,
        "confirmed_resources": len(ledger["resources"]),
        "elapsed_s": round(clock() - started, 3),
    }


def destroy(
    run_tag: str,
    approval: str,
    ledger: dict,
    *,
    paths: LifecyclePaths,
    tf_dir: Path | None = None,
    runner: CommandRunner = default_runner,
    environ: dict | None = None,
    fetch: Fetch | None = None,
    token: str | None = None,
    probe: ResourceProbe = default_resource_probe,
    monotonic: MonotonicClock | None = None,
    sleeper: Sleeper | None = None,
) -> dict:
    """Destroys exactly the verified saved destroy plan, then confirms deletion.

    Identity is re-verified, the saved destroy plan is re-checked against
    its recorded digest and drift rules, the approval phrase must name the
    run tag and destroy-plan digest, and success is reported only after
    read-only polling confirms every recorded provider id is gone.
    """
    validate_run_tag(run_tag)
    refuse_hosted_execution(environ)
    directory = tf_dir or default_terraform_dir()

    if not token or fetch is None:
        raise LifecycleError(
            "read-only provider verification is required before destroy; "
            "set a read-only LINODE_TOKEN locally and retry"
        )

    verify_teardown_identity(
        run_tag, ledger, paths=paths, tf_dir=directory, runner=runner, fetch=fetch, token=token
    )
    meta = verify_saved_plan(run_tag, stage="destroy", paths=paths, tf_dir=directory, runner=runner)
    ledger_addresses = sorted(r.get("address", "") for r in ledger["resources"])
    if sorted(meta["resource_addresses"]) != ledger_addresses:
        raise LifecycleError(
            "the saved destroy plan no longer matches the ledger's recorded "
            "resources; regenerate the teardown plan"
        )
    expected = DESTROY_APPROVAL_TEMPLATE.format(run_tag=run_tag, plan_sha256=meta["plan_sha256"])
    if approval != expected:
        raise ApprovalError(
            "destroy requires the exact owner approval phrase naming the run "
            f"tag and the reviewed destroy-plan digest (expected verbatim: "
            f"{expected!r}); nothing was executed"
        )

    write_pending(paths, run_tag=run_tag, operation="destroy", plan_sha256=meta["plan_sha256"])
    record_session_event(paths, "teardown_started", {})
    result = runner(
        ["terraform", "apply", "-input=false", str(paths.plan_path("destroy"))],
        directory,
        _tf_env(paths),
    )
    if result.returncode != 0:
        reconcile(run_tag, paths=paths, tf_dir=directory, runner=runner, fetch=fetch, token=token)
        raise LifecycleError(
            "terraform destroy exited nonzero; the run's resources may still "
            "exist and STILL BILL. A reconciliation record was written; re-run "
            "the teardown and the orphan report."
        )

    confirmation = confirm_deletion(
        run_tag,
        ledger,
        token=token,
        probe=probe,
        monotonic=monotonic,
        sleeper=sleeper,
    )
    updated = dict(ledger)
    updated["destroyed_at_utc"] = datetime.now(timezone.utc).isoformat()
    updated["deletion_confirmed"] = True
    write_private_json(paths.ledger_path, updated)
    _clear_pending(paths)
    record_session_event(paths, "deletion_confirmed", confirmation)
    return {
        "destroyed": True,
        **confirmation,
        "note": (
            "Only the verified saved destroy plan was executed; deletion of "
            "every recorded provider id was confirmed read-only. Run the "
            "orphan report now to verify nothing project-tagged remains."
        ),
    }


# -- session record ----------------------------------------------------------------


def record_session_event(paths: LifecyclePaths, event: str, detail: dict | None = None) -> None:
    """Appends one event to the run's external private session record."""
    session: dict = {"run_tag": paths.run_dir.name, "events": []}
    if paths.session_path.is_file():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            loaded = json.loads(paths.session_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("events"), list):
                session = loaded
    session["events"].append(
        {
            "event": event,
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "detail": detail or {},
        }
    )
    write_private_json(paths.session_path, session)


#: Session phases whose durations are derived from paired events.
_SESSION_PHASES = (
    ("provisioning", "apply_attempted", "provisioned"),
    ("setup", "provisioned", "pilot_started"),
    ("pilot", "pilot_started", "pilot_completed"),
    ("teardown", "teardown_started", "deletion_confirmed"),
)


def session_summary(paths: LifecyclePaths, *, hourly_price_usd: float | None = None) -> dict:
    """Observed billable duration and estimated cost, from recorded events.

    The billable window runs from the first successful provisioning to
    confirmed deletion — matching Akamai's billing model, where the service
    bills for its entire existence (powered off included). Idle time is the
    billable window minus the recognized phase durations.
    """
    if not paths.session_path.is_file():
        raise LifecycleError("no session record exists for this run")
    try:
        session = json.loads(paths.session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError("the session record is unreadable") from exc
    events = session.get("events") or []
    times: dict[str, datetime] = {}
    for entry in events:
        name = entry.get("event")
        if name and name not in times:
            with contextlib.suppress(ValueError, TypeError):
                times[name] = datetime.fromisoformat(entry["at_utc"])

    phases: dict[str, float | None] = {}
    for phase, start_event, end_event in _SESSION_PHASES:
        start = times.get(start_event)
        end = times.get(end_event)
        phases[f"{phase}_s"] = (
            round((end - start).total_seconds(), 1) if start and end and end >= start else None
        )

    provisioned = times.get("provisioned")
    confirmed = times.get("deletion_confirmed")
    billable_s: float | None = None
    if provisioned and confirmed and confirmed >= provisioned:
        billable_s = round((confirmed - provisioned).total_seconds(), 1)
    known_phase_s = sum(v for k, v in phases.items() if v is not None and k != "provisioning_s")
    idle_s = (
        round(billable_s - known_phase_s, 1)
        if billable_s is not None and billable_s >= known_phase_s
        else None
    )
    estimated_cost = (
        round(hourly_price_usd * billable_s / 3600.0, 2)
        if hourly_price_usd is not None and billable_s is not None
        else None
    )
    return {
        "run_tag": session.get("run_tag"),
        "event_count": len(events),
        "phases": phases,
        "observed_billable_s": billable_s,
        "idle_s": idle_s,
        "hourly_price_usd": hourly_price_usd,
        "estimated_total_cost_usd": estimated_cost,
        "deletion_confirmed": "deletion_confirmed" in times,
        "note": (
            "Billing runs from provisioning to CONFIRMED deletion (Akamai "
            "bills while the service exists, powered off included). A null "
            "billable duration means provisioning or confirmed deletion has "
            "not been recorded — the session is not over."
        ),
    }


# -- orphan report -----------------------------------------------------------------


def orphan_report(token: str, *, fetch: Fetch, ledger: dict | None = None) -> dict:
    """Read-only sweep of project-tagged billable resources vs the ledger.

    Lists instances, volumes, and firewalls via GET requests, keeps those
    carrying the project tag (or any untagged GPU instance, which is always
    suspicious for this account), and classifies them against the ledger.
    Returns a reporting dict; the caller decides what to print (labels and
    counts — resource ids belong in externally stored reports, not terminal
    scrollback that might be pasted into issues).
    """
    from blackwell_lab.cloud.preflight import _paginated  # read-only GET helper

    try:
        instances = _paginated(fetch, "/linode/instances", token)
        volumes = _paginated(fetch, "/volumes", token)
        firewalls = _paginated(fetch, "/networking/firewalls", token)
    except Exception:
        raise LifecycleError(
            "the read-only resource sweep failed (token scope, expiry, or "
            "connectivity); no error payload is echoed"
        ) from None

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
    for firewall in firewalls:
        if PROJECT_TAG in (firewall.get("tags") or []):
            findings.append(_classify("firewall", firewall))

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
