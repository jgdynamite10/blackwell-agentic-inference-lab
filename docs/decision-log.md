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

*Note: the evidence-predicate result representation and version numbers of
item 2 and the submission-stamping detail of items 4–5 are refined by
D-0011; the entry is retained unaltered as the historical record.*

## 2026-09-06 — D-0011: Phase 2 blocking corrections — claim-time submission, typed evidence result constraints, terminal-tool deadline, positive integer tool arguments (refines D-0010 items 2, 4, and 5)

**No genuine results predate this change.** No genuine benchmark has been
executed in any phase, so this revision cannot retroactively affect any real
measurement.

**Decision.** Per the owner's final blocking review of PR #5 (2026-09-06):

1. **Genuine bounded closed-loop scheduler (refines D-0010 items 4–5).**
   Task submission is stamped the moment a scheduler slot becomes available
   and the worker claims the task — never pre-stamped for the whole schedule
   at enqueue. At most `concurrency` slots exist, no unbounded backlog is
   ever pre-submitted, and a task that has not entered a slot consumes none
   of its timeout budget. Requested / achieved-maximum / mean in-flight
   concurrency remain recorded, and exact enforcement during ramp-up and
   drain remains unclaimed.
2. **Typed evidence result constraints (refines D-0010 item 2).** Evidence
   predicates no longer match substrings over a canonical JSON serialization
   of a tool response. Each alternative declares an explicit typed result
   constraint evaluated against the tool's structured response fields:
   a returned log line's message containing the required value (with
   `total_matches > 0`), a returned change's `change_id` matching exactly,
   a found (`found is true`) runbook's remediation list containing the exact
   remediation, a found metric with the exact name and non-empty points, or
   a returned service component with the exact status. Echoed request
   arguments, `available` listings, unknown-resource responses, and
   `found: false` responses can never satisfy evidence. The workload catalog
   is version **2.2.0** and the evaluator is version **3.1.0**; adversarial
   regression tests pin the previously exploitable cases (zero-match
   searches with the expected text only in the query; `found: false`
   runbook responses with the remediation id smuggled into the key).
3. **Deadline enforcement around every tool, including the terminal tool.**
   The remaining deadline is checked before and after every tool execution;
   a terminal recommendation whose tool latency reaches or crosses the
   deadline is a `task_timeout`, never a completion.
4. **Positive integer tool-argument validation.** `search_logs.limit`,
   `query_metrics.window_s`, and `check_recent_changes.window_s` reject
   booleans and every non-positive integer (`invalid_tool_arguments`).

**Rationale.** Owner blocking review of PR #5 (2026-09-06): the previous
scheduler pre-stamped submissions and silently charged queue wait against
task timeouts; JSON-serialization substring matching allowed echoed
arguments and not-found responses to count as evidence; the terminal tool
escaped the deadline; and non-positive limits/windows were accepted. All
four defects are corrected before any genuine measurement exists.

## 2026-09-06 — D-0012: Phase 2 complete; Phase 3A readiness authorized; 12-cell symmetric Akamai baseline matrix; pilot before freeze (extends the D-0002 baseline boundaries with the comparison-mode factor)

**Historical (superseded in part by D-0013 and D-0014, 2026-09-06).** Phase
3A is now complete. Phase 3B is authorized only for one bounded
compatibility/headroom pilot (D-0014). Planning estimates now use the
owner-observed Seattle $3.00/h plan price rather than the $2.50/h advertised
starting price recorded below. The text of this entry is the 3A authorization
as written.

**No genuine results predate this change.** No genuine benchmark has been
executed in any phase.

**Decision.** Per the owner's Phase 3A authorization memo (2026-09-06):

1. **Phase status.** Phase 2 (synthetic workload and evaluator) is
   **complete**. **Phase 3A — Akamai baseline readiness** is authorized:
   cloud-independent preparation only, with no provisioning, modification,
   or deletion of cloud resources, no provider credentials, no authenticated
   preflight execution in hosted contexts, no model-weight or large-container
   downloads, no genuine benchmark, and no genuine-result publication.
   **Phase 3B — provisioning and measurement — remains unauthorized** and
   requires separate explicit owner approval.
