"""Input validation and semantic result validation (beyond JSON Schema).

``validate_runner_config`` rejects malformed benchmark configurations before
any work happens: non-positive repetitions/timeouts, negative warm-up counts,
fewer tasks than requested concurrency, booleans passed where integers are
required, and empty required strings. Every rejection carries a sanitized
message (no paths, no environment values) and produces a nonzero CLI exit.

``validate_result_semantics`` enforces the logical invariants that a JSON
Schema cannot express: exact task accounting, rate/count agreement, matching
run ids across manifest/result/observations, observation-count agreement,
truthful concurrency bounds, and safe relative artifact references.
"""

from __future__ import annotations

import re

from blackwell_lab.workload.agent import ERROR_TAXONOMY

_SAFE_RELATIVE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_SHA256_PREFIXED = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Tolerance for comparing recomputed rates against recorded (rounded) rates.
_RATE_TOLERANCE = 5e-7

#: ``workload.task_source.kind`` discriminator (decision D-0024). A catalog
#: run draws tasks from the public scenario catalog and records its
#: ``catalog_digest``; a sealed run draws every task from one D-0023 custody
#: stage and records that stage's aggregate under ``task_source.digest``
#: with the content-free binding under ``task_source.sealed_set``. The two
#: are mutually exclusive. Manifests that predate the discriminator carry
#: no ``task_source`` and are catalog runs.
TASK_SOURCE_CATALOG = "catalog"
TASK_SOURCE_SEALED = "sealed"
_SEALED_SET_FIELDS = frozenset(
    {
        "schema_version",
        "custody_manifest_sha256",
        "custody_controller_digest",
        "import_request_digest",
        "set_identity",
        "stage",
        "stage_aggregate_digest",
        "task_count",
        "payload_schema_version",
    }
)


def catalog_task_source() -> dict[str, str]:
    """``workload.task_source`` for a run whose tasks come from the public catalog."""
    return {"kind": TASK_SOURCE_CATALOG}


def task_source_kind(workload: dict) -> str:
    """Resolve the task source of a manifest ``workload`` block (legacy = catalog)."""
    source = workload.get("task_source")
    if source is None:
        return TASK_SOURCE_CATALOG
    _check(isinstance(source, dict), "workload.task_source must be an object")
    kind = source.get("kind")
    _check(
        kind in (TASK_SOURCE_CATALOG, TASK_SOURCE_SEALED),
        "workload.task_source.kind must be catalog or sealed",
    )
    return str(kind)


class ConfigError(ValueError):
    """A benchmark configuration value is invalid (sanitized message)."""


class SemanticValidationError(ValueError):
    """A result document violates a semantic invariant."""


def _require_int(name: str, value: object, *, minimum: int) -> int:
    # bool is a subclass of int: reject it explicitly wherever ints are required.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer, got {type(value).__name__}")
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _require_positive_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number, got {type(value).__name__}")
    if value <= 0:
        raise ConfigError(f"{name} must be > 0, got {value}")
    return float(value)


def validate_runner_config(
    *,
    profile_name: object,
    concurrency: object,
    repetitions: object,
    warmup_passes: object,
    tasks_per_repetition: object,
    timeout_ms: object,
    max_turns: object,
    seed: object,
    allowed_profiles: tuple[str, ...],
    allowed_concurrency: tuple[int, ...],
) -> None:
    """Rejects invalid benchmark configurations with sanitized messages."""
    if not isinstance(profile_name, str) or not profile_name.strip():
        raise ConfigError("profile must be a non-empty string")
    if profile_name not in allowed_profiles:
        raise ConfigError(f"unknown profile: {profile_name!r}")
    concurrency_value = _require_int("concurrency", concurrency, minimum=1)
    if concurrency_value not in allowed_concurrency:
        raise ConfigError(f"concurrency must be one of {allowed_concurrency}")
    _require_int("repetitions", repetitions, minimum=1)
    _require_int("warmup_passes", warmup_passes, minimum=0)
    tasks = _require_int("tasks_per_repetition", tasks_per_repetition, minimum=1)
    if tasks < concurrency_value:
        raise ConfigError(
            f"tasks_per_repetition ({tasks}) must be >= requested concurrency "
            f"({concurrency_value}): a measurement cell cannot have fewer tasks "
            "than scheduler slots"
        )
    if timeout_ms is not None:
        _require_positive_number("timeout_ms", timeout_ms)
    _require_int("max_turns", max_turns, minimum=1)
    _require_int("seed", seed, minimum=-(2**63))


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise SemanticValidationError(message)


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= _RATE_TOLERANCE


