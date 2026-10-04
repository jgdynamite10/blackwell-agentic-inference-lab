"""Offline materialization of sealed qualification tasks (decision D-0025).

Historical external source entries carry a ``task_id`` and a scenario
document but no D-0024 ``instance`` object. This module turns an exact
frozen source set into the frozen ordered qualification schedule that the
**existing production generator** defines, and nothing else. It is invoked
as ``python -m blackwell_lab.cloud.sealed_materialize``.

Reused production surfaces, unchanged:

* The schedule of a stage is **one call** to
  :func:`blackwell_lab.workload.sampling.generate_task_instances` with
  ``stage_spec(stage)["template_ids"]``, ``stage_spec(stage)["tasks"]`` and
  :data:`blackwell_lab.cloud.qualification.MEASURED_REPETITION_SEED` — the
  same call, arguments and seed the qualification runner uses for the single
  measured repetition of a catalog cell. Its 20-element return value is the
  sole authoritative execution order. Nothing here derives occurrences, and
  no second generator, placeholder, timestamp, random value, or
  task-body-derived value exists.
* The scenario of every slot is the public catalog scenario
  ``catalog()[instance.template_id]``. Every source entry must be
  deep-equal to that catalog scenario; the source set must have exactly
  the frozen stage templates and the frozen per-template distribution.
* :func:`blackwell_lab.cloud.sealed_payload.encode_sealed_task` and
  :func:`blackwell_lab.cloud.sealed_payload.decode_sealed_task` are the
  payload contract. Every payload is encoded only through the encoder, must
  decode strictly and must re-encode byte for byte.

Output task identifiers are fixed: ``sealed-<stage>-<slot:04d>`` where
``slot`` is the zero-based position in the generator sequence. Their
lexicographic order equals materializer output order, custody index order,
sealed runtime order and the catalog cell's measured order. Historical
source identifiers are bound through the source aggregate and must be
valid and unique, but they never control occurrence, output identity,
prompt surface or runtime order.

Nothing here prints task bodies, source identifiers, private paths, blob
names or accepted answers. Failures are a
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
from blackwell_lab.cloud.qualification import MEASURED_REPETITION_SEED, stage_spec
from blackwell_lab.cloud.sealed_payload import (
    PAYLOAD_KIND,
    PAYLOAD_SCHEMA_VERSION,
    SealedPayloadError,
    decode_sealed_task,
    encode_sealed_task,
    scenario_document,
)
from blackwell_lab.paths import repository_root
from blackwell_lab.sealed_sets import custody
from blackwell_lab.sealed_sets.custody import (
    CustodyReceipt,
    import_authorized_set,
    load_bundle_directory,
    prepare_import_request,
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
from blackwell_lab.workload.sampling import TaskInstance, generate_task_instances
from blackwell_lab.workload.scenarios import Scenario, catalog, catalog_digest

MATERIALIZATION_VERSION = "1.0.0"
MATERIALIZATION_KIND = "sealed-qualification-task-materialization"
RECORD_KIND = "sealed-qualification-task-materialization-record"
RECORD_NAME = "materialization.json"
GENERATOR = "blackwell_lab.workload.sampling.generate_task_instances"
SEED_RULE = (
    "blackwell_lab.cloud.qualification.MEASURED_REPETITION_SEED: the frozen "
    "generation seed plus measured repetition index 1 (one repetition, no warm-up)"
)
#: One generator call per stage with the frozen stage template ids, the
#: frozen task count and the measured-repetition seed; the returned
#: sequence is the schedule.
SCHEDULE_RULE = "one-call-round-robin-generator-schedule"
TASK_ID_RULE = "sealed-<stage>-<slot:04d>"
SOURCE_ORDERING = "task-id-sorted"
OUTPUT_ORDERING = "generator-schedule"
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
# Frozen schedule and fixed identifiers
# --------------------------------------------------------------------------


def frozen_schedule(stage: str) -> tuple[TaskInstance, ...]:
    """The stage's execution schedule: exactly one production generator call.

    Identical to the schedule the qualification runner generates for the
    measured repetition of a catalog cell of this stage.
    """
    if stage not in STAGES:
        raise CustodyError("stage-separation")
    spec = stage_spec(stage)
    schedule = generate_task_instances(
        spec["template_ids"],
        spec["tasks"],
        MEASURED_REPETITION_SEED,
    )
    if len(schedule) != TASKS_PER_STAGE:
        raise CustodyError("generator-mismatch")
    return tuple(schedule)


def fixed_task_id(stage: str, slot: int) -> str:
    """``sealed-<stage>-<slot:04d>``: the custody id of one generator slot."""
    if stage not in STAGES or not isinstance(slot, int) or not 0 <= slot < TASKS_PER_STAGE:
        raise CustodyError("generator-mismatch")
    return f"sealed-{stage}-{slot:04d}"


def fixed_task_ids(stage: str) -> tuple[str, ...]:
    return tuple(fixed_task_id(stage, slot) for slot in range(TASKS_PER_STAGE))


def schedule_tuples(instances: Sequence[TaskInstance]) -> tuple[tuple[str, int, str, int], ...]:
    """Ordered ``(scenario_id, instance_seed, tracking_id, reported_minute)`` tuples."""
    return tuple(
        (item.template_id, item.instance_seed, item.tracking_id, item.reported_minute)
        for item in instances
    )


# --------------------------------------------------------------------------
# Source entries and the frozen source gate
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceEntry:
    """One parsed historical source entry. Private; never printed."""

    task_id: str
    scenario_id: str
    scenario: dict[str, Any]


@dataclass(frozen=True)
class MaterializedStage:
    """One materialized stage in generator-schedule order. Private; never printed."""

    stage: str
    tasks: tuple[OpaqueTask, ...]
    schedule: tuple[TaskInstance, ...]

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(task.task_id for task in self.tasks)

    @property
    def distribution(self) -> dict[str, int]:
        return dict(Counter(item.template_id for item in self.schedule))


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


def canonical_scenario_bytes(scenario: Scenario | Mapping[str, Any]) -> bytes:
    """Canonical JSON of a scenario document (tuples and lists compare equal)."""
    document = scenario_document(scenario) if isinstance(scenario, Scenario) else dict(scenario)
    return canonical_bytes(document)


def require_frozen_sources(
    stage: str,
    sources: Sequence[OpaqueTask],
    schedule: Sequence[TaskInstance] | None = None,
) -> tuple[SourceEntry, ...]:
    """The exact frozen source gate for one stage.

    Requires exactly twenty entries with valid unique ids, every scenario
    deep-equal to ``catalog()[scenario_id]``, scenario ids drawn only from
    the frozen stage templates, and a per-template distribution equal to the
    one-call generator schedule. Any other source set is refused.
    """
    if schedule is None:
        schedule = frozen_schedule(stage)
    elif stage not in STAGES:
        raise CustodyError("stage-separation")
    if len(sources) != TASKS_PER_STAGE:
        raise CustodyError("task-count")
    entries = tuple(parse_source_entry(task) for task in sources)
    seen: set[str] = set()
    for entry in entries:
        if entry.task_id in seen:
            raise CustodyError("duplicate-id")
        seen.add(entry.task_id)
    expected_templates = set(stage_spec(stage)["template_ids"])
    scenarios = catalog()
    for entry in entries:
        if entry.scenario_id not in expected_templates or entry.scenario_id not in scenarios:
            raise CustodyError("scenario-set-mismatch")
        if canonical_scenario_bytes(entry.scenario) != canonical_scenario_bytes(
            scenarios[entry.scenario_id]
        ):
            raise CustodyError("scenario-mismatch")
    observed = Counter(entry.scenario_id for entry in entries)
    if observed != Counter(item.template_id for item in schedule):
        raise CustodyError("distribution-mismatch")
    return entries


def materialize_stage(stage: str, sources: Sequence[OpaqueTask]) -> MaterializedStage:
    """Produce the stage's frozen schedule as D-0024 payloads with fixed ids.

    The source set only has to pass :func:`require_frozen_sources`; it does
    not influence the schedule, the surfaces, the ids or the order.
    """
    schedule = frozen_schedule(stage)
    require_frozen_sources(stage, sources, schedule)
    scenarios = catalog()
    tasks: list[OpaqueTask] = []
    for slot, instance in enumerate(schedule):
        task_id = fixed_task_id(stage, slot)
        scenario = scenarios[instance.template_id]
        payload = encode_sealed_task(
            task_id=task_id,
            scenario=scenario,
            instance_seed=instance.instance_seed,
            tracking_id=instance.tracking_id,
            reported_minute=instance.reported_minute,
        )
        try:
            decoded = decode_sealed_task(payload, expected_task_id=task_id)
        except SealedPayloadError:
            raise CustodyError("generator-mismatch") from None
        re_encoded = encode_sealed_task(
            task_id=task_id,
            scenario=decoded.scenario,
            instance_seed=decoded.instance.instance_seed,
            tracking_id=decoded.instance.tracking_id,
            reported_minute=decoded.instance.reported_minute,
        )
        if re_encoded != payload:
            raise CustodyError("generator-mismatch")
        if canonical_scenario_bytes(decoded.scenario) != canonical_scenario_bytes(scenario):
            raise CustodyError("generator-mismatch")
        if schedule_tuples([decoded.instance]) != schedule_tuples([instance]):
            raise CustodyError("generator-mismatch")
        if decoded.instance.instance_id != task_id:
            raise CustodyError("generator-mismatch")
        tasks.append(OpaqueTask(task_id, payload))
    materialized = MaterializedStage(stage=stage, tasks=tuple(tasks), schedule=schedule)
    if materialized.task_ids != fixed_task_ids(stage):
        raise CustodyError("generator-mismatch")
    if list(materialized.task_ids) != sorted(materialized.task_ids):
        raise CustodyError("generator-mismatch")
    return materialized


def distribution_summary(distribution: Mapping[str, int]) -> dict[str, Any]:
    """Frozen per-template distribution: template ids are public catalog ids."""
    ordered = {key: int(distribution[key]) for key in sorted(distribution)}
    return {
        "template_count": len(ordered),
        "counts": sorted(ordered.values(), reverse=True),
        "digest": sha256_digest(canonical_bytes(ordered)),
    }


def _source_summary(development: Sequence[OpaqueTask], holdout: Sequence[OpaqueTask]) -> dict:
    validation = validate_bundles(development, holdout)
    sorted_dev = sorted(development, key=lambda task: task.task_id)
    sorted_hold = sorted(holdout, key=lambda task: task.task_id)
    return {
        "development_task_count": validation.development.task_count,
        "holdout_task_count": validation.holdout.task_count,
        "development_aggregate_digest": validation.development.aggregate_digest,
        "holdout_aggregate_digest": validation.holdout.aggregate_digest,
        "set_identity": set_identity_for(sorted_dev, sorted_hold),
        "ordering": SOURCE_ORDERING,
    }


def _output_summary(dev: MaterializedStage, hold: MaterializedStage) -> dict:
    validation = validate_bundles(dev.tasks, hold.tasks)
    return {
        "development_task_count": validation.development.task_count,
        "holdout_task_count": validation.holdout.task_count,
        "development_aggregate_digest": validation.development.aggregate_digest,
        "holdout_aggregate_digest": validation.holdout.aggregate_digest,
        # Generator-schedule order equals fixed-id order; this is the identity
        # the unchanged D-0023 importer records when fed in that order.
        "set_identity": set_identity_for(dev.tasks, hold.tasks),
        "ordering": OUTPUT_ORDERING,
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
    source = _source_summary(development, holdout)
    dev = materialize_stage(DEVELOPMENT_STAGE, development)
    hold = materialize_stage(HOLDOUT_STAGE, holdout)
    request: dict[str, Any] = {
        "request_version": MATERIALIZATION_VERSION,
        "kind": MATERIALIZATION_KIND,
        "controller_commit": require_commit(commit),
        "implementation_digest": require_digest(implementation_digest),
        "algorithm": {
            "generator": GENERATOR,
            "seed": MEASURED_REPETITION_SEED,
            "seed_rule": SEED_RULE,
            "schedule_rule": SCHEDULE_RULE,
            "task_id_rule": TASK_ID_RULE,
            "tasks_per_stage": TASKS_PER_STAGE,
            "catalog_digest": catalog_digest(),
            "development_template_ids": list(stage_spec(DEVELOPMENT_STAGE)["template_ids"]),
            "holdout_template_ids": list(stage_spec(HOLDOUT_STAGE)["template_ids"]),
            "payload_schema_version": PAYLOAD_SCHEMA_VERSION,
            "payload_kind": PAYLOAD_KIND,
        },
        "source": source,
        "distribution": {
            DEVELOPMENT_STAGE: distribution_summary(dev.distribution),
            HOLDOUT_STAGE: distribution_summary(hold.distribution),
        },
        "output": _output_summary(dev, hold),
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
    """Re-read the written bundles in fixed order and re-derive everything."""
    written_dev = load_materialized_bundle(destination / DEVELOPMENT_STAGE, repo=repo)
    written_hold = load_materialized_bundle(destination / HOLDOUT_STAGE, repo=repo)
    for written, expected in ((written_dev, dev), (written_hold, hold)):
        if written != expected.tasks:
            raise CustodyError("tamper")
        decoded = []
        for task in written:
            try:
                decoded.append(decode_sealed_task(task.content, expected_task_id=task.task_id))
            except SealedPayloadError:
                raise CustodyError("tamper") from None
        if schedule_tuples([item.instance for item in decoded]) != schedule_tuples(
            frozen_schedule(expected.stage)
        ):
            raise CustodyError("tamper")
    validation = validate_bundles(written_dev, written_hold)
    if (
        validation.development.aggregate_digest != request["output"]["development_aggregate_digest"]
        or validation.holdout.aggregate_digest != request["output"]["holdout_aggregate_digest"]
        or set_identity_for(written_dev, written_hold) != request["output"]["set_identity"]
    ):
        raise CustodyError("tamper")


# --------------------------------------------------------------------------
# Handing materialized bundles to the unchanged D-0023 importer
# --------------------------------------------------------------------------


def load_materialized_bundle(raw: str | Path, *, repo: Path) -> tuple[OpaqueTask, ...]:
    """Load one materialized bundle in fixed task-id order.

    Uses the production bundle loader, then orders by task id so the
    sequence handed to the importer never depends on directory iteration,
    creation order or filesystem. The ids must be exactly the fixed ids of
    one stage.
    """
    tasks = tuple(sorted(load_bundle_directory(raw, repo=repo), key=lambda task: task.task_id))
    ids = tuple(task.task_id for task in tasks)
    if ids not in {fixed_task_ids(stage) for stage in STAGES}:
        raise CustodyError("materialized-ids")
    return tasks


def prepare_materialized_import(
    *,
    repo: Path,
    expected_commit: str,
    development: str | Path,
    holdout: str | Path,
) -> dict:
    """The unchanged D-0023 import request over the bundles in fixed order."""
    dev = load_materialized_bundle(development, repo=repo)
    hold = load_materialized_bundle(holdout, repo=repo)
    _require_stage_ids(dev, hold)
    return prepare_import_request(
        repo=repo, expected_commit=expected_commit, development=dev, holdout=hold
    )


def import_materialized_bundles(
    *,
    repo: Path,
    output_root: str | Path,
    expected_commit: str,
    expected_controller_digest: str,
    expected_request_digest: str,
    approval: str,
    development: str | Path,
    holdout: str | Path,
) -> CustodyReceipt:
    """The unchanged D-0023 import, fed the bundles in fixed task-id order.

    The approval is the D-0023 import phrase bound to the import-request
    digest; custody behavior, layout, and verification are untouched.
    """
    dev = load_materialized_bundle(development, repo=repo)
    hold = load_materialized_bundle(holdout, repo=repo)
    _require_stage_ids(dev, hold)
    return import_authorized_set(
        repo=repo,
        output_root=output_root,
        expected_commit=expected_commit,
        expected_controller_digest=expected_controller_digest,
        expected_request_digest=expected_request_digest,
        approval=approval,
        development=dev,
        holdout=hold,
    )


def _require_stage_ids(dev: Sequence[OpaqueTask], hold: Sequence[OpaqueTask]) -> None:
    if tuple(task.task_id for task in dev) != fixed_task_ids(DEVELOPMENT_STAGE):
        raise CustodyError("materialized-ids")
    if tuple(task.task_id for task in hold) != fixed_task_ids(HOLDOUT_STAGE):
        raise CustodyError("materialized-ids")


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m blackwell_lab.cloud.sealed_materialize",
        description=(
            "Offline materialization of sealed qualification tasks (D-0025). "
            "Produces the frozen one-call generator schedule as D-0024 payloads "
            "from an exact frozen source set. " + INTEGRITY_STATEMENT
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
    import_preparer = sub.add_parser(
        "prepare-import",
        help="Print the unchanged D-0023 import request for materialized bundles in fixed order.",
    )
    importer = sub.add_parser(
        "import-materialized",
        help="Run the unchanged D-0023 import over materialized bundles in fixed order.",
    )
    for target in (preparer, writer, import_preparer, importer):
        target.add_argument("--repo", required=True)
        target.add_argument("--commit", required=True)
        target.add_argument("--development", required=True)
        target.add_argument("--holdout", required=True)
    for target in (writer, importer):
        target.add_argument("--output", required=True)
        target.add_argument("--request-digest", required=True)
        target.add_argument("--approve", required=True)
    writer.add_argument("--implementation-digest", required=True)
    importer.add_argument("--controller-digest", required=True)
    return parser


def _emit(document: Mapping[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "approval-phrase":
            print(APPROVAL_TEMPLATE)
            return 0
        repo = Path(args.repo)
        if args.command == "prepare-import":
            _emit(
                prepare_materialized_import(
                    repo=repo,
                    expected_commit=args.commit,
                    development=args.development,
                    holdout=args.holdout,
                )
            )
            return 0
        if args.command == "import-materialized":
            receipt = import_materialized_bundles(
                repo=repo,
                output_root=args.output,
                expected_commit=args.commit,
                expected_controller_digest=args.controller_digest,
                expected_request_digest=args.request_digest,
                approval=args.approve,
                development=args.development,
                holdout=args.holdout,
            )
            _emit(receipt.public_dict())
            return 0
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
