"""workflow-controller-v3 acceptance, citations, and development reachability."""

from __future__ import annotations

from fakes import FakeClock

from blackwell_lab.cloud.qualification import (
    DEVELOPMENT_TEMPLATE_IDS,
    HOLDOUT_QUALITY_FLOOR,
    candidate_identity_digest,
)
from blackwell_lab.workload.agent import DEFAULT_MAX_TURNS, RETRIES, TaskExecution, ToolTrace
from blackwell_lab.workload.evaluator import evaluate
from blackwell_lab.workload.evidence import build_controller
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.tools import SimulatedToolbox
from blackwell_lab.workload.workflow import DiagnosisRelevantWorkflowController, WorkflowController
from blackwell_lab.workload.workflow_v3 import (
    REJECT_HYPOTHESIS_SUPPORT,
    REJECT_HYPOTHESIS_WITNESS_REF,
    StructuredEvidenceWorkflowController,
)

# Follow-up searches the reference sequence does not already perform.
# They use public log text. They do not change the support pattern.
EXTRA_SEARCHES = {
    "dns-failures-001": ("cfg-5150",),
    "gpu-saturation-001": ("kv-cache",),
}


def test_limits_and_controller_dispatch_stay_fixed():
    assert DEFAULT_MAX_TURNS == 12
    assert RETRIES == 0
    assert HOLDOUT_QUALITY_FLOOR == 0.50
    assert isinstance(build_controller("2.6.0", "ctx"), WorkflowController)
    assert not isinstance(build_controller("2.6.0", "ctx"), StructuredEvidenceWorkflowController)
    assert isinstance(build_controller("2.7.0", "ctx"), DiagnosisRelevantWorkflowController)
    assert isinstance(build_controller("2.8.0", "ctx"), StructuredEvidenceWorkflowController)
    assert isinstance(build_controller("2.8.1", "ctx"), StructuredEvidenceWorkflowController)
    assert build_controller("2.8.0", "ctx").requires_evidence_refs is False
    assert build_controller("2.8.1", "ctx").requires_evidence_refs is True


def _drive(scenario, version: str, steps: list[tuple[str, dict]]):
    toolbox = SimulatedToolbox(scenario, clock=FakeClock())
    controller = build_controller(version, f"ctx-{scenario.scenario_id}-{version}")
    trace: list[ToolTrace] = []
    ids: list[str] = []
    for tool, arguments in steps:
        result = toolbox.execute(tool, arguments, workload_version=version)
        observation = controller.record(tool, result.payload)
        ids.append(observation.observation_id)
        trace.append(
            ToolTrace(
                tool=result.tool,
                arguments=dict(arguments),
                result=result.payload,
                simulated_latency_ms=result.simulated_latency_ms,
            )
        )
    return controller, trace, ids


def _terminal(diagnosis: str, remediation: str, refs: list[str] | None = None) -> dict:
    payload = {
        "diagnosis_id": diagnosis,
        "rationale": "The recorded public evidence covers this hypothesis.",
        "remediation_id": remediation,
    }
    if refs is not None:
        payload["evidence_refs"] = refs
    return payload


def test_development_regressions_use_the_rendered_catalog():
    scenarios = catalog()
    dns = scenarios["dns-failures-001"]
    controller, _trace, ids = _drive(
        dns,
        "2.8.0",
        [
            ("search_logs", {"query": "SERVFAIL"}),
            ("retrieve_runbook", {"key": "dns-resolver"}),
        ],
    )
    servfail_only = controller.validate_terminal(
        _terminal("badger-queue-outage", "rollback-config-release-cfg-5150")
    )
    assert servfail_only.accepted is False
    assert REJECT_HYPOTHESIS_SUPPORT in servfail_only.failure_categories
    typo_too_early = controller.validate_terminal(
        _terminal("dns-search-domain-typo-cfg-5150", "rollback-config-release-cfg-5150")
    )
    assert typo_too_early.accepted is False
    _drive_more = _drive(
        dns,
        "2.8.0",
        [
            ("search_logs", {"query": "SERVFAIL"}),
            ("search_logs", {"query": "cfg-5150"}),
            ("retrieve_runbook", {"key": "dns-resolver"}),
        ],
    )
    covered = _drive_more[0].validate_terminal(
        _terminal("dns-search-domain-typo-cfg-5150", "rollback-config-release-cfg-5150")
    )
    assert covered.accepted is True
    assert ids

    memory = scenarios["memory-pressure-001"]
    oom, _trace, _ids = _drive(
        memory,
        "2.8.0",
        [
            ("search_logs", {"query": "out of memory"}),
            ("retrieve_runbook", {"key": "otter-inventory"}),
        ],
    )
    assert (
        oom.validate_terminal(
            _terminal("memory-leak-in-worker", "rollback-config-release-cfg-3300")
        ).accepted
        is False
    )
    assert (
        oom.validate_terminal(
            _terminal("unbounded-cache-cfg-3300", "rollback-config-release-cfg-3300")
        ).accepted
        is False
    )
    cache, _trace, _ids = _drive(
        memory,
        "2.8.0",
        [
            ("search_logs", {"query": "cache"}),
            ("retrieve_runbook", {"key": "otter-inventory"}),
        ],
    )
    assert (
        cache.validate_terminal(
            _terminal("unbounded-cache-cfg-3300", "rollback-config-release-cfg-3300")
        ).accepted
        is True
    )
    assert (
        cache.validate_terminal(
            _terminal("memory-leak-in-worker", "rollback-config-release-cfg-3300")
        ).accepted
        is False
    )

    deploy = scenarios["failed-deployment-001"]
    migration, _trace, _ids = _drive(
        deploy,
        "2.8.0",
        [
            ("search_logs", {"query": "readiness"}),
            ("retrieve_runbook", {"key": "ibis-notify"}),
        ],
    )
    assert (
        migration.validate_terminal(
            _terminal("missing-migration-dep-9004", "rollback-deploy-dep-9004")
        ).accepted
        is True
    )
    assert (
        migration.validate_terminal(
            _terminal("readiness-probe-misconfigured", "rollback-deploy-dep-9004")
        ).accepted
        is False
    )


