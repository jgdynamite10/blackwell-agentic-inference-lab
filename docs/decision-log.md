# Decision Log

Append-only record of project decisions, methodology revisions, and incidents.
Every entry states the date, the decision, the rationale, and — for
methodology changes — which results (if any) predate the change.
Methodology may not be revised after examining results without an entry here
([../AGENTS.md](../AGENTS.md), section 6).

---

## 2026-09-05 — D-0001: Phase 1 scope and repository foundation

**Decision.** Establish the repository foundation per the Phase 1
authorization: governance documents, methodology, schemas, minimal Python
package with tests and CI, results-privacy design, and a documentation-based
feasibility assessment.

**Rationale.** Owner authorization (Memo 2). No cloud credentials are
available in the working environment, so feasibility is based on official
public documentation with account-level checks recorded as unresolved.

## 2026-09-05 — D-0002: Baseline experiment boundaries

**Decision.** The initial baseline (Phase 3) is limited to: one cloud (Akamai),
one GPU (RTX PRO 6000 Blackwell Server Edition), one validated serving
configuration, two workload profiles, concurrency levels 1/4/8, and five
measured repetitions per cell.

**Rationale.** Keeps the frozen baseline small enough to reproduce exactly on
Google Cloud (Phase 5) and AWS (Phase 6), and bounds cost. Expansion requires
a new entry here plus owner approval.

## 2026-09-05 — D-0003: Two strictly separated comparison modes

**Decision.** Cross-cloud comparisons run in two modes — controlled-resource
(documented common CPU/memory limits on the benchmark containers) and
provider-native (unmodified purchasable configuration) — and their results are
never mixed in analysis or reporting.

**Rationale.** The same GPU across providers gives GPU parity, not system
parity. Controlled-resource mode isolates the GPU and serving stack;
provider-native mode reflects what a customer actually buys.

## 2026-09-05 — D-0004: Proposed controlled-resource envelope (provisional)

**Decision.** Propose 14 vCPUs / 100 GiB RAM as the provisional
controlled-resource envelope — a **joint total across the serving and
benchmark workload combined**, not per container — derived from the smallest
candidate host (`g7e.4xlarge`: 16 vCPU / 128 GiB). **Provisional** — the
exact allocation between containers and the cgroup enforcement mechanism are
frozen only after Phase 3 headroom validation empirically confirms headroom
for model loading, serving, telemetry, and benchmark execution.

**Rationale.** See [feasibility-report.md](feasibility-report.md), section 7.

## 2026-09-05 — D-0005: `g7e.4xlarge` as initial AWS candidate

**Decision.** Treat `g7e.4xlarge` (16 vCPU / 128 GiB) as the initial AWS
candidate and `g7e.8xlarge` as the alternative if memory-headroom findings
require it. Final selection happens during the AWS phase preflight.

**Rationale.** 128 GiB comfortably exceeds the ~60 GB BF16 artifact plus
runtime overhead, and 16 vCPUs matches Akamai's 1-GPU plan, strengthening the
controlled-resource design. See
[feasibility-report.md](feasibility-report.md), section 4.1.

## 2026-09-05 — D-0006: Governance update — private repository, execution boundary, licensing under review

> **SUPERSEDED (in part) by D-0007 (2026-09-06).** Items (a) and (b) below —
> the private-repository requirement and the licensing-under-review status —
> no longer apply. Items (c) and (d) — the credential-free execution boundary
> and the external-results rule — remain fully in force. This entry is
> retained unaltered below as the historical record.

**Decision.** Per the owner's governance update: (a) the repository is private
and remains private indefinitely; no material is published or mirrored
externally without explicit owner authorization; (b) licensing and publication
rights are under review — no license is granted and no LICENSE file is added
without explicit authorization; external contributions are not accepted
pending policy review; (c) the hosted Cloud Agent never requests, receives, or
uses provider credentials and performs cloud-independent work only; all
credentialed operations run in the owner's authenticated local environment;
(d) genuine benchmark data is never written inside the Git working tree — the
`LAB_RESULTS_DIR` guard distinguishes real-run mode (fail closed) from
explicit synthetic/test mode.

**Rationale.** Owner instruction (governance update memo, 2026-09-05). This
supersedes earlier statements describing the repository or methodology as
public; the methodology is described as documented and reproducible.

