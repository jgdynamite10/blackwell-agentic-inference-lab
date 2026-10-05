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
| `search_logs()` | Returns synthetic log lines whose message contains the query as a literal substring, seeded per scenario. Workload 2.4.0 requires gathering log evidence for log-dependent incidents. Workload 2.4.1 does not change this tool. |
| `retrieve_runbook()` | Looks up a published runbook by **service/system key**. A hit (`found=true`) returns `runbook.remediation_ids` for that service; `found=false` means the key did not identify a published runbook. |
| `check_recent_changes()` | Returns synthetic deploy/config-change events |
| `recommend_remediation()` | Terminal action: the agent submits `diagnosis_id`, `rationale`, and a `remediation_id` returned by a prior successful `retrieve_runbook`. Workload 2.5.0 adds `evidence_refs` (see below). |

The terminal tool takes structured arguments: the agent submits an exact
`diagnosis_id` (valid candidate ids are published through the task prompt and
tool evidence — e.g. `retrieve_runbook` returns the scenario's candidate
list — so the model selects among candidates rather than guessing a hidden
string), a free-text `rationale` (diagnostic only, never a success gate), and
a `remediation_id` sourced from `runbook.remediation_ids`. Valid
remediation IDs are **not** placed in the task prompt (decision D-0019).
Workload **2.4.0** is the qualification tool-contract correction; the
scenario catalog, accepted answers, evidence IDs, and evaluator 3.1.0
remain unchanged. Workload **2.4.1** is derived from 2.4.0 and changes
only the system-prompt evidence-collection instructions. It states that
`search_logs` uses literal substring matching rather than semantic
search; that queries should use exact identifiers, service names,
configuration IDs, job IDs, or diagnostic terms supported by information
already available to the agent; that a zero-match search must be retried
with a different specific token before a terminal recommendation; that
the agent must gather direct supporting evidence for its diagnosis before
that recommendation; and that a plausible change record or runbook
remediation is not a substitute for the required incident evidence.
Tool schemas stay the 2.4.0 text. This records a clarification of the
evidence-acquisition procedure. It is not a claim that qualification
quality improved. The prompt names no scenario, accepted answer, or log
line. Candidate **P1** is the authorized `qualify-agent` binding for
that prompt: workload 2.4.1 at temperature 0.2, with every other
generation, model, and serving pin identical to C2. C1 and C2 remain
workload 2.4.0. Development, holdout, and freeze each keep a separate
digest-bearing approval, and development remains the first required
gate. This does not claim that qualification quality improved.

### Workload 2.5.0 — evidence-grounding controller

Workload **2.5.0** is derived from 2.4.1 and binds the
provenance-checking controller **`evidence-grounding-v1`**
(`src/blackwell_lab/workload/evidence.py`; decision D-0022). The
scenario catalog, accepted answers, evidence predicates, evaluator 3.1.0,
thresholds, tool behavior, tool latencies, and the turn budget are
unchanged. Workload 2.5.0 **executes the 2.4.1 system prompt
byte-for-byte** (SHA-256
`37b3a4fb615dc21c8d39a5301dc4318870fea3498fbed196c50bfcbe67de1bd3`) and
the **2.4.0 tool-description prose unchanged** (the same text 2.4.1 uses).
No model-visible instruction is added: `evidence_refs` exists only in the
version-bound native-tool JSON argument schema of `recommend_remediation`,
and the controller `evidence-grounding-v1` is the **only** experimental
behavioral treatment. P2 therefore differs from P1 in `candidate_id`,
`workload_version`, and `controller` only.

Contract changes visible to the agent:

- Every non-terminal tool result carries an opaque **`observation_id`**
  (`obs-` plus 24 hex characters derived from a per-task-execution secret
  context and the observation sequence). IDs are valid only within the
  task execution that produced them; every task, repetition, and concurrent
  slot has its own controller state, so an ID from any other execution
  never resolves.
- `recommend_remediation` takes **`evidence_refs`**, a list of
  `observation_id` strings. It is advertised as required in the OpenAI
  function JSON schema (the only schema difference from 2.4.1; the
  description prose is identical); its absence is adjudicated by the
  controller, not by argument validation. On workloads 2.3.0–2.4.1 the
  argument is unknown and remains `invalid_tool_arguments`.
- Eligible (citable) observations are **successful, nonempty diagnostic
  results**: `search_logs` with integer `total_matches > 0` and at least
  one line carrying a string `message`; `get_service_health` with at least
  one service whose components are a nonempty string→string map with no
  `unknown-service`, `unknown`, or empty status; `query_metrics` with
  `found: true`, a nonempty metric name, and at least one numeric data
  point. Ineligible: zero-match searches, `found: false` lookups, unknown
  or unusable health, malformed results, `retrieve_runbook` and
  `check_recent_changes` (guidance only, never sufficient alone), and any
  fabricated, stale, cross-task, cross-repetition, or cross-concurrency ID.
- A terminal attempt is **accepted** only if `evidence_refs` is a nonempty
  list of strings each resolving to an eligible observation recorded
  earlier in the same task execution. Otherwise the tool returns only the
  generic payload `{"accepted": false, "failure_category":
  "direct_evidence_required"}`; the task continues within its **unchanged**
  `max_turns` budget (no added turns, retries, or hidden inference). If the
  budget is exhausted after at least one rejection the task errors as
  `direct_evidence_required`; a task that never attempts the terminal tool
  stays `no_terminal_recommendation`.
- The controller performs **provenance and structural validation only**.
  It never reads scenario identifiers, expected queries, accepted answers,
  or evaluator predicates: a structurally valid but irrelevant observation
  (for example the health of an unrelated healthy service) passes the
  controller and fails evaluator 3.1.0 exactly as before.
- Every turn, tool call, rejected attempt, token usage, latency, and failure
  is still recorded. Task observations gain an `evidence_grounding` summary
  (controller name, observation counts, terminal attempts, rejected
  attempts, accepted reference count) and run manifests record
  `workload.controller`. No prompt, completion, reasoning text, or raw
  observation payload is persisted by the controller.
- Workload/controller bindings are closed (`WORKLOAD_CONTROLLERS`): 2.3.0,
  2.4.0, and 2.4.1 bind no controller; 2.5.0 binds `evidence-grounding-v1`.
  Any other combination fails closed before a model client is constructed.

Candidate **P2** is the authorized `qualify-agent` binding for this
workload: workload 2.5.0, controller `evidence-grounding-v1`, temperature
0.2, with every other generation, model, serving, and schedule pin
identical to C2. C1, C2, and P1 serialization and identity digests are
unchanged. This records a controller addition; it is not a claim that
qualification quality improved.

Candidate **P2C** (decision D-0026) is the controlled public-catalog
version of P2: the same workload 2.5.0, controller, prompt bytes,
temperature 0.2, and pins, executed on exactly the D-0019 catalog schedule
that P1 executes (development, holdout, and freeze). P2C never binds a
`sealed_set` and never reads custody; P1 is its control, and the P1/P2C
pair isolates the effect of `evidence-grounding-v1`. P2C provides no
blind-generalization evidence, and its catalog scores are not comparable
with private sealed-set scores. P2 remains the sealed candidate.

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
(`src/blackwell_lab/workload/scenarios.py`, catalog version 2.3.0;
qualification tool-contract revision 2.4.0; evidence-acquisition
prompt clarification 2.4.1; evidence-grounding controller binding 2.5.0,
neither of which changes catalog content)
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
