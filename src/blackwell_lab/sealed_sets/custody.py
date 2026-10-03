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
    CONTROLLER_SOURCE_PATHS,
    INTEGRITY_STATEMENT,
    STAGES,
    TASKS_PER_STAGE,
    BundleValidation,
    CustodyError,
    OpaqueTask,
    StageIndex,
    approval_phrase,
    build_import_request,
    build_public_manifest,
    build_stage_index,
    canonical_bytes,
    canonical_manifest_bytes,
    controller_digest_from_sources,
    require_commit,
    require_execution_layout,
    running_controller_parts,
    set_identity_from_digests,
    sha256_digest,
    stage_index_digest,
    validate_bundles,
    verify_stage_index,
)

_DIR_MODE = 0o700
_FILE_MODE = 0o600
MANIFEST_NAME = "manifest.json"
RECEIPT_NAME = "receipt.json"
PRIVATE_DIR = "private"
INDEX_NAME = "index.json"
BLOB_DIR = "blobs"


def stage_private_dir(root: Path, stage: str) -> Path:
    """``<root>/private/<stage>``: the only private directory a stage reader touches."""
    if stage not in STAGES:
        raise CustodyError("stage-separation")
    return root / PRIVATE_DIR / stage


def stage_index_path(root: Path, stage: str) -> Path:
    return stage_private_dir(root, stage) / INDEX_NAME


def stage_blob_path(root: Path, stage: str, content_digest: str) -> Path:
    """Blob path built only from the selected stage and one of its own digests."""
    hex_digest = content_digest.removeprefix("sha256:")
    if len(hex_digest) != 64 or any(ch not in "0123456789abcdef" for ch in hex_digest):
        raise CustodyError("tamper")
    return stage_private_dir(root, stage) / BLOB_DIR / hex_digest


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
    set_identity: str
    import_request_digest: str

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
            "set_identity": self.set_identity,
            "import_request_digest": self.import_request_digest,
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
    """Schema-validate a public manifest after the layout gate.

    The layout gate runs first so a 1.1.0 combined-index package fails
    with ``layout-version-unsupported`` rather than a generic schema error.
    """
    if not isinstance(document, dict):
        raise CustodyError("manifest-invalid")
    require_execution_layout(document)
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
    """Require the worktree to match the supplied commit byte for byte.

    ``git status`` and ``git diff`` are not consulted. Skip-worktree and
    assume-unchanged hide tracked edits from both. The check is:

    1. ``HEAD`` equals the supplied commit.
    2. Every ``git ls-files -v -z`` tag is ``H``. ``S`` (skip-worktree),
       a lowercase assume-unchanged tag, and any other tag fail.
    3. ``git ls-tree -r -z`` for that commit and ``git ls-files -s -z``
       carry the same path, mode, kind, and object id at stage 0.
    4. Each committed path is present. Mode ``120000`` must be a symlink
       whose target bytes equal the blob. Mode ``100644`` or ``100755``
       must be a regular file, the executable bit must match, and the
       raw worktree bytes must equal the blob. Other modes fail.
    5. ``git ls-files -o --exclude-standard -z`` is empty.
    """
    commit = require_commit(expected_commit)
    if not repo.is_dir() or repo.is_symlink():
        raise CustodyError("commit-mismatch")
    head = _git(repo, "rev-parse", "HEAD")
    if head != commit:
        raise CustodyError("commit-mismatch")
    _reject_hidden_index_flags(repo)
    tree = _commit_entries(repo, commit)
    if _index_entries(repo) != tree:
        raise CustodyError("dirty-checkout")
    for path, (mode, _kind, oid) in tree.items():
        _compare_worktree_entry(repo, path, mode, oid)
    if _untracked_paths(repo):
        raise CustodyError("dirty-checkout")
    return commit


