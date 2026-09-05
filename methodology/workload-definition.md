# Workload Definition — Synthetic Cloud Operations Agent

The benchmark workload is a **synthetic Cloud Operations Agent**. It must not
connect to production Akamai systems or use customer information. All data the
agent sees is generated fixture data.

## Agent

The agent receives an incident description and must diagnose it and recommend
remediation using only its simulated tools:

| Tool | Simulated behavior |
| --- | --- |
| `get_service_health()` | Returns synthetic service/component health states for the scenario |
| `query_metrics()` | Returns synthetic time-series slices (latency, error rate, saturation) |
| `search_logs()` | Returns synthetic log lines matching a query, seeded per scenario |
| `retrieve_runbook()` | Returns the synthetic runbook entry for a service or symptom |
| `check_recent_changes()` | Returns synthetic deploy/config-change events |
| `recommend_remediation()` | Terminal action: the agent submits its remediation recommendation |

Tool responses are deterministic functions of (scenario, query). Tool
latencies are simulated with fixed, documented values so tool-execution time
is separable from model-serving time (measurement contract §2).

## Incident catalog

Deterministic incident definitions cover at least these condition classes:

1. Elevated latency
2. Pod failures
3. Memory pressure
4. GPU saturation
5. Storage latency
6. Failed deployments
7. Unhealthy upstream services
8. DNS failures
9. Rate limiting
10. Capacity exhaustion

Each incident definition specifies: the fixture data every tool returns, the
ground-truth root cause, the accepted remediation set, distractor signals, and
the success criteria the evaluator applies. Scenarios are versioned; the
workload version appears in every run manifest.

## Determinism policy

Scenarios are deterministic; **model outputs are not assumed to be**.
Experiments therefore use fixed generation settings, record seeds when the
serving engine supports them, and include five measured repetitions per cell
(measurement contract §5–6).

## Workload profiles

Two profiles are used in the baseline (their exact parameterization is frozen
in Phase 2 before any measurement):

| Profile | Intent | Shape |
| --- | --- | --- |
| **Interactive** | An on-call engineer working one incident with the agent | Tasks issued one at a time per agent slot; shorter contexts; latency-sensitive SLO (tight TTFT and task-time targets) |
| **Batch-heavy** | Automated triage sweep across many incidents | Task queue kept full per concurrency slot; longer contexts (more log/metric data per task); throughput-oriented SLO |

Both profiles draw from the same incident catalog so success criteria are
identical; they differ in arrival pattern, context size, and SLO targets.

## Concurrency

Concurrency levels 1, 4, and 8 denote simultaneous in-flight agent tasks
against the single serving endpoint. The driver enforces the level exactly;
arrival behavior within a level is defined per profile.

## Data safety

- All fixtures are generated, reviewed synthetic data: fictional service
  names, RFC 5737/3849 documentation IP ranges, invented hostnames.
- No fixture may embed real hostnames, real IPs, account identifiers,
  customer data, or copied production logs.
- Fixture generators and their seeds live in this repository (Phase 2) so the
  workload is fully reproducible.
