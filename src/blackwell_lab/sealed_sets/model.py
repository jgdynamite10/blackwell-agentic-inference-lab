"""Pure custody model: opaque bundles, digests, and the approval phrase.

No I/O and no task generation. Digests are SHA-256 integrity checks.
They are not encryption and not a cryptographic seal.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = "1.2.0"
REQUEST_VERSION = "1.0.0"
MANIFEST_KIND = "sealed-qualification-set"
#: Private layout of an execution-eligible custody set (D-0024). Each stage
#: has its own private index and blob directory, so a stage-specific reader
#: never has to open, list, or stat anything that belongs to the other stage.
PRIVATE_LAYOUT = "stage-separated"
STAGE_INDEX_KIND = "sealed-qualification-stage-index"
REPLAY_SCOPE = "selected-custody-location"
INTEGRITY_STATEMENT = (
    "SHA-256 digests provide integrity, not confidentiality. "
    "This manifest is not encryption and is not a cryptographic seal. "
    "Replay protection applies only to the selected custody location "
    "and is not a global anti-replay guarantee."
)
CONTROLLER_SOURCE_PATHS = (
    "src/blackwell_lab/sealed_sets/__init__.py",
    "src/blackwell_lab/sealed_sets/controller.py",
    "src/blackwell_lab/sealed_sets/custody.py",
    "src/blackwell_lab/sealed_sets/model.py",
)
_GIT_REGULAR_MODES = frozenset({"100644", "100755"})
DEVELOPMENT_STAGE = "development"
HOLDOUT_STAGE = "holdout"
STAGES = (DEVELOPMENT_STAGE, HOLDOUT_STAGE)
TASKS_PER_STAGE = 20
MAX_CONTENT_BYTES = 1_048_576

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

APPROVAL_TEMPLATE = (
    "I approve sealed qualification-set import using request sha256:{request_digest}"
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


def approval_phrase(import_request_digest: str) -> str:
    """Exact owner phrase for one import request. This function does not approve."""
    if not _DIGEST_RE.fullmatch(import_request_digest):
        raise CustodyError("approval-mismatch")
    return APPROVAL_TEMPLATE.format(request_digest=import_request_digest.removeprefix("sha256:"))


def controller_digest_from_sources(entries: Sequence[tuple[str, str, bytes]]) -> str:
    """Digest a controller-source sequence.

    Each entry is ``(repo-relative path, git mode, blob bytes)``. The
    preimage is ``path NUL mode NUL sha256(blob) NUL`` in the supplied
    order. Production callers pass :data:`CONTROLLER_SOURCE_PATHS` order.
    A different order is a different digest. This is integrity, not a seal.
    """
    if len(entries) != len(CONTROLLER_SOURCE_PATHS):
        raise CustodyError("controller-digest-mismatch")
    hasher = hashlib.sha256()
    for path, mode, blob in entries:
        if (
            not isinstance(path, str)
            or not isinstance(mode, str)
            or not isinstance(blob, bytes)
            or mode not in _GIT_REGULAR_MODES
            or not blob
        ):
            raise CustodyError("controller-digest-mismatch")
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(mode.encode("ascii"))
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(blob).digest())
        hasher.update(b"\0")
    return "sha256:" + hasher.hexdigest()


def _running_git_mode(path: Path) -> str:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CustodyError("controller-digest-mismatch")
    if stat.S_IMODE(info.st_mode) & 0o111:
        return "100755"
    return "100644"


def running_controller_parts() -> tuple[tuple[str, str, bytes], ...]:
    """Bytes and modes of the controller sources that are executing now."""
    root = Path(__file__).resolve().parent
    parts: list[tuple[str, str, bytes]] = []
    for rel in CONTROLLER_SOURCE_PATHS:
        path = root / Path(rel).name
        if path.is_symlink() or not path.is_file():
            raise CustodyError("controller-digest-mismatch")
        parts.append((rel, _running_git_mode(path), path.read_bytes()))
    return tuple(parts)


def running_controller_digest() -> str:
    """Digest of the executing controller sources, in canonical path order."""
    return controller_digest_from_sources(running_controller_parts())


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


def canonical_document_bytes(document: dict, *, exclude: str) -> bytes:
    """Canonical JSON for one digest, with ``exclude`` removed."""
    body = {key: value for key, value in document.items() if key != exclude}
    return (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def canonical_bytes(document: object) -> bytes:
    """Canonical JSON bytes of a whole document (sorted keys, trailing newline)."""
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


@dataclass(frozen=True)
class StageIndex:
    """Verified content of one stage-private index.

    ``rows`` are ``(task_id, content_digest)`` sorted by task id; ``order``
    is the supplied (import-time) sequence of content digests. Neither
    carries bodies. This object is private and is never printed.
    """

    stage: str
    rows: tuple[tuple[str, str], ...]
    order: tuple[str, ...]

    @property
    def aggregate_digest(self) -> str:
        return stage_aggregate_from_rows(self.rows)

    @property
    def content_digests(self) -> tuple[str, ...]:
        return tuple(digest for _task_id, digest in self.rows)


def stage_aggregate_from_rows(rows: Sequence[tuple[str, str]]) -> str:
    """The public stage aggregate: SHA-256 over id-sorted ``id TAB digest NL`` lines."""
    ordered = sorted(rows, key=lambda item: item[0])
    preimage = "".join(f"{task_id}\t{digest}\n" for task_id, digest in ordered).encode("utf-8")
    return sha256_digest(preimage)


def build_stage_index(stage: str, tasks: Sequence[OpaqueTask]) -> dict:
    """The stage-private index document for one stage (``PRIVATE_LAYOUT``).

    Rows are sorted by task id; ``order`` keeps the supplied sequence of
    content digests that the set identity binds. The document is private:
    it names task identifiers and must never be printed or copied into a
    public artifact.
    """
    if stage not in STAGES:
        raise CustodyError("stage-separation")
    digest = _stage_digest(tasks)
    rows = sorted(
        (
            {"task_id": task.task_id, "content_digest": sha256_digest(task.content)}
            for task in tasks
        ),
        key=lambda row: row["task_id"],
    )
    return {
        "kind": STAGE_INDEX_KIND,
        "schema_version": SCHEMA_VERSION,
        "private_layout": PRIVATE_LAYOUT,
        "stage": stage,
        "task_count": digest.task_count,
        "aggregate_digest": digest.aggregate_digest,
        "rows": rows,
        "order": [sha256_digest(task.content) for task in tasks],
    }


def stage_index_digest(document: dict) -> str:
    """Digest of the canonical stage-index bytes; recorded in the public manifest."""
    return sha256_digest(canonical_bytes(document))


def verify_stage_index(document: object, *, stage: str, manifest: dict) -> StageIndex:
    """Verify one stage-private index against the public manifest.

    Uses only that stage's public fields (count, aggregate, index digest).
    Nothing belonging to the other stage is consulted, so a stage-specific
    reader can call this without ever observing other-stage metadata.
    """
    if stage not in STAGES:
        raise CustodyError("stage-separation")
    if not isinstance(document, dict):
        raise CustodyError("tamper")
    expected_keys = {
        "kind",
        "schema_version",
        "private_layout",
        "stage",
        "task_count",
        "aggregate_digest",
        "rows",
        "order",
    }
    if set(document) != expected_keys:
        raise CustodyError("tamper")
    if document["kind"] != STAGE_INDEX_KIND:
        raise CustodyError("tamper")
    if document["schema_version"] != SCHEMA_VERSION or document["private_layout"] != PRIVATE_LAYOUT:
        raise CustodyError("layout-version-unsupported")
    if stage_index_digest(document) != manifest.get(f"{stage}_index_digest"):
        raise CustodyError("tamper")
    if document["stage"] != stage:
        raise CustodyError("tamper")
    rows_raw = document["rows"]
    order_raw = document["order"]
    if not isinstance(rows_raw, list) or len(rows_raw) != TASKS_PER_STAGE:
        raise CustodyError("tamper")
    if not isinstance(order_raw, list) or len(order_raw) != TASKS_PER_STAGE:
        raise CustodyError("tamper")
    if document["task_count"] != TASKS_PER_STAGE:
        raise CustodyError("tamper")
    if manifest.get(f"{stage}_task_count") != TASKS_PER_STAGE:
        raise CustodyError("tamper")
    seen_ids: set[str] = set()
    seen_digests: set[str] = set()
    rows: list[tuple[str, str]] = []
    for row in rows_raw:
        if not isinstance(row, dict) or set(row) != {"task_id", "content_digest"}:
            raise CustodyError("tamper")
        task_id = row["task_id"]
        digest = row["content_digest"]
        if not isinstance(task_id, str) or _ID_RE.fullmatch(task_id) is None:
            raise CustodyError("tamper")
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            raise CustodyError("tamper")
        if task_id in seen_ids or digest in seen_digests:
            raise CustodyError("tamper")
        seen_ids.add(task_id)
        seen_digests.add(digest)
        rows.append((task_id, digest))
    if rows != sorted(rows, key=lambda item: item[0]):
        raise CustodyError("tamper")
    if not all(isinstance(item, str) and _DIGEST_RE.fullmatch(item) for item in order_raw):
        raise CustodyError("tamper")
    order = tuple(str(item) for item in order_raw)
    if sorted(order) != sorted(seen_digests):
        raise CustodyError("tamper")
    aggregate = stage_aggregate_from_rows(rows)
    if aggregate != document["aggregate_digest"]:
        raise CustodyError("tamper")
    if aggregate != manifest.get(f"{stage}_aggregate_digest"):
        raise CustodyError("tamper")
    return StageIndex(stage=stage, rows=tuple(rows), order=order)


def require_execution_layout(document: object) -> dict:
    """Gate every reader on the stage-separated layout before any other check.

    An older combined-index package (schema 1.1.0) is a historical custody
    object: it is not mutated, not migrated, and not execution-eligible.
    """
    if not isinstance(document, dict):
        raise CustodyError("manifest-invalid")
    if document.get("kind") != MANIFEST_KIND:
        raise CustodyError("manifest-invalid")
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or document.get("private_layout") != PRIVATE_LAYOUT
    ):
        raise CustodyError("layout-version-unsupported")
    return document


def canonical_manifest_bytes(document: dict) -> bytes:
    """Bytes covered by the custody manifest digest.

    ``custody_manifest_digest`` is excluded so the digest is not circular.
    """
    return canonical_document_bytes(document, exclude="custody_manifest_digest")


def set_identity_from_digests(development: Sequence[str], holdout: Sequence[str]) -> str:
    """Order-sensitive identity. The arguments are content digests, not bodies."""
    lines = ["development", *development, "holdout", *holdout]
    return sha256_digest(("\n".join(lines) + "\n").encode("utf-8"))


def set_identity_for(development: Sequence[OpaqueTask], holdout: Sequence[OpaqueTask]) -> str:
    """Order-sensitive identity of content digests. Ids and bodies are omitted.

    Reordering the supplied tasks changes this identity even when the
    id-sorted stage aggregates stay the same.
    """
    return set_identity_from_digests(
        [sha256_digest(task.content) for task in development],
        [sha256_digest(task.content) for task in holdout],
    )


def build_import_request(
    *,
    commit: str,
    controller_digest: str,
    development: Sequence[OpaqueTask],
    holdout: Sequence[OpaqueTask],
) -> dict:
    """Content-free import request. Does not write and does not approve."""
    validation = validate_bundles(development, holdout)
    document = {
        "request_version": REQUEST_VERSION,
        "controller_commit": require_commit(commit),
        "controller_digest": require_digest(controller_digest),
        "set_identity": set_identity_for(development, holdout),
        "development_task_count": validation.development.task_count,
        "holdout_task_count": validation.holdout.task_count,
        "development_aggregate_digest": validation.development.aggregate_digest,
        "holdout_aggregate_digest": validation.holdout.aggregate_digest,
    }
    document["import_request_digest"] = sha256_digest(
        canonical_document_bytes(document, exclude="import_request_digest")
    )
    return document


def build_public_manifest(
    *,
    commit: str,
    controller_digest: str,
    validation: BundleValidation,
    request: dict,
    index_digests: Mapping[str, str],
) -> dict:
    """Content-free public manifest (schema 1.2.0, stage-separated layout).

    ``index_digests`` maps each stage to the digest of its stage-private
    index document, so a stage-specific reader can verify its own index
    against the public manifest without touching the other stage.
    """
    if set(index_digests) != set(STAGES):
        raise CustodyError("stage-separation")
    for stage in STAGES:
        require_digest(index_digests[stage])
    if request.get("request_version") != REQUEST_VERSION:
        raise CustodyError("request-mismatch")
    if request.get("controller_commit") != require_commit(commit):
        raise CustodyError("request-mismatch")
    if request.get("controller_digest") != require_digest(controller_digest):
        raise CustodyError("request-mismatch")
    if request.get("development_task_count") != validation.development.task_count:
        raise CustodyError("request-mismatch")
    if request.get("holdout_task_count") != validation.holdout.task_count:
        raise CustodyError("request-mismatch")
    if request.get("development_aggregate_digest") != validation.development.aggregate_digest:
        raise CustodyError("request-mismatch")
    if request.get("holdout_aggregate_digest") != validation.holdout.aggregate_digest:
        raise CustodyError("request-mismatch")
    identity = request.get("set_identity")
    request_digest = request.get("import_request_digest")
    if not isinstance(identity, str) or not _DIGEST_RE.fullmatch(identity):
        raise CustodyError("request-mismatch")
    if not isinstance(request_digest, str) or not _DIGEST_RE.fullmatch(request_digest):
        raise CustodyError("request-mismatch")
    fresh = {
        "request_version": REQUEST_VERSION,
        "controller_commit": commit,
        "controller_digest": controller_digest,
        "set_identity": identity,
        "development_task_count": validation.development.task_count,
        "holdout_task_count": validation.holdout.task_count,
        "development_aggregate_digest": validation.development.aggregate_digest,
        "holdout_aggregate_digest": validation.holdout.aggregate_digest,
    }
    if sha256_digest(canonical_document_bytes(fresh, exclude="import_request_digest")) != (
        request_digest
    ):
        raise CustodyError("request-mismatch")
    document = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "private_layout": PRIVATE_LAYOUT,
        "finalized": True,
        "integrity_statement": INTEGRITY_STATEMENT,
        "replay_scope": REPLAY_SCOPE,
        "request_version": REQUEST_VERSION,
        "controller_commit": commit,
        "controller_digest": controller_digest,
        "set_identity": identity,
        "development_task_count": validation.development.task_count,
        "holdout_task_count": validation.holdout.task_count,
        "development_aggregate_digest": validation.development.aggregate_digest,
        "holdout_aggregate_digest": validation.holdout.aggregate_digest,
        "development_index_digest": index_digests[DEVELOPMENT_STAGE],
        "holdout_index_digest": index_digests[HOLDOUT_STAGE],
        "import_request_digest": request_digest,
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
