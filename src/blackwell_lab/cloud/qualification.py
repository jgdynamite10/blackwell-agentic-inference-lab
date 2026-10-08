"""Bounded agent-quality qualification (decision D-0019).

Reuses the existing pilot/MVL runner, provenance, external-results, and
verification paths. This is not a new orchestration framework and it
never reuses or overwrites MVL-F identities or paths.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blackwell_lab.cloud.mvl import (
    FROZEN_COMPARISON_MODE,
    FROZEN_CONTAINER_DIGEST,
    FROZEN_ENGINE,
    FROZEN_ENGINE_VERSION,
    FROZEN_GPU,
    FROZEN_HOURLY_PRICE_USD,
    FROZEN_INSTANCE_TYPE,
    FROZEN_MODEL_ARTIFACT,
    FROZEN_MODEL_ARTIFACT_HASH,
    FROZEN_MODEL_REVISION,
    FROZEN_PRECISION,
    FROZEN_PROVIDER,
    FROZEN_REASONING_MODE,
    FROZEN_REGION,
    FROZEN_SEED,
    FROZEN_TOP_P,
    FROZEN_VLLM_IMAGE_DIGEST,
)
from blackwell_lab.cloud.sealed_binding import (
    SEALED_CANDIDATES,
    SEALED_STAGES,
    SealedSetBinding,
    binding_from_config,
    requires_sealed_set,
)
from blackwell_lab.workload.evidence import (
    CONTROLLER_EVIDENCE_GROUNDING_V1,
    CONTROLLER_WORKFLOW_V1,
    require_controller_binding,
    workload_treatments,
)
from blackwell_lab.workload.native_tools import (
    REASONING_PARSER,
    TOOL_CALL_PARSER,
    TOOL_CALL_TRANSPORT,
    tool_descriptions,
)
from blackwell_lab.workload.sampling import generate_task_instances
from blackwell_lab.workload.scenarios import WORKLOAD_VERSION, catalog
from blackwell_lab.workload.stats import percentile_nearest_rank
from blackwell_lab.workload.validation import ConfigError

QUALIFICATION_APPROVAL_TEMPLATE = (
    "I approve the Akamai agent qualification for run {run_tag} "
    "({run_label}) using candidate {candidate_id} config sha256:{config_sha256}"
)

QUALIFICATION_WORKLOAD_VERSION = "2.4.0"
CATALOG_WORKLOAD_VERSION = "2.3.0"
QUALIFICATION_WORKFLOW = "qualify-agent"
QUALIFICATION_ARTIFACT_FAMILY = "qualification-runs"
QUALIFICATION_LABEL = "agent-quality-qualification"

SPLIT_SEED = "blackwell-lab-agent-qualification-v1"
SPLIT_RULE = "sha256-hex-sort"

CANDIDATE_C1 = "C1"
CANDIDATE_C2 = "C2"
CANDIDATE_P1 = "P1"
CANDIDATE_P2 = "P2"
CANDIDATE_P2C = "P2C"
#: Historical candidate table (D-0019 through D-0026). Closed and
#: byte-identical to before D-0031; the workflow-controlled pair lives in
#: :data:`WORKFLOW_CANDIDATES` so historical identities never move.
AUTHORIZED_CANDIDATES = (
    CANDIDATE_C1,
    CANDIDATE_C2,
    CANDIDATE_P1,
    CANDIDATE_P2,
    CANDIDATE_P2C,
)
#: Workflow-controlled candidate pair (decision D-0031): W1 is the control,
#: W2 the ``evidence-refs`` citation treatment. Both bind
#: ``workflow-controller-v1``.
CANDIDATE_W1 = "W1"
CANDIDATE_W2 = "W2"
WORKFLOW_CANDIDATES = (CANDIDATE_W1, CANDIDATE_W2)
#: Every candidate any qualification command may name.
ALL_AUTHORIZED_CANDIDATES = (*AUTHORIZED_CANDIDATES, *WORKFLOW_CANDIDATES)
C1_TEMPERATURE = 1.0
C2_TEMPERATURE = 0.2
P1_TEMPERATURE = C2_TEMPERATURE
P2_TEMPERATURE = C2_TEMPERATURE
P2C_TEMPERATURE = C2_TEMPERATURE
W_TEMPERATURE = C2_TEMPERATURE
FROZEN_MAX_TOKENS = 1024

STAGE_DEVELOPMENT = "development"
STAGE_HOLDOUT = "holdout"
STAGE_FREEZE = "freeze"
AUTHORIZED_STAGES = (STAGE_DEVELOPMENT, STAGE_HOLDOUT, STAGE_FREEZE)

DEVELOPMENT_TASKS = 20
HOLDOUT_TASKS = 20
FREEZE_TASKS = 200
SCREEN_WARMUP_PASSES = 0
FREEZE_WARMUP_PASSES = 1
STAGE_REPETITIONS = 1
STAGE_PROFILE = "interactive"
STAGE_CONCURRENCY = 1
MEASURED_REPETITION_SEED = FROZEN_SEED + 1

DEVELOPMENT_QUALITY_FLOOR = 0.40
HOLDOUT_QUALITY_FLOOR = 0.50

STUDY_ENTRY_MIN_AGGREGATE = 0.70
STUDY_ENTRY_MIN_SCENARIO = 0.40
PRODUCTION_LIKE_MIN_AGGREGATE = 0.90
PRODUCTION_LIKE_MIN_SCENARIO = 0.80
MIN_VALID_NATIVE_TOOL_CALL_RATE = 0.99
MAX_INVALID_TOOL_NAME_RATE = 0.0
MAX_INVALID_ARGUMENT_RATE = 0.01
MAX_REQUEST_INFERENCE_ERROR_RATE = 0.01
MAX_TIMEOUTS = 0
MAX_INTERACTIVE_TTFT_P95_MS = 2_500.0
MAX_INTERACTIVE_E2E_P95_MS = 60_000.0

STRUCTURAL_TOOL_FAILURES = frozenset(
    {"malformed_tool_call", "invalid_tool_name", "invalid_tool_arguments"}
)
REQUEST_INFERENCE_ERRORS = frozenset({"endpoint_error"})
TIMEOUT_CATEGORIES = frozenset({"task_timeout"})

FORBIDDEN_IDENTITY_MARKERS = ("mvl-f", "mvlf", "mvl-baseline", "p3-mvl", "mvl")
FORBIDDEN_PATH_MARKERS = ("mvl-f", "mvlf", "mvl-baseline", "real-runs")

_RUN_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

HOLDOUT_MUST_NOT_REVISE_WORDING = "Holdout results must never be used to revise candidate wording."


class QualificationError(RuntimeError):
    """Fail-closed qualification error (sanitized; no private paths)."""


def _catalog_template_ids() -> tuple[str, ...]:
    return tuple(catalog())


def freeze_template_split(
    template_ids: Sequence[str] | None = None,
    *,
    seed: str = SPLIT_SEED,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Deterministic 6/4 split: sha256(seed:id) hex-sort, then template id."""
    ids = tuple(template_ids) if template_ids is not None else _catalog_template_ids()
    scored = sorted(
        (hashlib.sha256(f"{seed}:{template_id}".encode()).hexdigest(), template_id)
        for template_id in ids
    )
    development = tuple(template_id for _digest, template_id in scored[:6])
    holdout = tuple(template_id for _digest, template_id in scored[6:])
    return development, holdout


DEVELOPMENT_TEMPLATE_IDS, HOLDOUT_TEMPLATE_IDS = freeze_template_split()


def require_frozen_split() -> None:
    development, holdout = freeze_template_split()
    if development != DEVELOPMENT_TEMPLATE_IDS or holdout != HOLDOUT_TEMPLATE_IDS:
        raise QualificationError("the frozen 6/4 template split drifted")
    if set(development) & set(holdout):
        raise QualificationError("development and holdout templates must be disjoint")
    if set(development).union(holdout) != set(_catalog_template_ids()):
        raise QualificationError("the frozen split must cover every catalog template")
    if len(development) != 6 or len(holdout) != 4:
        raise QualificationError(
            "the frozen split must be exactly six development and four holdout"
        )