## 2026-09-06 — D-0007: Public Apache-2.0 release of source and methodology (supersedes D-0006 items a–b)

**Decision.** Per the owner's publication-readiness instruction of
2026-09-06:

1. The owner has determined that this project is **independently owned** and
   that **no external publication authorization is required**.
2. The owner **authorizes making the source code and documented methodology
   public**.
3. The repository is licensed under the **Apache License 2.0** (root
   `LICENSE` and `NOTICE` files; `pyproject.toml` carries
   `license = "Apache-2.0"`).
4. `jgdynamite10/blackwell-agentic-inference-lab` is the **canonical**
   repository for all development, issues, pull requests, releases, security
   reporting, and documentation.
5. The personal `jgdynamite/blackwell-agentic-inference-lab` repository will
   become an **archived redirect** (redirect README, then archived); it is
   not a synchronized mirror.
6. **Genuine benchmark data and account information remain private**: raw and
   processed results stay external to Git via `LAB_RESULTS_DIR`; credentials,
   account identifiers, project IDs, ARNs, private endpoints, provider bills,
   and infrastructure metadata are never committed.
7. **Every benchmark-result release still requires separate explicit owner
   approval** identifying the exact files and scope
   ([publication-governance.md](publication-governance.md), class B).

**Rationale.** Owner instruction (publication-readiness memo, 2026-09-06),
superseding D-0006 items (a) and (b). D-0006 items (c) and (d) — the
credential-free Cloud Agent boundary and the external-results design — are
unchanged.

*Note: item 5 above (archived redirect) is superseded by D-0008; the entry is
retained unaltered as the historical record.*

## 2026-09-06 — D-0008: Secondary repository is a one-way mirror (supersedes D-0007 item 5 only)

**Decision.** Per the owner's final-publication instruction of 2026-09-06,
superseding **only** the archived-redirect decision in D-0007 item 5 (all
other D-0007 items stand):

1. `jgdynamite10/blackwell-agentic-inference-lab` remains the **canonical**
   repository.
2. `jgdynamite/blackwell-agentic-inference-lab` is a **public, one-way
   mirror of canonical `main`** — not an archived redirect and not an
   independent development repository. It remains unarchived.
3. Development, issues, pull requests, releases, security reports, and CI
   decisions belong in `jgdynamite10`.
4. Changes must never flow from the secondary repository back to the
   primary.
5. The secondary mirror is synchronized only from reviewed and merged
   primary `main`.

**Rationale.** Owner instruction (final publication and secondary sync memo,
2026-09-06).

## 2026-09-06 — D-0009: Phase 2 authorization — synthetic workload, evaluator, and statistical rules

**Decision.** Per the owner's Phase 2 authorization of 2026-09-06:

1. **Phase 2 is authorized and in progress.** Phase 1 is complete. Phase 3
   and later phases still require separate explicit owner authorization.
2. **Workload versioning.** The synthetic Cloud Operations Agent workload
   (`cloud-ops-agent`) is version **2.0.0**
   (`src/blackwell_lab/workload/scenarios.py`): ten deterministic scenarios,
   one per documented incident class, each with fixture data for all six
   simulated tools, a ground-truth root cause, an accepted remediation set,
   distractor signals, and machine-checkable success criteria. The catalog is
   additionally content-addressed: the SHA-256 digest of the canonical
   catalog JSON is recorded as the workload artifact hash in every mock-run
   manifest. Any scenario or fixture change requires a version bump and a
   decision-log entry.
3. **Evaluator versioning and scoring.** The task-success evaluator is
   version **2.0.0** (`src/blackwell_lab/workload/evaluator.py`),
   deterministic and machine-checkable, with weights: root-cause diagnosis
   0.5 (fraction of scenario keywords present in the stated cause), evidence
   / appropriate tool use 0.2 (fraction of required tools consulted), and
   remediation 0.3 (all-or-nothing membership in the accepted set;
   distractors score zero). Error and timeout tasks score 0.0 and stay in
   the success-rate denominator.
4. **Proposed quality threshold (owner review required).** S_min = **0.85**:
   a task must have a fully correct root cause, an accepted remediation, and
   at least a quarter of the required evidence. **This value is a proposal
   and is NOT owner-approved yet**; it must be approved (or revised) before
   Phase 3 measurement.
