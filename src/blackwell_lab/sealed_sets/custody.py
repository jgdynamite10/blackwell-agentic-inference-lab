"""External custody writes and content-free verification.

Output stays outside every Git repository. Directories are created mode
0700 and files mode 0600. Failures are scrubbed before a finalized
manifest can remain valid. Nothing printed from this module includes
task bodies, task ids, or private paths.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import jsonschema

from blackwell_lab.paths import repository_root
from blackwell_lab.sealed_sets.model import (
    INTEGRITY_STATEMENT,
    TASKS_PER_STAGE,
    BundleValidation,
    CustodyError,
    OpaqueTask,
    approval_phrase,
    build_public_manifest,
    canonical_manifest_bytes,
    controller_source_digest,
    require_commit,
    require_digest,
    sha256_digest,
    validate_bundles,
)

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_MANIFEST_NAME = "manifest.json"
_RECEIPT_NAME = "receipt.json"
_INDEX_NAME = "index.json"
_BLOB_DIR = "blobs"


@dataclass(frozen=True)
class CustodyReceipt:
    """Content-free result. Counts and digests only."""

    status: str
    operation: str
    development_task_count: int
    holdout_task_count: int
    development_aggregate_digest: str
    holdout_aggregate_digest: str
    controller_commit: str
    controller_digest: str
    custody_manifest_digest: str

    def public_dict(self) -> dict[str, str | int]:
        return {
            "status": self.status,
            "operation": self.operation,
            "development_task_count": self.development_task_count,
            "holdout_task_count": self.holdout_task_count,
            "development_aggregate_digest": self.development_aggregate_digest,
            "holdout_aggregate_digest": self.holdout_aggregate_digest,
            "controller_commit": self.controller_commit,
            "controller_digest": self.controller_digest,
            "custody_manifest_digest": self.custody_manifest_digest,
            "integrity_statement": INTEGRITY_STATEMENT,
        }


def schema_path() -> Path:
    return repository_root() / "schemas" / "sealed-set-manifest.schema.json"


def load_manifest_schema() -> dict:
    path = schema_path()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CustodyError("manifest-invalid") from None
    if not isinstance(document, dict):
        raise CustodyError("manifest-invalid")
    return document


def validate_public_manifest(document: object) -> dict:
    if not isinstance(document, dict):
        raise CustodyError("manifest-invalid")
    schema = load_manifest_schema()
    try:
        jsonschema.validate(document, schema)
    except jsonschema.ValidationError:
        raise CustodyError("manifest-invalid") from None
    digest = document.get("custody_manifest_digest")
    if digest != sha256_digest(canonical_manifest_bytes(document)):
        raise CustodyError("manifest-invalid")
    return document


def require_external_directory(raw: str | Path, *, repo: Path) -> Path:
    """Accept one absolute directory outside every Git checkout."""
    if isinstance(raw, Path):
        text = str(raw)
    elif isinstance(raw, str):
        text = raw
    else:
        raise CustodyError("relative-path")
    if text != text.strip() or not text or "\x00" in text:
        raise CustodyError("relative-path")
    candidate = Path(text)
    if not candidate.is_absolute():
        raise CustodyError("relative-path")
    if _contains_symlink(candidate):
        raise CustodyError("symlink-escape")
    resolved = candidate.resolve()
    repo_root = repo.resolve()
    lab_root = repository_root().resolve()
    if _is_inside(resolved, repo_root) or _is_inside(resolved, lab_root):
        raise CustodyError("repository-path")
    if _git_ancestor(resolved) is not None:
        raise CustodyError("repository-path")
    return resolved


def verify_clean_checkout(repo: Path, expected_commit: str) -> str:
    """Require a clean Git checkout at the supplied 40-hex commit."""
    commit = require_commit(expected_commit)
    if not repo.is_dir() or repo.is_symlink():
        raise CustodyError("commit-mismatch")
    head = _git(repo, "rev-parse", "HEAD")
    if head != commit:
        raise CustodyError("commit-mismatch")
    status = _git(repo, "status", "--porcelain", "-uall")
    if status:
        raise CustodyError("dirty-checkout")
    return commit


def import_authorized_set(
    *,
    repo: Path,
    output_root: str | Path,
    expected_commit: str,
    expected_controller_digest: str,
    approval: str,
    development: Sequence[OpaqueTask],
    holdout: Sequence[OpaqueTask],
    checkpoint: Callable[[str], None] | None = None,
) -> CustodyReceipt:
    """Write one finalized custody set. Requires the exact approval phrase.

    This is the later owner-authorized import path. It does not generate
    tasks. Callers must already hold opaque bundles.
    """
    commit = verify_clean_checkout(repo, expected_commit)
    controller_digest = controller_source_digest()
    if expected_controller_digest != controller_digest:
        raise CustodyError("controller-digest-mismatch")
    require_digest(controller_digest)
    if approval != approval_phrase(commit, controller_digest):
        raise CustodyError("approval-mismatch")
    validation = validate_bundles(development, holdout)
    destination = require_external_directory(output_root, repo=repo)
    created = _prepare_destination(destination)
    try:
        _write_tree(
            destination,
            development=development,
            holdout=holdout,
            validation=validation,
            commit=commit,
            controller_digest=controller_digest,
            checkpoint=checkpoint,
        )
        receipt = verify_custody(
            destination,
            repo=repo,
            expected_commit=commit,
            operation="import",
        )
    except CustodyError:
        _scrub(destination, created=created)
        raise
    except Exception:
        _scrub(destination, created=created)
        raise CustodyError("interrupted") from None
    return receipt


def verify_custody(
    output_root: str | Path,
    *,
    repo: Path,
    expected_commit: str,
    operation: str = "verify",
) -> CustodyReceipt:
    """Recompute digests and return a content-free receipt."""
    commit = verify_clean_checkout(repo, expected_commit)
    controller_digest = controller_source_digest()
    destination = require_external_directory(output_root, repo=repo)
    if not destination.is_dir():
        raise CustodyError("manifest-invalid")
    _require_tree_modes(destination)
    manifest = _read_json(destination / _MANIFEST_NAME)
    manifest = validate_public_manifest(manifest)
    if manifest["controller_commit"] != commit:
        raise CustodyError("commit-mismatch")
    if manifest["controller_digest"] != controller_digest:
        raise CustodyError("controller-digest-mismatch")
    index = _read_json(destination / "private" / _INDEX_NAME)
    _check_index(index, manifest, destination)
    receipt_document = _read_json(destination / _RECEIPT_NAME)
    expected_stored = _receipt_from_manifest(manifest, operation="import").public_dict()
    if receipt_document != expected_stored:
        raise CustodyError("tamper")
    return _receipt_from_manifest(manifest, operation=operation)


def report_receipt(output_root: str | Path, *, repo: Path, expected_commit: str) -> CustodyReceipt:
    return verify_custody(
        output_root, repo=repo, expected_commit=expected_commit, operation="receipt"
    )


def load_bundle_directory(raw: str | Path, *, repo: Path) -> tuple[OpaqueTask, ...]:
    """Load an external opaque bundle. Filenames are ids and are not echoed."""
    directory = require_external_directory(raw, repo=repo)
    if not directory.is_dir():
        raise CustodyError("bundle-shape")
    if _mode(directory) != _DIR_MODE:
        raise CustodyError("permissive-mode")
    tasks: list[OpaqueTask] = []
    entries = list(directory.iterdir())
    if len(entries) != TASKS_PER_STAGE:
        raise CustodyError("task-count")
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise CustodyError("symlink-escape")
        if _mode(entry) != _FILE_MODE:
            raise CustodyError("permissive-mode")
        try:
            content = entry.read_bytes()
        except OSError:
            raise CustodyError("bundle-shape") from None
        tasks.append(OpaqueTask(entry.name, content))
    return tuple(tasks)


def _write_tree(
    destination: Path,
    *,
    development: Sequence[OpaqueTask],
    holdout: Sequence[OpaqueTask],
    validation: BundleValidation,
    commit: str,
    controller_digest: str,
    checkpoint: Callable[[str], None] | None,
) -> None:
    private = destination / "private"
    blobs = private / _BLOB_DIR
    _mkdir(private)
    _mkdir(blobs)
    index = {"development": [], "holdout": []}
    for stage, tasks in (("development", development), ("holdout", holdout)):
        rows = []
        for task in tasks:
            digest = sha256_digest(task.content)
            _write_bytes(blobs / digest.removeprefix("sha256:"), task.content)
            rows.append({"task_id": task.task_id, "content_digest": digest})
        rows.sort(key=lambda item: item["task_id"])
        index[stage] = rows
    if checkpoint is not None:
        checkpoint("before-manifest")
    _write_bytes(private / _INDEX_NAME, _canonical_bytes(index))
    manifest = build_public_manifest(
        commit=commit,
        controller_digest=controller_digest,
        validation=validation,
    )
    validate_public_manifest(manifest)
    _write_bytes(destination / _MANIFEST_NAME, _canonical_bytes(manifest))
    receipt = _receipt_from_manifest(manifest, operation="import")
    _write_bytes(destination / _RECEIPT_NAME, _canonical_bytes(receipt.public_dict()))


def _check_index(index: object, manifest: dict, destination: Path) -> None:
    if not isinstance(index, dict) or set(index) != {"development", "holdout"}:
        raise CustodyError("tamper")
    seen_ids: set[str] = set()
    seen_digests: set[str] = set()
    blob_root = destination / "private" / _BLOB_DIR
    for stage in ("development", "holdout"):
        rows = index[stage]
        if not isinstance(rows, list) or len(rows) != TASKS_PER_STAGE:
            raise CustodyError("tamper")
        ordered: list[tuple[str, str]] = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"task_id", "content_digest"}:
                raise CustodyError("tamper")
            task_id = row["task_id"]
            digest = row["content_digest"]
            if not isinstance(task_id, str) or not isinstance(digest, str):
                raise CustodyError("tamper")
            if task_id in seen_ids:
                raise CustodyError("tamper")
            if digest in seen_digests:
                raise CustodyError("tamper")
            seen_ids.add(task_id)
            seen_digests.add(digest)
            blob = blob_root / digest.removeprefix("sha256:")
            if blob.is_symlink() or not blob.is_file():
                raise CustodyError("tamper")
            try:
                payload = blob.read_bytes()
            except OSError:
                raise CustodyError("tamper") from None
            if sha256_digest(payload) != digest:
                raise CustodyError("tamper")
            ordered.append((task_id, digest))
        ordered.sort(key=lambda item: item[0])
        preimage = "".join(f"{task_id}\t{digest}\n" for task_id, digest in ordered).encode()
        aggregate = sha256_digest(preimage)
        if aggregate != manifest[f"{stage}_aggregate_digest"]:
            raise CustodyError("tamper")
    if tuple(sorted(seen_digests)) != tuple(manifest["file_digests"]):
        raise CustodyError("tamper")


def _receipt_from_manifest(manifest: dict, *, operation: str) -> CustodyReceipt:
    return CustodyReceipt(
        status="pass",
        operation=operation,
        development_task_count=manifest["development_task_count"],
        holdout_task_count=manifest["holdout_task_count"],
        development_aggregate_digest=manifest["development_aggregate_digest"],
        holdout_aggregate_digest=manifest["holdout_aggregate_digest"],
        controller_commit=manifest["controller_commit"],
        controller_digest=manifest["controller_digest"],
        custody_manifest_digest=manifest["custody_manifest_digest"],
    )


def _prepare_destination(destination: Path) -> bool:
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise CustodyError("symlink-escape")
        if _mode(destination) != _DIR_MODE:
            raise CustodyError("permissive-mode")
        if any(destination.iterdir()):
            raise CustodyError("destination-nonempty")
        return False
    _mkdir(destination)
    return True


def _mkdir(path: Path) -> None:
    previous = os.umask(0o077)
    try:
        path.mkdir(mode=_DIR_MODE)
    finally:
        os.umask(previous)
    os.chmod(path, _DIR_MODE)
    if _mode(path) != _DIR_MODE:
        raise CustodyError("permissive-mode")


def _write_bytes(path: Path, payload: bytes) -> None:
    previous = os.umask(0o077)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
    finally:
        os.umask(previous)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, _FILE_MODE)
    if _mode(path) != _FILE_MODE:
        raise CustodyError("permissive-mode")


def _require_tree_modes(root: Path) -> None:
    if root.is_symlink() or _mode(root) != _DIR_MODE:
        raise CustodyError("permissive-mode")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink() or _mode(current) != _DIR_MODE:
            raise CustodyError("permissive-mode")
        for name in dirnames:
            child = current / name
            if child.is_symlink():
                raise CustodyError("symlink-escape")
        for name in filenames:
            child = current / name
            if child.is_symlink():
                raise CustodyError("symlink-escape")
            if _mode(child) != _FILE_MODE:
                raise CustodyError("permissive-mode")


def _scrub(destination: Path, *, created: bool) -> None:
    if created:
        shutil.rmtree(destination, ignore_errors=True)
        return
    if not destination.is_dir() or destination.is_symlink():
        return
    for child in list(destination.iterdir()):
        if child.is_symlink() or child.is_file():
            child.unlink(missing_ok=True)
        else:
            shutil.rmtree(child, ignore_errors=True)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _contains_symlink(path: Path) -> bool:
    if not path.is_absolute():
        return False
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
        if not current.exists():
            return False
    return False


def _is_inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _git_ancestor(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        git_entry = candidate / ".git"
        if git_entry.exists() or git_entry.is_symlink():
            return candidate
    return None


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise CustodyError("commit-mismatch")
    completed = subprocess.run(
        [git, "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise CustodyError("commit-mismatch")
    return completed.stdout.strip()


def _read_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        raise CustodyError("manifest-invalid")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CustodyError("manifest-invalid") from None


def _canonical_bytes(document: object) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
