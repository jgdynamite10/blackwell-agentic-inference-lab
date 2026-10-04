"""Offline materialization of sealed qualification tasks (decision D-0025).

Historical external source entries carry a ``task_id`` and a scenario
document but no D-0024 ``instance`` object. This module adds exactly the
D-0024 payload envelope and the instance object that the **existing
production generator** produces, and nothing else. It is invoked as
``python -m blackwell_lab.cloud.sealed_materialize``.

Reused production surfaces, unchanged:

* :func:`blackwell_lab.workload.sampling.generate_task_instances` is the
  only source of ``instance_seed``, ``tracking_id`` and ``reported_minute``.
  No second derivation, placeholder, timestamp, random value, or
  task-body-derived value exists here.
* The seed is :data:`blackwell_lab.cloud.qualification.MEASURED_REPETITION_SEED`,
  the seed the qualification runner hands the generator for the single
  measured repetition of a development or holdout stage
  (``generation.seed + repetition_index`` with ``repetition_index == 1``).
* The occurrence of a task is the production convention applied to the
  frozen stage ordering: tasks execute in task-identifier order
  (D-0024), and a task's occurrence is the zero-based count of earlier
  tasks in that order that share its ``scenario_id`` — exactly the
  per-template counter ``generate_task_instances`` keeps over a schedule.
* :func:`blackwell_lab.cloud.sealed_payload.decode_sealed_task` and
  :func:`blackwell_lab.cloud.sealed_payload.encode_sealed_task` are the
  payload contract. Every materialized payload must decode strictly and
  re-encode byte for byte.

Task identifiers, scenario content, stage membership, task-identifier
ordering and the per-scenario distribution are preserved. Output bundles
are ordinary D-0023 bundle directories (filename = task id, ``0600``
files in a ``0700`` directory) that the unchanged custody tool can later
``prepare`` and ``import-bundles``.

Nothing here prints task bodies, task identifiers, private paths, blob
names, scenario identifiers, or accepted answers. Failures are a
:class:`~blackwell_lab.sealed_sets.model.CustodyError` with a short reason
code. SHA-256 digests are integrity checks, not encryption and not a seal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from blackwell_lab.cloud import sealed_payload
from blackwell_lab.cloud.qualification import MEASURED_REPETITION_SEED
from blackwell_lab.cloud.sealed_payload import (
    PAYLOAD_KIND,
    PAYLOAD_SCHEMA_VERSION,
    SealedPayloadError,
    decode_sealed_task,
    encode_sealed_task,
)
from blackwell_lab.paths import repository_root
from blackwell_lab.sealed_sets import custody
from blackwell_lab.sealed_sets.custody import (
    load_bundle_directory,
    require_external_directory,
    verify_clean_checkout,
)
from blackwell_lab.sealed_sets.model import (
    DEVELOPMENT_STAGE,
    HOLDOUT_STAGE,
    INTEGRITY_STATEMENT,
    STAGES,
    TASKS_PER_STAGE,
    CustodyError,
    OpaqueTask,
    canonical_bytes,
    canonical_document_bytes,
    require_commit,
    require_digest,
    set_identity_for,
    sha256_digest,
    validate_bundles,
)
from blackwell_lab.workload import sampling
from blackwell_lab.workload.sampling import generate_task_instances

MATERIALIZATION_VERSION = "1.0.0"
MATERIALIZATION_KIND = "sealed-qualification-task-materialization"
RECORD_KIND = "sealed-qualification-task-materialization-record"
RECORD_NAME = "materialization.json"
GENERATOR = "blackwell_lab.workload.sampling.generate_task_instances"
SEED_RULE = (
    "blackwell_lab.cloud.qualification.MEASURED_REPETITION_SEED: the frozen "
    "generation seed plus measured repetition index 1 (one repetition, no warm-up)"
)
OCCURRENCE_RULE = "task-id-sorted-per-template-counter"
ORDERING = "task-id-sorted"
APPROVAL_TEMPLATE = (
    "I approve sealed qualification-task materialization using request sha256:{request_digest}"
)
#: Exact shape of one historical source entry: an envelope-free document.
SOURCE_KEYS = frozenset({"task_id", "scenario"})
#: Sources whose committed bytes the approval binds, in digest order.
IMPLEMENTATION_SOURCE_PATHS = (
    "schemas/sealed-task-payload.schema.json",
    "src/blackwell_lab/cloud/sealed_materialize.py",
    "src/blackwell_lab/cloud/sealed_payload.py",
    "src/blackwell_lab/workload/sampling.py",
)
_GIT_REGULAR_MODES = frozenset({"100644", "100755"})
_DIGEST_FIELD = "materialization_request_digest"


def schema_path() -> Path:
    return repository_root() / "schemas" / "sealed-materialization-request.schema.json"


def load_request_schema() -> dict[str, Any]:
    try:
        document = json.loads(schema_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CustodyError("request-schema-unavailable") from None
    if not isinstance(document, dict):
        raise CustodyError("request-schema-unavailable")
    return document


def materialization_approval_phrase(request_digest: str) -> str:
    """Exact owner phrase for one materialization request. Does not approve."""
    try:
        require_digest(request_digest)
    except CustodyError:
        raise CustodyError("approval-mismatch") from None
    return APPROVAL_TEMPLATE.format(request_digest=request_digest.removeprefix("sha256:"))


# --------------------------------------------------------------------------
# Implementation digest (commit-bound, like the custody controller digest)
# --------------------------------------------------------------------------


def implementation_digest_from_sources(entries: Sequence[tuple[str, str, bytes]]) -> str:
    """``path NUL mode NUL sha256(blob) NUL`` over :data:`IMPLEMENTATION_SOURCE_PATHS`."""
    if len(entries) != len(IMPLEMENTATION_SOURCE_PATHS):
        raise CustodyError("implementation-digest-mismatch")
    hasher = hashlib.sha256()
    for (path, mode, blob), expected in zip(entries, IMPLEMENTATION_SOURCE_PATHS, strict=True):
        if (
            path != expected
            or not isinstance(mode, str)
            or mode not in _GIT_REGULAR_MODES
            or not isinstance(blob, bytes)
            or not blob
        ):
            raise CustodyError("implementation-digest-mismatch")
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(mode.encode("ascii"))
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(blob).digest())
        hasher.update(b"\0")
    return "sha256:" + hasher.hexdigest()


def _running_files() -> dict[str, Path]:
    return {
        "schemas/sealed-task-payload.schema.json": sealed_payload.schema_path(),
        "src/blackwell_lab/cloud/sealed_materialize.py": Path(__file__),
        "src/blackwell_lab/cloud/sealed_payload.py": Path(sealed_payload.__file__),
        "src/blackwell_lab/workload/sampling.py": Path(sampling.__file__),
    }


def running_implementation_parts() -> tuple[tuple[str, str, bytes], ...]:
    """Bytes and git modes of the implementation files executing now."""
    files = _running_files()
    parts: list[tuple[str, str, bytes]] = []
    for rel in IMPLEMENTATION_SOURCE_PATHS:
        path = files[rel]
        try:
            info = path.lstat()
        except OSError:
            raise CustodyError("implementation-digest-mismatch") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise CustodyError("implementation-digest-mismatch")
        mode = "100755" if stat.S_IMODE(info.st_mode) & 0o111 else "100644"
        parts.append((rel, mode, path.read_bytes()))
    return tuple(parts)


def running_implementation_digest() -> str:
    return implementation_digest_from_sources(running_implementation_parts())


def _commit_implementation_parts(repo: Path, commit: str) -> tuple[tuple[str, str, bytes], ...]:
    entries = custody._commit_entries(repo, commit)
    parts: list[tuple[str, str, bytes]] = []
    for rel in IMPLEMENTATION_SOURCE_PATHS:
        found = entries.get(rel)
        if found is None:
            raise CustodyError("implementation-absent")
        mode, kind, oid = found
        if kind != "blob" or mode not in _GIT_REGULAR_MODES:
            raise CustodyError("implementation-digest-mismatch")
        blob = custody._git_raw(
            repo, "cat-file", "blob", oid, reason="implementation-digest-mismatch"
        )
        parts.append((rel, mode, blob))
    return tuple(parts)


def require_bound_implementation(repo: Path, commit: str) -> str:
    """Digest the implementation blobs stored in ``commit`` and bind this process.

    The executing files must digest identically and must be the worktree
    files at their canonical paths inside ``repo``.
    """
    commit_digest = implementation_digest_from_sources(_commit_implementation_parts(repo, commit))
    if commit_digest != running_implementation_digest():
        raise CustodyError("implementation-digest-mismatch")
    repo_root = repo.resolve()
    for rel, path in _running_files().items():
        if path.resolve() != (repo_root / rel).resolve():
            raise CustodyError("implementation-location")
    return commit_digest


# --------------------------------------------------------------------------
# Source entries and stage materialization
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceEntry:
    """One parsed historical source entry. Private; never printed."""

    task_id: str
    scenario_id: str
    scenario: dict[str, Any]


@dataclass(frozen=True)
class MaterializedStage:
    """One materialized stage in task-identifier order. Private; never printed."""

    stage: str
    tasks: tuple[OpaqueTask, ...]
    distribution: dict[str, int]


def _reject_constant(_name: str) -> None:
    raise CustodyError("source-shape")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise CustodyError("source-shape")
        document[key] = value
    return document


def parse_source_entry(task: OpaqueTask) -> SourceEntry:
    """Strictly parse one source entry: exactly ``task_id`` and ``scenario``.

    The entry must not already carry an envelope or an ``instance`` object;
    materializing over a materialized payload is not this operation.
    """
    if not isinstance(task, OpaqueTask) or not isinstance(task.content, bytes):
        raise CustodyError("source-shape")
    try:
        document = json.loads(
            task.content.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise CustodyError("source-shape") from None
    if not isinstance(document, dict) or set(document) != SOURCE_KEYS:
        raise CustodyError("source-shape")
    if document["task_id"] != task.task_id:
        raise CustodyError("source-shape")
    scenario = document["scenario"]
    if not isinstance(scenario, dict):
        raise CustodyError("source-shape")
    scenario_id = scenario.get("scenario_id")
    if not isinstance(scenario_id, str) or not scenario_id:
        raise CustodyError("source-shape")
    return SourceEntry(task_id=task.task_id, scenario_id=scenario_id, scenario=scenario)


def frozen_stage_order(entries: Sequence[SourceEntry]) -> tuple[SourceEntry, ...]:
    """The frozen stage ordering: ascending task identifier (D-0024 execution order)."""
    ordered = tuple(sorted(entries, key=lambda entry: entry.task_id))
    if len({entry.task_id for entry in ordered}) != len(ordered):
        raise CustodyError("duplicate-id")
    return ordered


def occurrences(entries: Sequence[SourceEntry]) -> tuple[int, ...]:
    """Zero-based per-scenario occurrence of each entry, in the supplied order.

    This is the counter :func:`generate_task_instances` keeps over a
    schedule, applied to the frozen stage ordering.
    """
    seen: Counter[str] = Counter()
    result: list[int] = []
    for entry in entries:
        result.append(seen[entry.scenario_id])
        seen[entry.scenario_id] += 1
    return tuple(result)


def materialize_stage(
    stage: str, sources: Sequence[OpaqueTask], *, seed: int = MEASURED_REPETITION_SEED
) -> MaterializedStage:
    """Add the D-0024 envelope and generator-produced instance to each source entry."""
    if stage not in STAGES:
        raise CustodyError("stage-separation")
    if len(sources) != TASKS_PER_STAGE:
        raise CustodyError("task-count")
    ordered = frozen_stage_order([parse_source_entry(task) for task in sources])
    distribution = Counter(entry.scenario_id for entry in ordered)
    # One production generator call per scenario yields exactly the
    # instances for occurrences 0..count-1 of that scenario under ``seed``.
    schedules = {
        scenario_id: generate_task_instances((scenario_id,), count, seed)
        for scenario_id, count in distribution.items()
    }
    tasks: list[OpaqueTask] = []
    for entry, occurrence in zip(ordered, occurrences(ordered), strict=True):
        instance = schedules[entry.scenario_id][occurrence]
        if instance.template_id != entry.scenario_id:
            raise CustodyError("generator-mismatch")
        if instance.instance_id != f"{entry.scenario_id}#{occurrence:04d}":
            raise CustodyError("generator-mismatch")
        document = {
            "payload_schema_version": PAYLOAD_SCHEMA_VERSION,
            "kind": PAYLOAD_KIND,
            "task_id": entry.task_id,
            "instance": {
                "instance_seed": instance.instance_seed,
                "tracking_id": instance.tracking_id,
                "reported_minute": instance.reported_minute,
            },
            "scenario": entry.scenario,
        }
        payload = canonical_bytes(document)
        try:
            decoded = decode_sealed_task(payload, expected_task_id=entry.task_id)
        except SealedPayloadError:
            raise CustodyError("source-invalid") from None
        re_encoded = encode_sealed_task(
            task_id=entry.task_id,
            scenario=decoded.scenario,
            instance_seed=decoded.instance.instance_seed,
            tracking_id=decoded.instance.tracking_id,
            reported_minute=decoded.instance.reported_minute,
        )
        if re_encoded != payload or decoded.instance.template_id != entry.scenario_id:
            raise CustodyError("source-invalid")
        if (
            decoded.instance.instance_seed,
            decoded.instance.tracking_id,
            decoded.instance.reported_minute,
        ) != (instance.instance_seed, instance.tracking_id, instance.reported_minute):
            raise CustodyError("generator-mismatch")
        tasks.append(OpaqueTask(entry.task_id, payload))
    return MaterializedStage(stage=stage, tasks=tuple(tasks), distribution=dict(distribution))


def distribution_summary(distribution: Mapping[str, int]) -> dict[str, Any]:
    """Content-free distribution binding: sizes and a digest, no scenario names."""
    ordered = {key: int(distribution[key]) for key in sorted(distribution)}
    return {
        "template_count": len(ordered),
        "counts": sorted(ordered.values(), reverse=True),
        "digest": sha256_digest(canonical_bytes(ordered)),
    }


def _stage_summary(development: Sequence[OpaqueTask], holdout: Sequence[OpaqueTask]) -> dict:
    validation = validate_bundles(development, holdout)
    sorted_dev = sorted(development, key=lambda task: task.task_id)
    sorted_hold = sorted(holdout, key=lambda task: task.task_id)
    return {
        "development_task_count": validation.development.task_count,
        "holdout_task_count": validation.holdout.task_count,
        "development_aggregate_digest": validation.development.aggregate_digest,
        "holdout_aggregate_digest": validation.holdout.aggregate_digest,
        "set_identity": set_identity_for(sorted_dev, sorted_hold),
        "ordering": ORDERING,
    }


def build_materialization_request(
    *,
    commit: str,
    implementation_digest: str,
    development: Sequence[OpaqueTask],
    holdout: Sequence[OpaqueTask],
) -> tuple[dict[str, Any], MaterializedStage, MaterializedStage]:
    """Content-free request binding inputs, algorithm, and predicted outputs.

    Pure: no git, no filesystem writes. Returns the request together with
    the two materialized stages so the approved write path reuses exactly
    the bytes the request predicted.
    """
    source = _stage_summary(development, holdout)
    dev = materialize_stage(DEVELOPMENT_STAGE, development)
    hold = materialize_stage(HOLDOUT_STAGE, holdout)
    output = _stage_summary(dev.tasks, hold.tasks)
    request: dict[str, Any] = {
        "request_version": MATERIALIZATION_VERSION,
        "kind": MATERIALIZATION_KIND,
        "controller_commit": require_commit(commit),
        "implementation_digest": require_digest(implementation_digest),
        "algorithm": {
            "generator": GENERATOR,
            "seed": MEASURED_REPETITION_SEED,
            "seed_rule": SEED_RULE,
            "occurrence_rule": OCCURRENCE_RULE,
            "payload_schema_version": PAYLOAD_SCHEMA_VERSION,
            "payload_kind": PAYLOAD_KIND,
        },
        "source": source,
        "distribution": {
            DEVELOPMENT_STAGE: distribution_summary(dev.distribution),
            HOLDOUT_STAGE: distribution_summary(hold.distribution),
        },
        "output": output,
    }
    request[_DIGEST_FIELD] = sha256_digest(canonical_document_bytes(request, exclude=_DIGEST_FIELD))
    validate_request(request)
    return request, dev, hold


def validate_request(document: object) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise CustodyError("request-invalid")
    try:
        jsonschema.validate(document, load_request_schema())
    except jsonschema.ValidationError:
        raise CustodyError("request-invalid") from None
    expected = sha256_digest(canonical_document_bytes(document, exclude=_DIGEST_FIELD))
    if document.get(_DIGEST_FIELD) != expected:
        raise CustodyError("request-invalid")
    return document


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def prepare_materialization(
    *,
    repo: Path,
    expected_commit: str,
    development: Sequence[OpaqueTask],
    holdout: Sequence[OpaqueTask],
) -> dict[str, Any]:
    """Content-free materialization request for one exact input set. Does not write."""
    commit = verify_clean_checkout(repo, expected_commit)
    implementation_digest = require_bound_implementation(repo, commit)
    request, _dev, _hold = build_materialization_request(
        commit=commit,
        implementation_digest=implementation_digest,
        development=development,
        holdout=holdout,
    )
    return request


def materialize_authorized_bundles(
    *,
    repo: Path,
    output_root: str | Path,
    expected_commit: str,
    expected_implementation_digest: str,
    expected_request_digest: str,
    approval: str,
    development: Sequence[OpaqueTask],
    holdout: Sequence[OpaqueTask],
) -> dict[str, Any]:
    """Write the two materialized bundles for one approved request.

    The destination must not exist; nothing is ever overwritten. On any
    failure the directory this call created is removed.
    """
    commit = verify_clean_checkout(repo, expected_commit)
    implementation_digest = require_bound_implementation(repo, commit)
    if expected_implementation_digest != implementation_digest:
        raise CustodyError("implementation-digest-mismatch")
    request, dev, hold = build_materialization_request(
        commit=commit,
        implementation_digest=implementation_digest,
        development=development,
        holdout=holdout,
    )
    if request[_DIGEST_FIELD] != expected_request_digest:
        raise CustodyError("request-mismatch")
    if approval != materialization_approval_phrase(request[_DIGEST_FIELD]):
        raise CustodyError("approval-mismatch")
    destination = require_external_directory(output_root, repo=repo)
    if destination.is_symlink() or destination.exists():
        raise CustodyError("destination-exists")
    try:
        custody._mkdir(destination)
    except OSError:
        raise CustodyError("destination-unavailable") from None
    try:
        _write_bundles(destination, dev, hold)
        _verify_written(destination, repo=repo, request=request, dev=dev, hold=hold)
        record = _record(request, operation="materialize")
        custody._write_bytes(destination / RECORD_NAME, canonical_bytes(record))
    except CustodyError:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise CustodyError("interrupted") from None
    return record


def _record(request: dict[str, Any], *, operation: str) -> dict[str, Any]:
    return {
        "kind": RECORD_KIND,
        "status": "pass",
        "operation": operation,
        "integrity_statement": INTEGRITY_STATEMENT,
        "request": request,
    }


def _write_bundles(destination: Path, dev: MaterializedStage, hold: MaterializedStage) -> None:
    for materialized in (dev, hold):
        stage_dir = destination / materialized.stage
        custody._mkdir(stage_dir)
        for task in materialized.tasks:
            custody._write_bytes(stage_dir / task.task_id, task.content)


def _verify_written(
    destination: Path,
    *,
    repo: Path,
    request: dict[str, Any],
    dev: MaterializedStage,
    hold: MaterializedStage,
) -> None:
    """Re-read the written bundles with the production loader and re-derive."""
    written_dev = load_bundle_directory(destination / DEVELOPMENT_STAGE, repo=repo)
    written_hold = load_bundle_directory(destination / HOLDOUT_STAGE, repo=repo)
    if _stage_summary(written_dev, written_hold) != request["output"]:
        raise CustodyError("tamper")
    for stage, written, expected in (
        (DEVELOPMENT_STAGE, written_dev, dev),
        (HOLDOUT_STAGE, written_hold, hold),
    ):
        by_id = {task.task_id: task.content for task in written}
        if by_id != {task.task_id: task.content for task in expected.tasks}:
            raise CustodyError("tamper")
        observed: Counter[str] = Counter()
        for task in written:
            try:
                decoded = decode_sealed_task(task.content, expected_task_id=task.task_id)
            except SealedPayloadError:
                raise CustodyError("tamper") from None
            observed[decoded.scenario.scenario_id] += 1
        if distribution_summary(observed) != request["distribution"][stage]:
            raise CustodyError("tamper")


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m blackwell_lab.cloud.sealed_materialize",
        description=(
            "Offline materialization of sealed qualification tasks (D-0025). "
            "Adds only the D-0024 envelope and the production-generated "
            "instance object to externally supplied source entries. " + INTEGRITY_STATEMENT
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "approval-phrase",
        help="Print the approval phrase format without approving anything.",
    )
    preparer = sub.add_parser(
        "prepare-materialization",
        help="Print a content-free materialization request. Does not write or approve.",
    )
    writer = sub.add_parser(
        "materialize-bundles",
        help="Write the two materialized bundles after the exact approval phrase.",
    )
    for target in (preparer, writer):
        target.add_argument("--repo", required=True)
        target.add_argument("--commit", required=True)
        target.add_argument("--development", required=True)
        target.add_argument("--holdout", required=True)
    writer.add_argument("--output", required=True)
    writer.add_argument("--implementation-digest", required=True)
    writer.add_argument("--request-digest", required=True)
    writer.add_argument("--approve", required=True)
    return parser


def _emit(document: dict[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "approval-phrase":
            print(APPROVAL_TEMPLATE)
            return 0
        repo = Path(args.repo)
        development = load_bundle_directory(args.development, repo=repo)
        holdout = load_bundle_directory(args.holdout, repo=repo)
        if args.command == "prepare-materialization":
            _emit(
                prepare_materialization(
                    repo=repo,
                    expected_commit=args.commit,
                    development=development,
                    holdout=holdout,
                )
            )
            return 0
        if args.command == "materialize-bundles":
            _emit(
                materialize_authorized_bundles(
                    repo=repo,
                    output_root=args.output,
                    expected_commit=args.commit,
                    expected_implementation_digest=args.implementation_digest,
                    expected_request_digest=args.request_digest,
                    approval=args.approve,
                    development=development,
                    holdout=holdout,
                )
            )
            return 0
        print("BLOCKED: usage", file=sys.stderr)
        return 2
    except CustodyError as exc:
        print(f"BLOCKED: {exc.reason}", file=sys.stderr)
        return 1
    except Exception:
        print("BLOCKED: internal", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
