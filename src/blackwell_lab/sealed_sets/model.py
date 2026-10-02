"""Pure custody model: opaque bundles, digests, and the approval phrase.

No I/O and no task generation. Digests are SHA-256 integrity checks.
They are not encryption and not a cryptographic seal.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = "1.0.0"
MANIFEST_KIND = "sealed-qualification-set"
INTEGRITY_STATEMENT = (
    "SHA-256 digests provide integrity, not confidentiality. "
    "This manifest is not encryption and is not a cryptographic seal."
)
DEVELOPMENT_STAGE = "development"
HOLDOUT_STAGE = "holdout"
STAGES = (DEVELOPMENT_STAGE, HOLDOUT_STAGE)
TASKS_PER_STAGE = 20
MAX_CONTENT_BYTES = 1_048_576

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

APPROVAL_TEMPLATE = (
    "I approve sealed qualification-set generation at canonical commit "
    "{commit} using controller digest {controller_digest}"
)


class CustodyError(Exception):
    """Fail-closed custody error. ``reason`` is a short public code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class OpaqueTask:
    """One opaque bundle member. ``content`` is uninterpreted bytes."""

    task_id: str
    content: bytes


@dataclass(frozen=True)
class StageDigest:
    task_count: int
    aggregate_digest: str
    file_digests: tuple[str, ...]


@dataclass(frozen=True)
class BundleValidation:
    development: StageDigest
    holdout: StageDigest
    file_digests: tuple[str, ...]


def sha256_digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def approval_phrase(commit: str, controller_digest: str) -> str:
    """Exact owner phrase for a later generation. This function does not approve."""
    if not _COMMIT_RE.fullmatch(commit) or not _DIGEST_RE.fullmatch(controller_digest):
        raise CustodyError("approval-mismatch")
    return APPROVAL_TEMPLATE.format(commit=commit, controller_digest=controller_digest)


def controller_source_digest() -> str:
    """Digest of this package's Python sources, in filename order.

    The preimage is ``name NUL sha256(file-bytes) NUL`` for each ``*.py``
    file directly in the package directory. It identifies the controller
    that produced a manifest. It does not encrypt that source.
    """
    root = Path(__file__).resolve().parent
    files = sorted(path for path in root.glob("*.py") if path.is_file())
    if not files:
        raise CustodyError("controller-digest-mismatch")
    hasher = hashlib.sha256()
    for path in files:
        hasher.update(path.name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(path.read_bytes()).digest())
        hasher.update(b"\0")
    return "sha256:" + hasher.hexdigest()


def require_commit(commit: str) -> str:
    if not _COMMIT_RE.fullmatch(commit):
        raise CustodyError("commit-mismatch")
    return commit


def require_digest(value: str) -> str:
    if not _DIGEST_RE.fullmatch(value):
        raise CustodyError("controller-digest-mismatch")
    return value


def _require_task(task: OpaqueTask) -> None:
    if not isinstance(task, OpaqueTask):
        raise CustodyError("bundle-shape")
    if not isinstance(task.task_id, str) or _ID_RE.fullmatch(task.task_id) is None:
        raise CustodyError("unsafe-id")
    if not isinstance(task.content, bytes) or not task.content:
        raise CustodyError("bundle-shape")
    if len(task.content) > MAX_CONTENT_BYTES:
        raise CustodyError("bundle-shape")


def _stage_digest(tasks: Sequence[OpaqueTask]) -> StageDigest:
    if len(tasks) != TASKS_PER_STAGE:
        raise CustodyError("task-count")
    seen_ids: set[str] = set()
    seen_digests: set[str] = set()
    rows: list[tuple[str, str]] = []
    for task in tasks:
        _require_task(task)
        if task.task_id in seen_ids:
            raise CustodyError("duplicate-id")
        digest = sha256_digest(task.content)
        if digest in seen_digests:
            raise CustodyError("duplicate-content")
        seen_ids.add(task.task_id)
        seen_digests.add(digest)
        rows.append((task.task_id, digest))
    rows.sort(key=lambda item: item[0])
    preimage = "".join(f"{task_id}\t{digest}\n" for task_id, digest in rows).encode("utf-8")
    return StageDigest(
        task_count=len(rows),
        aggregate_digest=sha256_digest(preimage),
        file_digests=tuple(digest for _task_id, digest in rows),
    )


def validate_bundles(
    development: Sequence[OpaqueTask], holdout: Sequence[OpaqueTask]
) -> BundleValidation:
    """Validate counts, ids, and content separation. Does not write."""
    dev = _stage_digest(development)
    hold = _stage_digest(holdout)
    dev_ids = {task.task_id for task in development}
    hold_ids = {task.task_id for task in holdout}
    if dev_ids & hold_ids or set(dev.file_digests) & set(hold.file_digests):
        raise CustodyError("stage-separation")
    file_digests = tuple(sorted((*dev.file_digests, *hold.file_digests)))
    if len(file_digests) != TASKS_PER_STAGE * 2:
        raise CustodyError("duplicate-content")
    return BundleValidation(development=dev, holdout=hold, file_digests=file_digests)


def canonical_manifest_bytes(document: dict) -> bytes:
    """Bytes covered by the custody manifest digest.

    ``custody_manifest_digest`` is excluded so the digest is not circular.
    """
    body = {key: value for key, value in document.items() if key != "custody_manifest_digest"}
    return (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def build_public_manifest(
    *,
    commit: str,
    controller_digest: str,
    validation: BundleValidation,
) -> dict:
    document = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "finalized": True,
        "integrity_statement": INTEGRITY_STATEMENT,
        "controller_commit": require_commit(commit),
        "controller_digest": require_digest(controller_digest),
        "development_task_count": validation.development.task_count,
        "holdout_task_count": validation.holdout.task_count,
        "development_aggregate_digest": validation.development.aggregate_digest,
        "holdout_aggregate_digest": validation.holdout.aggregate_digest,
        "file_digests": list(validation.file_digests),
    }
    document["custody_manifest_digest"] = sha256_digest(canonical_manifest_bytes(document))
    return document


def synthetic_placeholders() -> tuple[tuple[OpaqueTask, ...], tuple[OpaqueTask, ...]]:
    """Fixed artificial payloads for offline validation. Not qualification tasks."""
    development = tuple(
        OpaqueTask(
            f"ph{index:02d}devplaceholder",
            f"synthetic-placeholder-development-{index:02d}\n".encode(),
        )
        for index in range(TASKS_PER_STAGE)
    )
    holdout = tuple(
        OpaqueTask(
            f"ph{index:02d}holdplaceholder",
            f"synthetic-placeholder-holdout-{index:02d}\n".encode(),
        )
        for index in range(TASKS_PER_STAGE)
    )
    return development, holdout
