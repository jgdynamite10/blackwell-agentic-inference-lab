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
| `recommend_remediation()` | Terminal action: the agent submits `diagnosis_id`, `rationale`, and `remediation_id` |

The terminal tool takes structured arguments: the agent submits an exact
`diagnosis_id` (valid candidate ids are published through the task prompt and
tool evidence — e.g. `retrieve_runbook` returns the scenario's candidate
list — so the model selects among candidates rather than guessing a hidden
string), a free-text `rationale` (diagnostic only, never a success gate), and
a `remediation_id`.

Each turn must produce exactly one native OpenAI-compatible function call.
The six `TOOL_SPECS` contracts are projected onto deterministic OpenAI
function definitions (`tools` on every chat-completions request;
`tool_choice: "auto"`; `parallel_tool_calls: false`). Streamed
`delta.tool_calls` fragments are assembled by index. Tool results return as
`role=tool` messages with the matching `tool_call_id`. The retired textual
`TOOL_CALL:` protocol is never accepted (decision D-0016).

Tool arguments are strictly validated (`invalid_tool_arguments` on any
violation): required strings must be non-empty, and every integer argument
(`search_logs.limit`, `query_metrics.window_s`,
`check_recent_changes.window_s`) must be a **positive integer** — booleans,
zero, and negative values are rejected.

Tool responses are deterministic functions of (scenario, query). Tool
latencies are simulated with fixed, documented values so tool-execution time
is separable from model-serving time (measurement contract §2). The fixed
values (implemented in `src/blackwell_lab/workload/tools.py`) are **consumed
through the injectable monotonic clock**: they occupy task duration, timeout
budget, request pacing, and cell wall time, and are recorded per invocation
in the tool trace:

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
ground-truth root cause with a stable **diagnosis id**, the **accepted and
distractor diagnosis ids** (published to the agent as candidates), the
accepted and distractor remediation sets, and **machine-checkable evidence
predicates** — each with the relevant tool, argument constraints, and an
explicit **typed result constraint** evaluated against the tool's structured
response fields (e.g. `found is true`, `total_matches > 0`, a returned log
line's message containing the required value, a returned change's
`change_id` matching exactly, a found runbook's remediation list containing
the exact remediation, a found metric with the exact name and non-empty
points). Result constraints are never evaluated against a serialization of
the whole response, so echoed request arguments, `available` listings,
unknown-resource responses, and `found: false` responses can never satisfy
evidence. Predicates declare permitted **alternative evidence paths** (a
reference and an alternative tool sequence both satisfy every predicate by
construction, and tests prove it). Scenarios are versioned; the workload
version appears in every run manifest. The Phase 2 catalog
(`src/blackwell_lab/workload/scenarios.py`, workload version 2.3.0)
implements one scenario per class and is additionally **content-addressed**:
the SHA-256 digest of the canonical catalog JSON is recorded in every run
manifest under `workload.catalog_digest` (never as a model artifact hash).

## Task instances and sample plan

Each measured repetition contains **200 seeded task instances**
(decision D-0010), deterministically balanced across the ten templates
(20 per template). An instance is a prompt-surface variant (synthetic
tracking id, report offset) of its template with a recorded instance seed;
variants never change the ground truth or the evidence predicates.
Byte-identical repetitions are never represented as independent quality
cases: every repetition uses a distinct seed, and results record the honest
identity counts (unique templates, unique instances, total attempts).

## Determinism policy

Scenarios are deterministic; **model outputs are not assumed to be**.
Experiments therefore use fixed generation settings, record seeds when the
serving engine supports them, and include five measured repetitions per cell
(measurement contract §5–6).

## Workload profiles

Two profiles are used in the baseline:

| Profile | Intent | Shape |
| --- | --- | --- |
| **Interactive** | An on-call engineer working one incident with the agent | Shorter contexts; smaller output budget; latency-sensitive SLO (tight TTFT and task-time targets) |
| **Batch-heavy** | Automated triage sweep across many incidents | Longer contexts (more log/metric data per task); larger output budget; throughput-oriented SLO |

Both profiles draw from the same incident catalog so success criteria are
identical. Phase 2 implements **one truthful bounded closed-loop scheduler**
for both profiles (decision D-0010); the profiles are differentiated by
context/input size, output budget, timeout, and SLO — **not** by arrival
algorithms that are not measurably implemented.

The exact parameterization (implemented in
`src/blackwell_lab/workload/runner.py`; SLO targets and timeouts are
**owner-approved**, decision D-0010):

| Parameter | Interactive | Batch-heavy |
| --- | --- | --- |
| Scheduler | Bounded closed-loop (shared) | Bounded closed-loop (shared) |
| Log-context limit (`search_logs`) | 10 lines | 50 lines |
| Metric window (`query_metrics`) | 900 s | 3,600 s |
| `max_tokens` per turn | 1,024 | 4,096 |
| Per-task timeout | 120,000 ms | 600,000 ms |
| Task-latency SLO (T_task) | 60,000 ms | 300,000 ms |
| TTFT SLO per turn (T_ttft) | 2,500 ms | none (throughput-oriented) |

The context-size difference is measurable and tested: the same log query
returns strictly more log context under the batch-heavy limit than under the
interactive limit.

## Concurrency

Concurrency levels 1, 4, and 8 denote requested simultaneous in-flight agent
tasks against the single serving endpoint. The scheduler is a **bounded
closed loop**: at most the requested number of slots exists, and a worker
claims the next task — stamping its submission at that instant — only when a
slot becomes available (no pre-submitted unbounded backlog; a task that has
not entered a slot consumes none of its timeout budget). In-flight work is
**not claimed to be exactly enforced** during ramp-up and drain. Every
result records the requested concurrency, the achieved maximum concurrency,
and the measured mean in-flight work; a configuration with fewer tasks than
the requested concurrency is rejected.

## Data safety

- All fixtures are generated, reviewed synthetic data: fictional service
  names, RFC 5737/3849 documentation IP ranges, invented hostnames.
- No fixture may embed real hostnames, real IPs, account identifiers,
  customer data, or copied production logs.
- Fixture generators and their seeds live in this repository (Phase 2) so the
  workload is fully reproducible.
