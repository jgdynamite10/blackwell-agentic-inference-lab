# Measurement Contract

This contract defines what is measured, where every timing boundary lies, and
how repetitions, warm-up, errors, costs, energy, and success are treated. It
binds every phase. Revisions after any results exist require a
[decision-log](../docs/decision-log.md) entry.

## 1. Units and clocks

- All timestamps are recorded in UTC with monotonic-clock deltas for
  durations (wall-clock timestamps for correlation only).
- Durations are reported in milliseconds; energy in joules (derived from
  DCGM power samples); costs in USD.

## 2. Timing boundaries

A **task** is one incident scenario executed end-to-end by the agent. A task
comprises one or more **turns**; each turn contains one model request and any
resulting tool calls.

| Measure | Start boundary | End boundary |
| --- | --- | --- |
| End-to-end task completion time | Benchmark driver submits the task — the actual driver submission instant, stamped the moment a bounded-scheduler slot becomes available and the worker claims the task (a task that has not entered a slot has not been submitted) | Terminal record handed to the evaluator (immediately on task termination — the implementation and this boundary agree) |
| Queue time | Request accepted by the serving endpoint | First scheduling of the request onto the engine (from engine queue telemetry; if the engine reports none, queue time is recorded as **unavailable with a reason**, never approximated silently) |
| Time to first token (TTFT) | Driver sends the request for a turn | Driver receives the **first meaningful model-output event** of that turn: a content chunk, a reasoning-output event, or a native tool-call delta. Native tool calls may emit no `delta.content`; waiting only for content would under-count TTFT. SSE fragments are never tokens. |
| Inter-token latency (ITL) | Token *n* received | Token *n+1* received; available **only when true per-token timing exists** — transport text chunks are never tokens, and without token events ITL is recorded as unavailable with a reason |
| Model-serving time | Request dispatch to the serving endpoint | Final token of the turn received (includes queue time; queue time is also reported separately) |
| Tool-execution time | Agent runtime invokes a simulated tool | Tool result returned to the agent runtime (simulated latencies are deterministic, recorded, and **consumed through the clock** — they occupy task duration, timeout budget, request pacing, and cell wall time) |
| Output throughput | Generated tokens per second per turn (persisted per turn), and aggregate tokens/s per cell | — |

Client-side boundaries are measured at the benchmark driver, which runs on the
same host as the serving container to exclude WAN variability; the loopback /
container-network path is recorded in the manifest.

Model turns are consumed as **typed stream events** that distinguish
transport text chunks, reasoning-output events, native tool-call deltas,
true token events, authoritative usage/token counts, and optional
serving-queue telemetry. Output-token counts come only from authoritative
usage data or the exact model tokenizer; network chunk counts and SSE
fragments are never reported as token counts. Durations use an injectable
monotonic clock; task timing starts at actual driver submission — the
instant the bounded scheduler's slot is claimed — so a task that has not
entered a slot consumes none of its timeout budget, and any residual
driver-side wait after submission (queue_wait) is part of end-to-end task
time.

Tool calls use the native OpenAI-compatible `tools` / `tool_calls` /
`role=tool` path (decision D-0016). The retired textual `TOOL_CALL:`
protocol is never accepted. Private gpu-mode manifests record
`tool_call_transport`, `tool_call_parser`, and `reasoning_parser` so native
results cannot be confused with the custom-text transport used by run-e.

## 3. Primary measures

1. End-to-end task completion time.
2. Time to first token.
3. Inter-token latency.
4. Queue time.
5. Model-serving time.
6. Tool-execution time.
7. Output throughput.
8. Task-success rate (per the evaluator, section 8).
9. GPU utilization (DCGM, 1 s sampling).
10. GPU memory consumption (peak and time series).
11. Power and energy consumption (DCGM power → integrated energy per cell and
    per task).
12. Error and timeout rates (section 7).
13. Successful tasks per GPU-hour.
14. Cost per successful task (section 9).
15. **SLO-attaining throughput**: the number of correctly completed tasks per
    GPU-hour that satisfy **both** the established quality threshold and the
    latency target.

Latency measures are reported as p50 / p90 / p95 / p99 plus mean; no measure
is reported as a bare mean alone. Minimum sample counts (fixed in Phase 2,
decisions D-0009/D-0010): **p95 requires at least 200 observations and p99
requires at least 1,000 observations** — with the nearest-rank method these
minimums place at least 10 observations at or beyond the percentile rank, so
a tail estimate is never dominated by a handful of samples. Below the
applicable minimum the percentile is **suppressed explicitly** (JSON `null`,
with the sample `count` recorded so suppression is machine-checkable and
schema-enforced) rather than fabricated. Percentiles are calculated **per
repetition** over that repetition's own observations, using the deterministic
nearest-rank method on observed values (no interpolation).