def require_bound_controller(repo: Path, commit: str) -> str:
    """Digest the controller blobs stored in ``commit`` and bind this process.

    The ordered set is :data:`CONTROLLER_SOURCE_PATHS`. Each path must be
    a regular blob of mode ``100644`` or ``100755`` in that commit, or
    the result is ``controller-absent``. The preimage is ``path NUL mode
    NUL sha256(blob-bytes) NUL`` in that order. The executing files are
    digested the same way and must match. Each executing file must be
    the worktree file at its canonical path. A clean repository that
    does not contain the controller, or a copy in another repository,
    does not bind.
    """
    commit_parts = _commit_controller_parts(repo, commit)
    running_parts = running_controller_parts()
    commit_digest = controller_digest_from_sources(commit_parts)
    if commit_digest != controller_digest_from_sources(running_parts):
        raise CustodyError("controller-digest-mismatch")
    repo_root = repo.resolve()
    running_root = Path(__file__).resolve().parent
    for rel in CONTROLLER_SOURCE_PATHS:
        if (running_root / Path(rel).name).resolve() != (repo_root / rel).resolve():
            raise CustodyError("controller-location")
    return commit_digest


def prepare_import_request(
    *,
    repo: Path,
    expected_commit: str,
    development: Sequence[OpaqueTask],
    holdout: Sequence[OpaqueTask],
) -> dict:
    """Content-free request for one exact input set. Does not write."""
    commit = verify_clean_checkout(repo, expected_commit)
    controller_digest = require_bound_controller(repo, commit)
    return build_import_request(
        commit=commit,
        controller_digest=controller_digest,
        development=development,
        holdout=holdout,
    )


def import_authorized_set(
    *,
    repo: Path,
    output_root: str | Path,
    expected_commit: str,
    expected_controller_digest: str,
    expected_request_digest: str,
    approval: str,
    development: Sequence[OpaqueTask],
    holdout: Sequence[OpaqueTask],
    checkpoint: Callable[[str], None] | None = None,
) -> CustodyReceipt:
    """Write one finalized custody set for one exact import request.

    This is the later owner-authorized import path. It does not generate
    tasks. The approval phrase binds the import-request digest, not an
    open-ended commit. Replay protection covers only this custody location.
    """
    commit = verify_clean_checkout(repo, expected_commit)
    controller_digest = require_bound_controller(repo, commit)
    if expected_controller_digest != controller_digest:
        raise CustodyError("controller-digest-mismatch")
    request = build_import_request(
        commit=commit,
        controller_digest=controller_digest,
        development=development,
        holdout=holdout,
    )
    if request["import_request_digest"] != expected_request_digest:
        raise CustodyError("request-mismatch")
    if approval != approval_phrase(request["import_request_digest"]):
        raise CustodyError("approval-mismatch")
    validation = validate_bundles(development, holdout)
    destination = require_external_directory(output_root, repo=repo)
    created = _prepare_destination(destination, request=request)
    try:
        _write_tree(
            destination,
            development=development,
            holdout=holdout,
            validation=validation,
            commit=commit,
            controller_digest=controller_digest,
            request=request,
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
    """Recompute digests and return a content-free receipt.

    This is **full-custody** verification: it deliberately walks the whole
    tree and verifies both stages. ``qualify-agent`` never calls it; the
    execution adapter uses stage-specific verification only.
    """
    commit = verify_clean_checkout(repo, expected_commit)
    controller_digest = require_bound_controller(repo, commit)
    destination = require_external_directory(output_root, repo=repo)
    if not destination.is_dir():
        raise CustodyError("manifest-invalid")
    _require_tree_modes(destination)
    manifest = _read_json(destination / MANIFEST_NAME)
    manifest = validate_public_manifest(manifest)
    if manifest["controller_commit"] != commit:
        raise CustodyError("commit-mismatch")
    if manifest["controller_digest"] != controller_digest:
        raise CustodyError("controller-digest-mismatch")
    _check_all_stages(manifest, destination)
    receipt_document = _read_json(destination / RECEIPT_NAME)
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
    request: dict,
    checkpoint: Callable[[str], None] | None,
) -> None:
    private = destination / PRIVATE_DIR
    _mkdir(private)
    indexes: dict[str, dict] = {}
    for stage, tasks in (("development", development), ("holdout", holdout)):
        stage_dir = stage_private_dir(destination, stage)
        _mkdir(stage_dir)
        _mkdir(stage_dir / BLOB_DIR)
        for task in tasks:
            digest = sha256_digest(task.content)
            _write_bytes(stage_blob_path(destination, stage, digest), task.content)
        indexes[stage] = build_stage_index(stage, tasks)
    if checkpoint is not None:
        checkpoint("before-manifest")
    fresh = build_import_request(
        commit=commit,
        controller_digest=controller_digest,
        development=development,
        holdout=holdout,
    )
    if fresh != request:
        raise CustodyError("request-mismatch")
    for stage in STAGES:
        for row in indexes[stage]["rows"]:
            digest = row["content_digest"]
            try:
                payload = stage_blob_path(destination, stage, digest).read_bytes()
            except OSError:
                raise CustodyError("request-mismatch") from None
            if sha256_digest(payload) != digest:
                raise CustodyError("request-mismatch")
    for stage in STAGES:
        _write_bytes(stage_index_path(destination, stage), canonical_bytes(indexes[stage]))
    manifest = build_public_manifest(
        commit=commit,
        controller_digest=controller_digest,
        validation=validation,
        request=request,
        index_digests={stage: stage_index_digest(indexes[stage]) for stage in STAGES},
    )
    validate_public_manifest(manifest)
    _write_bytes(destination / MANIFEST_NAME, canonical_bytes(manifest))
    receipt = _receipt_from_manifest(manifest, operation="import")
    _write_bytes(destination / RECEIPT_NAME, canonical_bytes(receipt.public_dict()))


