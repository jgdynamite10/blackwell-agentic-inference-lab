# Project Roadmap

Work proceeds phase by phase. **Each phase begins only after the project owner
explicitly authorizes it, and work stops at the end of the currently
authorized phase** ([AGENTS.md](../AGENTS.md), section 7).

Currently authorized: **Phase 1 only.**

## Phase 1 — Repository foundation and feasibility *(current)*

Create the repository foundation, project governance, research methodology,
schemas, safety controls, CI, and a read-only feasibility assessment. No cloud
resources are created; no model weights or large containers are downloaded; no
benchmarks are run.

Exit criteria: repository clones cleanly with passing tests and linting; the
complete roadmap, safety rules, and results-privacy design exist; the
measurement contract and experiment matrix are defined; feasibility findings
for Akamai Cloud, Google Cloud, and AWS are documented with verified facts
separated from assumptions; a pull request is open for owner review.

## Phase 2 — Synthetic workload and evaluator

Implement the Cloud Operations Agent, simulated tools
(`get_service_health()`, `query_metrics()`, `search_logs()`,
`retrieve_runbook()`, `check_recent_changes()`, `recommend_remediation()`),
deterministic incident scenarios, the task-success evaluator, and a local
benchmark runner — all runnable **without a GPU** (for example against a mock
or small CPU model endpoint). Includes unit tests, scenario validation, and
synthetic fixture data only.

## Phase 3 — Akamai Cloud baseline

Run **one validated model and serving configuration** on **one Akamai RTX PRO
6000 Blackwell GPU** using **two workload profiles** and **concurrency levels
1, 4, and 8**, with five measured repetitions per cell per the measurement
contract. This phase establishes the frozen baseline (model artifact and hash,
container digest, serving configuration, generation parameters, workload) that
all later portability phases reproduce. Requires explicit owner approval to
provision the instance.

## Phase 4 — NVIDIA optimization

On the same Akamai baseline hardware, add approved precision comparisons
(BF16 vs NVFP4, subject to artifact and runtime compatibility) and serving-path
comparisons (vLLM vs TensorRT-LLM vs NVIDIA NIM), operational telemetry
(DCGM, Prometheus, Grafana), and selected Nsight Systems profiling.

## Phase 5 — Google Cloud portability

Reproduce the frozen Phase 3 baseline on a Google Cloud **G4** single-GPU
configuration (`g4-standard-48`) while documenting every material
environmental difference (CPU architecture, vCPU count, memory, storage,
networking, virtualization, driver, region). Both controlled-resource and
provider-native modes are run and reported separately.

## Phase 6 — AWS portability

Reproduce the same frozen baseline on a single-GPU **Amazon EC2 G7e**
configuration while documenting every material environmental difference. Use
the AWS instance size selected during feasibility analysis (initial candidate
`g7e.4xlarge`; alternative `g7e.8xlarge` if memory-headroom findings require
it) and preserve the same model artifact, container digest, serving
configuration, workload, generation parameters, and measurement contract used
for the other providers.

## Phase 7 — Analysis and publication

Calculate outcome-level performance and economic measures across Akamai Cloud,
Google Cloud, and AWS; document limitations; sanitize approved results per
[publication-governance.md](publication-governance.md); and prepare the
technical report. Any release of the report or results beyond this private
repository requires the owner's separate, explicit authorization; none is
scheduled or automatic.

## Phase 8 — Optional Dynamo extension

Investigate multi-GPU or disaggregated prefill/decode serving with NVIDIA
Dynamo **only if** the single-GPU findings justify the additional complexity
and cost. This phase may be skipped entirely.

## Replication scope guard

Google Cloud and AWS are **later replication providers**. The initial baseline
is limited to Akamai Cloud, one GPU, one validated serving configuration, two
workload profiles, concurrency 1/4/8, and five measured repetitions per cell.
The baseline is not expanded without a decision-log entry and owner approval.