5. **Exact profile definitions.** Interactive: closed-loop arrival,
   `search_logs` limit 10 lines, metric window 900 s, `max_tokens` 1,024,
   per-task timeout 120,000 ms. Batch-heavy: queue-full arrival,
   `search_logs` limit 50 lines, metric window 3,600 s, `max_tokens` 4,096,
   per-task timeout 600,000 ms. Both draw from the same catalog with
   identical success criteria.
6. **Proposed SLO targets (owner review required).** Interactive: task
   completion T_task ≤ 60,000 ms and per-turn TTFT ≤ 2,500 ms. Batch-heavy:
   T_task ≤ 300,000 ms, no TTFT target (throughput-oriented). **These values
   are proposals and are NOT owner-approved yet**; they must be fixed before
   Phase 3 measurement (measurement contract §4).
7. **Percentile sample-count rules.** p95 requires ≥ 200 observations; p99
   requires ≥ 1,000 observations (nearest-rank method: these place at least
   10 observations at or beyond the percentile rank). Below the minimum the
   percentile is suppressed as explicit `null` with the sample `count`
   recorded; the benchmark-result schema enforces the suppression.
   Percentiles are computed per repetition over that repetition's own
   observations; cell-level pooling is a separate, clearly labeled Phase 7
   step.
8. **Insufficient or unavailable measurements are represented explicitly,
   never fabricated.** Empty series (e.g. queue time in mock mode) are
   `count: 0` with all statistics `null`. Mock runs record
   `execution_mode: "mock"` in the manifest (a new required field —
   `is_synthetic_example` is *not* the execution-mode indicator), omit GPU
   host fields, and record GPU telemetry as
   `telemetry_available: false` with an explicit reason; GPU-hour-derived
   measures are `null`. Schema versions bumped to 1.1.0.
9. **Retry policy retries=0** for measurement runs, with the documented
   error taxonomy: `endpoint_error`, `malformed_tool_call`,
   `invalid_tool_name`, `invalid_tool_arguments`,
   `no_terminal_recommendation`, `task_timeout`.

**Rationale.** Owner instruction (Phase 2 authorization memo, 2026-09-06).
The workload, evaluator, statistical rules, and schema representation follow
the measurement contract and workload definition; SLO targets and the quality
threshold are highlighted as unapproved proposals so measurement cannot begin
on values the owner never reviewed.

*Note: the proposals in items 3–6 and the sample plan implied by item 2 are
superseded by D-0010; the entry is retained unaltered as the historical
record.*

## 2026-09-06 — D-0010: Owner-approved methodology — gate-based success, attainable sample plan, truthful timing/concurrency/streaming semantics (supersedes the affected D-0009 proposals)

**No genuine results predate this change.** No genuine benchmark has been
executed in any phase; every document produced so far is synthetic or
functional-validation output, so this revision cannot retroactively affect
any real measurement.

**Decision.** Per the owner's Phase 2 final correction instruction of
2026-09-06:

1. **SLO targets and timeouts are owner-approved** (no longer proposals,
   superseding D-0009 item 6 and the timeout values in item 5):
   interactive — T_task ≤ 60,000 ms, per-turn TTFT ≤ 2,500 ms, per-task
   timeout 120,000 ms; batch-heavy — T_task ≤ 300,000 ms, **no** TTFT
   target, per-task timeout 600,000 ms.
2. **Deterministic gate-based task success; S_min = 1.0** (superseding the
   weighted keyword scoring of D-0009 items 3–4). A task succeeds only when
   **all mandatory gates** pass: the task completed; the submitted
   `diagnosis_id` is exactly an accepted diagnosis (candidates are published
   in the synthetic task/tool evidence — the model never guesses a hidden
   string); the submitted `remediation_id` is in the accepted set; and every
   mandatory machine-checkable **evidence predicate** declared by the
   scenario is satisfied by the recorded tool trace (permitted alternative
   evidence paths are declared per scenario). Component scores
   (diagnosis/remediation/evidence) are retained as **diagnostics only** and
   never define success. The evaluator is version **3.0.0**; the workload
   catalog (structured diagnosis candidates, evidence predicates,
   alternative paths) is version **2.1.0**.
