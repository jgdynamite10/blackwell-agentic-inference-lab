"""Deterministic, versioned incident-scenario catalog.

Every scenario is a pure data definition: the fixture data each simulated
tool returns, the ground-truth diagnosis (by **id**, not keyword), the
accepted remediation set, distractor diagnoses/remediations, and the
machine-checkable **evidence predicates** the evaluator applies. Scenarios
contain **only synthetic data**: fictional service names, hostnames under the
reserved ``.example`` TLD (RFC 2606), and IP addresses from the RFC 5737
documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24).
Nothing here references a real system, account, or provider.

Success criteria (evaluator v3, decision D-0010) are structured, not
keyword-substring based:

- the agent must submit an exact ``diagnosis_id`` drawn from the candidate
  list surfaced in the task prompt and runbook fixtures (no hidden strings);
- the agent must submit an accepted ``remediation_id``;
- every mandatory :class:`EvidencePredicate` must be satisfied by the task's
  recorded tool trace. A predicate is satisfied by **any one** of its
  alternatives (permitted alternative evidence paths), each of which
  constrains the tool name, relevant arguments, and — through an explicit
  **typed result constraint** — the tool's structured response fields.
  Result constraints are never evaluated against a serialization of the
  whole response, so echoed request arguments, ``available`` listings,
  unknown-resource responses, and ``found: false`` responses can never
  satisfy evidence.

The catalog is versioned via ``WORKLOAD_VERSION`` and content-addressed via
``catalog_digest()``; both appear in every run manifest so results are
attributable to an exact workload definition.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

WORKLOAD_NAME = "cloud-ops-agent"
WORKLOAD_VERSION = "2.2.0"

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
class LogLineContains:
    """Typed result constraint for ``search_logs``.

    Satisfied only when the search actually matched (``total_matches > 0``)
    and some **returned log line's** ``message`` contains ``text``
    (case-insensitively). The echoed ``query`` field is never inspected, so
    injecting expected text into the query cannot satisfy evidence, and a
    zero-match response never satisfies evidence.
    """

    text: str
    kind: str = "log_line_contains"


@dataclass(frozen=True)
class ChangeIdEquals:
    """Typed result constraint for ``check_recent_changes``.

    Satisfied only when a **returned change's** ``change_id`` equals
    ``change_id`` exactly (no substring matching).
    """

    change_id: str
    kind: str = "change_id_equals"


@dataclass(frozen=True)
class RunbookHasRemediation:
    """Typed result constraint for ``retrieve_runbook``.

    Satisfied only when the runbook was **found** (``found is true``) and its
    ``remediation_ids`` list contains ``remediation_id`` exactly. Unknown-key
    responses (``found: false``), their ``available`` listings, and the
    echoed ``key`` argument never satisfy evidence.
    """

    remediation_id: str
    kind: str = "runbook_has_remediation"


@dataclass(frozen=True)
class MetricAvailable:
    """Typed result constraint for ``query_metrics``.

    Satisfied only when the metric was **found** (``found is true``), the
    returned metric name equals ``metric`` exactly, and the returned points
    are non-empty. Metric names echoed back or listed under ``available`` in
    a not-found response never satisfy evidence.
    """

    metric: str
    kind: str = "metric_available"


@dataclass(frozen=True)
class HealthComponentStatus:
    """Typed result constraint for ``get_service_health``.

    Satisfied only when some **returned service** reports component
    ``component`` with exactly ``status``.
    """

    component: str
    status: str
    kind: str = "health_component_status"


#: The explicit typed result constraints an alternative may declare.
ResultConstraint = (
    LogLineContains
    | ChangeIdEquals
    | RunbookHasRemediation
    | MetricAvailable
    | HealthComponentStatus
)


@dataclass(frozen=True)
class EvidenceAlternative:
    """One permitted way to satisfy an evidence predicate.

    A recorded tool-trace entry matches this alternative when:

    - its tool name equals ``tool``;
    - for every ``(argument, substring)`` pair in ``argument_contains``, the
      trace entry's validated argument value contains the substring
      (case-insensitively);
    - its returned payload satisfies the explicit typed ``result``
      constraint, which is evaluated against the tool's structured response
      fields — never against a serialization of the whole response.

    Constraining both arguments and results means irrelevant queries (right
    tool, wrong question), echoed request arguments, ``available`` listings,
    and ``found: false`` responses cannot satisfy evidence.
    """

    tool: str
    result: ResultConstraint
    argument_contains: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class EvidencePredicate:
    """One mandatory, machine-checkable evidence requirement.

    The predicate is satisfied when **any one** alternative matches at least
    one entry of the task's tool trace (permitted alternative evidence
    paths). All of a scenario's predicates are mandatory gates.
    """

    predicate_id: str
    description: str
    alternatives: tuple[EvidenceAlternative, ...]


@dataclass(frozen=True)
class Scenario:
    """One deterministic incident definition.

    ``accepted_diagnoses`` is the exact-match diagnosis gate;
    ``distractor_diagnoses`` are plausible-but-wrong diagnosis ids. Both are
    surfaced to the agent (task prompt candidate list and runbook fixtures) so
    the model is never asked to guess a hidden string.
    ``accepted_remediations`` is the accepted set; ``distractor_remediations``
    are plausible-but-wrong actions that also appear in runbook fixtures.
    ``evidence_predicates`` are the mandatory machine-checkable evidence gates
    evaluated against the recorded tool trace.
    ``reference_tool_sequence`` is the canonical evidence path; the
    ``alternative_tool_sequence`` exercises each predicate's permitted
    alternative path and must also satisfy every predicate.
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
    accepted_diagnoses: tuple[str, ...] = ()
    distractor_diagnoses: tuple[str, ...] = ()
    accepted_remediations: tuple[str, ...] = ()
    distractor_remediations: tuple[str, ...] = ()
    evidence_predicates: tuple[EvidencePredicate, ...] = ()
    reference_tool_sequence: tuple[dict, ...] = ()
    alternative_tool_sequence: tuple[dict, ...] = ()

    @property
    def candidate_diagnoses(self) -> tuple[str, ...]:
        """All diagnosis ids surfaced to the agent, in sorted order so the
        accepted answer's position leaks nothing."""
        return tuple(sorted({*self.accepted_diagnoses, *self.distractor_diagnoses}))


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
            accepted_diagnoses=("synchronous-audit-logging-cfg-2041",),
            distractor_diagnoses=("upstream-dependency-slowdown", "traffic-spike-overload"),
            accepted_remediations=("rollback-config-release-cfg-2041",),
            distractor_remediations=("scale-out-zephyr-cart-api", "restart-zephyr-cart-pods"),
            evidence_predicates=(
                EvidencePredicate(
                    predicate_id="log-evidence-cfg-2041",
                    description=(
                        "Log search surfaced the synchronous-audit-logging line "
                        "attributing the regression to cfg-2041."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("cfg-2041"),
                            argument_contains=(("query", "audit"),),
                        ),
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("cfg-2041"),
                            argument_contains=(("query", "cfg-2041"),),
                        ),
                    ),
                ),
                EvidencePredicate(
                    predicate_id="change-correlation-cfg-2041",
                    description=(
                        "The cfg-2041 config release was correlated via the change "
                        "feed or the service runbook."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="check_recent_changes",
                            result=ChangeIdEquals("cfg-2041"),
                        ),
                        EvidenceAlternative(
                            tool="retrieve_runbook",
                            result=RunbookHasRemediation("rollback-config-release-cfg-2041"),
                            argument_contains=(("key", "zephyr-cart"),),
                        ),
                    ),
                ),
            ),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "query_metrics", "arguments": {"metric": "latency_p99_ms"}},
                {"tool": "search_logs", "arguments": {"query": "audit"}},
                {"tool": "check_recent_changes", "arguments": {}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
            alternative_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "search_logs", "arguments": {"query": "cfg-2041"}},
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
            accepted_diagnoses=("missing-env-var-dep-8102",),
            distractor_diagnoses=("upstream-ledger-outage", "node-resource-exhaustion"),
            accepted_remediations=("rollback-deploy-dep-8102",),
            distractor_remediations=(
                "increase-quokka-payments-replicas",
                "restart-quokka-payments-pods",
            ),
            evidence_predicates=(
                EvidencePredicate(
                    predicate_id="startup-log-evidence-dep-8102",
                    description=(
                        "Log search surfaced the startup failure naming the missing "
                        "variable introduced in dep-8102."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("dep-8102"),
                            argument_contains=(("query", "startup"),),
                        ),
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("dep-8102"),
                            argument_contains=(("query", "dep-8102"),),
                        ),
                    ),
                ),
                EvidencePredicate(
                    predicate_id="change-correlation-dep-8102",
                    description=(
                        "The dep-8102 release was correlated via the change feed or "
                        "the service runbook."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="check_recent_changes",
                            result=ChangeIdEquals("dep-8102"),
                        ),
                        EvidenceAlternative(
                            tool="retrieve_runbook",
                            result=RunbookHasRemediation("rollback-deploy-dep-8102"),
                            argument_contains=(("key", "quokka-payments"),),
                        ),
                    ),
                ),
            ),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "search_logs", "arguments": {"query": "startup failed"}},
                {"tool": "check_recent_changes", "arguments": {}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
            alternative_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "search_logs", "arguments": {"query": "dep-8102"}},
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
            accepted_diagnoses=("unbounded-cache-cfg-3300",),
            distractor_diagnoses=("memory-leak-in-worker", "traffic-growth-capacity"),
            accepted_remediations=("rollback-config-release-cfg-3300",),
            distractor_remediations=(
                "add-memory-to-otter-inventory-nodes",
                "restart-otter-inventory-workers",
            ),
            evidence_predicates=(
                EvidencePredicate(
                    predicate_id="cache-log-evidence-cfg-3300",
                    description=(
                        "Log search surfaced the disabled-eviction line attributing "
                        "unbounded cache growth to cfg-3300."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("cfg-3300"),
                            argument_contains=(("query", "cache"),),
                        ),
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("cfg-3300"),
                            argument_contains=(("query", "cfg-3300"),),
                        ),
                    ),
                ),
                EvidencePredicate(
                    predicate_id="change-correlation-cfg-3300",
                    description=(
                        "The cfg-3300 config release was correlated via the change "
                        "feed or the service runbook."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="check_recent_changes",
                            result=ChangeIdEquals("cfg-3300"),
                        ),
                        EvidenceAlternative(
                            tool="retrieve_runbook",
                            result=RunbookHasRemediation("rollback-config-release-cfg-3300"),
                            argument_contains=(("key", "otter-inventory"),),
                        ),
                    ),
                ),
            ),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "query_metrics", "arguments": {"metric": "memory_working_set_gib"}},
                {"tool": "search_logs", "arguments": {"query": "cache"}},
                {"tool": "check_recent_changes", "arguments": {}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
            alternative_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "search_logs", "arguments": {"query": "cfg-3300"}},
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
            accepted_diagnoses=("batch-job-gpu-contention",),
            distractor_diagnoses=("model-regression-slow-kernels", "traffic-spike-overload"),
            accepted_remediations=("throttle-batch-job-embed-refresh-44",),
            distractor_remediations=(
                "restart-lynx-inference-serving",
                "scale-out-lynx-inference",
            ),
            evidence_predicates=(
                EvidencePredicate(
                    predicate_id="batch-admission-evidence",
                    description=(
                        "Log search surfaced the scheduler line admitting "
                        "embed-refresh-44 to the online GPU pool."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("embed-refresh-44"),
                            argument_contains=(("query", "batch"),),
                        ),
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("embed-refresh-44"),
                            argument_contains=(("query", "embed-refresh-44"),),
                        ),
                    ),
                ),
                EvidencePredicate(
                    predicate_id="contention-signal",
                    description=(
                        "The batch-job start was correlated via the change feed, or "
                        "queue-depth metrics showed the batch backlog."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="check_recent_changes",
                            result=ChangeIdEquals("job-embed-refresh-44"),
                        ),
                        EvidenceAlternative(
                            tool="query_metrics",
                            result=MetricAvailable("batch_queue_depth"),
                            argument_contains=(("metric", "batch_queue_depth"),),
                        ),
                    ),
                ),
            ),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "query_metrics", "arguments": {"metric": "gpu_util_pct"}},
                {"tool": "search_logs", "arguments": {"query": "batch"}},
                {"tool": "check_recent_changes", "arguments": {}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
            alternative_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "query_metrics", "arguments": {"metric": "batch_queue_depth"}},
                {"tool": "search_logs", "arguments": {"query": "embed-refresh-44"}},
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
            accepted_diagnoses=("degraded-volume-7",),
            distractor_diagnoses=("cpu-saturation-primary", "network-partition-replica"),
            accepted_remediations=("failover-heron-metadata-to-replica",),
            distractor_remediations=(
                "increase-heron-metadata-cpu",
                "restart-heron-metadata-primary",
            ),
            evidence_predicates=(
                EvidencePredicate(
                    predicate_id="volume-latency-evidence",
                    description=(
                        "The degraded volume was evidenced by the storage-layer log "
                        "line or by elevated io-wait metrics."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("media latency"),
                            argument_contains=(("query", "volume-7"),),
                        ),
                        EvidenceAlternative(
                            tool="query_metrics",
                            result=MetricAvailable("disk_io_wait_pct"),
                            argument_contains=(("metric", "disk_io_wait"),),
                        ),
                    ),
                ),
                EvidencePredicate(
                    predicate_id="volume-context",
                    description=(
                        "The degraded volume-7 component was seen in service health, "
                        "or the failover runbook was consulted."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="get_service_health",
                            result=HealthComponentStatus("volume-7", "degraded"),
                        ),
                        EvidenceAlternative(
                            tool="retrieve_runbook",
                            result=RunbookHasRemediation("failover-heron-metadata-to-replica"),
                            argument_contains=(("key", "heron-metadata"),),
                        ),
                    ),
                ),
            ),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "query_metrics", "arguments": {"metric": "disk_io_wait_pct"}},
                {"tool": "search_logs", "arguments": {"query": "volume-7"}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
            alternative_tool_sequence=(
                {"tool": "query_metrics", "arguments": {"metric": "disk_io_wait_pct"}},
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
            accepted_diagnoses=("missing-migration-dep-9004",),
            distractor_diagnoses=("bad-image-artifact", "readiness-probe-misconfigured"),
            accepted_remediations=("rollback-deploy-dep-9004",),
            distractor_remediations=("delete-ibis-notify-pods", "restart-ibis-notify-api"),
            evidence_predicates=(
                EvidencePredicate(
                    predicate_id="readiness-log-evidence-dep-9004",
                    description=(
                        "Log search surfaced the readiness failure naming the schema "
                        "version required by dep-9004."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("schema_version"),
                            argument_contains=(("query", "readiness"),),
                        ),
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("dep-9004"),
                            argument_contains=(("query", "migration"),),
                        ),
                    ),
                ),
                EvidencePredicate(
                    predicate_id="change-correlation-dep-9004",
                    description=(
                        "The dep-9004 release was correlated via the change feed or "
                        "the service runbook."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="check_recent_changes",
                            result=ChangeIdEquals("dep-9004"),
                        ),
                        EvidenceAlternative(
                            tool="retrieve_runbook",
                            result=RunbookHasRemediation("rollback-deploy-dep-9004"),
                            argument_contains=(("key", "ibis-notify"),),
                        ),
                    ),
                ),
            ),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "search_logs", "arguments": {"query": "readiness"}},
                {"tool": "check_recent_changes", "arguments": {}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
            alternative_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "search_logs", "arguments": {"query": "migration"}},
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
            accepted_diagnoses=("unhealthy-upstream-heron-auth",),
            distractor_diagnoses=("frontend-bad-deploy", "edge-lb-misroute"),
            accepted_remediations=("failover-heron-auth-to-standby-pool",),
            distractor_remediations=(
                "restart-falcon-frontend-pods",
                "scale-out-falcon-frontend",
            ),
            evidence_predicates=(
                EvidencePredicate(
                    predicate_id="upstream-health-evidence",
                    description=(
                        "The unhealthy heron-auth upstream (token-signer) was seen in "
                        "cluster health or the timeout log line."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="get_service_health",
                            result=HealthComponentStatus("token-signer", "unhealthy"),
                        ),
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("timed out"),
                            argument_contains=(("query", "heron-auth"),),
                        ),
                    ),
                ),
                EvidencePredicate(
                    predicate_id="upstream-timeout-signal",
                    description=(
                        "Upstream auth timeouts were quantified via metrics, or the "
                        "heron-auth failover runbook was consulted."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="query_metrics",
                            result=MetricAvailable("upstream_auth_timeouts"),
                            argument_contains=(("metric", "upstream_auth_timeouts"),),
                        ),
                        EvidenceAlternative(
                            tool="retrieve_runbook",
                            result=RunbookHasRemediation("failover-heron-auth-to-standby-pool"),
                            argument_contains=(("key", "heron-auth"),),
                        ),
                    ),
                ),
            ),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {}},
                {"tool": "query_metrics", "arguments": {"metric": "upstream_auth_timeouts"}},
                {"tool": "search_logs", "arguments": {"query": "heron-auth"}},
                {"tool": "retrieve_runbook", "arguments": {"key": "heron-auth"}},
            ),
            alternative_tool_sequence=(
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
            accepted_diagnoses=("dns-search-domain-typo-cfg-5150",),
            distractor_diagnoses=("network-firewall-block", "badger-queue-outage"),
            accepted_remediations=("rollback-config-release-cfg-5150",),
            distractor_remediations=("restart-dns-cache-daemons", "restart-walrus-batch-workers"),
            evidence_predicates=(
                EvidencePredicate(
                    predicate_id="servfail-evidence-cfg-5150",
                    description=(
                        "Log search surfaced either the SERVFAIL lines naming the "
                        "misspelled search domain or the resolver config line."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("svc.1ab.example"),
                            argument_contains=(("query", "servfail"),),
                        ),
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("cfg-5150"),
                            argument_contains=(("query", "search domain"),),
                        ),
                    ),
                ),
                EvidencePredicate(
                    predicate_id="change-correlation-cfg-5150",
                    description=(
                        "The cfg-5150 resolver config release was correlated via the "
                        "change feed or the DNS runbook."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="check_recent_changes",
                            result=ChangeIdEquals("cfg-5150"),
                        ),
                        EvidenceAlternative(
                            tool="retrieve_runbook",
                            result=RunbookHasRemediation("rollback-config-release-cfg-5150"),
                            argument_contains=(("key", "dns-resolver"),),
                        ),
                    ),
                ),
            ),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {}},
                {"tool": "query_metrics", "arguments": {"metric": "dns_servfail_count"}},
                {"tool": "search_logs", "arguments": {"query": "SERVFAIL"}},
                {"tool": "check_recent_changes", "arguments": {}},
                {"tool": "retrieve_runbook", "arguments": {"key": "dns-resolver"}},
            ),
            alternative_tool_sequence=(
                {"tool": "get_service_health", "arguments": {}},
                {"tool": "search_logs", "arguments": {"query": "search domain"}},
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
            accepted_diagnoses=("misconfigured-rate-limit-cfg-6201",),
            distractor_diagnoses=("backend-capacity-shortfall", "client-abuse-wave"),
            accepted_remediations=("rollback-config-release-cfg-6201",),
            distractor_remediations=(
                "scale-out-marmot-search-backends",
                "restart-lynx-gateway-edge",
            ),
            evidence_predicates=(
                EvidencePredicate(
                    predicate_id="rate-limit-rule-evidence",
                    description=(
                        "Log search surfaced the rl-77 rule change attributing the "
                        "429s to cfg-6201."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("cfg-6201"),
                            argument_contains=(("query", "rate-limit"),),
                        ),
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("cfg-6201"),
                            argument_contains=(("query", "rl-77"),),
                        ),
                    ),
                ),
                EvidencePredicate(
                    predicate_id="throttle-signal",
                    description=(
                        "The cfg-6201 release was correlated via the change feed, or "
                        "the 429 rate was quantified via metrics."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="check_recent_changes",
                            result=ChangeIdEquals("cfg-6201"),
                        ),
                        EvidenceAlternative(
                            tool="query_metrics",
                            result=MetricAvailable("http_429_rate_pct"),
                            argument_contains=(("metric", "http_429"),),
                        ),
                    ),
                ),
            ),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "query_metrics", "arguments": {"metric": "http_429_rate_pct"}},
                {"tool": "search_logs", "arguments": {"query": "rate-limit"}},
                {"tool": "check_recent_changes", "arguments": {}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
            alternative_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "query_metrics", "arguments": {"metric": "http_429_rate_pct"}},
                {"tool": "search_logs", "arguments": {"query": "rl-77"}},
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
            accepted_diagnoses=("autoscaler-ceiling-exhausted",),
            distractor_diagnoses=("consumer-deadlock", "broker-disk-saturation"),
            accepted_remediations=("raise-badger-queue-autoscaler-ceiling",),
            distractor_remediations=("purge-badger-queue-backlog", "restart-badger-queue-broker"),
            evidence_predicates=(
                EvidencePredicate(
                    predicate_id="ceiling-evidence",
                    description=(
                        "The capped autoscaler was evidenced by the ceiling log line "
                        "or the at-ceiling health status."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="search_logs",
                            result=LogLineContains("max_replicas=24"),
                            argument_contains=(("query", "autoscaler"),),
                        ),
                        EvidenceAlternative(
                            tool="get_service_health",
                            result=HealthComponentStatus("autoscaler", "at-ceiling"),
                        ),
                    ),
                ),
                EvidencePredicate(
                    predicate_id="lag-signal",
                    description=(
                        "Consumer lag was quantified via metrics, or the capacity "
                        "runbook was consulted."
                    ),
                    alternatives=(
                        EvidenceAlternative(
                            tool="query_metrics",
                            result=MetricAvailable("consumer_lag_s"),
                            argument_contains=(("metric", "consumer_lag"),),
                        ),
                        EvidenceAlternative(
                            tool="retrieve_runbook",
                            result=RunbookHasRemediation("raise-badger-queue-autoscaler-ceiling"),
                            argument_contains=(("key", "badger-queue"),),
                        ),
                    ),
                ),
            ),
            reference_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
                {"tool": "query_metrics", "arguments": {"metric": "consumer_lag_s"}},
                {"tool": "search_logs", "arguments": {"query": "autoscaler"}},
                {"tool": "retrieve_runbook", "arguments": {"key": svc}},
            ),
            alternative_tool_sequence=(
                {"tool": "get_service_health", "arguments": {"service": svc}},
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
