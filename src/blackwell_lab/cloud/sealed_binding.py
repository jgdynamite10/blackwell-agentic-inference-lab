"""Bind a sealed D-0023 custody stage to P2 qualification execution (D-0024).

The frozen qualification config carries a content-free ``sealed_set``
binding: digests, identity, stage, and count. It never carries a
filesystem path, so the config digest is portable across machines. The
custody directory is a separate runtime argument (``--custody-dir``) that
is never printed and never persisted.

The loader is **stage specific**. It verifies the public manifest, the
receipt, and the private index structurally for both stages without
opening any blob, then opens, hashes, and decodes **only the selected
stage's** blobs. The other stage's bodies are never read, decoded, copied,
or returned. Every failure is a :class:`SealedSetError` whose message is a
short reason code — no paths, task ids, or payload content.

Digests here are SHA-256 integrity checks. They are not encryption and not
a cryptographic seal; see :mod:`blackwell_lab.sealed_sets`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blackwell_lab.cloud.sealed_payload import (
    PAYLOAD_SCHEMA_VERSION,
    SealedPayloadError,
    SealedTask,
    decode_sealed_task,
)
from blackwell_lab.sealed_sets.custody import (
    CustodyReceipt,
    require_external_directory,
    validate_public_manifest,
)
from blackwell_lab.sealed_sets.model import (
    STAGES,
    TASKS_PER_STAGE,
    CustodyError,
    running_controller_digest,
    set_identity_from_digests,
    sha256_digest,
)
from blackwell_lab.workload.sampling import TaskInstance
from blackwell_lab.workload.scenarios import Scenario
from blackwell_lab.workload.validation import ConfigError

BINDING_SCHEMA_VERSION = "1.0.0"
SEALED_STAGES = STAGES
SEALED_TASK_COUNT = TASKS_PER_STAGE
#: Candidates whose development and holdout stages execute sealed input.
SEALED_CANDIDATES = ("P2",)
BINDING_FIELDS = (
    "schema_version",
    "custody_manifest_sha256",
    "custody_controller_digest",
    "import_request_digest",
    "set_identity",
    "stage",
    "stage_aggregate_digest",
    "task_count",
    "payload_schema_version",
)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_MANIFEST_NAME = "manifest.json"
_RECEIPT_NAME = "receipt.json"
_INDEX_NAME = "index.json"
_PRIVATE_DIR = "private"
_BLOB_DIR = "blobs"


class SealedSetError(ConfigError):
    """Fail-closed sealed-binding error. ``reason`` is a short public code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"sealed-set: {reason}")


def requires_sealed_set(candidate_id: str, stage: str) -> bool:
    """True only for P2 development and P2 holdout (never C1, C2, P1, or freeze)."""
    return candidate_id in SEALED_CANDIDATES and stage in SEALED_STAGES


@dataclass(frozen=True)
class SealedSetBinding:
    """Content-free identity of one sealed stage, as frozen in the config."""

    schema_version: str
    custody_manifest_sha256: str
    custody_controller_digest: str
    import_request_digest: str
    set_identity: str
    stage: str
    stage_aggregate_digest: str
    task_count: int
    payload_schema_version: str

    @classmethod
    def from_config(cls, section: object, *, stage: str) -> SealedSetBinding:
        if section is None:
            raise SealedSetError("binding-missing")
        if not isinstance(section, dict):
            raise SealedSetError("binding-malformed")
        if set(section) != set(BINDING_FIELDS):
            raise SealedSetError("binding-malformed")
        for key in BINDING_FIELDS:
            value = section[key]
            if key == "task_count":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise SealedSetError("binding-malformed")
            elif not isinstance(value, str) or not value:
                raise SealedSetError("binding-malformed")
        if section["schema_version"] != BINDING_SCHEMA_VERSION:
            raise SealedSetError("binding-version-unsupported")
        if section["payload_schema_version"] != PAYLOAD_SCHEMA_VERSION:
            raise SealedSetError("payload-version-unsupported")
        if not _HEX64_RE.fullmatch(section["custody_manifest_sha256"]):
            raise SealedSetError("binding-malformed")
        for key in ("custody_controller_digest", "import_request_digest", "set_identity"):
            if not _DIGEST_RE.fullmatch(section[key]):
                raise SealedSetError("binding-malformed")
        if not _DIGEST_RE.fullmatch(section["stage_aggregate_digest"]):
            raise SealedSetError("binding-malformed")
        if section["stage"] not in SEALED_STAGES:
            raise SealedSetError("binding-malformed")
        if section["stage"] != stage:
            raise SealedSetError("binding-stage-mismatch")
        if section["task_count"] != SEALED_TASK_COUNT:
            raise SealedSetError("binding-task-count")
        return cls(**{key: section[key] for key in BINDING_FIELDS})

    def provenance(self) -> dict[str, Any]:
        """Content-free provenance recorded in manifests and receipts."""
        return {key: getattr(self, key) for key in BINDING_FIELDS}


