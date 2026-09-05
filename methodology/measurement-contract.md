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
| End-to-end task completion time | Benchmark driver submits the task (before any queueing) | Evaluator receives the agent's final answer |
| Queue time | Request accepted by the serving endpoint | First scheduling of the request onto the engine (from engine metrics; if unavailable, request receipt → first prefill token processed, documented per engine) |
| Time to first token (TTFT) | Driver sends the HTTP request for a turn | Driver receives the first generated token of that turn |
| Inter-token latency (ITL) | Token *n* received | Token *n+1* received; reported as the distribution across all gaps in a turn |
| Model-serving time | Request dispatch to the serving endpoint | Final token of the turn received (includes queue time; queue time is also reported separately) |
| Tool-execution time | Agent runtime invokes a simulated tool | Tool result returned to the agent runtime (simulated latencies are deterministic and recorded) |
| Output throughput | Generated tokens per second per turn, and aggregate tokens/s per cell | — |

Client-side boundaries are measured at the benchmark driver, which runs on the
same host as the serving container to exclude WAN variability; the loopback /
container-network path is recorded in the manifest.

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
is reported as a bare mean alone.

## 4. SLO definitions

SLO targets are fixed **before** Phase 3 measurement begins and recorded in
the experiment matrix. Placeholders to be finalized during Phase 2 evaluator
development (decision-log entry required):

- Latency target: end-to-end task completion time ≤ T_task; TTFT ≤ T_ttft
  per turn.
- Quality threshold: evaluator score ≥ S_min (section 8).

A task attains the SLO only if it succeeds on quality **and** meets the
latency target.

## 5. Repetitions and warm-up

- **Five measured repetitions per cell** (a cell = provider × mode × serving
  path × precision × workload profile × concurrency).
- Each cell begins with **warm-up runs, excluded from measurement**: at
  minimum one full workload pass or enough requests to reach steady-state
  (engine caches compiled, KV cache allocator warm; criterion recorded in the
  manifest). Warm-up data is retained privately but never mixed into results.
- Cold-start behavior (model load, engine build) is measured separately and
  labeled as such; it never contaminates steady-state cells.
- Repetition summary: median across the five repetitions with min/max range;
  per-repetition values are always retained.

## 6. Generation settings and determinism

- Fixed generation settings per experiment (temperature, top_p, max tokens,
  reasoning-mode flag), recorded in the manifest.
- Seeds are set and recorded wherever the serving engine supports them.
- Model outputs are **not** assumed perfectly deterministic; repetitions plus
  the evaluator absorb output variance. Any nondeterminism source noticed
  (e.g. batching-dependent kernels) is documented, not suppressed.

## 7. Errors and timeouts

- Per-task timeout: fixed per workload profile in the experiment matrix.
- A task is an **error** if the serving endpoint returns a failure, the agent
  runtime crashes, or a malformed tool call cannot be parsed after the
  documented retry policy (retries: 0 for measurement runs, so failures are
  visible, not hidden).
- Errors and timeouts count in the denominator of task-success rate and
  SLO-attaining throughput; they are never silently dropped. Error taxonomy
  and counts are part of the result record.
- A repetition is **invalid** (repeated, with cause documented) only for
  infrastructure faults external to the system under test (e.g. host
  crash) — never for "bad-looking" results.

## 8. Success scoring

- The evaluator (Phase 2) scores each task against the deterministic incident
  definition: correct diagnosis, appropriate tool usage, and a remediation
  recommendation matching the scenario's accepted set.
- Scoring is machine-checkable and versioned; the evaluator version is part of
  the run manifest. Human judgment is not part of scoring.
- Task-success = score ≥ S_min. The quality threshold is fixed before Phase 3
  measurement.

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
conditions. Runs without a complete manifest are invalid.
