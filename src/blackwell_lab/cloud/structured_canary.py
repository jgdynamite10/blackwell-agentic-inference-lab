"""Offline W5/W6 canary preflight. The canary schedule module stays unchanged."""

from __future__ import annotations

from blackwell_lab.cloud.canary import (
    CANARY_ARTIFACT_FAMILY,
    CANARY_BASE_SEED,
    canary_template_ids,
    output_label,
    require_canary_contract,
    require_measured_schedule,
)
from blackwell_lab.cloud.qualification import (
    FROZEN_COMPARISON_MODE,
    FROZEN_REASONING_MODE,
    FROZEN_SEED,
    FROZEN_TOP_P,
    HOLDOUT_TEMPLATE_IDS,
    STRUCTURED_EVIDENCE_CANDIDATES,
    QualificationError,
    candidate_controller,
    candidate_temperature,
    candidate_workload_version,
)


def require_structured_canary_metadata(template_ids: tuple[str, ...]) -> None:
    """W5/W6 may smoke-test only templates the development overlay reviews."""
    from blackwell_lab.workload.public_metadata import load_overlay

    reviewed = load_overlay()["templates"]
    missing = [item for item in template_ids if item not in reviewed]
    if missing or set(template_ids) & set(HOLDOUT_TEMPLATE_IDS):
        raise QualificationError("W5/W6 canary metadata covers the development catalog only")
    if tuple(template_ids) != canary_template_ids():
        raise QualificationError("the canary must schedule only the six development templates")


def preflight_canary_execution(config: dict, *, candidate_id: str, run_label: str) -> list:
    """Check the ten-task spec before a model client exists.

    Uses the approved config pins. It does not read a ledger, contact an
    endpoint, or accept holdout templates.
    """
    from blackwell_lab.cloud.realbench import RealRunSpec, _validate_spec
    from blackwell_lab.workload.model_client import GenerationSettings
    from blackwell_lab.workload.public_metadata import render_tool_payload
    from blackwell_lab.workload.scenarios import catalog as scenario_catalog

    contract = require_canary_contract()
    serving = config["serving"]
    run_spec = RealRunSpec(
        profile_name=contract["profile"],
        concurrency=contract["concurrency"],
        comparison_mode=FROZEN_COMPARISON_MODE,
        instance_type=config["cloud"]["instance_type"],
        region=config["cloud"]["region"],
        list_price_usd_per_hour=config["cloud"]["list_price_usd_per_hour"],
        price_source_date=config["cloud"]["price_source_date"],
        model=dict(config["model"]),
        engine=serving["engine"],
        engine_version=serving["engine_version"],
        container_digest=serving.get("container_digest") or serving.get("image_digest"),
        repetitions=contract["repetitions"],
        warmup_passes=contract["warmup_passes"],
        tasks_per_repetition=contract["tasks"],
        seed=CANARY_BASE_SEED,
        run_label=output_label(run_label, candidate_id),
        generation=GenerationSettings(
            temperature=candidate_temperature(candidate_id),
            top_p=FROZEN_TOP_P,
            seed=FROZEN_SEED,
            reasoning_mode=FROZEN_REASONING_MODE,
            workload_version=candidate_workload_version(candidate_id),
        ),
        template_ids=tuple(contract["template_ids"]),
        artifact_family=CANARY_ARTIFACT_FAMILY,
        workload_version=candidate_workload_version(candidate_id),
        controller=candidate_controller(candidate_id),
        sealed_set=None,
    )
    _validate_spec(run_spec)
    executed = require_measured_schedule(run_spec)
    if candidate_id in STRUCTURED_EVIDENCE_CANDIDATES:
        require_structured_canary_metadata(tuple(run_spec.template_ids or ()))
        for instance in executed:
            rendered = render_tool_payload(
                scenario_catalog()[instance.template_id],
                "retrieve_runbook",
                {"found": True},
            )
            if "diagnosis_hypotheses" not in rendered:
                raise QualificationError("the development overlay did not publish hypotheses")
    return executed