2. **Symmetric 12-cell Phase 3 matrix (reconciliation).** The original
   Phase 3 definition (six cells, no comparison-mode factor) was
   inconsistent with Phases 5–6, which run both controlled-resource and
   provider-native modes (12 cells per provider). Phase 3 now runs **both
   comparison modes** on the single Akamai instance: 2 modes × 2 workload
   profiles × 3 concurrency levels = **12 cells / 60 measured repetitions /
   12,000 measured task observations**, giving every later portability cell
   an exact same-mode Akamai counterpart. Modes run serially on the same
   instance, so the factor doubles measured GPU-hours, not instance count.
3. **Cost re-derivation.** Phase 3 planning estimates become ≈ 90–230
   GPU-hours (setup/validation 15–25 h, pilot 2–4 h, measured cells
   ≈ 70–200 h under the D-0010 sample plan) ≈ **$230–$580** at the
   officially stated $2.50/h starting price — still a preliminary,
   non-authoritative estimate pending an account-level quote
   (feasibility report §8; cost-guardrails budget table updated).
4. **Pilot before freeze.** A short owner-approved compatibility/headroom
   pilot (reduced task count; bootstrap, driver/CUDA and model
   compatibility, BF16 bring-up, headroom at concurrency 8,
   realized-latency sampling) precedes and is separate from the full
   baseline. The full-run settings — model artifact and hash, vLLM
   container digest, BF16 configuration, generation settings, warm-up,
   timeouts, resource allocation, and cgroup enforcement — are **frozen
   only after the pilot**, with a decision-log entry; the full 12-cell
   baseline then requires its own explicit authorization.
5. **Readiness deliverables.** Phase 3A delivers: Akamai Terraform for
   exactly one RTX PRO 6000 Blackwell single-GPU instance (pinned provider
   4.1.0, validated variables, unique project/run tags, state and tfvars
   outside Git); plan-by-default lifecycle tooling whose apply and destroy
   each require separate explicit local owner approval and refuse hosted
   execution, with an exact per-run resource ledger, exact-resource
   teardown plan, and read-only orphan report (never broad cleanup); an
   idempotent bootstrap design (pinned OS/container/runtime assumptions,
   NVIDIA driver/CUDA compatibility checks, pinned vLLM BF16 candidate,
   model-artifact digest verification before serving, health/readiness
   checks, and a workload watchdog documented as **not** a billing
   control on Akamai); a provider-neutral OpenAI-compatible client for
   configurable local serving endpoints that preserves the Phase 2 timing,
   evaluator, accounting, and evidence contracts, never counts transport
   chunks as tokens, uses authoritative usage and engine queue telemetry
   only when genuinely available, and fails visibly when required
   measurements are unavailable; truthful host/GPU telemetry and manifest
   collection (never fabricated; unavailable data carries an explicit
   reason); a sanitized, mock-tested authenticated read-only preflight for
   exact plan entitlement, eligible regions, and account-visible price
   (local operator only); and separate `blackwell-cloud` workflows for
   readiness validation, the gated pilot, the **disabled** full baseline,
   external result verification, and teardown plan / orphan report.
   Genuine output uses `RunMode.REAL` and the external `LAB_RESULTS_DIR`
   guard exclusively.

**Rationale.** Owner instruction (Phase 3A authorization memo, 2026-09-06).
The matrix reconciliation and cost re-derivation happen now — before any
measurement exists — so the baseline definition cannot drift after results
are observed; the pilot/freeze split keeps full-run settings from being
declared frozen before compatibility and headroom are empirically
validated.

## 2026-09-06 — D-0013: Phase 3A lifecycle-safety corrections (already implemented)

**No genuine results predate this change.** No genuine benchmark has been
executed in any phase. This entry documents safety behavior already
implemented and merged with Phase 3A; it does not authorize provisioning.

**Decision.** The Phase 3A lifecycle-safety corrections already in the
codebase are binding project policy:

1. **Region-availability parsing.** Authenticated preflight parses the live
   Akamai `GET /v4/regions/{region}/availability` response as a top-level
   JSON array (with optional paginated-object compatibility). Malformed
   payloads fail closed and are never treated as empty.
