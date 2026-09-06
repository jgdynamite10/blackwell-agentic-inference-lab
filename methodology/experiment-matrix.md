# Experiment Matrix

Defines the experimental cells for every phase. A **cell** is the unit of
measurement: provider × comparison mode × serving path × precision × workload
profile × concurrency. Every cell receives **five measured repetitions** plus
warm-up per the [measurement contract](measurement-contract.md); each
measured repetition contains **200 balanced task instances**
(decision D-0010), so a full cell yields **1,000 task observations** — enough
for the separately labeled cell-level p99 analysis in Phase 7.

## Factors

| Factor | Levels (planned) | Notes |
| --- | --- | --- |
| Provider | Akamai Cloud; Google Cloud; AWS | One single-GPU instance each; phases 3/5/6 |
| Comparison mode | controlled-resource; provider-native | Never mixed in analysis or reporting |
| Serving path | vLLM; TensorRT-LLM; NVIDIA NIM | vLLM is the baseline; others added in Phase 4 |
| Precision | BF16; NVFP4 | Subject to compatibility findings |
| Workload profile | interactive; batch-heavy | Defined in [workload-definition.md](workload-definition.md) |
| Concurrency | 1; 4; 8 | Requested in-flight agent tasks (bounded closed-loop scheduler; requested/achieved-max/mean in-flight are all recorded) |

## Comparison modes (strict separation)

1. **Controlled-resource mode.** A documented common CPU and system-memory
   envelope is applied on all three providers as a **joint total across the
   serving and benchmark workload combined** — not independently per
   container (provisional: 14 vCPUs / 100 GiB joint total, see
   [../docs/feasibility-report.md](../docs/feasibility-report.md) §7). The
   exact allocation between the two containers and the cgroup enforcement
   mechanism are frozen only after Phase 3 headroom validation. Purpose:
   reduce host-resource differences while comparing the GPU and serving
   stack. CPU architecture differences remain and are recorded.
2. **Provider-native mode.** The provider's normal purchasable instance
   configuration with no artificial caps. Purpose: evaluate the operational
   experience and economics a customer actually receives.

Results from the two modes are labeled with `comparison_mode` in every result
record, analyzed separately, and reported separately. No figure, table, or
claim may combine them.

## Phase 3 — frozen baseline (Akamai)

The initial baseline is deliberately small and is **not expanded** without a
decision-log entry and owner approval:

- 1 cloud (Akamai), 1 GPU (RTX PRO 6000 Blackwell SE, 96 GB), 1 validated
  serving configuration (candidate: vLLM + BF16; final choice frozen after
  validation), 2 workload profiles, concurrency {1, 4, 8}, 5 measured
  repetitions per cell.
- Cells: 1 × 1 × 1 × 1 × 2 × 3 = **6 cells / 30 measured repetitions**.
- Output: the frozen baseline definition — model artifact + hash, container
  digest, serving configuration, generation parameters, workload version —
  reused verbatim in Phases 5 and 6.

## Phase 4 — optimization (Akamai)

Adds precision and serving-path factors on the same hardware:

- Serving {vLLM, TensorRT-LLM, NIM} × precision {BF16, NVFP4} minus
  infeasible combinations (compatibility per the feasibility report), × 2
  profiles × 3 concurrency levels.
- Upper bound: 6 configurations × 6 = **36 cells**; infeasible combinations
  documented rather than silently skipped.

## Phases 5–6 — portability (Google Cloud, AWS)

- Reproduce the frozen Phase 3 baseline exactly (same artifact hash, container
  digest, serving config, workload, generation parameters, measurement
  contract) on `g4-standard-48` and the selected G7e size.
- Each provider runs both comparison modes: 6 baseline cells × 2 modes =
  **12 cells per provider**.
- Every material environmental difference (CPU, memory, storage, network,
  virtualization, driver, region) is recorded in manifests and the phase
  report.

## Fixed-before-measurement declarations

To prevent post-hoc methodology drift, the following are fixed and recorded
before Phase 3 measurement starts (decision-log entries required to change):

1. SLO latency targets and quality threshold (measurement contract §4, §8).
   **Owner-approved (decision D-0010)**: interactive T_task 60,000 ms and
   T_ttft 2,500 ms per turn; batch-heavy T_task 300,000 ms with no TTFT
   target; quality threshold **S_min = 1.0** (all mandatory evaluator gates
   must pass — success is deterministic and gate-based).
2. Per-profile task timeout values. **Owner-approved (D-0010)**: interactive
   120,000 ms; batch-heavy 600,000 ms.
3. The sample plan (D-0010): 200 balanced task instances per measured
   repetition; per-repetition p95 at n ≥ 200; per-repetition p99 suppressed
   below 1,000 observations; cell-level p99 from the pooled 1,000 raw
   observations is a separately labeled Phase 7 analysis.
4. Warm-up criterion (warm-up observations retained separately, labeled, and
   excluded from measured summaries).
5. Generation settings (candidate: temperature 1.0, top_p 0.95 — the model
   card's recommended sampling — with a fixed max-token budget; reasoning mode
   setting per workload profile).
6. The five-repetition count and median-with-range summary rule.