def stage_spec(stage: str) -> dict[str, Any]:
    if stage == STAGE_DEVELOPMENT:
        return {
            "stage": stage,
            "template_ids": DEVELOPMENT_TEMPLATE_IDS,
            "tasks": DEVELOPMENT_TASKS,
            "warmup_passes": SCREEN_WARMUP_PASSES,
            "repetitions": STAGE_REPETITIONS,
            "profile": STAGE_PROFILE,
            "concurrency": STAGE_CONCURRENCY,
            "quality_floor": DEVELOPMENT_QUALITY_FLOOR,
            "apply_study_entry_gate": False,
        }
    if stage == STAGE_HOLDOUT:
        return {
            "stage": stage,
            "template_ids": HOLDOUT_TEMPLATE_IDS,
            "tasks": HOLDOUT_TASKS,
            "warmup_passes": SCREEN_WARMUP_PASSES,
            "repetitions": STAGE_REPETITIONS,
            "profile": STAGE_PROFILE,
            "concurrency": STAGE_CONCURRENCY,
            "quality_floor": HOLDOUT_QUALITY_FLOOR,
            "apply_study_entry_gate": False,
        }
    if stage == STAGE_FREEZE:
        return {
            "stage": stage,
            "template_ids": tuple(_catalog_template_ids()),
            "tasks": FREEZE_TASKS,
            "warmup_passes": FREEZE_WARMUP_PASSES,
            "repetitions": STAGE_REPETITIONS,
            "profile": STAGE_PROFILE,
            "concurrency": STAGE_CONCURRENCY,
            "quality_floor": None,
            "apply_study_entry_gate": True,
        }
    raise ConfigError("qualification stage must be development, holdout, or freeze")


def validate_stage_request(stage: str, *, template_ids: Sequence[str], tasks: int) -> None:
    spec = stage_spec(stage)
    if tuple(template_ids) != spec["template_ids"]:
        raise ConfigError(f"qualification {stage} templates must equal the frozen split")
    if tasks != spec["tasks"]:
        raise ConfigError(f"qualification {stage} must use exactly {spec['tasks']} tasks")


def expected_stage_distribution(stage: str) -> Counter[str]:
    """Deterministic per-template counts from the frozen seed, templates, and tasks."""
    spec = stage_spec(stage)
    instances = generate_task_instances(
        spec["template_ids"],
        spec["tasks"],
        MEASURED_REPETITION_SEED,
    )
    return Counter(instance.template_id for instance in instances)


def require_complete_stage_evidence(stage: str, outcomes: Sequence[object]) -> None:
    """Fail closed unless measured observations match the frozen stage schedule."""
    spec = stage_spec(stage)
    expected_ids = tuple(spec["template_ids"])
    expected_count = int(spec["tasks"])
    if len(outcomes) != expected_count:
        raise QualificationError(
            f"qualification {stage} must produce exactly {expected_count} measured tasks"
        )
    observed_ids = [str(_field(outcome, "template_id") or "") for outcome in outcomes]
    if any(not template_id for template_id in observed_ids):
        raise QualificationError("qualification observations contain a blank template ID")
    observed_set = set(observed_ids)
    expected_set = set(expected_ids)
    if observed_set - expected_set:
        raise QualificationError("qualification observations include unexpected template IDs")
    missing = [template_id for template_id in expected_ids if template_id not in observed_set]
    if missing:
        raise QualificationError("qualification observations are missing expected templates")
    if observed_set != expected_set:
        raise QualificationError("qualification observations must equal the stage template set")
    if Counter(observed_ids) != expected_stage_distribution(stage):
        raise QualificationError(
            "qualification observations do not match the frozen per-template distribution"
        )


#: C1 and C2 stay on the 2.4.0 tool-contract correction. P1 is the
#: workload 2.4.1 prompt-only candidate. P2 is the workload 2.5.0
#: evidence-grounding candidate (controller ``evidence-grounding-v1``).
#: Development remains the first required gate for every candidate; this
#: mapping does not skip it.
P1_WORKLOAD_VERSION = "2.4.1"
P2_WORKLOAD_VERSION = "2.5.0"
P2_CONTROLLER = CONTROLLER_EVIDENCE_GROUNDING_V1
#: P2C is the controlled public-catalog version of P2 (decision D-0026):
#: the same workload 2.5.0 and ``evidence-grounding-v1`` treatment, but
#: every stage executes the D-0019 catalog schedule that P1 executes. P1
#: is its control; the sealed P2 variants are a different instrument.
P2C_WORKLOAD_VERSION = P2_WORKLOAD_VERSION
P2C_CONTROLLER = P2_CONTROLLER
P2C_CONTROL_CANDIDATE = CANDIDATE_P1
CANDIDATE_WORKLOAD_VERSIONS = {
    CANDIDATE_C1: QUALIFICATION_WORKLOAD_VERSION,
    CANDIDATE_C2: QUALIFICATION_WORKLOAD_VERSION,
    CANDIDATE_P1: P1_WORKLOAD_VERSION,
    CANDIDATE_P2: P2_WORKLOAD_VERSION,
    CANDIDATE_P2C: P2C_WORKLOAD_VERSION,
}
#: Controller bound to each candidate. It must agree with the workload's
#: own binding (:data:`blackwell_lab.workload.evidence.WORKLOAD_CONTROLLERS`).
CANDIDATE_CONTROLLERS: dict[str, str | None] = {
    CANDIDATE_C1: None,
    CANDIDATE_C2: None,
    CANDIDATE_P1: None,
    CANDIDATE_P2: P2_CONTROLLER,
    CANDIDATE_P2C: P2C_CONTROLLER,
}
#: Workflow-controlled pair (D-0031), kept in separate closed tables.
W1_WORKLOAD_VERSION = "2.6.0"
W2_WORKLOAD_VERSION = "2.6.1"
W_CONTROLLER = CONTROLLER_WORKFLOW_V1
W2_TREATMENT = "evidence-refs"
W2_CONTROL_CANDIDATE = CANDIDATE_W1
WORKFLOW_CANDIDATE_WORKLOAD_VERSIONS: dict[str, str] = {
    CANDIDATE_W1: W1_WORKLOAD_VERSION,
    CANDIDATE_W2: W2_WORKLOAD_VERSION,
}
WORKFLOW_CANDIDATE_CONTROLLERS: dict[str, str] = {
    CANDIDATE_W1: W_CONTROLLER,
    CANDIDATE_W2: W_CONTROLLER,
}
#: Treatment candidate -> its same-session development control (D-0027 for
#: P1/P2C; D-0031 for W1/W2). A control candidate never appears as a key.
DEVELOPMENT_CONTROL_PAIRS: dict[str, str] = {
    CANDIDATE_P2C: CANDIDATE_P1,
    CANDIDATE_W2: CANDIDATE_W1,
}
UNKNOWN_CANDIDATE_MESSAGE = "qualification candidate must be C1, C2, P1, P2, P2C, W1, or W2"
_UNKNOWN_CANDIDATE = UNKNOWN_CANDIDATE_MESSAGE

#: Candidates whose every stage executes the public catalog schedule.
#: P2 is absent on purpose: its development and holdout are sealed. The
#: workflow pair is catalog-only too (see :func:`is_catalog_candidate`).
CATALOG_CANDIDATES = (CANDIDATE_C1, CANDIDATE_C2, CANDIDATE_P1, CANDIDATE_P2C)


def is_catalog_candidate(candidate_id: str) -> bool:
    """True for every candidate whose stages execute the public catalog."""
    return candidate_id in CATALOG_CANDIDATES or candidate_id in WORKFLOW_CANDIDATES


def control_candidate_for(candidate_id: str) -> str | None:
    """The same-session development control of a treatment candidate, if any."""
    return DEVELOPMENT_CONTROL_PAIRS.get(candidate_id)


def is_development_control(candidate_id: str) -> bool:
    """True when a completed development run of this candidate mints a control."""
    return candidate_id in DEVELOPMENT_CONTROL_PAIRS.values()


