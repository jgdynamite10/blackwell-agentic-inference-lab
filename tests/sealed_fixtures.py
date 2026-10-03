"""Synthetic external custody fixtures for sealed-stage tests (D-0024).

Everything here is artificial: task bodies are re-encodings of the public
synthetic scenario catalog with invented instance surfaces, task ids are
``syn-…`` placeholders, and the custody tree is written to a pytest
``tmp_path`` outside every Git repository using the production custody
writer so the on-disk layout (stage-separated, custody manifest 1.2.0),
modes, stage indexes, manifest, and receipt are byte-for-byte what a real
import would produce. A separate writer reproduces the retired 1.1.0
combined-index layout only so tests can prove it is rejected. No real
sealed task, accepted answer, private result, or private path appears
anywhere.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from blackwell_lab.cloud.sealed_binding import BINDING_SCHEMA_VERSION
from blackwell_lab.cloud.sealed_payload import PAYLOAD_SCHEMA_VERSION, encode_sealed_task
from blackwell_lab.sealed_sets import custody
from blackwell_lab.sealed_sets.model import (
    INTEGRITY_STATEMENT,
    OpaqueTask,
    build_import_request,
    canonical_bytes,
    canonical_manifest_bytes,
    running_controller_digest,
    sha256_digest,
    validate_bundles,
)
from blackwell_lab.workload.scenarios import catalog

SYNTHETIC_COMMIT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def synthetic_stage(stage: str, *, count: int = 20, salt: str = "") -> tuple[OpaqueTask, ...]:
    """Twenty artificial opaque tasks cycling through the public synthetic catalog."""
    scenarios = list(catalog().values())
    tasks = []
    for index in range(count):
        scenario = scenarios[index % len(scenarios)]
        seed_source = f"{salt}{stage}:{index}".encode()
        digest = hashlib.sha256(seed_source).hexdigest()
        task_id = f"syn-{stage[:3]}-task-{index:02d}"
        payload = encode_sealed_task(
            task_id=task_id,
            scenario=scenario,
            instance_seed=int(digest[:8], 16),
            tracking_id=f"SYN-{digest[8:16]}",
            reported_minute=1 + (int(digest[16:20], 16) % 59),
        )
        tasks.append(OpaqueTask(task_id, payload))
    return tuple(tasks)


def write_synthetic_custody(
    root: Path,
    *,
    commit: str = SYNTHETIC_COMMIT,
    development: tuple[OpaqueTask, ...] | None = None,
    holdout: tuple[OpaqueTask, ...] | None = None,
) -> dict:
    """Write a finalized custody tree with the production writer; return its manifest."""
    development = development if development is not None else synthetic_stage("development")
    holdout = holdout if holdout is not None else synthetic_stage("holdout")
    controller_digest = running_controller_digest()
    request = build_import_request(
        commit=commit,
        controller_digest=controller_digest,
        development=development,
        holdout=holdout,
    )
    validation = validate_bundles(development, holdout)
    custody._mkdir(root)
    custody._write_tree(
        root,
        development=development,
        holdout=holdout,
        validation=validation,
        commit=commit,
        controller_digest=controller_digest,
        request=request,
        checkpoint=None,
    )
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def manifest_file_sha256(root: Path) -> str:
    return hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()


def binding_for(root: Path, manifest: dict, stage: str) -> dict:
    """The content-free ``sealed_set`` config section for one custody stage."""
    return {
        "schema_version": BINDING_SCHEMA_VERSION,
        "custody_manifest_sha256": manifest_file_sha256(root),
        "custody_controller_digest": manifest["controller_digest"],
        "import_request_digest": manifest["import_request_digest"],
        "set_identity": manifest["set_identity"],
        "stage": stage,
        "stage_aggregate_digest": manifest[f"{stage}_aggregate_digest"],
        "task_count": manifest[f"{stage}_task_count"],
        "payload_schema_version": PAYLOAD_SCHEMA_VERSION,
    }


def placeholder_binding(stage: str) -> dict:
    """Shape-valid binding with artificial digests for config-only tests."""
    return {
        "schema_version": BINDING_SCHEMA_VERSION,
        "custody_manifest_sha256": "0" * 64,
        "custody_controller_digest": "sha256:" + "1" * 64,
        "import_request_digest": "sha256:" + "2" * 64,
        "set_identity": "sha256:" + "3" * 64,
        "stage": stage,
        "stage_aggregate_digest": "sha256:" + "4" * 64,
        "task_count": 20,
        "payload_schema_version": PAYLOAD_SCHEMA_VERSION,
    }


def stage_dir(root: Path, stage: str) -> Path:
    return root / "private" / stage


def stage_index_file(root: Path, stage: str) -> Path:
    return stage_dir(root, stage) / "index.json"


def stage_blob_paths(root: Path, stage: str) -> set[Path]:
    """Blob paths of one stage, read from its stage-private index (test oracle only)."""
    index = json.loads(stage_index_file(root, stage).read_text(encoding="utf-8"))
    return {
        stage_dir(root, stage) / "blobs" / row["content_digest"].removeprefix("sha256:")
        for row in index["rows"]
    }


def stage_task_ids(root: Path, stage: str) -> set[str]:
    """Task ids of one stage from its private index (test oracle only; never printed)."""
    index = json.loads(stage_index_file(root, stage).read_text(encoding="utf-8"))
    return {row["task_id"] for row in index["rows"]}


def other_stage(stage: str) -> str:
    return "holdout" if stage == "development" else "development"


def write_legacy_combined_custody(
    root: Path,
    *,
    commit: str = SYNTHETIC_COMMIT,
    development: tuple[OpaqueTask, ...] | None = None,
    holdout: tuple[OpaqueTask, ...] | None = None,
) -> dict:
    """Write a synthetic package in the retired 1.1.0 combined-index layout.

    Shape: ``private/index.json`` holding both stages and ``private/blobs/``
    holding all forty blobs, with a schema-1.1.0 manifest whose digest is
    recomputed over its own canonical bytes. Used only to prove that such a
    package is rejected for P2 execution with a stable, content-free reason.
    """
    development = development if development is not None else synthetic_stage("development")
    holdout = holdout if holdout is not None else synthetic_stage("holdout")
    controller_digest = running_controller_digest()
    request = build_import_request(
        commit=commit,
        controller_digest=controller_digest,
        development=development,
        holdout=holdout,
    )
    validation = validate_bundles(development, holdout)
    custody._mkdir(root)
    private = root / "private"
    blobs = private / "blobs"
    custody._mkdir(private)
    custody._mkdir(blobs)
    index: dict[str, object] = {}
    for stage, tasks in (("development", development), ("holdout", holdout)):
        rows = []
        order = []
        for task in tasks:
            digest = sha256_digest(task.content)
            custody._write_bytes(blobs / digest.removeprefix("sha256:"), task.content)
            rows.append({"task_id": task.task_id, "content_digest": digest})
            order.append(digest)
        rows.sort(key=lambda item: item["task_id"])
        index[stage] = rows
        index[f"{stage}_order"] = order
    custody._write_bytes(private / "index.json", canonical_bytes(index))
    manifest = {
        "schema_version": "1.1.0",
        "kind": "sealed-qualification-set",
        "finalized": True,
        "integrity_statement": INTEGRITY_STATEMENT,
        "replay_scope": "selected-custody-location",
        "request_version": request["request_version"],
        "controller_commit": commit,
        "controller_digest": controller_digest,
        "set_identity": request["set_identity"],
        "development_task_count": 20,
        "holdout_task_count": 20,
        "development_aggregate_digest": validation.development.aggregate_digest,
        "holdout_aggregate_digest": validation.holdout.aggregate_digest,
        "import_request_digest": request["import_request_digest"],
        "file_digests": list(validation.file_digests),
    }
    manifest["custody_manifest_digest"] = sha256_digest(canonical_manifest_bytes(manifest))
    custody._write_bytes(root / "manifest.json", canonical_bytes(manifest))
    receipt = custody.CustodyReceipt(
        status="pass",
        operation="import",
        development_task_count=20,
        holdout_task_count=20,
        development_aggregate_digest=manifest["development_aggregate_digest"],
        holdout_aggregate_digest=manifest["holdout_aggregate_digest"],
        controller_commit=commit,
        controller_digest=controller_digest,
        custody_manifest_digest=manifest["custody_manifest_digest"],
        set_identity=manifest["set_identity"],
        import_request_digest=manifest["import_request_digest"],
    )
    custody._write_bytes(root / "receipt.json", canonical_bytes(receipt.public_dict()))
    return manifest


def tree_snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """(mode, sha256) of every entry under ``root`` for before/after comparisons."""
    snapshot: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        snapshot[str(path.relative_to(root))] = (info.st_mode & 0o777, digest)
    snapshot["."] = (root.lstat().st_mode & 0o777, "")
    return snapshot