The sample plan makes these minimums attainable (decision D-0010): each
measured repetition contains **200 balanced task instances**
(`tasks_per_repetition`), so per-repetition p95 of task-level measures is
reported at n ≥ 200 while per-repetition p99 remains suppressed at n = 200.
The five repetitions provide **1,000 task observations per cell**; the
cell-level p99 over the pooled raw observations is a **separately labeled
Phase 7 analysis step** and is never mixed into per-repetition records.

Every measure in a result is either **available with data** or **explicitly
unavailable with a reason** — values are never fabricated. Examples: queue
time without engine queue telemetry, ITL without true token events, token
throughput without authoritative usage data, and every primary
latency/throughput/SLO measure of mock execution (see section 13).

## 4. SLO definitions

SLO targets are **owner-approved and fixed** (decision D-0010):

| Profile | T_task (end-to-end) | T_ttft (per turn) | Per-task timeout |
| --- | --- | --- | --- |
| Interactive | ≤ 60,000 ms | ≤ 2,500 ms | 120,000 ms |
| Batch-heavy | ≤ 300,000 ms | none (throughput-oriented) | 600,000 ms |

- Quality threshold: **S_min = 1.0** — a task satisfies the quality SLO only
  when **every mandatory evaluator gate** passes (section 8). Success is
  deterministic and gate-based, never a threshold on a weighted mean.

A task attains the SLO only if it succeeds on quality **and** meets the
latency target. Mock execution never reports SLO attainment (section 13).

## 5. Repetitions, sample plan, and warm-up

- **Five measured repetitions per cell** (a cell = provider × mode × serving
  path × precision × workload profile × concurrency).
- **Each measured repetition contains 200 seeded task instances**
  (decision D-0010), deterministically **balanced across the ten incident
  templates** (20 instances per template). Instances are prompt-surface
  variants generated from a per-repetition seed; template id, instance id,
  instance seed, unique-template count, unique-instance count, and total
  attempts are recorded in every result (`sample_design`).
- Every repetition draws a **distinct seed**. Byte-identical repetitions are
  never represented as independent quality cases; independent quality cases
  are bounded by the unique-template count, and results state this
  explicitly.
- A measurement configuration with **fewer tasks than the requested
  concurrency is rejected** before any work happens.
- Each cell begins with **warm-up passes, excluded from measurement**: at
  minimum one full workload pass or enough requests to reach steady-state
  (engine caches compiled, KV cache allocator warm; criterion recorded in the
  manifest). Warm-up observations are **retained privately, labeled
  `warmup`, in their own observation files** — never mixed into measured
  summaries.
- Cold-start behavior (model load, engine build) is measured separately and
  labeled as such; it never contaminates steady-state cells.
- Repetition summary: median across the five repetitions with min/max range;
  per-repetition values are always retained.
- **Raw observations are retained** (section 14): summary metrics are derived
  from the raw per-task observations, each repetition is persisted promptly
  when it completes, and the raw files are what Phase 7 pools for the
  cell-level p99.

## 6. Generation settings and determinism

- Fixed generation settings per experiment (temperature, top_p, max tokens,
  reasoning-mode flag), recorded in the manifest.
- Seeds are set and recorded wherever the serving engine supports them.
- Model outputs are **not** assumed perfectly deterministic; repetitions plus
  the evaluator absorb output variance. Any nondeterminism source noticed
  (e.g. batching-dependent kernels) is documented, not suppressed.

## 7. Errors and timeouts

- Per-task timeout: fixed per workload profile (section 4). The timeout
  budget starts at driver submission (the slot-claim instant; unslotted
  tasks consume no budget) and is consumed by model turns **and simulated
  tool delays**; the model client receives and honors a per-turn deadline,
  so a timed-out task returns close to its deadline rather than after an
  arbitrary blocking delay. The remaining deadline is also enforced before
  and after **every tool execution, including the terminal tool**: a
  terminal recommendation whose tool latency reaches or crosses the deadline
  is a `task_timeout`, never a completion.
- A task is an **execution error** if the serving endpoint returns a failure
  (`endpoint_error`), the agent emits a malformed/invalid tool call
  (`malformed_tool_call`, `invalid_tool_name`, `invalid_tool_arguments`),
  no terminal recommendation is produced (`no_terminal_recommendation`), or
  an unexpected client/tool/runtime exception occurs — contained and
  sanitized as `agent_runtime_error` so one task can never abort a
  repetition and no raw exception text enters any record. Retry policy:
  **retries = 0** for measurement runs, so failures are visible, not hidden.
- Task accounting is unambiguous:
  `attempted = succeeded + quality_failed + errored + timed_out`.
  **Quality failure** (the task completed but failed evaluator gates) is a
  separate category and never appears in the execution-error taxonomy.
  Semantic validation enforces the sums and rate agreement.