#: Config keys that would select, override, or privatize the P2C task
#: schedule. No candidate supports a private scenario or a frozen template
#: override; P2C refuses the keys explicitly so a config cannot even carry
#: them.
P2C_FORBIDDEN_CONFIG_KEYS = frozenset(
    {
        "sealed_set",
        "custody_dir",
        "template_ids",
        "frozen_template_id",
        "frozen_template_ids",
        "private_scenarios",
        "private_scenario",
        "scenarios",
        "scenario_ids",
        "task_source",
    }
)
P2C_SYSTEM_PROMPT_SHA256 = "37b3a4fb615dc21c8d39a5301dc4318870fea3498fbed196c50bfcbe67de1bd3"
P2C_EVALUATOR_VERSION = "3.1.0"
P2C_TASK_SOURCE = "catalog"
#: The frozen P2C contract. Every value is re-derived from the running code
#: by :func:`require_p2c_contract` before any model client exists.
P2C_CONTRACT: dict[str, Any] = {
    "candidate_id": CANDIDATE_P2C,
    "workload_version": P2C_WORKLOAD_VERSION,
    "controller": P2C_CONTROLLER,
    "system_prompt_sha256": P2C_SYSTEM_PROMPT_SHA256,
    "temperature": P2C_TEMPERATURE,
    "top_p": FROZEN_TOP_P,
    "seed": FROZEN_SEED,
    "max_tokens": FROZEN_MAX_TOKENS,
    "evaluator_version": P2C_EVALUATOR_VERSION,
    "task_source": P2C_TASK_SOURCE,
    "control_candidate": P2C_CONTROL_CANDIDATE,
}


def candidate_workload_version(candidate_id: str) -> str:
    """Workload contract bound to one authorized qualification candidate."""
    try:
        return {**CANDIDATE_WORKLOAD_VERSIONS, **WORKFLOW_CANDIDATE_WORKLOAD_VERSIONS}[candidate_id]
    except KeyError as exc:
        raise ConfigError(_UNKNOWN_CANDIDATE) from exc


def candidate_controller(candidate_id: str) -> str | None:
    """Agent controller bound to one candidate, cross-checked with the workload.

    Fails closed if the candidate table and the workload binding disagree,
    so no P2 run can start with a controller the workload does not bind.
    """
    try:
        controller = {**CANDIDATE_CONTROLLERS, **WORKFLOW_CANDIDATE_CONTROLLERS}[candidate_id]
    except KeyError as exc:
        raise ConfigError(_UNKNOWN_CANDIDATE) from exc
    return require_controller_binding(candidate_workload_version(candidate_id), controller)


def candidate_treatments(candidate_id: str) -> tuple[str, ...]:
    """Explicit experimental treatments of a candidate (empty for most)."""
    return workload_treatments(candidate_workload_version(candidate_id))


def candidate_temperature(candidate_id: str) -> float:
    if candidate_id == CANDIDATE_C1:
        return C1_TEMPERATURE
    if candidate_id == CANDIDATE_C2:
        return C2_TEMPERATURE
    if candidate_id == CANDIDATE_P1:
        return P1_TEMPERATURE
    if candidate_id == CANDIDATE_P2:
        return P2_TEMPERATURE
    if candidate_id == CANDIDATE_P2C:
        return P2C_TEMPERATURE
    if candidate_id in WORKFLOW_CANDIDATES:
        return W_TEMPERATURE
    raise ConfigError(_UNKNOWN_CANDIDATE)


def frozen_candidate_fields(candidate_id: str) -> dict[str, Any]:
    if candidate_id not in ALL_AUTHORIZED_CANDIDATES:
        raise ConfigError(_UNKNOWN_CANDIDATE)
    controller = candidate_controller(candidate_id)
    treatments = candidate_treatments(candidate_id)
    return {
        "candidate_id": candidate_id,
        "workflow": QUALIFICATION_WORKFLOW,
        "workload_version": candidate_workload_version(candidate_id),
        # The controller key exists only for controller-bound candidates so
        # the C1, C2, and P1 identity serializations stay byte-identical.
        **({"controller": controller} if controller else {}),
        # The treatments key exists only for W2, so every other identity
        # serialization (including W1's) is unchanged by its presence.
        **({"treatments": list(treatments)} if treatments else {}),
        "catalog_workload_version": CATALOG_WORKLOAD_VERSION,
        "provider": FROZEN_PROVIDER,
        "region": FROZEN_REGION,
        "instance_type": FROZEN_INSTANCE_TYPE,
        "gpu": FROZEN_GPU,
        "comparison_mode": FROZEN_COMPARISON_MODE,
        "model": {
            "artifact": FROZEN_MODEL_ARTIFACT,
            "revision": FROZEN_MODEL_REVISION,
            "artifact_hash": FROZEN_MODEL_ARTIFACT_HASH,
            "precision": FROZEN_PRECISION,
        },
        "serving": {
            "engine": FROZEN_ENGINE,
            "engine_version": FROZEN_ENGINE_VERSION,
            "container_digest": FROZEN_CONTAINER_DIGEST,
            "tool_call_transport": TOOL_CALL_TRANSPORT,
            "tool_call_parser": TOOL_CALL_PARSER,
            "reasoning_parser": REASONING_PARSER,
        },
        "generation": {
            "temperature": candidate_temperature(candidate_id),
            "top_p": FROZEN_TOP_P,
            "max_tokens": FROZEN_MAX_TOKENS,
            "seed": FROZEN_SEED,
            "reasoning_mode": FROZEN_REASONING_MODE,
        },
    }


