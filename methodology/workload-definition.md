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
is separable from model-serving time (measurement contract §2). The fixed
values (implemented in `src/blackwell_lab/workload/tools.py`, recorded as
tool-execution time, never slept):

| Tool | Simulated latency |
| --- | --- |
| `get_service_health` | 5 ms |
| `query_metrics` | 20 ms |
| `search_logs` | 30 ms |
| `retrieve_runbook` | 10 ms |
| `check_recent_changes` | 15 ms |
| `recommend_remediation` | 5 ms |

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
workload version appears in every run manifest. The Phase 2 catalog
(`src/blackwell_lab/workload/scenarios.py`, workload version 2.0.0) implements
one scenario per class and is additionally **content-addressed**: the SHA-256
digest of the canonical catalog JSON is recorded in every run manifest.

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

The exact Phase 2 parameterization (implemented in
`src/blackwell_lab/workload/runner.py`; decision D-0009 — SLO targets are
**proposals pending owner approval**):

| Parameter | Interactive | Batch-heavy |
| --- | --- | --- |
| Arrival | Closed-loop: each slot issues its next task only after its previous task finishes | Queue-full: the task queue is kept full for every slot |
| Log-context limit (`search_logs`) | 10 lines | 50 lines |
| Metric window (`query_metrics`) | 900 s | 3,600 s |
| `max_tokens` per turn | 1,024 | 4,096 |
| Per-task timeout | 120,000 ms | 600,000 ms |
| Proposed task-latency SLO (T_task) | 60,000 ms | 300,000 ms |
| Proposed TTFT SLO per turn (T_ttft) | 2,500 ms | none (throughput-oriented) |

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