2. **Provider identity verification before teardown.** `teardown-plan` and
   `destroy` require successful read-only API verification of every ledger
   resource (Terraform address, type, provider ID, exact label, project tag,
   run tag, and region where applicable) **before** any Terraform plan or
   apply that could delete something. Only an explicit HTTP 404 during
   post-destroy confirmation means absent.
3. **Reconciliation cleanliness.** Reconciliation may be marked clean only
   when Terraform state is readable and complete, the provider API was
   successfully checked, provider resources and state agree exactly, and no
   unexpected, untracked, or missing resource exists. Provider lookup
   failures write a dirty recovery ledger and retain any pending record.
4. **Pinned toolchain.** Terraform CLI is exactly **1.9.8** in `versions.tf`,
   CI, lifecycle validation, and documentation. Other local versions are
   refused.
5. **Pinned bootstrap and GPU probe.** Bootstrap fails closed before any
   package mutation unless exact non-empty versions are set for the NVIDIA
   driver, NVIDIA Container Toolkit, Docker, and required repository/key
   material. The GPU probe uses a digest-pinned NVIDIA CUDA image and runs
   `nvidia-smi` inside the container.
6. **Live provenance before every genuine cell.** The pilot re-observes
   model digest, container digest, engine version, container CUDA runtime,
   instance identity, and host/GPU facts immediately before each cell.
   Configuration values are never manifest facts.
7. **Creation-only apply plans.** Apply-stage plans reject update, delete,
   replace, unknown, and unrelated actions. No maintenance/update workflow
   is authorized.
8. **Provider-native-only pilot.** Controlled-resource labeling is rejected
   until the joint 14-vCPU/100-GiB serving-plus-benchmark cgroup envelope
   is genuinely implemented and observed.

**Rationale.** These controls were implemented during Phase 3A so that any
later owner-authorized apply, pilot, or destroy cannot silently skip
identity, reconciliation, provenance, or pin checks. Recording them as
D-0013 closes the dangling decision reference already present in the
codebase.

## 2026-09-06 — D-0014: Phase 3B bounded Akamai compatibility/headroom pilot authorized

**No genuine results predate this change.** No genuine benchmark has been
executed in any phase. The owner-run capability probe recorded below was
not a benchmark and produced no benchmark results.

**Decision.** Phase 3A is **complete**. Phase 3B is authorized **only** for
one bounded Akamai compatibility/headroom pilot. The full 12-cell Akamai
baseline remains unauthorized. Phase 4 and later phases remain
unauthorized.

### Sanitized owner-verified Akamai validation

Recorded from the owner's local authenticated environment. No provider
resource IDs, account IDs, IP addresses, credentials, raw API responses, or
AWS/GCP quota values are recorded:

- exact plan: `g3-gpu-rtxpro6000-blackwell-1`;
- hardware: one RTX PRO 6000 Blackwell GPU;
- selected pilot region: `us-sea`;
- observed catalog base price: $3.00/hour;
- Seattle had no observed regional surcharge;
- an owner-run temporary instance capability probe successfully reached
  running;
- that temporary instance was subsequently deleted;
- the capability probe created no firewall;
- a later read-only check found zero matching test instances, firewalls,
  and volumes;
- billing is not continuing for those test resources;
- no genuine benchmark, model serving, or benchmark-result collection
  occurred during that capability probe.

The direct Seattle create/delete observation establishes stronger evidence
for `us-sea` than an additional `us-ord` connectivity preflight. `us-ord`
was only an example and is not the selected pilot region. A saved Terraform
plan verifies the intended configuration and planned actions only. It does
not prove live capacity. Capacity is known when the provider accepts
provisioning and the instance reaches the expected running state.

### Authorized pilot envelope

| Constraint | Bound |
| --- | --- |
| Provider | Akamai Cloud |
| Region | `us-sea` |
| Plan | `g3-gpu-rtxpro6000-blackwell-1` |
| Maximum resources | exactly one GPU instance and its one project/run-tagged firewall |
| Comparison mode | provider-native only |
| Intended maximum instance lifetime | six hours |
| Maximum authorized pilot-session cost | $25 total |
| Owner checkpoint | three elapsed hours; continuing requires an explicit owner decision |
| Billing stop | the instance must be **deleted**; shutdown is insufficient |
| Apply / destroy | retain separate digest-bearing approval phrases |
| Teardown | may target only the exact recorded lifecycle-ledger resources |
| After destroy | provider verification and an orphan report are mandatory |
| Results | genuine results remain external and private |
| Publication | no automatic publication is authorized |

