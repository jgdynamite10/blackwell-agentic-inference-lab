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
    TASKS_PER_STAGE,
    BundleValidation,
    CustodyError,
    OpaqueTask,
    approval_phrase,
    build_import_request,
    build_public_manifest,
    canonical_manifest_bytes,
    controller_digest_from_sources,
    require_commit,
    running_controller_parts,
    set_identity_from_digests,
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
    """Recompute digests and return a content-free receipt."""
    commit = verify_clean_checkout(repo, expected_commit)
    controller_digest = require_bound_controller(repo, commit)
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
    request: dict,
    checkpoint: Callable[[str], None] | None,
) -> None:
    private = destination / "private"
    blobs = private / _BLOB_DIR
    _mkdir(private)
    _mkdir(blobs)
    index: dict[str, object] = {
        "development": [],
        "holdout": [],
        "development_order": [],
        "holdout_order": [],
    }
    for stage, tasks in (("development", development), ("holdout", holdout)):
        rows = []
        order = []
        for task in tasks:
            digest = sha256_digest(task.content)
            _write_bytes(blobs / digest.removeprefix("sha256:"), task.content)
            rows.append({"task_id": task.task_id, "content_digest": digest})
            order.append(digest)
        rows.sort(key=lambda item: item["task_id"])
        index[stage] = rows
        index[f"{stage}_order"] = order
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
    for stage in ("development", "holdout"):
        rows = index[stage]
        if not isinstance(rows, list):
            raise CustodyError("request-mismatch")
        for row in rows:
            if not isinstance(row, dict):
                raise CustodyError("request-mismatch")
            digest = row["content_digest"]
            blob = blobs / str(digest).removeprefix("sha256:")
            try:
                payload = blob.read_bytes()
            except OSError:
                raise CustodyError("request-mismatch") from None
            if sha256_digest(payload) != digest:
                raise CustodyError("request-mismatch")
    _write_bytes(private / _INDEX_NAME, _canonical_bytes(index))
    manifest = build_public_manifest(
        commit=commit,
        controller_digest=controller_digest,
        validation=validation,
        request=request,
    )
    validate_public_manifest(manifest)
    _write_bytes(destination / _MANIFEST_NAME, _canonical_bytes(manifest))
    receipt = _receipt_from_manifest(manifest, operation="import")
    _write_bytes(destination / _RECEIPT_NAME, _canonical_bytes(receipt.public_dict()))


def _check_index(index: object, manifest: dict, destination: Path) -> None:
    expected_keys = {"development", "holdout", "development_order", "holdout_order"}
    if not isinstance(index, dict) or set(index) != expected_keys:
        raise CustodyError("tamper")
    seen_ids: set[str] = set()
    seen_digests: set[str] = set()
    blob_root = destination / "private" / _BLOB_DIR
    supplied_orders: dict[str, list[str]] = {}
    for stage in ("development", "holdout"):
        rows = index[stage]
        order = index[f"{stage}_order"]
        if not isinstance(rows, list) or len(rows) != TASKS_PER_STAGE:
            raise CustodyError("tamper")
        if not isinstance(order, list) or len(order) != TASKS_PER_STAGE:
            raise CustodyError("tamper")
        if not all(isinstance(item, str) for item in order):
            raise CustodyError("tamper")
        supplied_orders[stage] = [str(item) for item in order]
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
        if sorted(supplied_orders[stage]) != sorted(digest for _task_id, digest in ordered):
            raise CustodyError("tamper")
    identity = set_identity_from_digests(supplied_orders["development"], supplied_orders["holdout"])
    if identity != manifest.get("set_identity"):
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
        set_identity=manifest["set_identity"],
        import_request_digest=manifest["import_request_digest"],
    )


def _prepare_destination(destination: Path, *, request: dict) -> bool:
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise CustodyError("symlink-escape")
        if _mode(destination) != _DIR_MODE:
            raise CustodyError("permissive-mode")
        manifest_path = destination / _MANIFEST_NAME
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


def _canonical_bytes(document: object) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