- Errors and timeouts count in the denominator of task-success rate and
  SLO-attaining throughput; they are never silently dropped. Error taxonomy
  and counts are part of the result record.
- A repetition is **invalid** (repeated, with cause documented) only for
  infrastructure faults external to the system under test (e.g. host
  crash) — never for "bad-looking" results.

## 8. Success scoring

- The evaluator (Phase 2, version 3.x) is **gate-based and deterministic**
  (decision D-0010). A task succeeds only when **all mandatory gates** pass:
  - the task completed with a terminal recommendation
    (`diagnosis_id`, `rationale`, `remediation_id`);
  - the submitted `diagnosis_id` is exactly one of the scenario's accepted
    diagnoses — valid candidate ids are published through the synthetic task
    and tool evidence, so the model selects among candidates rather than
    guessing a hidden string;
  - the submitted `remediation_id` is in the accepted set (distractors
    fail);
  - **every mandatory evidence predicate** declared by the scenario is
    satisfied by the recorded tool trace (tool, validated arguments, and an
    explicit **typed result constraint** evaluated against the tool's
    structured response fields — never against a serialization of the whole
    response, so echoed request arguments, `available` listings,
    unknown-resource responses, and `found: false` responses can never
    satisfy evidence). Scenarios declare permitted alternative evidence
    paths; keyword-substring matching over free text is never a success
    criterion.
- Component scores (diagnosis / remediation / evidence) are retained as
  **diagnostics only**; the overall score is binary (1.0 on success, else
  0.0) and **S_min = 1.0**.
- Scoring is machine-checkable and versioned; the evaluator version is part of
  the run manifest. Human judgment is not part of scoring.

## 9. Costs

- Cost per successful task = (instance list price per hour × measured
  wall-clock hours attributed to the cell) ÷ successful tasks in the cell.
- List on-demand pricing at the time of the run is used (recorded in the
  manifest with source and date); taxes, discounts, and commitments are
  excluded and stated as excluded.
- Storage and egress attributable to the experiment are reported separately,
  not amortized into per-task cost.

## 10. Energy

- GPU power sampled via DCGM at 1 s; energy per cell integrated over the
  measurement window; energy per successful task derived. Host (CPU/system)
  power is out of scope unless a provider exposes it; this limitation is
  stated in reports.

## 11. Telemetry non-interference

- Telemetry overhead (DCGM exporter, Prometheus scrape) runs identically in
  every cell so it cancels in comparisons; its configuration is recorded.
  Nsight profiling runs are **separate labeled runs**, never mixed with
  measurement cells.

## 12. Manifest requirement

Every run records a manifest conforming to
[../schemas/run-manifest.schema.json](../schemas/run-manifest.schema.json),
identifying: Git commit, model artifact and hash, container digest,
serving-engine version, driver, CUDA version, operating system, hardware,
cloud region, generation settings, workload, concurrency, and timing
conditions. Runs without a complete manifest are invalid. Mock-mode manifests
record `execution_mode: "mock"`, use the mock engine, omit the model block,
container digest, and GPU host fields (nothing is fabricated), and use the
truthful `not-applicable` comparison classification; the workload catalog
digest lives in its own `workload.catalog_digest` field and is never
represented as a model artifact hash.

## 13. Mock execution is functional-only

Mock execution (Phase 2, `execution_mode: "mock"`) validates the harness,
workload, evaluator, schemas, and privacy guards — it measures no model and
no serving system. Therefore:

- mock host-clock latency, throughput, and production SLO attainment are
  **never presented as benchmark performance**: the primary measures are
  explicitly unavailable with reasons, and host-clock replay timings live
  only in the clearly separated `mock_diagnostics` namespace;
- mock Python replay speed is **never** reported as model tokens/sec (the
  mock client emits multi-word transport chunks, which are not tokens);
- mock **quality/evaluator validation remains valid** — gates, accounting,
  and determinism checks are meaningful in mock execution.

## 14. Raw task observations

Every warm-up and measured task produces a raw observation conforming to
[../schemas/task-observation.schema.json](../schemas/task-observation.schema.json):
run/repetition/task/template/instance identifiers, submission/start/end
timing, sanitized status and error category, per-turn timing/usage metadata,
complete tool traces, the terminal diagnosis/rationale/remediation, and the
evaluator's gates and component scores.

- Observations are persisted **outside Git** under `LAB_RESULTS_DIR`
  (validated before any work when persistence is requested), promptly per
  repetition, with atomic temp-file-plus-rename writes and private file
  permissions where supported; results reference the observation files by
  safe relative filename plus SHA-256.
- Warm-up observations are labeled `warmup`, retained separately, and
  excluded from every measured summary.
- Summary metrics are **derived from these raw observations**; Phase 7's
  cell-level pooled analysis reads them directly.
- No temporary or final result file is ever written inside the repository,
  and CLI output never prints absolute private paths (safe relative
  filenames and counts only).