@dataclass(frozen=True)
class SealedStageTasks:
    """The selected stage, decoded and ordered by task id."""

    binding: SealedSetBinding
    tasks: tuple[SealedTask, ...]

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(task.task_id for task in self.tasks)

    @property
    def instances(self) -> list[TaskInstance]:
        return [task.instance for task in self.tasks]

    @property
    def scenarios_by_id(self) -> dict[str, Scenario]:
        return {task.scenario.scenario_id: task.scenario for task in self.tasks}

    @property
    def scenario_ids(self) -> tuple[str, ...]:
        return tuple(sorted({task.scenario.scenario_id for task in self.tasks}))


def binding_from_config(config: dict, *, candidate_id: str, stage: str) -> SealedSetBinding | None:
    """Parse and validate the config binding; ``None`` when the cell is not sealed.

    A ``sealed_set`` section on a candidate or stage that does not execute
    sealed input is refused, so C1, C2, P1, and freeze configs stay exactly
    as they were.
    """
    section = config.get("sealed_set") if isinstance(config, dict) else None
    if not requires_sealed_set(candidate_id, stage):
        if section is not None or (isinstance(config, dict) and "sealed_set" in config):
            raise SealedSetError("binding-not-applicable")
        return None
    return SealedSetBinding.from_config(section, stage=stage)


def resolve_sealed_stage(
    config: dict,
    *,
    candidate_id: str,
    stage: str,
    custody_dir: str | None,
    repo: Path,
    opened: Callable[[Path], None] | None = None,
) -> SealedStageTasks | None:
    """Load the selected sealed stage for this cell, or ``None`` if not sealed.

    ``--custody-dir`` is refused for cells that do not use sealed input.
    """
    binding = binding_from_config(config, candidate_id=candidate_id, stage=stage)
    if binding is None:
        if custody_dir is not None:
            raise SealedSetError("custody-dir-not-applicable")
        return None
    if custody_dir is None:
        raise SealedSetError("custody-dir-required")
    return load_sealed_stage(
        binding,
        custody_dir=custody_dir,
        repo=repo,
        canonical_commit=str(config.get("canonical_commit", "")),
        opened=opened,
    )


def load_sealed_stage(
    binding: SealedSetBinding,
    *,
    custody_dir: str | Path,
    repo: Path,
    canonical_commit: str,
    opened: Callable[[Path], None] | None = None,
) -> SealedStageTasks:
    """Verify the custody set and decode only ``binding.stage``.

    ``opened`` is an optional observer invoked with every blob path this
    loader reads; tests use it to prove the other stage is never opened.
    """
    try:
        return _load(binding, custody_dir, repo, canonical_commit, opened)
    except SealedSetError:
        raise
    except CustodyError as exc:
        raise SealedSetError(exc.reason) from None
    except SealedPayloadError as exc:
        raise SealedSetError(exc.reason) from None
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise SealedSetError("custody-unreadable") from None