def validate_result_semantics(
    manifest: dict,
    result: dict,
    *,
    measured_observations: dict | None = None,
    warmup_observations: dict | None = None,
) -> None:
    """Enforces cross-document invariants that JSON Schema cannot express."""
    run_id = manifest.get("run_id")
    _check(bool(run_id), "manifest has no run_id")
    _check(result.get("run_id") == run_id, "result run_id does not match manifest run_id")
    _check(
        result.get("execution_mode") == manifest.get("execution_mode"),
        "result execution_mode does not match manifest execution_mode",
    )

    manifest_ref = result.get("manifest_ref", "")
    _check(
        bool(_SAFE_RELATIVE_NAME.match(manifest_ref)),
        "manifest_ref must be a safe relative filename (no separators, no traversal)",
    )
    _check(
        manifest_ref == f"{run_id}.manifest.json",
        "manifest_ref does not reference this run's manifest",
    )

    tasks = result["tasks"]
    attempted = tasks["attempted"]
    succeeded = tasks["succeeded"]
    quality_failed = tasks["quality_failed"]
    errored = tasks["errored"]
    timed_out = tasks["timed_out"]
    _check(
        attempted == succeeded + quality_failed + errored + timed_out,
        "task accounting must sum exactly: "
        "attempted = succeeded + quality_failed + errored + timed_out",
    )
    _check(attempted >= 1, "a repetition must attempt at least one task")
    _check(_close(tasks["success_rate"], succeeded / attempted), "success_rate mismatch")

    errors = result["errors"]
    _check(_close(errors["error_rate"], errored / attempted), "error_rate mismatch")
    _check(_close(errors["timeout_rate"], timed_out / attempted), "timeout_rate mismatch")
    taxonomy = errors["taxonomy"]
    unknown = sorted(set(taxonomy) - set(ERROR_TAXONOMY))
    _check(not unknown, f"unknown execution-error categories in taxonomy: {unknown}")
    _check(
        sum(taxonomy.values()) == errored + timed_out,
        "taxonomy counts must sum to errored + timed_out (quality failures are "
        "never execution errors)",
    )

    workload = manifest["workload"]
    sample = result["sample_design"]
    _check(
        sample["tasks_per_repetition"] == workload["tasks_per_repetition"],
        "sample_design.tasks_per_repetition does not match the manifest workload",
    )
    _check(sample["total_attempts"] == attempted, "sample_design.total_attempts != attempted")
    _check(
        sample["unique_instance_count"] <= sample["total_attempts"],
        "unique_instance_count cannot exceed total_attempts",
    )
    _check(
        sample["unique_template_count"] <= sample["unique_instance_count"],
        "unique_template_count cannot exceed unique_instance_count",
    )

    concurrency = result["concurrency"]
    _check(
        concurrency["requested"] == workload["concurrency"],
        "concurrency.requested does not match the manifest workload concurrency",
    )
    _check(
        concurrency["achieved_max"] <= concurrency["requested"],
        "achieved_max concurrency cannot exceed requested (bounded scheduler)",
    )
    _check(
        concurrency["mean_in_flight"] <= concurrency["requested"] + _RATE_TOLERANCE,
        "mean_in_flight cannot exceed requested concurrency",
    )

    slo = result["slo"]
    if slo["slo_attaining_tasks"] is not None:
        _check(
            slo["slo_attaining_tasks"] <= succeeded,
            "slo_attaining_tasks cannot exceed succeeded tasks",
        )

    observations = result["observations"]
    _check(
        observations["measured_count"] == attempted,
        "observations.measured_count must equal tasks.attempted",
    )
    if observations["persisted"]:
        _check(
            bool(_SAFE_RELATIVE_NAME.match(observations["measured_file"])),
            "measured_file must be a safe relative filename",
        )
        _check(
            bool(_SHA256_HEX.match(observations["measured_sha256"])),
            "measured_sha256 must be a 64-char lowercase hex digest",
        )
        if observations["warmup_count"] > 0:
            _check(
                bool(_SAFE_RELATIVE_NAME.match(observations.get("warmup_file", ""))),
                "warmup_file must be a safe relative filename",
            )
            _check(
                bool(_SHA256_HEX.match(observations.get("warmup_sha256", ""))),
                "warmup_sha256 must be a 64-char lowercase hex digest",
            )

    for label, document, expected_phase, expected_count in (
        ("measured", measured_observations, "measured", observations["measured_count"]),
        ("warmup", warmup_observations, "warmup", observations["warmup_count"]),
    ):
        if document is None:
            continue
        _check(
            document.get("run_id") == run_id,
            f"{label} observation file run_id does not match the result",
        )
        _check(
            document.get("phase") == expected_phase,
            f"{label} observation file has the wrong phase label",
        )
        _check(
            len(document.get("observations", [])) == expected_count,
            f"{label} observation count does not match the result accounting",
        )

    validate_task_source_semantics(workload)
    if task_source_kind(workload) == TASK_SOURCE_SEALED:
        _validate_sealed_semantics(workload, sample, observations, measured_observations)