def load_stage_index(root: Path, manifest: dict, stage: str) -> StageIndex:
    """Stage-specific verification of one stage's private index.

    Touches only ``<root>/private``, ``<root>/private/<stage>`` and that
    stage's ``index.json`` (``lstat`` for modes, one ``open`` for the
    index). It never lists, walks, stats, or opens anything under the
    other stage, so a development reader cannot observe holdout
    identifiers, digests, order, filenames, or bytes (and vice versa).
    """
    stage_dir = stage_private_dir(root, stage)
    _require_private_dir(root / PRIVATE_DIR)
    _require_private_dir(stage_dir)
    _require_private_dir(stage_dir / BLOB_DIR)
    index_bytes = _read_private_file(stage_index_path(root, stage), reason="tamper")
    try:
        document = json.loads(index_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise CustodyError("tamper") from None
    if canonical_bytes(document) != index_bytes:
        raise CustodyError("tamper")
    return verify_stage_index(document, stage=stage, manifest=manifest)


def read_stage_blob(root: Path, stage: str, content_digest: str) -> bytes:
    """Read one blob by a path built only from the selected stage's own digest."""
    payload = _read_private_file(stage_blob_path(root, stage, content_digest), reason="tamper")
    if sha256_digest(payload) != content_digest:
        raise CustodyError("tamper")
    return payload


def verify_stage_tree(root: Path, manifest: dict, stage: str) -> StageIndex:
    """Stage-specific verification of one stage's index and every blob it names."""
    index = load_stage_index(root, manifest, stage)
    for _task_id, digest in index.rows:
        read_stage_blob(root, stage, digest)
    return index


def _check_all_stages(manifest: dict, destination: Path) -> None:
    """Full-custody cross-stage checks. Not used by stage-specific readers."""
    indexes = {stage: verify_stage_tree(destination, manifest, stage) for stage in STAGES}
    dev_ids = {task_id for task_id, _digest in indexes["development"].rows}
    hold_ids = {task_id for task_id, _digest in indexes["holdout"].rows}
    if dev_ids & hold_ids:
        raise CustodyError("tamper")
    dev_digests = set(indexes["development"].content_digests)
    hold_digests = set(indexes["holdout"].content_digests)
    if dev_digests & hold_digests:
        raise CustodyError("tamper")
    identity = set_identity_from_digests(indexes["development"].order, indexes["holdout"].order)
    if identity != manifest.get("set_identity"):
        raise CustodyError("tamper")
    if tuple(sorted(dev_digests | hold_digests)) != tuple(manifest["file_digests"]):
        raise CustodyError("tamper")
    for stage in STAGES:
        blob_dir = stage_private_dir(destination, stage) / BLOB_DIR
        names = {entry.name for entry in blob_dir.iterdir()}
        expected = {digest.removeprefix("sha256:") for digest in indexes[stage].content_digests}
        if names != expected:
            raise CustodyError("tamper")
        stage_entries = {entry.name for entry in stage_private_dir(destination, stage).iterdir()}
        if stage_entries != {INDEX_NAME, BLOB_DIR}:
            raise CustodyError("tamper")
    if {entry.name for entry in (destination / PRIVATE_DIR).iterdir()} != set(STAGES):
        raise CustodyError("tamper")


def _require_private_dir(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise CustodyError("tamper") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CustodyError("symlink-escape" if stat.S_ISLNK(info.st_mode) else "tamper")
    if stat.S_IMODE(info.st_mode) != _DIR_MODE:
        raise CustodyError("permissive-mode")


_OPEN_FLAGS = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)


def _read_private_file(path: Path, *, reason: str) -> bytes:
    """Read one regular ``0600`` file by exact path, without following symlinks.

    The descriptor is re-checked with ``fstat`` so the bytes hashed are
    the regular file that was inspected, not a swap-in.
    """
    try:
        info = path.lstat()
    except OSError:
        raise CustodyError(reason) from None
    if stat.S_ISLNK(info.st_mode):
        raise CustodyError("symlink-escape")
    if not stat.S_ISREG(info.st_mode):
        raise CustodyError(reason)
    if stat.S_IMODE(info.st_mode) != _FILE_MODE:
        raise CustodyError("permissive-mode")
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError:
        raise CustodyError(reason) from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise CustodyError(reason)
        if stat.S_IMODE(opened.st_mode) != _FILE_MODE:
            raise CustodyError("permissive-mode")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read()
    except OSError:
        raise CustodyError(reason) from None
    finally:
        if fd >= 0:
            os.close(fd)


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
        set_identity=manifest["set_identity"],
        import_request_digest=manifest["import_request_digest"],
    )


