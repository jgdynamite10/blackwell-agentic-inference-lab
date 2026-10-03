"""Synthetic external custody fixtures for sealed-stage tests (D-0024).

Everything here is artificial: task bodies are re-encodings of the public
synthetic scenario catalog with invented instance surfaces, task ids are
``syn-…`` placeholders, and the custody tree is written to a pytest
``tmp_path`` outside every Git repository using the production custody
writer so the on-disk layout, modes, index, manifest, and receipt are
byte-for-byte what a real import would produce. No real sealed task,
accepted answer, private result, or private path appears anywhere.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from blackwell_lab.cloud.sealed_binding import BINDING_SCHEMA_VERSION
from blackwell_lab.cloud.sealed_payload import PAYLOAD_SCHEMA_VERSION, encode_sealed_task
from blackwell_lab.sealed_sets import custody
from blackwell_lab.sealed_sets.model import (
    OpaqueTask,
    build_import_request,
    running_controller_digest,
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


def stage_blob_paths(root: Path, stage: str) -> set[Path]:
    """Blob paths of one stage, read from the private index (test oracle only)."""
    index = json.loads((root / "private" / "index.json").read_text(encoding="utf-8"))
    return {
        root / "private" / "blobs" / row["content_digest"].removeprefix("sha256:")
        for row in index[stage]
    }


def tree_snapshot(root: Path) -> dict[str, tuple[int, str]]:
    """(mode, sha256) of every entry under ``root`` for before/after comparisons."""
    snapshot: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        snapshot[str(path.relative_to(root))] = (info.st_mode & 0o777, digest)
    snapshot["."] = (root.lstat().st_mode & 0o777, "")
    return snapshot
