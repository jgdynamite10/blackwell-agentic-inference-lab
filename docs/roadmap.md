# Project Roadmap

Work proceeds phase by phase. **Each phase begins only after the project owner
explicitly authorizes it, and work stops at the end of the currently
authorized phase** ([AGENTS.md](../AGENTS.md), section 7).

Phase status: **Phases 1 and 2 are complete.** **Phase 3A is complete.**
**Phase 3B D-0014 pilot is complete as a diagnostic.** Decision **D-0017
authorizes implementation** of the Akamai minimum valuable lab
(provider-native, three cells); **live execution still requires separate
digest-bearing approval phrases**. **Phase 4 and later phases remain
unauthorized.**

## Phase 1 — Repository foundation and feasibility *(complete)*

Create the repository foundation, project governance, research methodology,
schemas, safety controls, CI, and a read-only feasibility assessment. No cloud
resources are created; no model weights or large containers are downloaded; no
benchmarks are run.

Exit criteria: repository clones cleanly with passing tests and linting; the
complete roadmap, safety rules, and results-privacy design exist; the
measurement contract and experiment matrix are defined; feasibility findings
for Akamai Cloud, Google Cloud, and AWS are documented with verified facts
separated from assumptions; a pull request is open for owner review.

## Phase 2 — Synthetic workload and evaluator *(complete)*

Implement the Cloud Operations Agent, simulated tools
(`get_service_health()`, `query_metrics()`, `search_logs()`,
`retrieve_runbook()`, `check_recent_changes()`, `recommend_remediation()`),
deterministic incident scenarios, the task-success evaluator, and a local
benchmark runner — all runnable **without a GPU** (for example against a mock
or small CPU model endpoint). Includes unit tests, scenario validation, and
synthetic fixture data only.

Phase 2 must also define the **minimum task/sample counts required for
meaningful p95 and p99 latency reporting**, before any GPU measurement
begins: tail percentiles from undersized samples are noise, so the scenario
and repetition counts must be justified against the reported percentiles.

## Phase 3 — Akamai Cloud baseline

Run **one validated model and serving configuration** on **one Akamai RTX PRO
6000 Blackwell GPU** using **both comparison modes (controlled-resource and
provider-native)**, **two workload profiles**, and **concurrency levels
1, 4, and 8** — the 12-cell baseline matrix of decision D-0012, with five
measured repetitions per cell per the measurement contract. This phase
establishes the frozen baseline (model artifact and hash, container digest,
serving configuration, generation parameters, workload) that all later
portability phases reproduce symmetrically in both modes.

The phase is split into two separately authorized sub-phases:

### Phase 3A — baseline readiness *(complete)*

Cloud-independent preparation only — no cloud resource was created, modified,
or deleted by Phase 3A itself. Delivered: the finalized 12-cell experimental
design and cost re-derivation; Akamai Terraform for exactly one single-GPU
instance plus its firewall, with external state, reviewed saved plans,
identity-safe teardown, and an orphan report; an idempotent bootstrap design
with pinned packages, digest-pinned CUDA GPU probe, and health checks; the
provider-neutral OpenAI-compatible client; truthful host/GPU telemetry;
sanitized authenticated preflight; and the `blackwell-cloud` workflows
(readiness, gated pilot, disabled full baseline, result verification,
teardown plan, orphan report). Lifecycle-safety policy is recorded as
decision D-0013.

### Phase 3B — provisioning and measurement *(D-0014 pilot complete; D-0017 MVL implementation authorized; live MVL not yet approved)*

Decision D-0014 authorized **only one** Akamai compatibility/headroom pilot:

- region `us-sea`, plan `g3-gpu-rtxpro6000-blackwell-1`;
- exactly one GPU instance and its one project/run-tagged firewall;
- provider-native comparison mode only;
- intended maximum instance lifetime six hours; maximum session cost $25;
- owner checkpoint at three elapsed hours;
- three diagnostic cells only (interactive/1, batch-heavy/4, batch-heavy/8),
  each with one warm-up pass, one measured repetition, and 20 tasks.

Pilot observations are diagnostic and must not be represented as comparative
benchmark findings. Apply, pilot, and destroy still require their separate
exact local approval phrases. Teardown may target only ledger-recorded
resources; post-destroy provider verification and an orphan report are
mandatory.

Decision **D-0017** freezes the Akamai **minimum valuable lab**: the same
pins, provider-native only, three cells (interactive/1, batch-heavy/4,
batch-heavy/8), 1 warm-up + 3 × 200 tasks (1,800 measured observations).
p50 and p95 are primary; p99 is exploratory. Live apply and
`mvl-baseline` still require their separate digest-bearing phrases. The
D-0014 six-hour / $25 envelope stays separately named. Controlled-resource
mode, additional engines, and a 12-cell matrix remain optional future
work. Phase 4 remains unauthorized. Every provisioning action follows
[AGENTS.md](../AGENTS.md) §1 and [cost-guardrails.md](cost-guardrails.md).

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
technical report. Genuine results remain external and private; any release of
the report or results requires the owner's separate, explicit approval
identifying the exact files and scope; none is scheduled or automatic.

## Phase 8 — Optional Dynamo extension

Investigate multi-GPU or disaggregated prefill/decode serving with NVIDIA
Dynamo **only if** the single-GPU findings justify the additional complexity
and cost. This phase may be skipped entirely.

## Replication scope guard

Google Cloud and AWS are **later replication providers**. The initial baseline
is limited to Akamai Cloud, one GPU, one validated serving configuration, both
comparison modes, two workload profiles, concurrency 1/4/8, and five measured
repetitions per cell (12 cells; decision D-0012). The baseline is not expanded
without a decision-log entry and owner approval.