def _prepare_destination(destination: Path, *, request: dict) -> bool:
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise CustodyError("symlink-escape")
        if _mode(destination) != _DIR_MODE:
            raise CustodyError("permissive-mode")
        manifest_path = destination / MANIFEST_NAME
        if manifest_path.exists():
            if manifest_path.is_symlink() or not manifest_path.is_file():
                raise CustodyError("destination-nonempty")
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise CustodyError("destination-nonempty") from None
            if isinstance(existing, dict) and (
                existing.get("set_identity") == request["set_identity"]
                or existing.get("import_request_digest") == request["import_request_digest"]
            ):
                raise CustodyError("replay")
            raise CustodyError("destination-nonempty")
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


def _reject_hidden_index_flags(repo: Path) -> None:
    """Reject skip-worktree, assume-unchanged, and every non-``H`` index tag."""
    for record in _git_records(repo, "ls-files", "-v", "-z"):
        if len(record) < 2 or record[1:2] != b" ":
            raise CustodyError("dirty-checkout")
        if record[:1] != b"H":
            raise CustodyError("dirty-checkout")


def _commit_entries(repo: Path, commit: str) -> dict[str, tuple[str, str, str]]:
    entries: dict[str, tuple[str, str, str]] = {}
    for record in _git_records(repo, "ls-tree", "-r", "-z", commit):
        try:
            meta, path_b = record.split(b"\t", 1)
            mode_b, kind_b, oid_b = meta.split(b" ")
            path = path_b.decode("utf-8")
            mode = mode_b.decode("ascii")
            kind = kind_b.decode("ascii")
            oid = oid_b.decode("ascii")
        except (ValueError, UnicodeError):
            raise CustodyError("dirty-checkout") from None
        if not _safe_git_path(path) or path in entries:
            raise CustodyError("dirty-checkout")
        entries[path] = (mode, kind, oid)
    return entries