Pilot observations are **diagnostic**. They may not be represented as
comparative benchmark findings.

### Authorized diagnostic cells only

For every cell: one warm-up pass, one measured repetition, 20 tasks per
repetition, and live provenance rechecked immediately before execution.

1. interactive profile, concurrency 1;
2. batch-heavy profile, concurrency 4;
3. batch-heavy profile, concurrency 8.

These counts are deliberately too small for publishable p95/p99 claims.
They exist only to validate model/runtime compatibility, BF16 serving,
GPU-memory and CPU headroom, concurrency-8 behavior, telemetry, result
persistence, and realized task latency.

Terraform apply, the pilot command, and destroy continue to require their
separate exact local approval phrases. Lifecycle and Terraform run on the
owner's laptop; bootstrap, local serving, provenance, and the pilot run on
the GPU instance after a manual private copy of the approved config and a
read-only ledger snapshot. Terraform state, tfvars, and provider credentials
are never copied to the instance. Result-transfer failure must not prevent
an identity-verified, digest-approved emergency teardown.

This decision authorizes the envelope; it does not execute any provider
operation.

**Rationale.** Owner Phase 3B pilot-authorization instruction (2026-09-06).
A bounded compatibility/headroom session is the next authorized step after
Phase 3A readiness. The full 12-cell baseline and later phases stay
unauthorized until a further owner decision.

## 2026-09-07 — D-0015: Offline Phase 3B candidate pins and multi-platform Terraform lockfile

**No genuine results predate this change.** No genuine benchmark has been
executed in any phase. The candidate pins below were resolved from official
public metadata only. They have **not** been empirically validated on an
RTX PRO 6000 Blackwell Server Edition GPU, and they do not freeze the
baseline.

**Decision.**

1. **Terraform lockfile.** The committed Linode provider 4.1.0 lockfile is
   generated with Terraform 1.9.8 via `terraform providers lock
   -platform=darwin_arm64 -platform=linux_amd64` from official HashiCorp
   Registry signed metadata. Checksums are never edited by hand. Lifecycle
   and readiness `terraform init` always pass `-lockfile=readonly`. Saved-plan
   digest verification continues to reject any lockfile change. This
   permanently corrects the macOS ARM64 `h1:` checksum drift observed during
   the completed provisioning test: restoring the previous single-platform
   committed lockfile after `init` changed the digest that saved plans
   recorded.
2. **vLLM candidate.** Select `docker.io/vllm/vllm-openai:v0.27.1` (linux/amd64
   manifest `sha256:c2f3b1b964e47809b722b5e75b61b1e7b39a50f70388cf2bf2418f16a9f31da2`)
   because the NVIDIA model card and official vLLM recipes name v0.27.1 for
   Nemotron 3.5 Lightning BF16. v0.28.0 was evaluated and not selected: it
   is newer and documents additional SM12x work, which is not a sufficient
   reason to override the named recipe.
3. **Host and probe pins.** Record the official public Ubuntu 24.04
   **open** driver `nvidia-driver-580-server-open=580.173.02-0ubuntu0.24.04.1`
   (Blackwell requires NVIDIA open kernel modules; the proprietary
   `nvidia-driver-580-server` package is rejected), NVIDIA Container Toolkit
   `1.20.0-1`, the official repository list, and the SHA-256 of the official
   NVIDIA apt key
   (`c880576d6cf75a48e5027a871bac70fd0421ab07d2b55f30877b21f1c87959c9`).
   Bootstrap downloads that key to a temp file, verifies the digest, and
   installs the keyring atomically; a mismatch performs no repository or
   package installation. The digest-pinned CUDA 13.0.0 GPU-probe image is
   documented in [infra/akamai/README.md](../infra/akamai/README.md). The
   Nemotron BF16 revision is the public Hugging Face commit
   `a9904d24bcc1d289a1950fa9d2b978c47cf903b9`. Model acquisition uses the
   digest-pinned vLLM image (overridden entrypoint), a revision-specific
   staging directory, and atomic promotion; an unmanifested directory is
   untrusted. The complete per-file model digest manifest remains unresolved
   until the authorized live download. These pins remain offline candidates
   until verified empirically on the GPU host.