def _load(
    binding: SealedSetBinding,
    custody_dir: str | Path,
    repo: Path,
    canonical_commit: str,
    opened: Callable[[Path], None] | None,
) -> SealedStageTasks:
    root = require_external_directory(custody_dir, repo=repo)
    if not root.is_dir():
        raise SealedSetError("custody-missing")
    _require_tree_modes(root)

    manifest_path = root / _MANIFEST_NAME
    manifest_bytes = _read_regular(manifest_path, reason="manifest-invalid")
    if hashlib.sha256(manifest_bytes).hexdigest() != binding.custody_manifest_sha256:
        raise SealedSetError("manifest-hash-mismatch")
    manifest = validate_public_manifest(_parse_json(manifest_bytes, reason="manifest-invalid"))
    if _canonical_bytes(manifest) != manifest_bytes:
        raise SealedSetError("manifest-invalid")
    if not manifest.get("finalized"):
        raise SealedSetError("manifest-invalid")

    if manifest["controller_commit"] != canonical_commit:
        raise SealedSetError("commit-mismatch")
    if manifest["controller_digest"] != binding.custody_controller_digest:
        raise SealedSetError("controller-digest-mismatch")
    if running_controller_digest() != binding.custody_controller_digest:
        raise SealedSetError("controller-digest-mismatch")
    if manifest["import_request_digest"] != binding.import_request_digest:
        raise SealedSetError("import-request-mismatch")
    if manifest["set_identity"] != binding.set_identity:
        raise SealedSetError("set-identity-mismatch")
    stage = binding.stage
    if stage not in SEALED_STAGES:
        raise SealedSetError("binding-stage-mismatch")
    if manifest[f"{stage}_aggregate_digest"] != binding.stage_aggregate_digest:
        raise SealedSetError("aggregate-mismatch")
    if manifest[f"{stage}_task_count"] != binding.task_count:
        raise SealedSetError("task-count-mismatch")

    receipt_bytes = _read_regular(root / _RECEIPT_NAME, reason="receipt-invalid")
    receipt = _parse_json(receipt_bytes, reason="receipt-invalid")
    expected_receipt = _receipt_from_manifest(manifest).public_dict()
    if receipt != expected_receipt:
        raise SealedSetError("receipt-invalid")

    private = root / _PRIVATE_DIR
    index_bytes = _read_regular(private / _INDEX_NAME, reason="tamper")
    index = _parse_json(index_bytes, reason="tamper")
    rows_by_stage = _check_index_structure(index, manifest, private / _BLOB_DIR)

    tasks: list[SealedTask] = []
    blob_root = private / _BLOB_DIR
    for task_id, digest in rows_by_stage[stage]:
        blob = blob_root / digest.removeprefix("sha256:")
        if opened is not None:
            opened(blob)
        payload = _read_regular(blob, reason="tamper")
        if sha256_digest(payload) != digest:
            raise SealedSetError("tamper")
        tasks.append(decode_sealed_task(payload, expected_task_id=task_id))

    _require_consistent_tasks(tasks)
    if len(tasks) != binding.task_count:
        raise SealedSetError("task-count-mismatch")
    return SealedStageTasks(binding=binding, tasks=tuple(tasks))


def require_manifest_provenance(manifest: dict, binding: SealedSetBinding) -> None:
    """Fail unless ``manifest.workload.sealed_set`` equals the config binding exactly."""
    workload = manifest.get("workload") if isinstance(manifest, dict) else None
    recorded = workload.get("sealed_set") if isinstance(workload, dict) else None
    if recorded != binding.provenance():
        raise SealedSetError("provenance-mismatch")
    if workload.get("catalog_digest") != binding.stage_aggregate_digest:
        raise SealedSetError("provenance-mismatch")
    if workload.get("tasks_per_repetition") != binding.task_count:
        raise SealedSetError("provenance-mismatch")


def require_sealed_stage_evidence(sealed: SealedStageTasks, outcomes: list[object]) -> None:
    """Measured observations must be exactly the sealed stage, one per task."""
    if len(outcomes) != sealed.binding.task_count:
        raise SealedSetError("incomplete-stage-evidence")
    observed_instances: list[str] = []
    observed_templates: set[str] = set()
    for outcome in outcomes:
        instance_id, template_id = _outcome_ids(outcome)
        observed_instances.append(instance_id)
        observed_templates.add(template_id)
    if sorted(observed_instances) != sorted(sealed.task_ids):
        raise SealedSetError("incomplete-stage-evidence")
    if observed_templates != set(sealed.scenario_ids):
        raise SealedSetError("incomplete-stage-evidence")


def _outcome_ids(outcome: object) -> tuple[str, str]:
    if isinstance(outcome, dict):
        return str(outcome.get("instance_id") or ""), str(outcome.get("template_id") or "")
    instance = getattr(outcome, "instance", None)
    execution = getattr(outcome, "execution", None)
    instance_id = getattr(instance, "instance_id", None) or getattr(execution, "instance_id", None)
    template_id = getattr(execution, "scenario_id", None) or getattr(instance, "template_id", None)
    return str(instance_id or ""), str(template_id or "")


def _require_consistent_tasks(tasks: list[SealedTask]) -> None:
    scenarios: dict[str, Scenario] = {}
    surfaces: set[tuple[str, str, int]] = set()
    ids: set[str] = set()
    for task in tasks:
        if task.task_id in ids:
            raise SealedSetError("duplicate-task")
        ids.add(task.task_id)
        existing = scenarios.get(task.scenario.scenario_id)
        if existing is None:
            scenarios[task.scenario.scenario_id] = task.scenario
        elif existing != task.scenario:
            raise SealedSetError("malformed-task")
        surface = (
            task.instance.template_id,
            task.instance.tracking_id,
            task.instance.reported_minute,
        )
        if surface in surfaces:
            raise SealedSetError("duplicate-task")
        surfaces.add(surface)