def test_w6_must_cite_every_required_witness():
    scenario = catalog()["unhealthy-upstream-001"]
    steps = [
        ("get_service_health", {}),
        ("search_logs", {"query": "heron-auth"}),
        ("retrieve_runbook", {"key": "heron-auth"}),
    ]
    controller, _trace, ids = _drive(scenario, "2.8.1", steps)
    health_id, log_id, _runbook = ids
    arguments = _terminal(
        "unhealthy-upstream-heron-auth",
        "failover-heron-auth-to-standby-pool",
    )
    only_log = dict(arguments)
    only_log["evidence_refs"] = [log_id]
    rejected = controller.validate_terminal(only_log)
    assert rejected.accepted is False
    assert REJECT_HYPOTHESIS_WITNESS_REF in rejected.failure_categories
    assert "heron-auth" not in rejected.tool_payload()["guidance"]
    both = dict(arguments)
    both["evidence_refs"] = [health_id, log_id]
    # A fresh controller: the previous rejection is fine, and citing both still passes.
    fresh, _trace, fresh_ids = _drive(scenario, "2.8.1", steps)
    both["evidence_refs"] = [fresh_ids[0], fresh_ids[1]]
    assert fresh.validate_terminal(both).accepted is True


def test_gpu_admission_alone_fails_and_both_witnesses_pass():
    scenario = catalog()["gpu-saturation-001"]
    host_steps = [
        ("search_logs", {"query": "embed-refresh-44"}),
        ("retrieve_runbook", {"key": "lynx-inference"}),
    ]
    alone, _trace, _ids = _drive(scenario, "2.8.0", host_steps)
    assert (
        alone.validate_terminal(
            _terminal("batch-job-gpu-contention", "throttle-batch-job-embed-refresh-44")
        ).accepted
        is False
    )
    both, _trace, _ids = _drive(
        scenario,
        "2.8.0",
        [
            ("search_logs", {"query": "embed-refresh-44"}),
            ("search_logs", {"query": "kv-cache"}),
            ("retrieve_runbook", {"key": "lynx-inference"}),
        ],
    )
    assert (
        both.validate_terminal(
            _terminal("batch-job-gpu-contention", "throttle-batch-job-embed-refresh-44")
        ).accepted
        is True
    )


def test_development_paths_stay_inside_twelve_turns_and_pass_the_evaluator():
    scenarios = catalog()
    for template_id in DEVELOPMENT_TEMPLATE_IDS:
        scenario = scenarios[template_id]
        steps = [
            (step["tool"], dict(step["arguments"])) for step in scenario.reference_tool_sequence
        ]
        extras = EXTRA_SEARCHES.get(template_id, ())
        steps.extend(("search_logs", {"query": query}) for query in extras)
        steps.append(
            (
                "recommend_remediation",
                _terminal(scenario.accepted_diagnoses[0], scenario.accepted_remediations[0]),
            )
        )
        assert len(steps) <= 12
        evidence = [step for step in steps if step[0] != "recommend_remediation"]
        controller, trace, _ids = _drive(scenario, "2.8.0", evidence)
        verdict = controller.validate_terminal(steps[-1][1])
        assert verdict.accepted is True, template_id
        execution = TaskExecution(
            scenario_id=scenario.scenario_id,
            status="completed",
            diagnosis_id=scenario.accepted_diagnoses[0],
            rationale="covered",
            remediation_id=scenario.accepted_remediations[0],
            tool_trace=trace,
        )
        evaluation = evaluate(scenario, execution)
        failed = [gate.gate_id for gate in evaluation.gates if not gate.passed]
        assert evaluation.success, (template_id, failed)


def test_w5_identity_is_not_a_historical_digest():
    digest = candidate_identity_digest("W5")
    assert digest not in {
        "2b7c5745084f4459af66e58fa5701d5c513108a9536385acbed00722c02e68cf",
        "06d673972a696efcddc9ce00d6c9f6a15a032e0c516d4dc8b54f01dbba0917a2",
        "bdb9947bf5c0934833d077536ae85740857c36bfd08d88f7fee8b7b54f0f857e",
        "66df267f60f062f867678331fc466305cde5432bc9cba861ebe90e8cef108414",
    }