def _index_entries(repo: Path) -> dict[str, tuple[str, str, str]]:
    entries: dict[str, tuple[str, str, str]] = {}
    for record in _git_records(repo, "ls-files", "-s", "-z"):
        try:
            meta, path_b = record.split(b"\t", 1)
            mode_b, oid_b, stage_b = meta.split(b" ")
            path = path_b.decode("utf-8")
            mode = mode_b.decode("ascii")
            oid = oid_b.decode("ascii")
            stage = stage_b.decode("ascii")
        except (ValueError, UnicodeError):
            raise CustodyError("dirty-checkout") from None
        if stage != "0" or not _safe_git_path(path) or path in entries:
            raise CustodyError("dirty-checkout")
        kind = "commit" if mode == "160000" else "blob"
        entries[path] = (mode, kind, oid)
    return entries


def _commit_controller_parts(repo: Path, commit: str) -> tuple[tuple[str, str, bytes], ...]:
    entries = _commit_entries(repo, commit)
    parts: list[tuple[str, str, bytes]] = []
    for rel in CONTROLLER_SOURCE_PATHS:
        found = entries.get(rel)
        if found is None:
            raise CustodyError("controller-absent")
        mode, kind, oid = found
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise CustodyError("controller-digest-mismatch")
        blob = _git_raw(
            repo,
            "cat-file",
            "blob",
            oid,
            reason="controller-digest-mismatch",
        )
        parts.append((rel, mode, blob))
    return tuple(parts)


def _compare_worktree_entry(repo: Path, rel: str, mode: str, oid: str) -> None:
    path = _worktree_path(repo, rel)
    if not os.path.lexists(path):
        raise CustodyError("dirty-checkout")
    info = path.lstat()
    if mode == "120000":
        if not stat.S_ISLNK(info.st_mode):
            raise CustodyError("dirty-checkout")
        try:
            target = os.fsencode(os.readlink(path))
        except OSError:
            raise CustodyError("dirty-checkout") from None
        if target != _git_raw(repo, "cat-file", "blob", oid):
            raise CustodyError("dirty-checkout")
        return
    if mode not in {"100644", "100755"}:
        raise CustodyError("dirty-checkout")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CustodyError("dirty-checkout")
    executable = bool(stat.S_IMODE(info.st_mode) & 0o111)
    if executable != (mode == "100755"):
        raise CustodyError("dirty-checkout")
    try:
        payload = path.read_bytes()
    except OSError:
        raise CustodyError("dirty-checkout") from None
    if payload != _git_raw(repo, "cat-file", "blob", oid):
        raise CustodyError("dirty-checkout")


def _worktree_path(repo: Path, rel: str) -> Path:
    current = repo
    parts = rel.split("/")
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise CustodyError("dirty-checkout")
    return current / parts[-1]


def _safe_git_path(path: str) -> bool:
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or path == ".git"
        or path.startswith(".git/")
    ):
        return False
    return all(part not in {"", ".", ".."} for part in path.split("/"))


def _untracked_paths(repo: Path) -> bool:
    return any(_git_records(repo, "ls-files", "-o", "--exclude-standard", "-z"))


def _git_records(repo: Path, *args: str) -> tuple[bytes, ...]:
    raw = _git_raw(repo, *args)
    return tuple(record for record in raw.split(b"\0") if record)


def _git_raw(repo: Path, *args: str, reason: str = "dirty-checkout") -> bytes:
    git = shutil.which("git")
    if git is None:
        raise CustodyError(reason)
    completed = subprocess.run(
        [git, "-C", str(repo), *args],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise CustodyError(reason)
    return completed.stdout


def _git(repo: Path, *args: str, reason: str = "commit-mismatch") -> str:
    try:
        return _git_raw(repo, *args, reason=reason).decode("utf-8").strip()
    except UnicodeError:
        raise CustodyError(reason) from None


def _read_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        raise CustodyError("manifest-invalid")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CustodyError("manifest-invalid") from None