def _check_index_structure(
    index: object, manifest: dict, blob_root: Path
) -> dict[str, list[tuple[str, str]]]:
    """Validate both stages' index rows against the manifest without opening blobs."""
    expected_keys = {"development", "holdout", "development_order", "holdout_order"}
    if not isinstance(index, dict) or set(index) != expected_keys:
        raise SealedSetError("tamper")
    seen_ids: set[str] = set()
    seen_digests: set[str] = set()
    orders: dict[str, list[str]] = {}
    rows_by_stage: dict[str, list[tuple[str, str]]] = {}
    for stage in SEALED_STAGES:
        rows = index[stage]
        order = index[f"{stage}_order"]
        if not isinstance(rows, list) or len(rows) != TASKS_PER_STAGE:
            raise SealedSetError("tamper")
        if not isinstance(order, list) or len(order) != TASKS_PER_STAGE:
            raise SealedSetError("tamper")
        if not all(isinstance(item, str) and _DIGEST_RE.fullmatch(item) for item in order):
            raise SealedSetError("tamper")
        orders[stage] = list(order)
        parsed: list[tuple[str, str]] = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"task_id", "content_digest"}:
                raise SealedSetError("tamper")
            task_id = row["task_id"]
            digest = row["content_digest"]
            if not isinstance(task_id, str) or not isinstance(digest, str):
                raise SealedSetError("tamper")
            if not _DIGEST_RE.fullmatch(digest):
                raise SealedSetError("tamper")
            if task_id in seen_ids or digest in seen_digests:
                raise SealedSetError("tamper")
            seen_ids.add(task_id)
            seen_digests.add(digest)
            blob = blob_root / digest.removeprefix("sha256:")
            info = _lstat(blob, reason="tamper")
            if not stat.S_ISREG(info.st_mode):
                raise SealedSetError("tamper")
            parsed.append((task_id, digest))
        if parsed != sorted(parsed, key=lambda item: item[0]):
            raise SealedSetError("tamper")
        preimage = "".join(f"{task_id}\t{digest}\n" for task_id, digest in parsed).encode()
        if sha256_digest(preimage) != manifest[f"{stage}_aggregate_digest"]:
            raise SealedSetError("tamper")
        if sorted(orders[stage]) != sorted(digest for _task_id, digest in parsed):
            raise SealedSetError("tamper")
        rows_by_stage[stage] = parsed
    if set_identity_from_digests(orders["development"], orders["holdout"]) != manifest.get(
        "set_identity"
    ):
        raise SealedSetError("tamper")
    if tuple(sorted(seen_digests)) != tuple(manifest["file_digests"]):
        raise SealedSetError("tamper")
    return rows_by_stage


def _receipt_from_manifest(manifest: dict) -> CustodyReceipt:
    return CustodyReceipt(
        status="pass",
        operation="import",
        development_task_count=manifest["development_task_count"],
        holdout_task_count=manifest["holdout_task_count"],
        development_aggregate_digest=manifest["development_aggregate_digest"],
        holdout_aggregate_digest=manifest["holdout_aggregate_digest"],
        controller_commit=manifest["controller_commit"],
        controller_digest=manifest["controller_digest"],
        custody_manifest_digest=manifest["custody_manifest_digest"],
        set_identity=manifest["set_identity"],
        import_request_digest=manifest["import_request_digest"],
    )


def _require_tree_modes(root: Path) -> None:
    if root.is_symlink() or _mode(root) != _DIR_MODE:
        raise SealedSetError("permissive-mode")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink() or _mode(current) != _DIR_MODE:
            raise SealedSetError("permissive-mode")
        for name in dirnames:
            if (current / name).is_symlink():
                raise SealedSetError("symlink-escape")
        for name in filenames:
            child = current / name
            if child.is_symlink():
                raise SealedSetError("symlink-escape")
            if _mode(child) != _FILE_MODE:
                raise SealedSetError("permissive-mode")


def _lstat(path: Path, *, reason: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError:
        raise SealedSetError(reason) from None


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


_OPEN_FLAGS = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)


def _read_regular(path: Path, *, reason: str) -> bytes:
    info = _lstat(path, reason=reason)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SealedSetError(reason)
    if stat.S_IMODE(info.st_mode) != _FILE_MODE:
        raise SealedSetError("permissive-mode")
    # Open through os.open with O_NOFOLLOW and re-check the descriptor so the
    # bytes hashed are the regular file that was inspected, not a swap-in.
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError:
        raise SealedSetError(reason) from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise SealedSetError(reason)
        if stat.S_IMODE(opened.st_mode) != _FILE_MODE:
            raise SealedSetError("permissive-mode")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read()
    except OSError:
        raise SealedSetError(reason) from None
    finally:
        if fd >= 0:
            os.close(fd)


def _parse_json(payload: bytes, *, reason: str) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise SealedSetError(reason) from None


def _canonical_bytes(document: object) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