def validate_task_source_semantics(workload: dict) -> None:
    """Catalog and sealed provenance are mutually exclusive (decision D-0024).

    A catalog run (``task_source`` absent or ``kind: catalog``) carries a
    well-formed ``catalog_digest`` and no sealed provenance anywhere. A
    sealed run carries ``task_source.digest`` equal to its
    ``sealed_set.stage_aggregate_digest`` and **no** ``catalog_digest``;
    the stage aggregate is never stored in ``catalog_digest``.
    """
    _check("sealed_set" not in workload, "workload.sealed_set is not a valid location")
    kind = task_source_kind(workload)
    source = workload.get("task_source")
    if kind == TASK_SOURCE_CATALOG:
        _check(
            source is None or set(source) == {"kind"},
            "catalog task_source carries no sealed provenance",
        )
        digest = workload.get("catalog_digest")
        _check(
            isinstance(digest, str) and _SHA256_PREFIXED.match(digest) is not None,
            "catalog run must record a well-formed catalog_digest",
        )
        return
    _check(
        isinstance(source, dict) and set(source) == {"kind", "digest", "sealed_set"},
        "sealed task_source must carry exactly kind, digest and sealed_set",
    )
    sealed = source["sealed_set"]
    _check(
        isinstance(sealed, dict) and set(sealed) == _SEALED_SET_FIELDS,
        "sealed task_source.sealed_set must carry exactly the binding fields",
    )
    _check(
        source["digest"] == sealed["stage_aggregate_digest"],
        "sealed task_source.digest must equal sealed_set.stage_aggregate_digest",
    )
    _check(
        "catalog_digest" not in workload,
        "sealed run must not record a catalog_digest",
    )


def _validate_sealed_semantics(
    workload: dict,
    sample: dict,
    observations: dict,
    measured_observations: dict | None,
) -> None:
    """Invariants of a sealed-stage run (decision D-0024): the executed task
    set is the bound custody stage, exactly once, with no warm-up."""
    sealed = workload["task_source"]["sealed_set"]
    task_count = sealed.get("task_count")
    _check(
        workload.get("tasks_per_repetition") == task_count,
        "sealed run tasks_per_repetition must equal the bound task count",
    )
    _check(
        sample["total_attempts"] == task_count,
        "sealed run must attempt exactly the bound task count",
    )
    _check(
        sample["unique_instance_count"] == task_count,
        "sealed run instances must be distinct",
    )
    scenario_count = workload.get("scenario_count")
    if scenario_count is not None:
        _check(
            scenario_count == sample["unique_template_count"],
            "sealed run scenario_count must equal the distinct scenarios observed",
        )
    _check(observations["warmup_count"] == 0, "sealed runs record no warm-up observations")
    if measured_observations is not None:
        instance_ids = [
            item.get("instance_id") for item in measured_observations.get("observations", [])
        ]
        _check(
            len(instance_ids) == task_count and len(set(instance_ids)) == task_count,
            "sealed run observations must cover each sealed task exactly once",
        )
        _check(
            all(isinstance(value, str) and value for value in instance_ids),
            "sealed run observations must carry non-empty instance ids",
        )
