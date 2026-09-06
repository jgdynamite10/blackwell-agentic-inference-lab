"""Deterministic, versioned incident-scenario catalog.

Every scenario is a pure data definition: the fixture data each simulated
tool returns, the ground-truth root cause, the accepted remediation set,
distractor signals, and the machine-checkable success criteria the evaluator
applies. Scenarios contain **only synthetic data**: fictional service names,
hostnames under the reserved ``.example`` TLD (RFC 2606), and IP addresses
from the RFC 5737 documentation ranges (192.0.2.0/24, 198.51.100.0/24,
203.0.113.0/24). Nothing here references a real system, account, or provider.

The catalog is versioned via ``WORKLOAD_VERSION`` and content-addressed via
``catalog_digest()``; both appear in every run manifest so results are
attributable to an exact workload definition.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

WORKLOAD_NAME = "cloud-ops-agent"
WORKLOAD_VERSION = "2.0.0"

#: The ten incident condition classes required by
#: methodology/workload-definition.md ("Incident catalog").
INCIDENT_CLASSES = (
    "elevated-latency",
    "pod-failures",
    "memory-pressure",
    "gpu-saturation",
    "storage-latency",
    "failed-deployment",
    "unhealthy-upstream",
    "dns-failures",
    "rate-limiting",
    "capacity-exhaustion",
)

# Reserved documentation networks (RFC 5737). All fixture IPs come from here.
_DOC_NET = "203.0.113."
_DOC_NET2 = "198.51.100."


def _host(service: str, index: int) -> str:
    """An invented hostname under the reserved .example TLD."""
    return f"{service}-{index}.node.lab.example"


def _metric_series(base: float, amplitude: float, spike_from: int, points: int = 60) -> list[dict]:
    """A deterministic 60-point series (one point per minute offset).

    Values follow a fixed formula: flat around ``base`` with a small
    deterministic ripple, then elevated by ``amplitude`` from ``spike_from``.
    """
    series = []
    for i in range(points):
        ripple = ((i * 7) % 5) * 0.01 * base
        value = base + ripple + (amplitude if i >= spike_from else 0.0)
        series.append({"t_offset_s": i * 60, "value": round(value, 3)})
    return series


def _noise_logs(service: str, start_index: int, count: int) -> list[dict]:
    """Deterministic INFO-level background noise so profiles can differ in
    how much log context they surface."""
    lines = []
    for i in range(count):
        lines.append(
            {
                "t_offset_s": 30 * (start_index + i),
                "host": _host(service, (i % 3) + 1),
                "level": "INFO",
                "message": (
                    f"request completed path=/api/v1/items status=200 "
                    f"client={_DOC_NET2}{(i % 40) + 10} duration_ms={40 + (i * 3) % 25}"
                ),
            }
        )
    return lines


@dataclass(frozen=True)
class Scenario:
    """One deterministic incident definition.

    ``root_cause_keywords`` are the machine-checkable success criteria for the
    diagnosis component: the evaluator credits the fraction of keywords that
    appear (case-insensitively) in the agent's stated root cause.
    ``accepted_remediations`` is the accepted set; ``distractor_remediations``
    are plausible-but-wrong actions that also appear in runbook fixtures.
    ``required_evidence`` lists the tools an appropriately-diagnosing agent
    must consult before recommending.
    """

    scenario_id: str
    incident_class: str
    title: str
    description: str
    affected_service: str
    health: dict[str, dict[str, str]]
    metrics: dict[str, dict]
    logs: list[dict] = field(default_factory=list)
    runbooks: dict[str, dict] = field(default_factory=dict)
    recent_changes: list[dict] = field(default_factory=list)
    root_cause_id: str = ""
    root_cause_summary: str = ""
    root_cause_keywords: tuple[str, ...] = ()
    accepted_remediations: tuple[str, ...] = ()
    distractor_remediations: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    reference_tool_sequence: tuple[dict, ...] = ()


def _build_scenarios() -> tuple[Scenario, ...]:
    scenarios: list[Scenario] = []

    # ------------------------------------------------------------------ 1
    svc = "zephyr-cart"
    scenarios.append(
        Scenario(
            scenario_id="elevated-latency-001",
            incident_class="elevated-latency",
            title="Checkout API p99 latency regression",
            description=(
                f"Pager alert: {svc} p99 latency breached 1200 ms (baseline 180 ms) "
                "starting at t+20m. Error rate is normal. Diagnose the root cause and "
                "recommend a remediation."
            ),
            affected_service=svc,
            health={
                svc: {"api": "degraded", "worker": "healthy", "cache": "healthy"},
                "heron-auth": {"api": "healthy"},
                "otter-inventory": {"api": "healthy"},
            },
            metrics={
                "latency_p99_ms": {"unit": "ms", "points": _metric_series(180, 1050, 20)},
                "error_rate_pct": {"unit": "percent", "points": _metric_series(0.2, 0.0, 20)},
                "cpu_util_pct": {"unit": "percent", "points": _metric_series(38, 9, 20)},
            },
            logs=[
                {
                    "t_offset_s": 1230,
                    "host": _host(svc, 1),
                    "level": "WARN",
                    "message": (
                        "audit-log flush blocking request thread pool queue_depth=412 flush_ms=940"
                    ),
                },
                {
                    "t_offset_s": 1290,
                    "host": _host(svc, 2),
                    "level": "WARN",
                    "message": "synchronous audit logging enabled by config release cfg-2041",
                },
                *_noise_logs(svc, 0, 22),
            ],
            runbooks={
                svc: {
                    "title": "zephyr-cart latency runbook",
                    "steps": [
                        "Check whether a recent deploy or config release correlates "
                        "with the latency shift.",
                        "If a config release is implicated, roll it back before scaling.",
                    ],
                    "remediation_ids": [
                        "rollback-config-release-cfg-2041",
                        "scale-out-zephyr-cart-api",
                    ],
                }
            },
            recent_changes=[
                {
                    "change_id": "cfg-2041",
                    "t_offset_s": 1200,
                    "kind": "config-release",
                    "service": svc,
                    "summary": "Enable synchronous audit logging on request threads",
                },
                {
                    "change_id": "dep-7710",
                    "t_offset_s": -86400,
                    "kind": "deploy",
                    "service": "otter-inventory",
                    "summary": "Routine dependency bump (yesterday)",
                },
            ],
            root_cause_id="synchronous-audit-logging-cfg-2041",
            root_cause_summary=(
                "Config release cfg-2041 enabled synchronous audit logging on request "
                "threads, blocking the thread pool and inflating p99 latency."
            ),
            root_cause_keywords=("audit logging", "cfg-2041"),
            accepted_remediations=("rollback-config-release-cfg-2041",),
            distractor_remediations=("scale-out-zephyr-cart-api", "restart-zephyr-cart-pods"),
            required_evidence=("get_service_health", "search_logs", "check_recent_changes"),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "query_metrics", "arguments": {"metric": "latency_p99_ms"}},
                {"tool": "search_logs", "arguments": {"query": "audit"}},
                {"tool": "check_recent_changes", "arguments": {}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
        )
    )

    # ------------------------------------------------------------------ 2
    svc = "quokka-payments"
    scenarios.append(
        Scenario(
            scenario_id="pod-failures-001",
            incident_class="pod-failures",
            title="Payments pods crash-looping after rollout",
            description=(
                f"Alert: {svc} has 4 of 6 pods in CrashLoopBackOff since t+5m. "
                "Payment success rate is dropping. Diagnose and recommend remediation."
            ),
            affected_service=svc,
            health={
                svc: {"api": "unhealthy", "worker": "degraded"},
                "zephyr-cart": {"api": "healthy"},
            },
            metrics={
                "pod_restarts": {"unit": "count", "points": _metric_series(0, 14, 5)},
                "success_rate_pct": {"unit": "percent", "points": _metric_series(99.6, -38, 5)},
            },
            logs=[
                {
                    "t_offset_s": 320,
                    "host": _host(svc, 1),
                    "level": "ERROR",
                    "message": (
                        "startup failed: required environment variable "
                        "LEDGER_ENDPOINT_MODE is unset (introduced in release dep-8102)"
                    ),
                },
                {
                    "t_offset_s": 335,
                    "host": _host(svc, 2),
                    "level": "ERROR",
                    "message": "container exited with code 1 during boot sequence",
                },
                *_noise_logs(svc, 0, 20),
            ],
            runbooks={
                svc: {
                    "title": "quokka-payments crash-loop runbook",
                    "steps": [
                        "Inspect pod startup logs for missing configuration.",
                        "Roll back the offending release if boot-time config is missing.",
                    ],
                    "remediation_ids": [
                        "rollback-deploy-dep-8102",
                        "increase-quokka-payments-replicas",
                    ],
                }
            },
            recent_changes=[
                {
                    "change_id": "dep-8102",
                    "t_offset_s": 240,
                    "kind": "deploy",
                    "service": svc,
                    "summary": "Release adding ledger endpoint selection (new env var)",
                },
                {
                    "change_id": "cfg-1990",
                    "t_offset_s": -3600,
                    "kind": "config-release",
                    "service": "marmot-search",
                    "summary": "Unrelated search tuning an hour earlier",
                },
            ],
            root_cause_id="missing-env-var-dep-8102",
            root_cause_summary=(
                "Deploy dep-8102 requires the new LEDGER_ENDPOINT_MODE environment "
                "variable, which was not set, so pods fail at startup and crash-loop."
            ),
            root_cause_keywords=("environment variable", "dep-8102"),
            accepted_remediations=("rollback-deploy-dep-8102",),
            distractor_remediations=(
                "increase-quokka-payments-replicas",
                "restart-quokka-payments-pods",
            ),
            required_evidence=("get_service_health", "search_logs", "check_recent_changes"),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "search_logs", "arguments": {"query": "startup failed"}},
                {"tool": "check_recent_changes", "arguments": {}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
        )
    )

    # ------------------------------------------------------------------ 3
    svc = "otter-inventory"
    scenarios.append(
        Scenario(
            scenario_id="memory-pressure-001",
            incident_class="memory-pressure",
            title="Inventory service OOM kills under memory pressure",
            description=(
                f"Alert: {svc} workers are being OOM-killed every ~10 minutes since "
                "t+15m; node memory pressure is critical. Diagnose and recommend "
                "remediation."
            ),
            affected_service=svc,
            health={
                svc: {"api": "degraded", "worker": "unhealthy", "cache": "degraded"},
                "badger-queue": {"broker": "healthy"},
            },
            metrics={
                "memory_working_set_gib": {"unit": "GiB", "points": _metric_series(6.5, 9.2, 15)},
                "oom_kills": {"unit": "count", "points": _metric_series(0, 3, 15)},
            },
            logs=[
                {
                    "t_offset_s": 960,
                    "host": _host(svc, 3),
                    "level": "ERROR",
                    "message": "worker killed: out of memory (anon-rss grew unbounded)",
                },
                {
                    "t_offset_s": 990,
                    "host": _host(svc, 3),
                    "level": "WARN",
                    "message": (
                        "cache eviction disabled: cfg-3300 set cache_max_entries=0 "
                        "(unbounded) — cache size 9.1 GiB and growing"
                    ),
                },
                *_noise_logs(svc, 0, 20),
            ],
            runbooks={
                svc: {
                    "title": "otter-inventory memory runbook",
                    "steps": [
                        "Check whether the in-process cache is bounded.",
                        "Restore the cache entry limit before adding hardware.",
                    ],
                    "remediation_ids": [
                        "rollback-config-release-cfg-3300",
                        "add-memory-to-otter-inventory-nodes",
                    ],
                }
            },
            recent_changes=[
                {
                    "change_id": "cfg-3300",
                    "t_offset_s": 900,
                    "kind": "config-release",
                    "service": svc,
                    "summary": "Set cache_max_entries=0 (unbounded cache) for hit-rate test",
                },
            ],
            root_cause_id="unbounded-cache-cfg-3300",
            root_cause_summary=(
                "Config release cfg-3300 removed the cache entry bound, so the "
                "in-process cache grows unbounded and workers are OOM-killed."
            ),
            root_cause_keywords=("cache", "cfg-3300", "unbounded"),
            accepted_remediations=("rollback-config-release-cfg-3300",),
            distractor_remediations=(
                "add-memory-to-otter-inventory-nodes",
                "restart-otter-inventory-workers",
            ),
            required_evidence=("query_metrics", "search_logs", "check_recent_changes"),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "query_metrics", "arguments": {"metric": "memory_working_set_gib"}},
                {"tool": "search_logs", "arguments": {"query": "cache"}},
                {"tool": "check_recent_changes", "arguments": {}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
        )
    )

    # ------------------------------------------------------------------ 4
    svc = "lynx-inference"
    scenarios.append(
        Scenario(
            scenario_id="gpu-saturation-001",
            incident_class="gpu-saturation",
            title="Online inference starved by batch embedding job",
            description=(
                f"Alert: {svc} online request latency tripled at t+10m; GPU "
                "utilization is pinned at 100%. Diagnose and recommend remediation."
            ),
            affected_service=svc,
            health={
                svc: {"online-serving": "degraded", "batch-lane": "healthy"},
                "falcon-frontend": {"api": "healthy"},
            },
            metrics={
                "gpu_util_pct": {"unit": "percent", "points": _metric_series(62, 38, 10)},
                "online_latency_p95_ms": {"unit": "ms", "points": _metric_series(240, 510, 10)},
                "batch_queue_depth": {"unit": "count", "points": _metric_series(2, 55, 10)},
            },
            logs=[
                {
                    "t_offset_s": 640,
                    "host": _host(svc, 1),
                    "level": "WARN",
                    "message": (
                        "scheduler: batch job embed-refresh-44 admitted to online GPU "
                        "pool without priority cap"
                    ),
                },
                {
                    "t_offset_s": 700,
                    "host": _host(svc, 1),
                    "level": "WARN",
                    "message": "online queue wait exceeded 800 ms; kv-cache allocator saturated",
                },
                *_noise_logs(svc, 0, 20),
            ],
            runbooks={
                svc: {
                    "title": "lynx-inference GPU saturation runbook",
                    "steps": [
                        "Identify non-interactive jobs sharing the online GPU pool.",
                        "Throttle or evict batch work before restarting serving.",
                    ],
                    "remediation_ids": [
                        "throttle-batch-job-embed-refresh-44",
                        "restart-lynx-inference-serving",
                    ],
                }
            },
            recent_changes=[
                {
                    "change_id": "job-embed-refresh-44",
                    "t_offset_s": 600,
                    "kind": "batch-job-start",
                    "service": svc,
                    "summary": "Embedding refresh batch job started on shared GPU pool",
                },
            ],
            root_cause_id="batch-job-gpu-contention",
            root_cause_summary=(
                "Batch job embed-refresh-44 was admitted to the online GPU pool "
                "without a priority cap, saturating the GPUs and starving online "
                "inference."
            ),
            root_cause_keywords=("batch job", "gpu", "embed-refresh-44"),
            accepted_remediations=("throttle-batch-job-embed-refresh-44",),
            distractor_remediations=(
                "restart-lynx-inference-serving",
                "scale-out-lynx-inference",
            ),
            required_evidence=("query_metrics", "search_logs", "check_recent_changes"),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "query_metrics", "arguments": {"metric": "gpu_util_pct"}},
                {"tool": "search_logs", "arguments": {"query": "batch"}},
                {"tool": "check_recent_changes", "arguments": {}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
        )
    )

    # ------------------------------------------------------------------ 5
    svc = "heron-metadata"
    scenarios.append(
        Scenario(
            scenario_id="storage-latency-001",
            incident_class="storage-latency",
            title="Metadata store slowed by degraded volume",
            description=(
                f"Alert: {svc} read latency rose 8x at t+30m; CPU and memory are "
                "normal. Diagnose and recommend remediation."
            ),
            affected_service=svc,
            health={
                svc: {"primary": "degraded", "replica": "healthy", "volume-7": "degraded"},
                "ibis-notify": {"api": "healthy"},
            },
            metrics={
                "read_latency_p95_ms": {"unit": "ms", "points": _metric_series(4, 31, 30)},
                "disk_io_wait_pct": {"unit": "percent", "points": _metric_series(3, 44, 30)},
                "cpu_util_pct": {"unit": "percent", "points": _metric_series(22, 1, 30)},
            },
            logs=[
                {
                    "t_offset_s": 1830,
                    "host": _host(svc, 1),
                    "level": "WARN",
                    "message": (
                        "storage layer: volume-7 media latency above threshold "
                        "(await 46 ms, baseline 3 ms); relocation recommended"
                    ),
                },
                *_noise_logs(svc, 0, 20),
            ],
            runbooks={
                svc: {
                    "title": "heron-metadata storage runbook",
                    "steps": [
                        "Check io-wait and per-volume latency counters.",
                        "Fail over to the healthy replica while the volume is migrated.",
                    ],
                    "remediation_ids": [
                        "failover-heron-metadata-to-replica",
                        "increase-heron-metadata-cpu",
                    ],
                }
            },
            recent_changes=[
                {
                    "change_id": "maint-556",
                    "t_offset_s": -7200,
                    "kind": "maintenance",
                    "service": "walrus-batch",
                    "summary": "Unrelated maintenance on batch fleet two hours earlier",
                },
            ],
            root_cause_id="degraded-volume-7",
            root_cause_summary=(
                "The primary's backing volume-7 is degraded (media latency 46 ms vs "
                "3 ms baseline), inflating read latency while CPU stays normal."
            ),
            root_cause_keywords=("volume-7", "degraded"),
            accepted_remediations=("failover-heron-metadata-to-replica",),
            distractor_remediations=(
                "increase-heron-metadata-cpu",
                "restart-heron-metadata-primary",
            ),
            required_evidence=("get_service_health", "query_metrics", "search_logs"),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "query_metrics", "arguments": {"metric": "disk_io_wait_pct"}},
                {"tool": "search_logs", "arguments": {"query": "volume-7"}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
        )
    )

    # ------------------------------------------------------------------ 6
    svc = "ibis-notify"
    scenarios.append(
        Scenario(
            scenario_id="failed-deployment-001",
            incident_class="failed-deployment",
            title="Notification rollout stuck on readiness probe",
            description=(
                f"Alert: {svc} deploy dep-9004 has been stuck for 25 minutes: new "
                "pods never become ready and the rollout is halted. Diagnose and "
                "recommend remediation."
            ),
            affected_service=svc,
            health={
                svc: {"api": "degraded", "rollout": "stalled"},
                "quokka-payments": {"api": "healthy"},
            },
            metrics={
                "ready_replicas": {"unit": "count", "points": _metric_series(6, -3, 5)},
                "probe_failures": {"unit": "count", "points": _metric_series(0, 12, 5)},
            },
            logs=[
                {
                    "t_offset_s": 400,
                    "host": _host(svc, 2),
                    "level": "ERROR",
                    "message": (
                        "readiness probe failing: schema_version=41 required by "
                        "dep-9004 but database reports schema_version=40 "
                        "(migration 41 not applied)"
                    ),
                },
                *_noise_logs(svc, 0, 20),
            ],
            runbooks={
                svc: {
                    "title": "ibis-notify deployment runbook",
                    "steps": [
                        "Compare the release's required schema version with the "
                        "database's current version.",
                        "Roll back the release if the migration was not applied first.",
                    ],
                    "remediation_ids": [
                        "rollback-deploy-dep-9004",
                        "delete-ibis-notify-pods",
                    ],
                }
            },
            recent_changes=[
                {
                    "change_id": "dep-9004",
                    "t_offset_s": 60,
                    "kind": "deploy",
                    "service": svc,
                    "summary": "Release requiring database schema version 41",
                },
            ],
            root_cause_id="missing-migration-dep-9004",
            root_cause_summary=(
                "Deploy dep-9004 requires database schema version 41, but migration "
                "41 was never applied, so readiness probes fail and the rollout stalls."
            ),
            root_cause_keywords=("schema", "migration", "dep-9004"),
            accepted_remediations=("rollback-deploy-dep-9004",),
            distractor_remediations=("delete-ibis-notify-pods", "restart-ibis-notify-api"),
            required_evidence=("get_service_health", "search_logs", "check_recent_changes"),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "search_logs", "arguments": {"query": "readiness"}},
                {"tool": "check_recent_changes", "arguments": {}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
        )
    )

    # ------------------------------------------------------------------ 7
    svc = "falcon-frontend"
    scenarios.append(
        Scenario(
            scenario_id="unhealthy-upstream-001",
            incident_class="unhealthy-upstream",
            title="Frontend errors caused by unhealthy auth upstream",
            description=(
                f"Alert: {svc} 5xx rate climbed to 9% at t+8m. Frontend pods look "
                "healthy. Diagnose and recommend remediation."
            ),
            affected_service=svc,
            health={
                svc: {"api": "degraded"},
                "heron-auth": {"api": "unhealthy", "token-signer": "unhealthy"},
                "marmot-search": {"api": "healthy"},
            },
            metrics={
                "error_rate_pct": {"unit": "percent", "points": _metric_series(0.4, 8.6, 8)},
                "upstream_auth_timeouts": {"unit": "count", "points": _metric_series(0, 65, 8)},
            },
            logs=[
                {
                    "t_offset_s": 500,
                    "host": _host(svc, 1),
                    "level": "ERROR",
                    "message": (
                        "upstream heron-auth timed out after 2000 ms "
                        f"(endpoint {_DOC_NET}41); circuit breaker half-open"
                    ),
                },
                {
                    "t_offset_s": 520,
                    "host": _host("heron-auth", 1),
                    "level": "ERROR",
                    "message": "token-signer worker pool exhausted; requests queuing",
                },
                *_noise_logs(svc, 0, 20),
            ],
            runbooks={
                "heron-auth": {
                    "title": "heron-auth degradation runbook",
                    "steps": [
                        "Confirm which upstream is failing before touching the frontend.",
                        "Fail over auth to the standby signer pool.",
                    ],
                    "remediation_ids": [
                        "failover-heron-auth-to-standby-pool",
                        "restart-falcon-frontend-pods",
                    ],
                }
            },
            recent_changes=[
                {
                    "change_id": "cfg-4102",
                    "t_offset_s": -1800,
                    "kind": "config-release",
                    "service": "badger-queue",
                    "summary": "Unrelated queue retention change 30 minutes earlier",
                },
            ],
            root_cause_id="unhealthy-upstream-heron-auth",
            root_cause_summary=(
                "The heron-auth upstream (token-signer pool) is exhausted and timing "
                "out; falcon-frontend 5xx responses are collateral."
            ),
            root_cause_keywords=("heron-auth", "upstream"),
            accepted_remediations=("failover-heron-auth-to-standby-pool",),
            distractor_remediations=(
                "restart-falcon-frontend-pods",
                "scale-out-falcon-frontend",
            ),
            required_evidence=("get_service_health", "query_metrics", "search_logs"),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {}},
                {"tool": "query_metrics", "arguments": {"metric": "upstream_auth_timeouts"}},
                {"tool": "search_logs", "arguments": {"query": "heron-auth"}},
                {"tool": "retrieve_runbook", "arguments": {"key": "heron-auth"}},
            ),
        )
    )

    # ------------------------------------------------------------------ 8
    svc = "walrus-batch"
    scenarios.append(
        Scenario(
            scenario_id="dns-failures-001",
            incident_class="dns-failures",
            title="Batch jobs failing on DNS resolution",
            description=(
                f"Alert: {svc} job failure rate hit 40% at t+12m with connection "
                "errors. Diagnose and recommend remediation."
            ),
            affected_service=svc,
            health={
                svc: {"scheduler": "healthy", "workers": "degraded"},
                "dns-resolver": {"resolver": "degraded"},
            },
            metrics={
                "job_failure_rate_pct": {"unit": "percent", "points": _metric_series(1, 39, 12)},
                "dns_servfail_count": {"unit": "count", "points": _metric_series(0, 210, 12)},
            },
            logs=[
                {
                    "t_offset_s": 760,
                    "host": _host(svc, 4),
                    "level": "ERROR",
                    "message": (
                        "resolve badger-queue.svc.lab.example: SERVFAIL "
                        "(search domain 'svc.1ab.example' not found)"
                    ),
                },
                {
                    "t_offset_s": 780,
                    "host": _host("dns-resolver", 1),
                    "level": "WARN",
                    "message": (
                        "config release cfg-5150 loaded: search domain changed to "
                        "'svc.1ab.example' (typo: digit one for the letter l)"
                    ),
                },
                *_noise_logs(svc, 0, 20),
            ],
            runbooks={
                "dns-resolver": {
                    "title": "cluster DNS runbook",
                    "steps": [
                        "Check resolver SERVFAIL counters and recent resolver config.",
                        "Revert bad resolver config before restarting caches.",
                    ],
                    "remediation_ids": [
                        "rollback-config-release-cfg-5150",
                        "restart-dns-cache-daemons",
                    ],
                }
            },
            recent_changes=[
                {
                    "change_id": "cfg-5150",
                    "t_offset_s": 700,
                    "kind": "config-release",
                    "service": "dns-resolver",
                    "summary": "Resolver search-domain update (contains a typo)",
                },
            ],
            root_cause_id="dns-search-domain-typo-cfg-5150",
            root_cause_summary=(
                "Resolver config release cfg-5150 set a misspelled search domain "
                "('svc.1ab.example'), so service lookups SERVFAIL and jobs fail."
            ),
            root_cause_keywords=("search domain", "cfg-5150"),
            accepted_remediations=("rollback-config-release-cfg-5150",),
            distractor_remediations=("restart-dns-cache-daemons", "restart-walrus-batch-workers"),
            required_evidence=("search_logs", "check_recent_changes"),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {}},
                {"tool": "query_metrics", "arguments": {"metric": "dns_servfail_count"}},
                {"tool": "search_logs", "arguments": {"query": "SERVFAIL"}},
                {"tool": "check_recent_changes", "arguments": {}},
                {"tool": "retrieve_runbook", "arguments": {"key": "dns-resolver"}},
            ),
        )
    )

    # ------------------------------------------------------------------ 9
    svc = "marmot-search"
    scenarios.append(
        Scenario(
            scenario_id="rate-limiting-001",
            incident_class="rate-limiting",
            title="Search clients receiving 429s after limit rule change",
            description=(
                f"Alert: {svc} is returning 429 Too Many Requests to 30% of "
                "clients since t+6m; backend load is low. Diagnose and recommend "
                "remediation."
            ),
            affected_service=svc,
            health={
                svc: {"api": "degraded", "index": "healthy"},
                "lynx-gateway": {"edge": "healthy"},
            },
            metrics={
                "http_429_rate_pct": {"unit": "percent", "points": _metric_series(0.1, 29.9, 6)},
                "backend_cpu_pct": {"unit": "percent", "points": _metric_series(31, -6, 6)},
            },
            logs=[
                {
                    "t_offset_s": 380,
                    "host": _host("lynx-gateway", 2),
                    "level": "WARN",
                    "message": (
                        "rate-limit rule rl-77 active: limit 50 req/min per client "
                        "(previous 500 req/min) from config release cfg-6201"
                    ),
                },
                *_noise_logs(svc, 0, 20),
            ],
            runbooks={
                svc: {
                    "title": "marmot-search throttling runbook",
                    "steps": [
                        "Compare active rate-limit rules with their previous values.",
                        "Revert a mis-scoped limit rule before scaling backends.",
                    ],
                    "remediation_ids": [
                        "rollback-config-release-cfg-6201",
                        "scale-out-marmot-search-backends",
                    ],
                }
            },
            recent_changes=[
                {
                    "change_id": "cfg-6201",
                    "t_offset_s": 300,
                    "kind": "config-release",
                    "service": "lynx-gateway",
                    "summary": "Rate-limit rule rl-77 tightened 10x (intended for one abuser)",
                },
            ],
            root_cause_id="misconfigured-rate-limit-cfg-6201",
            root_cause_summary=(
                "Config release cfg-6201 tightened rate-limit rule rl-77 tenfold for "
                "all clients instead of one abuser, causing widespread 429s."
            ),
            root_cause_keywords=("rate-limit", "cfg-6201"),
            accepted_remediations=("rollback-config-release-cfg-6201",),
            distractor_remediations=(
                "scale-out-marmot-search-backends",
                "restart-lynx-gateway-edge",
            ),
            required_evidence=("query_metrics", "search_logs", "check_recent_changes"),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "query_metrics", "arguments": {"metric": "http_429_rate_pct"}},
                {"tool": "search_logs", "arguments": {"query": "rate-limit"}},
                {"tool": "check_recent_changes", "arguments": {}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
        )
    )

    # ----------------------------------------------------------------- 10
    svc = "badger-queue"
    scenarios.append(
        Scenario(
            scenario_id="capacity-exhaustion-001",
            incident_class="capacity-exhaustion",
            title="Queue consumer lag from exhausted autoscaler ceiling",
            description=(
                f"Alert: {svc} consumer lag has grown for 40 minutes and is now "
                "22 minutes of backlog. Consumers are all healthy. Diagnose and "
                "recommend remediation."
            ),
            affected_service=svc,
            health={
                svc: {"broker": "healthy", "consumers": "healthy", "autoscaler": "at-ceiling"},
                "ibis-notify": {"api": "healthy"},
            },
            metrics={
                "consumer_lag_s": {"unit": "seconds", "points": _metric_series(20, 1300, 5)},
                "consumer_replicas": {"unit": "count", "points": _metric_series(24, 0, 5)},
                "ingest_rate_msgs_s": {"unit": "msgs/s", "points": _metric_series(900, 850, 5)},
            },
            logs=[
                {
                    "t_offset_s": 300,
                    "host": _host(svc, 1),
                    "level": "WARN",
                    "message": (
                        "autoscaler: desired replicas 40 exceeds ceiling max_replicas=24; "
                        "scaling capped since t+5m"
                    ),
                },
                *_noise_logs(svc, 0, 20),
            ],
            runbooks={
                svc: {
                    "title": "badger-queue capacity runbook",
                    "steps": [
                        "Compare ingest rate with consumer throughput and replica ceiling.",
                        "Raise the autoscaler ceiling with owner approval; never purge "
                        "the queue to hide lag.",
                    ],
                    "remediation_ids": [
                        "raise-badger-queue-autoscaler-ceiling",
                        "purge-badger-queue-backlog",
                    ],
                }
            },
            recent_changes=[
                {
                    "change_id": "traffic-note-12",
                    "t_offset_s": 0,
                    "kind": "traffic-event",
                    "service": svc,
                    "summary": "Sustained ingest growth began (approximately 2x baseline)",
                },
            ],
            root_cause_id="autoscaler-ceiling-exhausted",
            root_cause_summary=(
                "Sustained 2x ingest growth pushed desired consumer replicas past the "
                "autoscaler ceiling (max_replicas=24), so consumption capacity is "
                "exhausted and lag grows."
            ),
            root_cause_keywords=("autoscaler", "ceiling"),
            accepted_remediations=("raise-badger-queue-autoscaler-ceiling",),
            distractor_remediations=("purge-badger-queue-backlog", "restart-badger-queue-broker"),
            required_evidence=("get_service_health", "query_metrics", "search_logs"),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "query_metrics", "arguments": {"metric": "consumer_lag_s"}},
                {"tool": "search_logs", "arguments": {"query": "autoscaler"}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
        )
    )

    return tuple(scenarios)


_CATALOG: tuple[Scenario, ...] = _build_scenarios()


def catalog() -> dict[str, Scenario]:
    """The full scenario catalog keyed by scenario id (deterministic order)."""
    return {s.scenario_id: s for s in _CATALOG}


def canonical_catalog_json() -> str:
    """Canonical JSON serialization of the catalog (sorted keys, no whitespace
    variance) used for content addressing and safety scanning."""
    payload = {
        "workload_name": WORKLOAD_NAME,
        "workload_version": WORKLOAD_VERSION,
        "scenarios": [asdict(s) for s in _CATALOG],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def catalog_digest() -> str:
    """``sha256:<hex>`` digest of the canonical catalog JSON.

    Recorded in run manifests as the workload artifact hash so every result is
    attributable to an exact, content-addressed scenario definition.
    """
    digest = hashlib.sha256(canonical_catalog_json().encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
