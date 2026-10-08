"""Ten-task diagnostic canary for a qualification candidate (decision D-0031).

The canary is a cheap, predeclared smoke test of agent reliability that runs
*before* an operator spends a full twenty-task development qualification. It
is diagnostic only:

* exactly ten tasks, drawn only from the six frozen development templates,
  generated from the fixed measured canary seed in a deterministic order;
  each development template appears at least once, and the four extra
  instances are the second occurrences of the first four templates in
  frozen-split order (round-robin, independent of any result);
* the four frozen holdout templates are excluded and stay unseen until
  the official qualification;
* every canary instance is disjoint from every instance of the official
  development, holdout, and freeze schedules (measured and warmup passes);
* the same binary evaluator, the same tool contract, and the same 0.40
  quality floor as development (0.40 means 40 percent, four of ten);
* a result below four of ten stops; four or more means the operator *may*
  separately approve the official twenty-task development qualification,
  which still requires at least eight of twenty;
* a canary never creates or authenticates a same-session development
  control, never authorizes a P2C or W2 development run, never authorizes
  comparative or cross-cloud benchmarking, and never launches the official
  qualification itself;
* a canary carries its own digest-bearing approval phrase and its own
  artifact family so no canary artifact can be mistaken for a qualification
  cell or a control record.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from blackwell_lab.cloud import qualification
from blackwell_lab.cloud.qualification import QualificationError
from blackwell_lab.workload.sampling import generate_task_instances
from blackwell_lab.workload.validation import ConfigError

CANARY_WORKFLOW = "canary-agent"
CANARY_STAGE = "canary"
CANARY_ARTIFACT_FAMILY = "canary-runs"
CANARY_TASKS = 10
#: How the four tasks beyond one-per-development-template are chosen.
#: ``generate_task_instances`` assigns task ``i`` to template ``i mod 6``
#: in frozen-split order. The choice reads no results.
CANARY_ALLOCATION = "round-robin-frozen-split"
CANARY_WARMUP_PASSES = 0
CANARY_REPETITIONS = 1
#: D-0031 measured schedule seed. ``run_real_cell`` executes measured
#: repetition ``i`` (1-based) at ``spec.seed + i``, so the RealRunSpec base
#: seed is internal and one less than this value. Public schedules, the
#: pre-client disjointness check, the instances passed to the runner, the
#: result sample design, and the receipt all use the measured seed.
CANARY_MEASURED_SEED = 20261007
CANARY_SEED = CANARY_MEASURED_SEED
CANARY_BASE_SEED = CANARY_MEASURED_SEED - 1
CANARY_QUALITY_FLOOR = qualification.DEVELOPMENT_QUALITY_FLOOR
CANARY_MIN_PASSES = 4
CANARY_APPROVAL_TEMPLATE = (
    "I approve the Akamai diagnostic agent canary for run {run_tag} "
    "({run_label}) using candidate {candidate_id} config sha256:{config_sha256}; "
    "the canary is diagnostic only and authorizes no qualification"
)
CANARY_RECEIPT_KIND = "diagnostic-canary"
#: Candidates a canary may exercise: every public-catalog candidate. P2
#: (sealed input) is excluded; a canary never opens custody.
CANARY_CANDIDATES = tuple(
    candidate
    for candidate in qualification.ALL_AUTHORIZED_CANDIDATES
    if qualification.is_catalog_candidate(candidate)
)
_FORBIDDEN_CANARY_CONFIG_KEYS = (
    "development_control",
    "sealed_set",
    "custody_dir",
    "template_ids",
    "scenario_ids",
    "task_ids",
    "private_scenarios",
    "stage",
)
NEXT_STEP_STOP = (
    "STOP: fewer than four of ten canary tasks passed every gate. Do not approve "
    "the official qualification; correct the agent and re-run the canary."
)
NEXT_STEP_MAY_APPROVE = (
    "The canary passed its diagnostic floor. The operator MAY separately approve "
    "the official twenty-task development qualification with its own approval "
    "phrase; that run still requires at least eight of twenty (0.40 = 40%). "
    "The canary itself authorizes nothing."
)


def canary_template_ids() -> tuple[str, ...]:
    """The six frozen development templates, in frozen-split order.

    Holdout templates are excluded. The canary never draws from them.
    """
    return qualification.DEVELOPMENT_TEMPLATE_IDS


def canary_template_sequence() -> tuple[str, ...]:
    """Predeclared template order for the ten canary tasks.

    Round-robin over the frozen development templates: task ``i`` uses
    template ``i mod 6``. Each of the six appears once, then the first
    four in frozen-split order appear a second time. The sequence is a
    function of the frozen split and the task count only.
    """
    templates = canary_template_ids()
    if len(templates) != 6:
        raise QualificationError("the canary draws from exactly six development templates")
    return tuple(templates[index % len(templates)] for index in range(CANARY_TASKS))


def canary_schedule() -> list:
    """The ten predeclared instances from the development templates only."""
    templates = canary_template_ids()
    holdout = set(qualification.HOLDOUT_TEMPLATE_IDS)
    if set(templates) & holdout:
        raise QualificationError("the canary must not include a holdout template")
    if len(templates) != 6 or len(holdout) != 4:
        raise QualificationError("the canary draws from exactly six development templates")
    if tuple(templates) != qualification.DEVELOPMENT_TEMPLATE_IDS:
        raise QualificationError("the canary must use the frozen development templates")
    schedule = generate_task_instances(templates, CANARY_TASKS, CANARY_MEASURED_SEED)
    expected = canary_template_sequence()
    if [item.template_id for item in schedule] != list(expected):
        raise QualificationError("the canary generator and the documented allocation diverged")
    return schedule


def canary_spec() -> dict[str, Any]:
    return {
        "stage": CANARY_STAGE,
        "template_ids": canary_template_ids(),
        "tasks": CANARY_TASKS,
        "warmup_passes": CANARY_WARMUP_PASSES,
        "repetitions": CANARY_REPETITIONS,
        "profile": qualification.STAGE_PROFILE,
        "concurrency": qualification.STAGE_CONCURRENCY,
        "quality_floor": CANARY_QUALITY_FLOOR,
        "min_passes": CANARY_MIN_PASSES,
        "seed": CANARY_MEASURED_SEED,
        "apply_study_entry_gate": False,
        "diagnostic_only": True,
    }


def _official_instance_keys() -> set[tuple[str, int, str]]:
    keys: set[tuple[str, int, str]] = set()
    for stage in qualification.AUTHORIZED_STAGES:
        spec = qualification.stage_spec(stage)
        seeds = [qualification.MEASURED_REPETITION_SEED]
        seeds.extend(
            qualification.FROZEN_SEED - index - 1 for index in range(int(spec["warmup_passes"]))
        )
        for seed in seeds:
            for instance in generate_task_instances(spec["template_ids"], spec["tasks"], seed):
                keys.add((instance.template_id, instance.instance_seed, instance.tracking_id))
    return keys


def require_canary_balance(schedule: list | None = None) -> None:
    schedule = schedule if schedule is not None else canary_schedule()
    if len(schedule) != CANARY_TASKS:
        raise QualificationError(f"the canary must schedule exactly {CANARY_TASKS} tasks")
    observed_ids = [instance.template_id for instance in schedule]
    holdout = set(qualification.HOLDOUT_TEMPLATE_IDS)
    if any(template_id in holdout for template_id in observed_ids):
        raise QualificationError("the canary must not include a holdout template")
    if set(observed_ids) != set(canary_template_ids()):
        raise QualificationError("the canary must represent every development template")
    expected = list(canary_template_sequence())
    if Counter(observed_ids) != Counter(expected):
        raise QualificationError(
            "the canary must allocate the four extra tasks by frozen-split round-robin"
        )
    if observed_ids != expected:
        raise QualificationError(
            "the canary order must be the deterministic development-template round-robin"
        )


def require_canary_disjoint(schedule: list | None = None) -> None:
    """No canary instance may coincide with an official schedule instance."""
    schedule = schedule if schedule is not None else canary_schedule()
    official = _official_instance_keys()
    for instance in schedule:
        key = (instance.template_id, instance.instance_seed, instance.tracking_id)
        if key in official:
            raise QualificationError("the canary schedule overlaps an official qualification task")
    official_seeds = {qualification.MEASURED_REPETITION_SEED, qualification.FROZEN_SEED}
    official_seeds.update(
        qualification.FROZEN_SEED - index - 1 for index in range(qualification.FREEZE_WARMUP_PASSES)
    )
    if CANARY_SEED in official_seeds:
        raise QualificationError("the canary seed must differ from every official schedule seed")


def require_canary_contract() -> dict[str, Any]:
    """Fail closed unless the canary still satisfies its frozen contract."""
    if CANARY_QUALITY_FLOOR != 0.40 or CANARY_MIN_PASSES != 4 or CANARY_TASKS != 10:
        raise QualificationError("the canary contract drifted")
    if qualification.DEVELOPMENT_TASKS != 20 or qualification.DEVELOPMENT_QUALITY_FLOOR != 0.40:
        raise QualificationError("the official development gate drifted")
    if CANARY_ARTIFACT_FAMILY == qualification.QUALIFICATION_ARTIFACT_FAMILY:
        raise QualificationError("canary artifacts must not share the qualification family")
    if CANARY_ARTIFACT_FAMILY == "real-runs":
        raise QualificationError("canary artifacts must not share the real-run family")
    if CANARY_BASE_SEED + 1 != CANARY_MEASURED_SEED or CANARY_SEED != CANARY_MEASURED_SEED:
        raise QualificationError("the canary base seed and measured seed diverged")
    schedule = canary_schedule()
    require_canary_balance(schedule)
    require_canary_disjoint(schedule)
    return canary_spec()


def measured_repetition_seed(base_seed: int) -> int:
    """Seed ``run_real_cell`` uses for the single measured repetition.

    The runner computes ``spec.seed + repetition_index`` with
    ``repetition_index`` starting at 1. This helper names that measured
    seed; it does not change the runner's formula.
    """
    return base_seed + 1


def _instance_key(instance: object) -> tuple[str, int, str]:
    return (instance.template_id, instance.instance_seed, instance.tracking_id)


def require_measured_schedule(spec: object) -> list:
    """Fail closed unless the spec will execute the declared canary schedule.

    Called before a model client exists. The instances compared here are the
    ones ``run_real_cell`` derives for the measured repetition, not a
    separately declared list that the runner might ignore.
    """
    family = getattr(spec, "artifact_family", None)
    if family != CANARY_ARTIFACT_FAMILY:
        raise QualificationError("canary artifacts must use the canary-runs family")
    if (
        getattr(spec, "warmup_passes", None) != CANARY_WARMUP_PASSES
        or getattr(spec, "repetitions", None) != CANARY_REPETITIONS
        or getattr(spec, "tasks_per_repetition", None) != CANARY_TASKS
    ):
        raise QualificationError("the canary executes exactly ten measured tasks and no warmup")
    if tuple(getattr(spec, "template_ids", None) or ()) != canary_template_ids():
        raise QualificationError("the canary must schedule only the six development templates")
    measured = measured_repetition_seed(int(spec.seed))
    if measured != CANARY_MEASURED_SEED:
        raise QualificationError("the canary measured seed must be 20261007")
    executed = generate_task_instances(tuple(spec.template_ids), CANARY_TASKS, measured)
    declared = canary_schedule()
    if [_instance_key(item) for item in executed] != [_instance_key(item) for item in declared]:
        raise QualificationError(
            "the executed canary schedule does not match the declared schedule"
        )
    require_canary_balance(executed)
    require_canary_disjoint(executed)
    return executed


def require_canary_candidate(candidate_id: str) -> None:
    if candidate_id not in CANARY_CANDIDATES:
        raise ConfigError(
            "canary candidate must be one of the public-catalog candidates: "
            + ", ".join(CANARY_CANDIDATES)
        )


def approval_phrase(run_tag: str, run_label: str, candidate_id: str, config_sha256: str) -> str:
    return CANARY_APPROVAL_TEMPLATE.format(
        run_tag=run_tag,
        run_label=run_label,
        candidate_id=candidate_id,
        config_sha256=config_sha256,
    )


def require_approval(
    approve: str | None, *, run_tag: str, run_label: str, candidate_id: str, config_sha256: str
) -> None:
    expected = approval_phrase(run_tag, run_label, candidate_id, config_sha256)
    if approve != expected:
        raise QualificationError(
            "the canary approval phrase is missing or does not match the exact run, "
            "candidate, and config digest; nothing was executed"
        )
    # A qualification approval phrase never authorizes a canary and vice versa.
    if approve == qualification.approval_phrase(run_tag, run_label, candidate_id, config_sha256):
        raise QualificationError("a qualification approval phrase does not authorize a canary")


def output_label(run_label: str, candidate_id: str) -> str:
    label = f"{run_label}-{candidate_id.lower()}-{CANARY_STAGE}"
    if not qualification._RUN_LABEL_RE.match(label):
        raise ConfigError("composed canary output label is unsafe")
    refuse_identities(run_tag="", run_label=label, config={})
    return label


def refuse_identities(*, run_tag: str, run_label: str, config: dict) -> None:
    """MVL-F identities are refused; canary artifacts stay in their own family."""
    blob = qualification._identity_blob(run_tag, run_label, config, CANARY_ARTIFACT_FAMILY)
    for marker in (
        *qualification.FORBIDDEN_IDENTITY_MARKERS,
        *qualification.FORBIDDEN_PATH_MARKERS,
    ):
        if marker in blob:
            raise QualificationError("MVL-F identities and output paths are refused for the canary")
    if config.get("workflow") in {
        "mvl-baseline",
        "mvl",
        "pilot",
        qualification.QUALIFICATION_WORKFLOW,
    }:
        raise QualificationError("the canary config workflow must be canary-agent")


def validate_canary_config(config: dict, *, candidate_id: str) -> None:
    """A canary config is a qualification config pinned to the canary schedule.

    It must carry no control, sealed, custody, or schedule-selection keys.
    """
    if not isinstance(config, dict):
        raise ConfigError("canary config must be a JSON object")
    require_canary_candidate(candidate_id)
    present = sorted(key for key in _FORBIDDEN_CANARY_CONFIG_KEYS if key in config)
    if present:
        raise ConfigError("canary config refuses keys: " + ", ".join(present))
    if config.get("workflow") not in (None, CANARY_WORKFLOW):
        raise ConfigError("canary config workflow must equal canary-agent")
    if config.get("tasks_per_repetition") not in (None, CANARY_TASKS):
        raise ConfigError(f"canary tasks_per_repetition must equal {CANARY_TASKS}")
    if config.get("warmup_passes") not in (None, CANARY_WARMUP_PASSES):
        raise ConfigError("canary warmup_passes must equal 0")
    if config.get("repetitions") not in (None, CANARY_REPETITIONS):
        raise ConfigError("canary repetitions must equal 1")
    generation = config.get("generation") or {}
    if generation.get("seed") not in (None, CANARY_SEED, qualification.FROZEN_SEED):
        raise ConfigError("canary generation.seed must be the canary seed or omitted")
    shadow = {
        key: value
        for key, value in config.items()
        if key not in {"workflow", "tasks_per_repetition", "warmup_passes", "generation"}
    }
    if generation:
        shadow["generation"] = {
            **generation,
            **({"seed": qualification.FROZEN_SEED} if "seed" in generation else {}),
        }
    # The shadow is validated as a holdout cell: identical pins, no
    # same-session control binding (a canary refuses one), and the canary's
    # own schedule keys were removed above.
    qualification.validate_authorized_qualification_config(
        shadow, candidate_id=candidate_id, stage=qualification.STAGE_HOLDOUT
    )
    refuse_identities(run_tag="", run_label="", config=config)


def load_canary_config(path, *, candidate_id: str) -> tuple[dict, str]:
    import hashlib

    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("canary config is not valid JSON") from exc
    if not isinstance(config, dict):
        raise ConfigError("canary config must be a JSON object")
    validate_canary_config(config, candidate_id=candidate_id)
    return config, digest


def evaluate_canary(metrics: qualification.QualificationMetrics) -> dict[str, Any]:
    """Binary verdict: the development floor applied to ten tasks."""
    if metrics.attempted != CANARY_TASKS:
        raise QualificationError(f"the canary must produce exactly {CANARY_TASKS} measured tasks")
    passes = int(metrics.succeeded)
    passed = metrics.aggregate_quality >= CANARY_QUALITY_FLOOR and passes >= CANARY_MIN_PASSES
    return {
        "stage": CANARY_STAGE,
        "diagnostic_only": True,
        "tasks": CANARY_TASKS,
        "passed_tasks": passes,
        "quality_floor": CANARY_QUALITY_FLOOR,
        "quality_floor_percent": 40,
        "min_passes": CANARY_MIN_PASSES,
        "aggregate_quality": metrics.aggregate_quality,
        "continue": passed,
        "stopped": not passed,
        "authorizes_qualification": False,
        "creates_development_control": False,
        "official_gate": {
            "tasks": qualification.DEVELOPMENT_TASKS,
            "quality_floor": qualification.DEVELOPMENT_QUALITY_FLOOR,
            "min_passes": 8,
        },
        "next_step": NEXT_STEP_MAY_APPROVE if passed else NEXT_STEP_STOP,
    }


def require_complete_canary_evidence(outcomes) -> None:
    if len(outcomes) != CANARY_TASKS:
        raise QualificationError(f"the canary must produce exactly {CANARY_TASKS} measured tasks")
    observed_ids = [str(qualification._field(outcome, "template_id") or "") for outcome in outcomes]
    if any(template_id in set(qualification.HOLDOUT_TEMPLATE_IDS) for template_id in observed_ids):
        raise QualificationError("the canary must not include a holdout template")
    if Counter(observed_ids) != Counter(canary_template_sequence()):
        raise QualificationError(
            "the canary must observe the predeclared development-template allocation"
        )


def sanitized_receipt(
    *,
    run_label: str,
    candidate_id: str,
    config_sha256: str,
    identity_digest: str,
    verdict: dict[str, Any],
    files,
    qualification_environment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": CANARY_RECEIPT_KIND,
        "workflow": CANARY_WORKFLOW,
        "run_label": run_label,
        "candidate_id": candidate_id,
        "stage": CANARY_STAGE,
        "config_sha256": config_sha256,
        "candidate_identity_sha256": identity_digest,
        "workload_version": qualification.candidate_workload_version(candidate_id),
        "controller": qualification.candidate_controller(candidate_id),
        "treatments": list(qualification.candidate_treatments(candidate_id)),
        "schedule": {
            "tasks": CANARY_TASKS,
            "seed": CANARY_MEASURED_SEED,
            "templates": len(canary_template_ids()),
            "template_source": "development",
            "holdout_excluded": True,
            "allocation": CANARY_ALLOCATION,
            "disjoint_from_official_schedules": True,
        },
        "verdict": verdict,
        "stopped": bool(verdict["stopped"]),
        "diagnostic_only": True,
        "authorizes_qualification": False,
        "authorizes_comparative_benchmarking": False,
        "authorizes_cross_cloud": False,
        "creates_development_control": False,
        "files": [str(name) for name in files],
        **(
            {"qualification_environment": qualification_environment}
            if qualification_environment is not None
            else {}
        ),
    }