4. **Cost convention.** Akamai access for this project is provided without
   a direct compute charge. Normalized economic cost continues to use the
   applicable $3/hour planning rate. No account or employment information is
   recorded.
5. **Scope unchanged.** NIM, TensorRT-LLM, NVFP4, and Dynamo remain out of
   this pilot. The full 12-cell baseline remains unauthorized. No
   provisioning, destroy, model-weight download, or container-layer download
   is authorized by this entry.

**Rationale.** The completed provisioning test showed that a
darwin_arm64-only extra `h1:` line made saved-plan lock verification fail
after the committed lockfile was restored. A multi-platform official lock
plus read-only init removes that class of drift without weakening digest
checks. Offline pin resolution is required before the next authorized live
session so bootstrap cannot install floating or empty versions. The same-day
correction to the open-kernel driver package, NVIDIA key-content pin, real
`bootstrap.env` enforcement, and atomic containerized model fetch happened
before any live GPU session and does not change the still-unvalidated
status of these candidates.

## 2026-09-08 — D-0016: Native OpenAI-compatible tool calling replaces the custom-text protocol before baseline freeze

**No genuine baseline results predate this change.** Run-e proved
infrastructure, serving, live provenance, result persistence, and teardown,
and it produced genuine tokens. Its quality outcomes (59/60
`malformed_tool_call`, one `quality_failed`) are **diagnostic only** and
are **not** baseline evidence. No run-e performance comparison may be
published as a final benchmark result.

**Decision.**

1. **Transport replacement.** The custom textual `TOOL_CALL:` / user-role
   `TOOL_RESULT:` protocol is retired before baseline freeze. The provider-
   neutral model adapter now uses native OpenAI-compatible `tools`, streamed
   `delta.tool_calls`, and `role=tool` results with matching
   `tool_call_id`. The retired text format is never accepted as a fallback.
2. **Official vLLM 0.27.1 / Nemotron 3.5 Lightning pairing.** Launch
   retains every existing digest, CUDA, driver, model-revision, network,
   and resource pin, and adds `--reasoning-parser nemotron_v3`,
   `--tool-call-parser qwen3_coder`, and `--enable-auto-tool-choice`.
   Requests send `tool_choice: "auto"` (the documented pairing with
   `--enable-auto-tool-choice` / `qwen3_coder`; `required` is not the
   documented pairing and is known to empty or corrupt streamed arguments
   with this parser) and `parallel_tool_calls: false`. The model's bundled
   chat template is used; no custom template is invented.
3. **Reasoning mode.** Official default thinking-on is frozen as
   `reasoning_mode=True`. `--reasoning-parser nemotron_v3` separates
   reasoning fields from executable tool calls. Reasoning is timing-only
   (eligible for TTFT) and is never persisted. `force_nonempty_content` is
   a coding-agent note only and is not added.
4. **TTFT.** Time to first token is the first meaningful model-output
   event: content, reasoning output, or a native tool-call delta. Token
   counts continue to come only from authoritative API usage fields.
5. **Identity.** Private gpu-mode manifests record
   `tool_call_transport=openai-native-tools`,
   `tool_call_parser=qwen3_coder`, and `reasoning_parser=nemotron_v3` so
   native-tool results cannot be confused with run-e's custom-text
   protocol. Observation schema 1.1.0 may carry sanitized structural
   diagnostics only. The previous pilot-config digest `d4118755…` is
   retired; this entry does not publish a replacement digest.

**Rationale.** Run-e showed the custom text protocol could not drive the
single-tool-per-turn agent loop on the pinned Nemotron / vLLM 0.27.1 path.
The correction is bounded to the wire protocol, launch parser flags, and
measurement-contract wording. Task definitions, evaluator rules, maximum
turns, seeds, temperatures, top_p values, concurrency cells, and success
criteria are unchanged. The full 12-cell baseline remains unauthorized.