3. **Attainable sample plan.** Each measured repetition contains
   **200 balanced task instances** (`tasks_per_repetition`, measurement
   default 200), deterministically balanced across the ten incident
   templates with seeded per-instance surface variants. Per-repetition p95
   is reported at n ≥ 200; per-repetition p99 remains suppressed below
   1,000 observations. The five repetitions provide 1,000 task observations
   per cell for a **separately labeled cell-level p99** calculated in
   Phase 7 from the retained raw observations. Every repetition draws a
   distinct seed; **byte-identical repetitions are never represented as
   independent quality cases**, and independent quality cases are bounded by
   the unique-template count, which is recorded in every result
   (`sample_design`).
4. **Timing and timeout semantics.** Task timing starts at **actual driver
   submission** (not worker dequeue); the terminal record is handed to the
   evaluator immediately (the documented end boundary and the implementation
   agree); simulated tool delays are consumed through the injectable
   monotonic clock and therefore occupy task duration, timeout budget,
   request pacing, and cell wall time; the model client receives and honors
   a deadline; unexpected client/tool/runtime exceptions are contained and
   sanitized as `agent_runtime_error` (one task can never abort a
   repetition); retries remain 0. The error taxonomy of D-0009 item 9 gains
   `agent_runtime_error`.
5. **One truthful scheduler.** Phase 2 uses a single **bounded closed-loop
   scheduler** (no pre-submitted unbounded backlog); requested concurrency,
   achieved maximum concurrency, and measured mean in-flight work are all
   recorded, and concurrency is not claimed to be exactly enforced during
   ramp-up and drain. The "queue-full arrival" phrasing of D-0009 item 5 is
   withdrawn: profiles are differentiated by context/input size, output
   budget, timeout, and SLO — not by unimplemented arrival algorithms.
   Measurement configurations with fewer tasks than the requested
   concurrency are rejected.
6. **Streaming metrics and mock validity.** Model turns are typed event
   streams distinguishing transport text chunks, true token events,
   authoritative usage/token counts, and optional serving-queue telemetry.
   TTFT uses the first non-empty content event; ITL exists only with true
   per-token timing; token counts come only from authoritative usage data or
   the exact model tokenizer — transport chunks are never counted as tokens.
   Mock execution is **functional-only**: mock host-clock latency,
   throughput, and production SLO attainment are represented as
   null/unavailable with explicit reasons (host-clock timings live only in
   the clearly separated `mock_diagnostics` namespace); mock quality and
   evaluator validation remain valid; mock Python replay speed is never
   reported as model tokens/sec.
7. **Raw, auditable observations.** A versioned task-observation schema
   (1.0.0) records every warm-up and measured task — identifiers, timing,
   sanitized status, per-turn metadata, tool traces, terminal
   recommendation, and evaluator gates — persisted **outside Git** under
   `LAB_RESULTS_DIR` promptly per repetition with atomic
   temp-file-plus-rename writes, private permissions, and SHA-256 references
   from the result. Warm-up observations are labeled separately and excluded
   from measured summaries; summary metrics are derived from the raw
   observations. CLI output never prints absolute private paths.
8. **Schema and accounting invariants.** Manifest and result schemas are
   version **2.0.0**: unambiguous task accounting
   (`attempted = succeeded + quality_failed + errored + timed_out`, with
   quality failure separate from the execution-error taxonomy);
   available/unavailable measure branches; mock mode requires the mock
   engine and forbids GPU host fields, the container digest, and the model
   block, and uses the truthful `not-applicable` comparison classification;
   gpu mode requires a non-mock engine, GPU fields, the container digest,
   and the model block. The workload catalog digest lives in its own
   `workload.catalog_digest` field and is never represented as a model
   artifact hash. Semantic validation beyond JSON Schema enforces exact
   sums, rate/count agreement, matching run ids, truthful concurrency
   bounds, observation counts, and safe relative artifact references.

**Rationale.** Owner instruction (Phase 2 final correction memo,
2026-09-06), approving the previously highlighted proposals and correcting
the evaluator, sample plan, timing, concurrency, streaming, and accounting
semantics before any genuine measurement exists.