def serialize_candidate(candidate_id: str) -> bytes:
    return json.dumps(
        frozen_candidate_fields(candidate_id),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def candidate_identity_digest(candidate_id: str) -> str:
    return hashlib.sha256(serialize_candidate(candidate_id)).hexdigest()


PROMPT_VARIANT_P1 = CANDIDATE_P1


def experimental_behavior_fields(identity: str) -> dict[str, Any]:
    """Candidate identity plus the version-bound system prompt.

    The prompt is comparison evidence. It is not part of
    :func:`serialize_candidate`, so C1 and C2 identity digests stay
    byte-stable. Workload version is provenance for the prompt binding.
    """
    fields = frozen_candidate_fields(identity)
    from blackwell_lab.workload.agent import system_prompt

    scenario = next(iter(catalog().values()))
    fields["system_prompt"] = system_prompt(scenario, str(fields["workload_version"]))
    return fields


def serialize_experimental_behavior(identity: str) -> bytes:
    return json.dumps(
        experimental_behavior_fields(identity),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def experimental_configuration_digest(identity: str) -> str:
    return hashlib.sha256(serialize_experimental_behavior(identity)).hexdigest()


# --------------------------------------------------------------------------
# P2C: controlled public-catalog qualification (decision D-0026)
# --------------------------------------------------------------------------


def stage_schedule(stage: str) -> dict[str, Any]:
    """The ordered D-0019 catalog schedule of one stage, as the runner derives it.

    Mirrors ``realbench.run_real_cell`` exactly: warm-up pass ``i`` uses
    seed ``FROZEN_SEED - i - 1`` and the single measured repetition uses
    ``FROZEN_SEED + 1``. The same function serves every catalog candidate,
    so P1 and P2C cannot be scheduled differently.
    """
    spec = stage_spec(stage)
    template_ids = tuple(spec["template_ids"])
    tasks = int(spec["tasks"])
    warmup = [
        list(generate_task_instances(template_ids, tasks, FROZEN_SEED - index - 1))
        for index in range(int(spec["warmup_passes"]))
    ]
    measured = [
        list(generate_task_instances(template_ids, tasks, FROZEN_SEED + repetition))
        for repetition in range(1, int(spec["repetitions"]) + 1)
    ]
    return {
        "stage": stage,
        "template_ids": template_ids,
        "tasks": tasks,
        "seed": FROZEN_SEED,
        "warmup_seeds": tuple(FROZEN_SEED - index - 1 for index in range(len(warmup))),
        "measured_seeds": tuple(
            FROZEN_SEED + repetition for repetition in range(1, len(measured) + 1)
        ),
        "warmup": warmup,
        "measured": measured,
    }


def p2c_experiment_record() -> dict[str, Any]:
    """Content-free provenance of the controlled experiment (receipts, reports)."""
    return {
        "kind": "controlled-public-catalog",
        "control_candidate": P2C_CONTROL_CANDIDATE,
        "treatment": P2C_CONTROLLER,
        "task_source": P2C_TASK_SOURCE,
        "schedule": "d-0019-catalog",
        "blind_generalization_evidence": False,
        "comparable_with_private_sealed_scores": False,
    }


def require_p2c_contract() -> dict[str, Any]:
    """Fail closed unless the running code still satisfies the frozen P2C contract.

    Re-derives every contract value from the live candidate table, prompt
    table, generation pins, evaluator, sealed-candidate table, and stage
    specs. Called from config validation, so it runs before any model
    client is constructed and before any turn is streamed.
    """
    from blackwell_lab.workload.agent import SYSTEM_PROMPT_V241, system_prompt
    from blackwell_lab.workload.evaluator import EVALUATOR_VERSION

    problems: list[str] = []
    if CANDIDATE_P2C not in AUTHORIZED_CANDIDATES or CANDIDATE_P2C not in CATALOG_CANDIDATES:
        problems.append("candidate table")
    if candidate_workload_version(CANDIDATE_P2C) != P2C_CONTRACT["workload_version"]:
        problems.append("workload_version")
    if candidate_controller(CANDIDATE_P2C) != P2C_CONTRACT["controller"]:
        problems.append("controller")
    if candidate_controller(CANDIDATE_P2C) != CONTROLLER_EVIDENCE_GROUNDING_V1:
        problems.append("controller")
    if candidate_temperature(CANDIDATE_P2C) != P2C_CONTRACT["temperature"]:
        problems.append("temperature")
    if FROZEN_TOP_P != P2C_CONTRACT["top_p"]:
        problems.append("top_p")
    if FROZEN_SEED != P2C_CONTRACT["seed"]:
        problems.append("seed")
    if FROZEN_MAX_TOKENS != P2C_CONTRACT["max_tokens"]:
        problems.append("max_tokens")
    if EVALUATOR_VERSION != P2C_CONTRACT["evaluator_version"]:
        problems.append("evaluator")
    scenario = next(iter(catalog().values()))
    prompt = system_prompt(scenario, candidate_workload_version(CANDIDATE_P2C))
    control_prompt = system_prompt(scenario, candidate_workload_version(P2C_CONTROL_CANDIDATE))
    if prompt != SYSTEM_PROMPT_V241 or prompt != control_prompt:
        problems.append("system_prompt")
    if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != P2C_CONTRACT["system_prompt_sha256"]:
        problems.append("system_prompt")
    if CANDIDATE_P2C in SEALED_CANDIDATES or any(
        requires_sealed_set(CANDIDATE_P2C, stage) for stage in AUTHORIZED_STAGES
    ):
        problems.append("task_source")
    if candidate_temperature(P2C_CONTROL_CANDIDATE) != candidate_temperature(CANDIDATE_P2C):
        problems.append("control temperature")
    for stage in AUTHORIZED_STAGES:
        spec = stage_spec(stage)
        if spec["repetitions"] != STAGE_REPETITIONS or spec["concurrency"] != STAGE_CONCURRENCY:
            problems.append(f"{stage} schedule")
    if problems:
        raise QualificationError(
            "P2C contract drift: " + ", ".join(dict.fromkeys(problems)) + "; nothing was executed"
        )
    return dict(P2C_CONTRACT)


def require_p2c_config(config: dict, *, candidate_id: str) -> None:
    """P2C configs carry no sealed, custody, private-scenario, or schedule keys.

    Also pins the workload, controller, and generation sections to the
    contract when they are present (absence falls back to the frozen
    values that the CLI assembles).
    """
    if candidate_id != CANDIDATE_P2C:
        return
    present = sorted(key for key in P2C_FORBIDDEN_CONFIG_KEYS if key in config)
    if present:
        raise ConfigError(
            "P2C executes the public catalog schedule and refuses config keys: "
            + ", ".join(present)
        )
    if config.get("workload_version") not in (None, P2C_CONTRACT["workload_version"]):
        raise ConfigError("qualify-agent P2C workload_version must equal 2.5.0")
    if config.get("controller") not in (None, P2C_CONTRACT["controller"]):
        raise ConfigError("qualify-agent P2C controller must equal evidence-grounding-v1")
    generation = config.get("generation") or {}
    if generation.get("temperature") not in (None, P2C_CONTRACT["temperature"]):
        raise ConfigError("qualify-agent P2C generation.temperature must equal 0.2")
    if generation.get("seed") not in (None, P2C_CONTRACT["seed"]):
        raise ConfigError("qualify-agent P2C generation.seed must equal 20260906")
    if generation.get("max_tokens") not in (None, P2C_CONTRACT["max_tokens"]):
        raise ConfigError("qualify-agent P2C generation.max_tokens must equal 1024")
    if generation.get("top_p") not in (None, P2C_CONTRACT["top_p"]):
        raise ConfigError("qualify-agent P2C generation.top_p must equal 0.95")
    for key in ("system_prompt", "prompt", "evaluator_version", "evaluator"):
        if key in config or key in generation:
            raise ConfigError(f"qualify-agent P2C does not accept a {key} override")
    require_p2c_contract()


def require_p2c_runtime(candidate_id: str, *, custody_dir: str | None) -> None:
    """``--custody-dir`` is refused for P2C before the config is even read."""
    if candidate_id == CANDIDATE_P2C and custody_dir is not None:
        raise ConfigError("P2C executes the public catalog schedule; --custody-dir is refused")


def require_p2c_catalog_execution(
    candidate_id: str,
    *,
    stage: str,
    template_ids: Sequence[str] | None,
    sealed_set: object,
    sealed_tasks: object,
) -> None:
    """The assembled P2C cell must be exactly the catalog cell of its stage.

    Called after the run specification is assembled and before the model
    client is constructed.
    """
    if candidate_id != CANDIDATE_P2C:
        return
    if sealed_set is not None or sealed_tasks is not None:
        raise ConfigError("P2C must not execute sealed input")
    if template_ids is None or tuple(template_ids) != tuple(stage_spec(stage)["template_ids"]):
        raise ConfigError(f"P2C {stage} must schedule exactly the frozen catalog templates")


# --------------------------------------------------------------------------
# W1 / W2: workflow-controlled candidate pair (decision D-0031)
# --------------------------------------------------------------------------

W_SYSTEM_PROMPT_SHA256 = P2C_SYSTEM_PROMPT_SHA256
W_EVALUATOR_VERSION = P2C_EVALUATOR_VERSION
W_TASK_SOURCE = P2C_TASK_SOURCE
#: Config keys the pair refuses, exactly as P2C does.
W_FORBIDDEN_CONFIG_KEYS = P2C_FORBIDDEN_CONFIG_KEYS
#: The frozen pair contract. Every value is re-derived from the running
#: code by :func:`require_w_pair_contract` before any model client exists.
W_PAIR_CONTRACT: dict[str, Any] = {
    "control_candidate": CANDIDATE_W1,
    "treatment_candidate": CANDIDATE_W2,
    "controller": W_CONTROLLER,
    "control_workload_version": W1_WORKLOAD_VERSION,
    "treatment_workload_version": W2_WORKLOAD_VERSION,
    "control_treatments": [],
    "treatment_treatments": [W2_TREATMENT],
    "system_prompt_sha256": W_SYSTEM_PROMPT_SHA256,
    "temperature": W_TEMPERATURE,
    "top_p": FROZEN_TOP_P,
    "seed": FROZEN_SEED,
    "max_tokens": FROZEN_MAX_TOKENS,
    "evaluator_version": W_EVALUATOR_VERSION,
    "task_source": W_TASK_SOURCE,
    "max_turns": 12,
}


def w_pair_experiment_record(candidate_id: str) -> dict[str, Any]:
    """Content-free provenance of the workflow-controlled pair (receipts, reports)."""
    if candidate_id not in WORKFLOW_CANDIDATES:
        raise ConfigError(_UNKNOWN_CANDIDATE)
    return {
        "kind": "workflow-controlled-pair",
        "controller": W_CONTROLLER,
        "control_candidate": CANDIDATE_W1,
        "treatment_candidate": CANDIDATE_W2,
        "role": "control" if candidate_id == CANDIDATE_W1 else "treatment",
        "treatments": list(candidate_treatments(candidate_id)),
        "task_source": W_TASK_SOURCE,
        "schedule": "d-0019-catalog",
        "historical_candidates_unchanged": True,
        "blind_generalization_evidence": False,
        "comparable_with_private_sealed_scores": False,
        "comparable_with_p1_p2c_scores": False,
    }


def require_w_pair_contract() -> dict[str, Any]:
    """Fail closed unless the running code still satisfies the frozen pair contract.

    The pair must share controller, prompt bytes, generation pins,
    evaluator, catalog task source, and stage schedules; the only permitted
    difference is the explicit ``evidence-refs`` treatment (and the
    version-bound ``evidence_refs`` argument it requires).
    """
    from blackwell_lab.workload.agent import SYSTEM_PROMPT_V241, system_prompt
    from blackwell_lab.workload.evaluator import EVALUATOR_VERSION
    from blackwell_lab.workload.tools import TOOL_SPECS, TOOL_SPECS_V250, tool_specs

    problems: list[str] = []
    if WORKFLOW_CANDIDATES != (CANDIDATE_W1, CANDIDATE_W2):
        problems.append("candidate table")
    if any(candidate in AUTHORIZED_CANDIDATES for candidate in WORKFLOW_CANDIDATES):
        problems.append("historical table")
    if candidate_workload_version(CANDIDATE_W1) != W_PAIR_CONTRACT["control_workload_version"]:
        problems.append("control workload_version")
    if candidate_workload_version(CANDIDATE_W2) != W_PAIR_CONTRACT["treatment_workload_version"]:
        problems.append("treatment workload_version")
    for candidate in WORKFLOW_CANDIDATES:
        if candidate_controller(candidate) != W_CONTROLLER:
            problems.append("controller")
        if candidate_temperature(candidate) != W_PAIR_CONTRACT["temperature"]:
            problems.append("temperature")
    if list(candidate_treatments(CANDIDATE_W1)) != W_PAIR_CONTRACT["control_treatments"]:
        problems.append("control treatments")
    if list(candidate_treatments(CANDIDATE_W2)) != W_PAIR_CONTRACT["treatment_treatments"]:
        problems.append("treatment treatments")
    if FROZEN_TOP_P != W_PAIR_CONTRACT["top_p"] or FROZEN_SEED != W_PAIR_CONTRACT["seed"]:
        problems.append("generation pins")
    if FROZEN_MAX_TOKENS != W_PAIR_CONTRACT["max_tokens"]:
        problems.append("max_tokens")
    if EVALUATOR_VERSION != W_PAIR_CONTRACT["evaluator_version"]:
        problems.append("evaluator")
    scenario = next(iter(catalog().values()))
    prompts = {system_prompt(scenario, candidate_workload_version(c)) for c in WORKFLOW_CANDIDATES}
    if prompts != {SYSTEM_PROMPT_V241}:
        problems.append("system_prompt")
    prompt_digest = hashlib.sha256(SYSTEM_PROMPT_V241.encode("utf-8")).hexdigest()
    if prompt_digest != W_PAIR_CONTRACT["system_prompt_sha256"]:
        problems.append("system_prompt")
    if tool_specs(W1_WORKLOAD_VERSION) is not TOOL_SPECS:
        problems.append("control tool contract")
    if tool_specs(W2_WORKLOAD_VERSION) is not TOOL_SPECS_V250:
        problems.append("treatment tool contract")
    if any(
        candidate in SEALED_CANDIDATES or requires_sealed_set(candidate, stage)
        for candidate in WORKFLOW_CANDIDATES
        for stage in AUTHORIZED_STAGES
    ):
        problems.append("task_source")
    if control_candidate_for(CANDIDATE_W2) != CANDIDATE_W1 or control_candidate_for(CANDIDATE_W1):
        problems.append("control pairing")
    for stage in AUTHORIZED_STAGES:
        spec = stage_spec(stage)
        if spec["repetitions"] != STAGE_REPETITIONS or spec["concurrency"] != STAGE_CONCURRENCY:
            problems.append(f"{stage} schedule")
    if stage_spec(STAGE_DEVELOPMENT)["tasks"] != DEVELOPMENT_TASKS or DEVELOPMENT_TASKS != 20:
        problems.append("development tasks")
    if stage_spec(STAGE_DEVELOPMENT)["quality_floor"] != DEVELOPMENT_QUALITY_FLOOR:
        problems.append("development floor")
    if DEVELOPMENT_QUALITY_FLOOR != 0.40:
        problems.append("development floor")
    if problems:
        raise QualificationError(
            "W1/W2 pair contract drift: "
            + ", ".join(dict.fromkeys(problems))
            + "; nothing was executed"
        )
    return dict(W_PAIR_CONTRACT)


def require_w_config(config: dict, *, candidate_id: str) -> None:
    """W1/W2 configs carry no sealed, custody, private-scenario, or schedule keys."""
    if candidate_id not in WORKFLOW_CANDIDATES:
        return
    present = sorted(key for key in W_FORBIDDEN_CONFIG_KEYS if key in config)
    if present:
        raise ConfigError(
            f"{candidate_id} executes the public catalog schedule and refuses config keys: "
            + ", ".join(present)
        )
    expected_version = candidate_workload_version(candidate_id)
    if config.get("workload_version") not in (None, expected_version):
        raise ConfigError(
            f"qualify-agent {candidate_id} workload_version must equal {expected_version}"
        )
    if config.get("controller") not in (None, W_CONTROLLER):
        raise ConfigError(f"qualify-agent {candidate_id} controller must equal {W_CONTROLLER}")
    if "treatments" in config and list(config.get("treatments") or []) != list(
        candidate_treatments(candidate_id)
    ):
        raise ConfigError(
            f"qualify-agent {candidate_id} treatments must equal the frozen pair contract"
        )
    generation = config.get("generation") or {}
    if generation.get("temperature") not in (None, W_PAIR_CONTRACT["temperature"]):
        raise ConfigError(f"qualify-agent {candidate_id} generation.temperature must equal 0.2")
    if generation.get("seed") not in (None, W_PAIR_CONTRACT["seed"]):
        raise ConfigError(f"qualify-agent {candidate_id} generation.seed must equal {FROZEN_SEED}")
    if generation.get("max_tokens") not in (None, W_PAIR_CONTRACT["max_tokens"]):
        raise ConfigError(f"qualify-agent {candidate_id} generation.max_tokens must equal 1024")
    if generation.get("top_p") not in (None, W_PAIR_CONTRACT["top_p"]):
        raise ConfigError(f"qualify-agent {candidate_id} generation.top_p must equal 0.95")
    for key in ("system_prompt", "prompt", "evaluator_version", "evaluator", "max_turns"):
        if key in config or key in generation:
            raise ConfigError(f"qualify-agent {candidate_id} does not accept a {key} override")
    require_w_pair_contract()


def require_w_runtime(candidate_id: str, *, custody_dir: str | None) -> None:
    """``--custody-dir`` is refused for the pair before the config is even read."""
    if candidate_id in WORKFLOW_CANDIDATES and custody_dir is not None:
        raise ConfigError(
            f"{candidate_id} executes the public catalog schedule; --custody-dir is refused"
        )


def require_w_catalog_execution(
    candidate_id: str,
    *,
    stage: str,
    template_ids: Sequence[str] | None,
    sealed_set: object,
    sealed_tasks: object,
) -> None:
    """The assembled W1/W2 cell must be exactly the catalog cell of its stage."""
    if candidate_id not in WORKFLOW_CANDIDATES:
        return
    if sealed_set is not None or sealed_tasks is not None:
        raise ConfigError(f"{candidate_id} must not execute sealed input")
    if template_ids is None or tuple(template_ids) != tuple(stage_spec(stage)["template_ids"]):
        raise ConfigError(
            f"{candidate_id} {stage} must schedule exactly the frozen catalog templates"
        )


def require_safe_run_label(run_label: str) -> str:
    if not _RUN_LABEL_RE.match(run_label):
        raise ConfigError("run_label must be short lowercase letters/digits/hyphens")
    return run_label


def output_label(run_label: str, stage: str, candidate_id: str) -> str:
    label = f"{run_label}-{candidate_id.lower()}-{stage}"
    if not _RUN_LABEL_RE.match(label):
        raise ConfigError("composed qualification output label is unsafe")
    refuse_mvl_identities(
        run_tag="", run_label=label, config={}, artifact_family=QUALIFICATION_ARTIFACT_FAMILY
    )
    return label


def _identity_blob(*parts: object) -> str:
    pieces: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            pieces.append(json.dumps(part, sort_keys=True, default=str))
        else:
            pieces.append(str(part))
    return " ".join(pieces).casefold()


def refuse_mvl_identities(
    *,
    run_tag: str,
    run_label: str,
    config: dict,
    artifact_family: str,
    extra: str = "",
) -> None:
    blob = _identity_blob(run_tag, run_label, config, artifact_family, extra)
    for marker in FORBIDDEN_IDENTITY_MARKERS:
        if marker in blob:
            raise QualificationError(
                "MVL-F identities and output paths are refused for qualification"
            )
    for marker in FORBIDDEN_PATH_MARKERS:
        if marker in blob:
            raise QualificationError(
                "MVL-F identities and output paths are refused for qualification"
            )
    if artifact_family != QUALIFICATION_ARTIFACT_FAMILY:
        raise QualificationError("qualification artifacts must use qualification-runs")
    workflow = config.get("workflow")
    if workflow in {"mvl-baseline", "mvl", "pilot"}:
        raise QualificationError("MVL-F identities and output paths are refused for qualification")


def require_external_config(path_argument: str, repo: Path) -> Path:
    path = Path(path_argument)
    if not path.is_absolute() or not path.is_file():
        raise ConfigError(
            "qualify-agent config must be an existing absolute path outside the repository"
        )
    resolved = path.resolve()
    for candidate in (path, resolved):
        try:
            candidate.relative_to(repo)
        except ValueError:
            continue
        raise ConfigError(
            "qualify-agent config must be an existing absolute path outside the repository"
        )
    return resolved


def validate_authorized_qualification_config(
    config: dict, *, candidate_id: str, stage: str
) -> None:
    if not isinstance(config, dict):
        raise ConfigError("qualify-agent config must be a JSON object")
    required = (
        "endpoint",
        "cloud",
        "model",
        "serving",
        "host",
        "model_verification",
        "canonical_commit",
        "candidate_id",
    )
    for key in required:
        if key not in config:
            raise ConfigError(f"qualify-agent config is missing required section: {key}")

    if config.get("workflow") not in (None, QUALIFICATION_WORKFLOW):
        raise ConfigError("qualify-agent workflow must equal qualify-agent")
    if config.get("candidate_id") != candidate_id:
        raise ConfigError("qualify-agent config candidate_id must match --candidate")
    if candidate_id not in ALL_AUTHORIZED_CANDIDATES:
        raise ConfigError(_UNKNOWN_CANDIDATE)
    expected_version = candidate_workload_version(candidate_id)
    if config.get("workload_version") not in (None, expected_version):
        raise ConfigError(
            f"qualify-agent {candidate_id} workload_version must equal {expected_version}"
        )
    expected_controller = candidate_controller(candidate_id)
    if "controller" in config and config.get("controller") != expected_controller:
        raise ConfigError(
            f"qualify-agent {candidate_id} controller must equal "
            f"{expected_controller or 'null'} (workload {expected_version})"
        )
    if WORKLOAD_VERSION != CATALOG_WORKLOAD_VERSION:
        raise ConfigError("the scenario catalog identity must remain 2.3.0")

    commit = config.get("canonical_commit")
    if not isinstance(commit, str) or len(commit) != 40:
        raise ConfigError("qualify-agent config canonical_commit must be the 40-character SHA")

    verification = config["model_verification"]
    if not isinstance(verification, dict):
        raise ConfigError("qualify-agent config is missing required section: model_verification")
    for key in ("artifact_dir", "digest_manifest"):
        if not verification.get(key):
            raise ConfigError(f"qualify-agent config is missing model_verification.{key}")

    if config.get("comparison_mode") not in (None, FROZEN_COMPARISON_MODE):
        raise ConfigError("qualify-agent comparison_mode must equal provider-native")

    cloud = config["cloud"]
    if not isinstance(cloud, dict):
        raise ConfigError("qualify-agent config is missing required section: cloud")
    if cloud.get("instance_type") != FROZEN_INSTANCE_TYPE:
        raise ConfigError(f"qualify-agent cloud.instance_type must equal {FROZEN_INSTANCE_TYPE}")
    if cloud.get("region") != FROZEN_REGION:
        raise ConfigError(f"qualify-agent cloud.region must equal {FROZEN_REGION}")
    if cloud.get("list_price_usd_per_hour") not in (None, FROZEN_HOURLY_PRICE_USD):
        raise ConfigError("qualify-agent cloud.list_price_usd_per_hour must equal 3.0")

    model = config["model"]
    if not isinstance(model, dict):
        raise ConfigError("qualify-agent config is missing required section: model")
    if model.get("artifact") != FROZEN_MODEL_ARTIFACT:
        raise ConfigError("qualify-agent model.artifact does not match the frozen identity")
    if model.get("revision") != FROZEN_MODEL_REVISION:
        raise ConfigError("qualify-agent model.revision does not match the frozen identity")
    if model.get("artifact_hash") != FROZEN_MODEL_ARTIFACT_HASH:
        raise ConfigError("qualify-agent model.artifact_hash does not match the frozen aggregate")
    if model.get("precision") != FROZEN_PRECISION:
        raise ConfigError("qualify-agent model.precision must equal bf16")

    serving = config["serving"]
    if not isinstance(serving, dict):
        raise ConfigError("qualify-agent config is missing required section: serving")
    if serving.get("engine") != FROZEN_ENGINE:
        raise ConfigError("qualify-agent serving.engine must equal vllm")
    if serving.get("engine_version") != FROZEN_ENGINE_VERSION:
        raise ConfigError("qualify-agent serving.engine_version must equal 0.27.1")
    digest = serving.get("container_digest") or serving.get("image_digest")
    if digest not in {FROZEN_CONTAINER_DIGEST, FROZEN_VLLM_IMAGE_DIGEST}:
        raise ConfigError("qualify-agent serving.container_digest does not match the frozen image")

    if config.get("expected_gpu_model") not in (None, FROZEN_GPU):
        raise ConfigError(f"qualify-agent expected_gpu_model must equal {FROZEN_GPU}")

    spec = stage_spec(stage)
    if config.get("warmup_passes") not in (None, spec["warmup_passes"]):
        raise ConfigError(f"qualify-agent {stage} warmup_passes must equal {spec['warmup_passes']}")
    if config.get("repetitions") not in (None, spec["repetitions"]):
        raise ConfigError("qualify-agent repetitions must equal 1")
    if config.get("tasks_per_repetition") not in (None, spec["tasks"]):
        raise ConfigError(f"qualify-agent {stage} tasks_per_repetition must equal {spec['tasks']}")

    generation = config.get("generation") or {}
    if generation:
        expected_temperature = candidate_temperature(candidate_id)
        if generation.get("temperature") != expected_temperature:
            raise ConfigError(
                f"qualify-agent {candidate_id} generation.temperature "
                f"must equal {expected_temperature}"
            )
        if generation.get("top_p") != FROZEN_TOP_P:
            raise ConfigError("qualify-agent generation.top_p must equal 0.95")
        if generation.get("reasoning_mode") is not True:
            raise ConfigError("qualify-agent generation.reasoning_mode must be true")
        if generation.get("seed") not in (None, FROZEN_SEED):
            raise ConfigError("qualify-agent generation.seed must equal the frozen seed")
        if generation.get("max_tokens") not in (None, FROZEN_MAX_TOKENS):
            raise ConfigError("qualify-agent generation.max_tokens must equal 1024")

    raw_cells = config.get("cells")
    if raw_cells is not None:
        if not isinstance(raw_cells, list) or len(raw_cells) != 1:
            raise ConfigError("qualify-agent cells must be exactly interactive/1")
        cell = raw_cells[0]
        if not isinstance(cell, dict):
            raise ConfigError("qualify-agent cells must be exactly interactive/1")
        if cell.get("profile") != STAGE_PROFILE or cell.get("concurrency") != STAGE_CONCURRENCY:
            raise ConfigError("qualify-agent cells must be exactly interactive/1")
        if cell.get("comparison_mode") not in (None, FROZEN_COMPARISON_MODE):
            raise ConfigError("qualify-agent cells must be provider-native")

    refuse_mvl_identities(
        run_tag="",
        run_label="",
        config=config,
        artifact_family=QUALIFICATION_ARTIFACT_FAMILY,
    )
    # P2C (D-0026) is checked before the generic sealed-binding rule so a
    # sealed or schedule-overriding key on P2C fails with a P2C reason.
    require_p2c_config(config, candidate_id=candidate_id)
    # W1/W2 (D-0031) refuse the same keys with their own reason.
    require_w_config(config, candidate_id=candidate_id)
    require_sealed_binding(config, candidate_id=candidate_id, stage=stage)
    from blackwell_lab.cloud.matched_control import require_development_control_section

    require_development_control_section(config, candidate_id=candidate_id, stage=stage)


def require_sealed_binding(
    config: dict, *, candidate_id: str, stage: str
) -> SealedSetBinding | None:
    """P2 development and holdout must bind a sealed stage (decision D-0024).

    Returns the validated binding, or ``None`` for cells that execute the
    public catalog (C1, C2, P1, and every freeze stage). A binding on such
    a cell is refused, so their configs and digests are unchanged. The
    binding's stage must equal the requested stage and its count must be
    exactly twenty; anything missing, partial, malformed, or mismatched
    fails here, before any model client exists.
    """
    if set(SEALED_CANDIDATES) != {CANDIDATE_P2} or set(SEALED_STAGES) != {
        STAGE_DEVELOPMENT,
        STAGE_HOLDOUT,
    }:
        raise ConfigError("sealed-set candidate table drifted from P2 development/holdout")
    return binding_from_config(config, candidate_id=candidate_id, stage=stage)


def load_qualification_config(path: Path, *, candidate_id: str, stage: str) -> tuple[dict, str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("qualify-agent config is not valid JSON") from exc
    if not isinstance(config, dict):
        raise ConfigError("qualify-agent config must be a JSON object")
    validate_authorized_qualification_config(config, candidate_id=candidate_id, stage=stage)
    return config, digest


def require_clean_canonical_commit(config: dict, *, git_head: str, tree_clean: bool) -> str:
    expected = config["canonical_commit"]
    if git_head != expected:
        raise QualificationError(
            "the working tree commit does not match the frozen canonical_commit "
            "in the approved config; nothing was executed"
        )
    if not tree_clean:
        raise QualificationError(
            "the working tree is dirty; qualification requires an exact canonical clean commit"
        )
    return git_head


def approval_phrase(run_tag: str, run_label: str, candidate_id: str, config_sha256: str) -> str:
    return QUALIFICATION_APPROVAL_TEMPLATE.format(
        run_tag=run_tag,
        run_label=run_label,
        candidate_id=candidate_id,
        config_sha256=config_sha256,
    )


def require_approval(
    provided: str | None,
    *,
    run_tag: str,
    run_label: str,
    candidate_id: str,
    config_sha256: str,
) -> None:
    expected = approval_phrase(run_tag, run_label, candidate_id, config_sha256)
    if (provided or "") != expected:
        raise QualificationError(
            "the qualification requires the exact owner approval phrase "
            f"(expected verbatim: {expected!r}); nothing was executed."
        )


def _field(outcome: object, name: str) -> object:
    if isinstance(outcome, dict):
        if name == "success":
            evaluation = outcome.get("evaluation")
            if isinstance(evaluation, dict) and "success" in evaluation:
                return evaluation.get("success")
        return outcome.get(name)
    execution = getattr(outcome, "execution", None)
    if name == "template_id":
        if execution is not None and getattr(execution, "scenario_id", None):
            return execution.scenario_id
        instance = getattr(outcome, "instance", None)
        if instance is not None:
            return getattr(instance, "template_id", None)
    if name == "success":
        evaluation = getattr(outcome, "evaluation", None)
        if evaluation is not None:
            return getattr(evaluation, "success", None)
    if name == "error_category" and execution is not None:
        return getattr(execution, "error_category", None)
    if name == "e2e_ms" and execution is not None:
        return getattr(execution, "e2e_ms", None)
    if name == "ttft_ms":
        if execution is not None:
            turns = getattr(execution, "turns", ())
            values = [getattr(turn, "ttft_ms", None) for turn in turns]
            values = [value for value in values if value is not None]
            if values:
                return min(values)
        if isinstance(outcome, dict):
            turns = outcome.get("turns") or []
            values = [
                turn.get("ttft_ms")
                for turn in turns
                if isinstance(turn, dict) and turn.get("ttft_ms") is not None
            ]
            if values:
                return min(values)
        return None
    return getattr(outcome, name, None)


@dataclass(frozen=True)
class QualificationMetrics:
    attempted: int
    succeeded: int
    aggregate_quality: float
    scenario_quality: dict[str, float]
    scenario_attempted: dict[str, int]
    valid_native_tool_call_rate: float
    invalid_tool_name_rate: float
    invalid_argument_rate: float
    request_inference_error_rate: float
    timeouts: int
    interactive_ttft_p95_ms: float | None
    interactive_e2e_p95_ms: float | None
    provenance_ok: bool
    verification_ok: bool


def _tool_trace(outcome: object) -> list[object]:
    if isinstance(outcome, dict):
        trace = outcome.get("tool_trace") or []
        return list(trace) if isinstance(trace, list) else []
    trace = getattr(outcome, "tool_trace", None)
    return list(trace) if trace else []


def compute_qualification_metrics(
    outcomes: Sequence[object],
    *,
    provenance_ok: bool,
    verification_ok: bool,
    expected_template_ids: Sequence[str] | None = None,
) -> QualificationMetrics:
    attempted = len(outcomes)
    if attempted < 1:
        raise QualificationError("qualification produced no task observations")
    succeeded = 0
    scenario_success: Counter[str] = Counter()
    scenario_attempted: Counter[str] = Counter()
    invalid_names = 0
    invalid_args = 0
    inference_errors = 0
    timeouts = 0
    valid_tool_calls = 0
    invalid_tool_calls = 0
    e2e_values: list[float] = []
    ttft_values: list[float] = []
    for outcome in outcomes:
        template_id = str(_field(outcome, "template_id") or "")
        scenario_attempted[template_id] += 1
        if _field(outcome, "success") is True:
            succeeded += 1
            scenario_success[template_id] += 1
        category = _field(outcome, "error_category")
        if category == "invalid_tool_name":
            invalid_names += 1
        if category == "invalid_tool_arguments":
            invalid_args += 1
        if category in REQUEST_INFERENCE_ERRORS:
            inference_errors += 1
        if category in TIMEOUT_CATEGORIES:
            timeouts += 1
        valid_tool_calls += len(_tool_trace(outcome))
        if category in STRUCTURAL_TOOL_FAILURES:
            invalid_tool_calls += 1
        e2e = _field(outcome, "e2e_ms")
        if isinstance(e2e, (int, float)):
            e2e_values.append(float(e2e))
        ttft = _field(outcome, "ttft_ms")
        if isinstance(ttft, (int, float)):
            ttft_values.append(float(ttft))
    tool_attempts = valid_tool_calls + invalid_tool_calls
    if tool_attempts < 1:
        raise QualificationError("qualification observed no native tool-call attempts")
    if expected_template_ids is not None:
        scenario_quality = {
            template_id: (
                scenario_success[template_id] / scenario_attempted[template_id]
                if scenario_attempted[template_id]
                else 0.0
            )
            for template_id in expected_template_ids
        }
    else:
        scenario_quality = {
            template_id: (scenario_success[template_id] / count if count else 0.0)
            for template_id, count in scenario_attempted.items()
        }
    return QualificationMetrics(
        attempted=attempted,
        succeeded=succeeded,
        aggregate_quality=succeeded / attempted,
        scenario_quality=scenario_quality,
        scenario_attempted=dict(scenario_attempted),
        valid_native_tool_call_rate=valid_tool_calls / tool_attempts,
        invalid_tool_name_rate=invalid_names / attempted,
        invalid_argument_rate=invalid_args / attempted,
        request_inference_error_rate=inference_errors / attempted,
        timeouts=timeouts,
        interactive_ttft_p95_ms=(
            percentile_nearest_rank(sorted(ttft_values), 0.95) if ttft_values else None
        ),
        interactive_e2e_p95_ms=(
            percentile_nearest_rank(sorted(e2e_values), 0.95) if e2e_values else None
        ),
        provenance_ok=provenance_ok,
        verification_ok=verification_ok,
    )


def _rate_pass(value: float, minimum: float | None = None, maximum: float | None = None) -> bool:
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def evaluate_common_structural_requirements(metrics: QualificationMetrics) -> dict[str, bool]:
    ttft = metrics.interactive_ttft_p95_ms
    e2e = metrics.interactive_e2e_p95_ms
    return {
        "valid_native_tool_call_rate": _rate_pass(
            metrics.valid_native_tool_call_rate, minimum=MIN_VALID_NATIVE_TOOL_CALL_RATE
        ),
        "invalid_tool_name_rate": _rate_pass(
            metrics.invalid_tool_name_rate, maximum=MAX_INVALID_TOOL_NAME_RATE
        ),
        "invalid_argument_rate": _rate_pass(
            metrics.invalid_argument_rate, maximum=MAX_INVALID_ARGUMENT_RATE
        ),
        "request_inference_error_rate": _rate_pass(
            metrics.request_inference_error_rate, maximum=MAX_REQUEST_INFERENCE_ERROR_RATE
        ),
        "timeouts": metrics.timeouts <= MAX_TIMEOUTS,
        "interactive_ttft_p95": ttft is not None and ttft <= MAX_INTERACTIVE_TTFT_P95_MS,
        "interactive_e2e_p95": e2e is not None and e2e <= MAX_INTERACTIVE_E2E_P95_MS,
        "provenance": metrics.provenance_ok,
        "verification": metrics.verification_ok,
    }


def evaluate_quality_level(
    metrics: QualificationMetrics,
    *,
    min_aggregate: float,
    min_scenario: float,
    require_structural: bool,
) -> dict[str, Any]:
    scenario_failures = {
        template_id: rate
        for template_id, rate in metrics.scenario_quality.items()
        if rate < min_scenario
    }
    checks = {
        "aggregate_quality": metrics.aggregate_quality >= min_aggregate,
        "every_scenario": not scenario_failures,
    }
    structural = evaluate_common_structural_requirements(metrics) if require_structural else {}
    checks.update(structural)
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "scenario_failures": scenario_failures,
        "aggregate_quality": metrics.aggregate_quality,
        "min_aggregate": min_aggregate,
        "min_scenario": min_scenario,
    }


def evaluate_study_entry_gate(metrics: QualificationMetrics) -> dict[str, Any]:
    result = evaluate_quality_level(
        metrics,
        min_aggregate=STUDY_ENTRY_MIN_AGGREGATE,
        min_scenario=STUDY_ENTRY_MIN_SCENARIO,
        require_structural=True,
    )
    result["level"] = "study-entry"
    result["production_grade"] = False
    result["note"] = (
        "Passing this gate only permits a configuration to enter comparative "
        "measurement. It is not called production-grade. These are "
        "project-defined targets, not industry standards."
    )
    return result


def evaluate_production_like_target(metrics: QualificationMetrics) -> dict[str, Any]:
    result = evaluate_quality_level(
        metrics,
        min_aggregate=PRODUCTION_LIKE_MIN_AGGREGATE,
        min_scenario=PRODUCTION_LIKE_MIN_SCENARIO,
        require_structural=True,
    )
    result["level"] = "production-like"
    result["invalidates_measurement"] = False
    result["note"] = (
        "A measured configuration may fail this project-defined production-like "
        "target without invalidating its measurement. The study must report "
        "that failure. These are project-defined targets, not industry standards."
    )
    return result


def evaluate_stage_thresholds(stage: str, metrics: QualificationMetrics) -> dict[str, Any]:
    spec = stage_spec(stage)
    if spec["apply_study_entry_gate"]:
        study_entry = evaluate_study_entry_gate(metrics)
        production_like = evaluate_production_like_target(metrics)
        return {
            "stage": stage,
            "continue": study_entry["passed"],
            "stopped": not study_entry["passed"],
            "study_entry": study_entry,
            "production_like": production_like,
        }
    floor = float(spec["quality_floor"])
    passed = metrics.aggregate_quality >= floor
    return {
        "stage": stage,
        "continue": passed,
        "stopped": not passed,
        "quality_floor": floor,
        "aggregate_quality": metrics.aggregate_quality,
        "note": HOLDOUT_MUST_NOT_REVISE_WORDING if stage == STAGE_HOLDOUT else None,
    }


def outcomes_from_records(records: Iterable[object]) -> list[object]:
    outcomes: list[object] = []
    for record in records:
        direct = getattr(record, "outcomes", None)
        if direct:
            outcomes.extend(direct)
            continue
        measured = getattr(record, "measured_observations", None)
        if isinstance(measured, dict):
            outcomes.extend(measured.get("observations") or [])
        result = getattr(record, "result", None)
        if isinstance(result, dict):
            observations = result.get("observations")
            if isinstance(observations, dict) and observations.get("observations"):
                outcomes.extend(observations["observations"])
    return outcomes


def sanitized_receipt(
    *,
    run_label: str,
    candidate_id: str,
    stage: str,
    config_sha256: str,
    identity_digest: str,
    gates: dict[str, Any],
    files: Sequence[str],
    stopped: bool,
    message: str | None = None,
    sealed_set: SealedSetBinding | None = None,
    matched_control: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if requires_sealed_set(candidate_id, stage) != (sealed_set is not None):
        raise QualificationError("sealed-set provenance is required exactly for P2 dev/holdout")
    development_binding = (
        control_candidate_for(candidate_id) is not None and stage == STAGE_DEVELOPMENT
    )
    if development_binding and matched_control is None:
        raise QualificationError(
            f"{candidate_id} development receipt requires matched-control provenance"
        )
    if matched_control is not None and not development_binding:
        raise QualificationError(
            "matched-control provenance is valid only for P2C or W2 development"
        )
    return {
        "workflow": QUALIFICATION_WORKFLOW,
        "artifact_family": QUALIFICATION_ARTIFACT_FAMILY,
        "diagnostic_label": QUALIFICATION_LABEL,
        "not_mvl": True,
        "not_comparative": True,
        "candidate_id": candidate_id,
        "stage": stage,
        "run_label": run_label,
        "config_sha256": config_sha256,
        "candidate_identity_sha256": identity_digest,
        "workload_version": candidate_workload_version(candidate_id),
        "controller": candidate_controller(candidate_id),
        "catalog_workload_version": CATALOG_WORKLOAD_VERSION,
        # Content-free sealed-stage provenance (digests, identity, stage,
        # count). Present only for sealed cells so other receipts are unchanged.
        **({"sealed_set": sealed_set.provenance()} if sealed_set is not None else {}),
        # Controlled-experiment provenance (D-0026). Present only for P2C so
        # historical receipts stay byte-identical.
        **(
            {"controlled_experiment": p2c_experiment_record()}
            if candidate_id == CANDIDATE_P2C
            else {}
        ),
        # D-0031: every live W2 receipt records the pair. W1 is the control
        # and does not gain a treatment block. P1 and P2C are unchanged.
        **(
            {"controlled_experiment": w_pair_experiment_record(candidate_id)}
            if candidate_id == CANDIDATE_W2
            else {}
        ),
        **({"matched_control": matched_control} if matched_control is not None else {}),
        "gates": gates,
        "stopped": stopped,
        "files": list(files),
        "holdout_wording_rule": HOLDOUT_MUST_NOT_REVISE_WORDING,
        "note": (
            message
            or (
                "Qualification artifacts are labeled separately from MVL and "
                "comparative results. No infrastructure changes were made and "
                "no automatic teardown was performed."
            )
        ),
    }


def failure_record(message: str) -> dict[str, Any]:
    return {
        "failure_record": True,
        "is_valid_result": False,
        "workflow": QUALIFICATION_WORKFLOW,
        "artifact_family": QUALIFICATION_ARTIFACT_FAMILY,
        "diagnostic_label": QUALIFICATION_LABEL,
        "diagnostic_only": True,
        "error_type": "QualificationError",
        "note": message,
    }


def tool_contract_mentions_required_workflow() -> bool:
    descriptions = tool_descriptions(QUALIFICATION_WORKLOAD_VERSION)
    system = descriptions["retrieve_runbook"] + descriptions["recommend_remediation"]
    return (
        "service or system" in descriptions["retrieve_runbook"]
        and "remediation_ids" in descriptions["retrieve_runbook"]
        and "found=false" in descriptions["retrieve_runbook"]
        and "retrieve_runbook" in descriptions["recommend_remediation"]
        and "log-dependent" in descriptions["search_logs"]
        and "service or system" in system
    )
