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

## 2026-09-08 — D-0017: Akamai minimum valuable lab

**No genuine MVL results predate this change.** Run-e's quality and
performance remain diagnostic and nonpublishable because they used the
retired text protocol. Run-e infrastructure, serving, live provenance,
teardown, and the locally verified model aggregate
`sha256:5d6a435f3e0faf95dd4a610c66fefd9c4ccbe350af950216dab9fec6f3d3da0d`
at revision `a9904d24bcc1d289a1950fa9d2b978c47cf903b9` support this freeze.
Raw run-e results do not enter Git.

**Decision.** The owner authorizes **implementation** of the Akamai
**minimum valuable lab** (`blackwell-cloud mvl-baseline`). This is an
initial provider-native Akamai baseline, sufficient for exploratory
reporting and a first project article. It is **not** a complete
controlled-resource or cross-cloud study. Live execution still requires
separate digest-bearing apply and `mvl-baseline` approval phrases. Phase 4
optimization remains unauthorized. Dynamo, NIM, TensorRT-LLM, NVFP4, and
any other model are not added.

**Reporting.** p50 and p95 are primary. p99 is exploratory because of
sample size (1,800 measured observations across three cells).
Controlled-resource mode and additional engines are optional future work.
AWS and GCP will later repeat this same MVL matrix if quota permits.

**Frozen identity.**

| Dimension | Frozen value |
| --- | --- |
| Provider / region / plan | Akamai Cloud / `us-sea` / `g3-gpu-rtxpro6000-blackwell-1` |
| GPU | one RTX PRO 6000 Blackwell Server Edition |
| Model | `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16` |
| Model revision | `a9904d24bcc1d289a1950fa9d2b978c47cf903b9` |
| Aggregate artifact hash | `sha256:5d6a435f3e0faf95dd4a610c66fefd9c4ccbe350af950216dab9fec6f3d3da0d` |
| Per-file digests | host-resident sha256sum manifest at `MODEL_DIGEST_MANIFEST`; live re-verified; never committed |
| Precision | BF16 only |
| Serving | vLLM 0.27.1 linux/amd64 `sha256:c2f3b1b964e47809b722b5e75b61b1e7b39a50f70388cf2bf2418f16a9f31da2` |
| CUDA / driver / Docker / CTK | existing exact pins (CUDA 13.0, open driver 580.178.04, docker.io 29.1.3, CTK 1.20.0-1); driver pin restated by D-0018 |
| Tool transport | native OpenAI `tools` / `tool_choice=auto` / `parallel_tool_calls=false` |
| Parsers | `--reasoning-parser nemotron_v3`, `--tool-call-parser qwen3_coder`, auto tool choice enabled |
| Reasoning mode | true |
| Sampling | temperature 1.0, top_p 0.95, max_tokens 1024, seed 20260906 |
| Workload | `cloud-ops-agent` 2.3.0; existing scenarios, evaluator, SLOs, timeouts |
| Measurement | 1 warm-up pass, 3 measured repetitions, 200 balanced tasks per repetition |

**Matrix.** Provider-native only:

1. interactive / concurrency 1
2. batch-heavy / concurrency 4
3. batch-heavy / concurrency 8

Three cells, 9 measured repetitions, 1,800 measured task observations.
AWS and GCP later repeat this same matrix if quota permits.

**Canary.** One diagnostic-only canary on the same deployment covers all
ten scenarios once, exercises native `tool_calls` and `role=tool` round
trips, and fails closed on structural errors. Evaluator quality is
recorded but is not a gate. Failure persists a sanitized external
failure record, runs zero measured tasks, and tells the owner to
generate a teardown plan from their laptop. Success continues into the
three measured cells. An interrupted MVL is restarted later under a new
run label; there is no resume ledger.

**Safety.** The existing six-hour infrastructure envelope is unchanged.
The canary duration is used to check that measured work is reasonably
projected to finish in the remaining session. The watchdog recognizes
`blackwell-cloud mvl-baseline` and remains a workload safeguard — it
does not stop Akamai billing. Completion or failure never deletes
provider resources and never claims the GPU host generated a Terraform
destroy plan. Teardown-plan must be run from the owner's laptop.

**Rationale.** Owner authorization of 2026-09-08 (Akamai minimum valuable
lab). Implementation is code-and-review only until the owner issues the
exact apply and `mvl-baseline` phrases.

## 2026-09-15 — D-0018: Replace the unavailable open-driver pin

**No genuine MVL results predate this change.** The first authorized
`p3-mvl-20260910a` apply reached a clean reconcile and then stopped in
reviewed bootstrap. The instance and firewall were owner-destroyed and
confirmed absent. No measured MVL observations were collected.

**Decision.** Replace only the active NVIDIA open-driver package pin:

- retired: `nvidia-driver-580-server-open=580.173.02-0ubuntu0.24.04.1`
- current: `nvidia-driver-580-server-open=580.178.04-0ubuntu0.24.04.1`

The new value is the exact installable version in the official Ubuntu
24.04 `noble-updates` restricted `linux/amd64` Packages index, retrieved
2026-09-15. Required sibling `580-server` packages in that pocket are
the same version. The pin remains exact and non-floating. CUDA, Docker,
NVIDIA Container Toolkit, vLLM, the model, the workload, MVL counts,
sampling, evaluation, Terraform, and lifecycle behavior are unchanged.

**Rationale.** On the live Ubuntu 24.04 GPU image, apt resolved sibling
packages from `noble-updates` to `580.178.04-0ubuntu0.24.04.1`, so the
retired `580.173.02` metapackage was not installable. This is a
blocker-only pin correction, not a serving or methodology expansion.

## 2026-09-18 — D-0019: Bounded agent-quality qualification (not another baseline)

**MVL-F remains immutable and diagnostic-only.** Its infrastructure and
serving measurements are valid for its exact pins. Its quality outcome
was **3 / 1,800** successful tasks. That outcome **disqualifies MVL-F as
the comparative reference**. Diagnosis passed **1,766 / 1,800**;
remediation passed **29 / 1,800**. The dominant observed failure was
**remediation selection**, not diagnosis. Raw MVL-F artifacts, staging
packages, and ZIP files are not modified by this entry.

**No evaluator weakening is authorized.** Evaluator **3.1.0**, its
accepted answers, evidence gates, and `S_min = 1.0` quality threshold
are unchanged. Observation schema is not extended with `finish_reason`
or reasoning-token fields.

**Decision.** The next authorized step is a **bounded agent-quality
qualification**, not another baseline. Implementation is the
credential-free `blackwell-cloud qualify-agent` command. Live execution
still requires the exact digest-bearing approval phrase. This entry
does not execute inference, provision resources, or publish results.

Interactive latency wording is recorded in the correct order and is
not reversed: **TTFT is compared with 2,500 ms**; **end-to-end task
latency is compared with 60,000 ms**.

### Two distinct quality levels

These are **project-defined targets, not industry standards**.

**A. Study-entry qualification gate.** Passing this gate only permits a
configuration to enter comparative measurement. It is **not** called
production-grade.

- aggregate quality success ≥ 70%;
- every scenario ≥ 40%;
- valid native tool-call rate ≥ 99%;
- invalid tool-name rate = 0;
- invalid-argument rate ≤ 1%;
- request/inference error rate ≤ 1%;
- timeouts = 0;
- interactive TTFT p95 ≤ 2,500 ms;
- interactive end-to-end p95 ≤ 60,000 ms;
- provenance and result verification pass.

**B. Project-defined production-like target.** A measured configuration
may fail this target without invalidating its measurement. The study
must report that failure.

- aggregate quality success ≥ 90%;
- every scenario ≥ 80%;
- the same structural, error, timeout, latency, provenance, and
  integrity requirements as the study-entry gate.

### Frozen qualification design

The ten scenario templates are split **before any prompt wording
change** by a deterministic SHA-256 / sort rule with fixed seed
`blackwell-lab-agent-qualification-v1`. Templates are scored as
`sha256("{seed}:{template_id}")` and sorted by hex digest, then
template id. The first six are development; the last four are holdout.
No template was hand-selected.

**Development templates (6):**

- `dns-failures-001`
- `memory-pressure-001`
- `failed-deployment-001`
- `unhealthy-upstream-001`
- `capacity-exhaustion-001`
- `gpu-saturation-001`

**Holdout templates (4):**

- `rate-limiting-001`
- `pod-failures-001`
- `elevated-latency-001`
- `storage-latency-001`

Stages:

1. **Development screen** — 20 tasks, interactive, concurrency 1.
   Continue only at ≥ 40% aggregate quality.
2. **Holdout screen** — 20 tasks drawn only from the frozen holdout
   templates. Continue only at ≥ 50% aggregate quality. Holdout
   results must never be used to revise candidate wording.
3. **Freeze run** — all ten templates, 200 balanced tasks,
   interactive, concurrency 1, one excluded warmup and one measured
   repetition. Apply the full study-entry gate.

### Workload 2.4.0 candidate and two frozen candidates

Workload **2.4.0** is the qualification tool-contract correction. The
scenario catalog, accepted diagnosis/remediation IDs, required
evidence IDs, evaluator logic, and quality scoring remain the 2.3.0
catalog / evaluator 3.1.0 identity. Valid remediation IDs are **not**
placed in the task prompt. The generic system prompt and tool
descriptions state the required workflow: inspect evidence; select a
published diagnosis ID; infer the affected service or system; call
`retrieve_runbook` with that key; select an exact remediation ID from
`runbook.remediation_ids`; call `recommend_remediation` with that ID
and an evidence-based rationale.

Exactly two candidates are predeclared. No third candidate is
authorized. Publishing remediation answers in the task prompt is not
an authorized candidate.

| Field | C1 | C2 |
| --- | --- | --- |
| Workload | 2.4.0 tool-contract correction | identical |
| Model / revision / digest | existing MVL pins | identical |
| vLLM / parsers / seed / reasoning | existing MVL pins | identical |
| max tokens | interactive 1,024 | identical |
| top_p | 0.95 | 0.95 |
| temperature | 1.0 | **0.2** |

C2 is used only if C1 fails the qualification gate. Serialized C1/C2
identities differ only in candidate id, resulting config digest, and
temperature.

**Rationale.** Owner qualification-contract instruction of 2026-09-18.
MVL-F cannot be the comparative reference after a 3 / 1,800 quality
outcome driven by remediation selection. The correction is a bounded
tool-contract qualification with frozen holdout templates and no
evaluator weakening.

## 2026-09-28 — D-0020: Provider-neutral engine/precision contract (no launch)

**MVL-F, C1/C2, workload 2.4.0, evaluator 3.1.0, thresholds, lifecycle,
Terraform, and publication policy are unchanged.** Existing authorized
vLLM BF16 single-GPU behavior is unchanged. This entry does not execute
inference, provision resources, download models or containers, or
publish results.

**Decision.** The next authorized Phase 4 step is a **provider-neutral
engine/precision contract**, not a live optimization cell. Implementation
is the credential-free `blackwell_lab.engines` package and the offline
`blackwell-cloud engine-contract` command. Live Phase 4 engine/precision
execution remains unauthorized and still requires a later digest-bearing
approval phrase.

### Contract 1.0.0

A declaration must name engine identity, numerical precision, topology
(`single-gpu` / `multi-gpu` / `multi-node`), and immutable model and
container identity (artifact, 40-hex revision, algorithm:hex hash,
`repo@sha256:` digest, exact engine version). Floating labels such as
`latest` fail closed. Required entitlements that are unknown or false
fail closed. Evaluation returns `ready`, `conditional`, or `blocked`.
`require_ready_contract` is the genuine execution gate and refuses both
`conditional` and `blocked` before a serving client is used.
`require_supported_contract` is the same gate. The offline CLI still
reports the exact classification and returns nonzero for conditional
and blocked so automation cannot treat conditional as ready.

The core registers one builtin profile: `vllm-bf16-single-gpu`. Other
known engines and precisions (`tensorrt-llm`, `nim`, `nvfp4`, `fp8`,
`w4a16`) and multi-GPU / multi-node topologies are valid to declare and
are blocked until a later component module registers them.

### Isolated component registry

Later component agents add a module under
`src/blackwell_lab/engines/components/` and call
`register_engine_profile`. The core discovers those modules with
`pkgutil` and never names them. Component modules must not edit the
core contract or each other. No component-specific launch command is
authorized by this decision.

Optional additive manifest fields `serving.topology` and
`serving.engine_profile_id` keep existing 3.1.0 vLLM BF16 manifests
valid when omitted.

**Rationale.** Owner engine/precision-contract instruction of 2026-09-28.
Phase 4 comparisons need a shared fail-closed surface before any
component implements TensorRT-LLM, NIM, NVFP4, or multi-GPU / multi-node
launch paths. Recording the contract first prevents later modules from
editing each other or weakening existing vLLM BF16 provenance.

### 2026-09-28 correction — genuine gates require ready

Review found that `require_supported_contract` originally allowed
`conditional` to proceed. That wording is withdrawn before any Phase 4
result exists. Conditional remains a classification for recognized but
unresolved paths. Genuine `RealRunSpec` validation, live provenance, and
manifest assembly now require `ready`. Component discovery runs on that
evaluation path, not only in the offline CLI. Entitlement parsing
rejects malformed members instead of coercing them. The public register
is [component-readiness.md](component-readiness.md).

## 2026-09-29 — D-0021: Workload 2.4.1 evidence-acquisition prompt clarification

**Workloads 2.3.0 and 2.4.0, candidates C1 and C2, evaluator 3.1.0,
scenario content, accepted answers, thresholds, tool behavior, lifecycle,
and publication policy are unchanged.** No qualification outcome is
published with this entry. Those earlier definitions predate this
clarification. This entry does not execute qualification, provision
resources, download models, or claim that the new prompt improves quality.

**Decision.** Add immutable workload version **2.4.1**, derived from
2.4.0. The only contract change is the generic system-prompt
evidence-collection instruction:

1. `search_logs` uses literal substring matching, not semantic search.
2. Search queries should use exact identifiers, service names,
   configuration IDs, job IDs, or diagnostic terms supported by
   information already available to the agent.
3. A zero-match search must be retried with a different specific token
   before a terminal recommendation.
4. The agent must gather direct supporting evidence for its diagnosis
   before submitting the terminal recommendation.
5. A plausible change record or runbook remediation is not a substitute
   for the required incident evidence.

The instruction names no scenario, accepted diagnosis, accepted
remediation, log line, or evaluator predicate. Tool descriptions and
tool schemas remain the 2.4.0 text. Unknown workload versions still fail
closed.

Candidate **P1** is an authorized `qualify-agent` candidate:
workload 2.4.1, temperature 0.2, and the existing top_p, seed, max
tokens, Nemotron model and revision, vLLM image and engine, native
OpenAI tool transport, development and holdout schedules, evaluator
3.1.0, thresholds, accepted answers, and task counts. The candidate
workload mapping is C1 to 2.4.0, C2 to 2.4.0, and P1 to 2.4.1. Compared with
C2, serialized experimental behavior differs only in candidate
identity, workload version, the version-bound system prompt, and the
resulting digests. Workload-version metadata is provenance for that
prompt binding, not an additional treatment. P1 uses the same
development, holdout, and freeze stages. Development remains the first
required gate. Each stage keeps its own digest-bearing approval.
Authorizing the candidate does not execute it. C1 and C2 identity
serialization is unchanged.

**Rationale.** Owner instruction to clarify evidence acquisition in the
system prompt only. Private diagnostic notes motivated the wording and
are not recorded here.

## 2026-10-02 — D-0022: Workload 2.5.0 evidence-grounding controller and candidate P2

**Workloads 2.3.0, 2.4.0, and 2.4.1, candidates C1, C2, and P1, evaluator
3.1.0, scenario content, accepted answers, evidence predicates, thresholds,
tool behavior, tool latencies, the turn budget, the Nemotron BF16 model and
revision, vLLM 0.27.1, the native OpenAI tool transport, infrastructure, the
six-hour lifecycle, and publication policy are unchanged.** The 2.4.1 system
prompt is byte-identical to D-0021 and is the prompt workload 2.5.0
executes. No qualification outcome is published
with this entry. This entry does not execute qualification, provision
resources, run inference, download models, or claim that the controller
improves quality.

**Decision.** Add immutable workload version **2.5.0**, derived from 2.4.1,
bound to the provenance-checking controller **`evidence-grounding-v1`**
(`src/blackwell_lab/workload/evidence.py`; methodology in
`methodology/workload-definition.md`):

1. Every non-terminal tool result carries an opaque, task-local
   `observation_id`. Controller state is isolated per task execution,
   repetition, and concurrency slot; IDs from any other execution never
   resolve.
2. `recommend_remediation` gains `evidence_refs`, a list of
   `observation_id` strings that must each resolve to an **eligible**
   observation recorded earlier in the same task.
3. Eligible observations are successful, nonempty diagnostic results: a
   log search with at least one match, a usable service-health response,
   or a found metric with data points. Zero-match searches, not-found
   lookups, unknown or unusable health, malformed results, runbooks, change
   records (guidance only), and fabricated, stale, cross-task,
   cross-repetition, or cross-concurrency references are ineligible.
4. A rejected attempt returns only the generic payload
   `{"accepted": false, "failure_category": "direct_evidence_required"}`
   and the task continues inside its unchanged `max_turns` budget. Budget
   exhaustion after at least one rejection is the new execution-error
   category `direct_evidence_required`; a task that never attempts the
   terminal tool remains `no_terminal_recommendation`.
5. The controller validates provenance and structure only. It never reads
   scenario identifiers, expected queries, accepted answers, or evaluator
   predicates; irrelevant but structurally valid evidence passes the
   controller and fails the evaluator as before.
6. Measurement accounting is retained for every turn, tool call, rejected
   attempt, usage record, latency, and failure. Task observations gain an
   `evidence_grounding` count summary and run manifests record
   `workload.controller`. No prompt, completion, reasoning text, raw
   observation payload, or credential is persisted by the controller.
7. Workload 2.5.0 executes the **byte-identical 2.4.1 system prompt**
   (SHA-256
   `37b3a4fb615dc21c8d39a5301dc4318870fea3498fbed196c50bfcbe67de1bd3`)
   and the **unchanged 2.4.0 tool-description prose** that 2.4.1 also
   uses. No model-visible instruction text is added. `evidence_refs`
   exists only in the version-bound native-tool JSON argument schema of
   `recommend_remediation`; `evidence-grounding-v1` is the **only**
   experimental behavioral treatment, so P2 is a single-treatment
   candidate. (Correction recorded in the same pull request before any
   execution: an earlier draft of this entry appended a grounding
   instruction to the prompt and to the terminal tool description; the
   independent review rejected that as a second and third treatment, and
   both were removed. No results predate the correction.)
8. Workload/controller bindings are closed: 2.3.0, 2.4.0, and 2.4.1 bind
   no controller; 2.5.0 binds `evidence-grounding-v1`. Any other
   combination, in `qualify-agent` configuration or in a real-run spec,
   fails closed before a model client is constructed.

Candidate **P2** is an authorized `qualify-agent` candidate: workload
2.5.0, controller `evidence-grounding-v1`, temperature 0.2, and the
existing top_p, seed, max tokens, model and revision, container and
engine, tool transport, development, holdout, and freeze schedules,
evaluator 3.1.0, thresholds, accepted answers, and task counts. The
candidate mapping is C1 to 2.4.0, C2 to 2.4.0, P1 to 2.4.1, and P2 to
2.5.0. Compared with P1, serialized experimental behavior differs **only**
in `candidate_id`, `workload_version`, and `controller` (and therefore the
resulting digests); the `system_prompt` field is byte-identical to P1's.
The `controller` field is serialized
only for P2, so C1, C2, and P1 identity digests are unchanged. Development
remains the first required gate; each stage keeps its own digest-bearing
approval. Authorizing the candidate does not execute it.

**Rationale.** Owner instruction to add a provenance-grounding controller
as a bounded, offline-tested treatment, separable from the prompt-only P1
variant. Private diagnostic notes motivated the design and are not recorded
here.

## 2026-10-02 — D-0023: Sealed qualification-set custody manifest

**No sealed qualification set is generated by this decision.** No
benchmark result, qualification result, or custody manifest predates it.
Workload, evaluator, qualification, and infrastructure behavior are
unchanged. Live import remains unauthorized until a later frozen commit
and a separate digest-bearing approval.

**Decision.** Add the public custody manifest schema
[sealed-set-manifest.schema.json](../schemas/sealed-set-manifest.schema.json)
at version 1.1.0, and the credential-free custody tool documented in
[sealed-set-custody.md](sealed-set-custody.md). The contract is:

1. The schema is content-free. It records counts, SHA-256 digests, the
   frozen commit, the commit-bound controller digest, the opaque set
   identity, and the import-request digest. It has no task bodies, task
   identifiers, accepted answers, scenario names, or private filenames.
2. Custody output stays outside every Git repository. Directories are
   mode `0700` and files are mode `0600`. Each stage is exactly twenty
   opaque tasks. A failed import does not leave a schema-valid finalized
   manifest. Replay rejection applies only to the selected custody
   directory.
3. Controller verification is commit-bound. The digest is computed from
   the ordered controller-source blobs and modes stored in the supplied
   commit. The executing sources must match those blobs and must be the
   worktree files at those canonical paths. A checkout that does not
   contain the controller does not bind.
4. Import authorization is a content-free request. The request binds the
   frozen commit, the commit-bound controller digest, the opaque set
   identity, both stage counts, both aggregate digests, and the request
   schema/tool version. The approval phrase binds the SHA-256 digest of
   that request. Changed, substituted, or reordered input is a different
   request.
5. SHA-256 hashes provide integrity, not confidentiality. This schema and
   tool do not encrypt, do not apply a cryptographic seal, and do not
   provide global anti-replay. A global guarantee would require an
   external owner-controlled registry, which this repository does not
   implement.

**Rationale.** Owner authorization to record the custody schema and the
fail-closed import binding before any real development or holdout set
exists. The entry is limited to that contract.

## 2026-10-03 — D-0024: Bind sealed qualification sets to P2 execution

**No sealed set is imported, opened, or executed by this decision.** No
qualification result predates it. The P2 prompt, controller, evaluator,
thresholds, sampling rules, model, engine, precision, and infrastructure
are unchanged. C1, C2, P1, and the P2 freeze stage are unchanged. Live
`qualify-agent` execution, live import under D-0023, and Phase 4 remain
unauthorized and keep their separate digest-bearing approval phrases.

**Decision.** `blackwell-cloud qualify-agent` executes the P2 development
and P2 holdout cells only from a D-0023 custody stage, never from the
in-repository scenario catalog. The contract is:

1. **Path-free binding in the frozen config.** P2 development and holdout
   configs carry a `sealed_set` object with exactly `schema_version`
   (`1.0.0`), `custody_manifest_sha256`, `custody_controller_digest`,
   `import_request_digest`, `set_identity`, `stage`,
   `stage_aggregate_digest`, `task_count` (exactly 20), and
   `payload_schema_version` (`1.0.0`). The binding stage must equal the
   config stage. The binding carries no filesystem path, so the config
   digest is portable across machines. A `sealed_set` block on any other
   candidate or stage is rejected. The C1, C2, and P1 identity digests
   are unchanged.
2. **Separate runtime custody location.** The custody directory is the
   `--custody-dir` argument: an absolute path outside every Git
   repository, with no symlink escape, holding `0700` directories and
   `0600` files as required by D-0023. It is rejected for non-sealed
   cells, required for sealed cells, and never printed, never written to
   receipts or manifests, and never committed. No new transport mechanism
   is introduced: the qualification runner already executes locally in
   the owner's environment and only the model is remote, so the sealed
   stage is read on the same machine that holds custody.
3. **Stage-separated custody layout (custody manifest 1.2.0).** The
   D-0023 custody tool now writes `private_layout: stage-separated`:
   `private/<stage>/index.json` and `private/<stage>/blobs/<hex>` for
   each stage, with no combined index and no shared blob directory. Each
   stage index names only its own stage's identifiers, content digests,
   order, count, and aggregate; the public manifest carries
   `development_index_digest` and `holdout_index_digest` so a stage
   reader authenticates its own index without reading the other stage's.
   The stage aggregate, set identity, import-request, and
   custody-manifest digest algorithms are unchanged. Full-custody
   `verify` and `receipt` still verify both stages (disjointness, set
   identity, `file_digests`, exact directory contents); the execution
   adapter never calls them. Packages written under the 1.1.0
   combined-index layout are immutable historical custody objects: they
   are not migrated or rewritten, and the adapter rejects them as
   `layout-version-unsupported` at the manifest gate before any private
   file is opened. **This supersedes the D-0023 private layout for any
   package used in execution; D-0023's other terms are unchanged.**
4. **Stage-specific loading.** The loader verifies the manifest layout
   version, the custody manifest SHA-256 and schema, the manifest's
   controller digest, the running commit-bound controller digest, the
   import-request digest, the set identity, the selected stage's count
   and aggregate, and the receipt, then reads `private/<stage>/index.json`
   by exact path, verifies it against the manifest's stage index digest,
   and opens, hashes, and decodes **only the selected stage's** twenty
   blobs at the exact paths that index names, under the versioned payload
   contract
   [sealed-task-payload.schema.json](../schemas/sealed-task-payload.schema.json).
   It performs no `listdir`, `scandir`, `walk`, `glob`, or `iterdir`
   anywhere in the custody directory and builds no path under the other
   stage, so a development run does not open, decode, list, stat, or
   otherwise observe holdout-private identifiers, digests, order, blob
   filenames, or bytes, and vice versa. The selected stage completes
   unaffected when the other stage's index, blobs, or directory are
   missing, corrupt, unreadable, or mode-invalid; the same damage to the
   selected stage fails closed. Duplicate task identifiers, inconsistent
   scenario copies, and malformed payloads are rejected. Tasks execute in
   task-identifier order; `sample_design.seed` remains the recorded
   generation seed and no longer selects the schedule for sealed cells.
5. **Explicit task source; no catalog-digest overload.** The run manifest
   `workload` block carries a `task_source` discriminator. Catalog runs
   (C1, C2, P1, P2 freeze, every mock run) record
   `task_source = {"kind": "catalog"}` and `catalog_digest` with its
   original meaning, and carry no sealed provenance. Sealed runs record
   `task_source = {"kind": "sealed", "digest": <stage aggregate>,
   "sealed_set": {<nine binding fields>}}` and carry **no**
   `catalog_digest`. The run-manifest schema (3.1.0, additive) requires
   `catalog_digest` when and only when the source is not sealed, forbids
   `sealed_set` outside `task_source`, and requires `task_source.digest`
   to equal `sealed_set.stage_aggregate_digest`; manifests written before
   this decision carry no `task_source` and validate as catalog runs.
   Verification fails if a stage aggregate is substituted for a catalog
   digest, if a catalog digest is substituted for a stage aggregate, if a
   catalog manifest carries sealed provenance, if a sealed manifest lacks
   the discriminator, if any sealed field differs from the executed
   binding, if the per-task evidence does not cover exactly the sealed
   task identifiers and scenario set, or if the aggregate or counts
   disagree. The public receipt records the same nine-field `sealed_set`.
   Receipts, manifests, console output, and errors never contain task
   bodies, accepted answers, prompts, completions, task identifiers,
   private filenames, or the custody path.
6. **Fail closed before the model client.** Every binding and custody
   failure (missing or malformed binding, stage or count mismatch,
   missing or misplaced custody directory, unsupported layout version,
   permissive modes, manifest hash, controller, import-request, identity,
   aggregate, index digest, receipt, tamper, malformed task, duplicate
   task, task-source or provenance mismatch) aborts before the model
   client is constructed, before the endpoint is contacted, and before
   the results directory or ledger is touched. There is no fallback to
   the catalog, to config copies, to the ledger, or to the provider.
   Failures report a short reason code only.
7. **Offline validation.** `qualify-agent --validate-only --custody-dir
   <dir>` performs the same stage-specific binding and stage load with
   zero model calls, zero endpoint contact, no approval check, and no
   `LAB_RESULTS_DIR` access, and prints a content-free report (counts,
   digests, identity, stage, `custody_access: stage-specific`,
   `other_stage_observed: false`, and `model_client_constructed: false`).
   It does not perform full-custody verification.
8. **Re-import requirement for any previously imported package.** A real
   package finalized under the 1.1.0 combined-index layout is not
   eligible for P2 execution and is not altered. Before any P2
   development or holdout execution, the unchanged real task source
   bundles must be re-prepared and re-imported under the stage-separated
   layout into a new, empty external directory from a frozen canonical
   commit that contains this change. That yields a new commit-bound
   controller digest, a new import-request digest, and a new
   custody-manifest digest, and requires a new owner approval phrase
   bound to the new request digest; the resulting frozen P2 configs must
   carry those new values. Because the aggregate and set-identity
   algorithms are unchanged, a faithful re-import of the same bundles
   reproduces the historical stage aggregates and set identity.
9. **Integrity, not confidentiality.** As under D-0023, SHA-256 digests
   bind bytes and do not conceal them. This decision adds no encryption,
   no cryptographic seal, and no global anti-replay; replay protection
   remains local to the selected custody directory.

**Rationale.** Owner authorization to make the sealed custody stage the
only input to P2 development and holdout execution before any real set
exists, so that the qualification cannot silently fall back to public
catalog tasks, so that holdout-private metadata and bodies are never
observed by a development run, and so that a stage aggregate is never
recorded under a field whose meaning is the public catalog digest. Items 3,
4, 5, and 8 were corrected in review of the first implementation, which
had read a combined private index covering both stages and had recorded the
stage aggregate as `workload.catalog_digest`; no sealed run predates that
correction. Tested only with synthetic custody fixtures.

## 2026-10-04 — D-0025: Offline materialization of sealed qualification tasks

**No real bundle is read, materialized, imported, or executed by this
decision.** No qualification result predates it. The evaluator, scenario
catalog, thresholds, prompts, sampling rules (`sampling.py` and
`generate_task_instances`), model, engine, precision, infrastructure, P2
behavior, the D-0024 payload schema and decoder, the D-0023 custody
controller, and the D-0024 execution adapter are unchanged. D-0022,
D-0023, and D-0024 are not edited. Live materialization, live import, live
`qualify-agent`, and Phase 4 remain unauthorized and keep their separate
digest-bearing approval phrases.

**Problem.** The historical external source entries carry a `task_id` and a
scenario document but not the D-0024 `instance` object (`instance_seed`,
`tracking_id`, `reported_minute`) that the payload contract requires, so
they cannot be imported as execution-eligible sealed tasks as they stand.
A first implementation derived each task's occurrence from the order of the
historical source task identifiers and preserved whatever per-scenario
distribution the sources carried. That preserved only the per-template
surface multiset, not the frozen ordered qualification schedule, and was
rejected in review. This decision replaces it; no bundle was materialized
under the rejected design.

**Decision.** A narrowly scoped offline workflow,
`python -m blackwell_lab.cloud.sealed_materialize`, produces the frozen
qualification schedule as D-0024 payloads. Its contract is:

1. **The one-call production generator defines the schedule.** For each
   stage the materializer makes exactly one call
   `generate_task_instances(stage_spec(stage)["template_ids"],
   stage_spec(stage)["tasks"], MEASURED_REPETITION_SEED)` — the call the
   qualification runner makes for the measured repetition of a catalog
   cell of that stage. The returned twenty-element round-robin sequence is
   the sole authoritative execution schedule: its order, its scenarios,
   and its `(scenario_id, instance_seed, tracking_id, reported_minute)`
   tuples. Templates are never generated separately, occurrence is never
   calculated from source task identifiers, and no second derivation,
   placeholder, timestamp, random value, or task-body-derived value
   exists.
2. **Seed.** `MEASURED_REPETITION_SEED` is `20260907`
   (`FROZEN_SEED + 1`): the runner builds the stage spec with
   `seed = FROZEN_SEED` and derives the generator seed of the single
   measured repetition as `spec.seed + repetition_index` with
   `repetition_index == 1` (`STAGE_REPETITIONS == 1`, no warm-up pass).
   The request schema pins the seed as a constant.
3. **Task-identifier order is not the catalog occurrence rule.** D-0024
   executes a sealed stage in task-identifier order; that order carries no
   occurrence semantics of its own. Output identifiers are therefore
   chosen so that task-identifier order *is* the generator order: the
   fixed values `sealed-development-0000` … `sealed-development-0019` and
   `sealed-holdout-0000` … `sealed-holdout-0019`, where the suffix is the
   zero-based position of the slot in the full round-robin generator
   sequence. Lexicographic order of these identifiers equals materializer
   output order, custody index order, sealed runtime order, and the
   measured order of a P1 catalog cell. Each payload's `instance_id` is
   its fixed identifier.
4. **Source identifiers do not influence materialized execution.**
   Historical source task identifiers are not preserved as executable
   identifiers and do not control occurrence, output identity, prompt
   surface, or runtime order. They remain bound only through the source
   aggregate (task-identifier-ordered set identity) of the request, and
   must be valid and unique for the request to exist.
5. **Exact frozen source gate.** Before a request is approvable the source
   set must satisfy, per stage: exactly twenty entries; each entry is a
   JSON object with exactly `task_id` (equal to its filename) and
   `scenario`, carrying no envelope or `instance` object (`source-shape`);
   unique identifiers (`duplicate-id`); every `scenario_id` a member of
   `DEVELOPMENT_TEMPLATE_IDS` for development and `HOLDOUT_TEMPLATE_IDS`
   for holdout, with no unknown, missing, substituted, or swapped scenario
   (`scenario-set-mismatch`); every scenario document, including repeated
   copies, canonically deep-equal to `catalog()[scenario_id]`
   (`scenario-mismatch`); and a per-template multiset equal to the
   one-call generator schedule — 4/4/3/3/3/3 for development and 5/5/5/5
   for holdout (`distribution-mismatch`). Stage disjointness and task
   count are enforced by the unchanged custody validator. **Arbitrary and
   non-frozen distributions are rejected**; nothing is preserved or
   rebalanced.
6. **Payload construction.** For each slot the payload is encoded only by
   the production `encode_sealed_task` from the slot's `TaskInstance`
   surface, the canonical equality-verified catalog scenario, and the
   fixed identifier; it is decoded with `decode_sealed_task`, must
   re-encode byte for byte, and the ordered tuples of the decoded sequence
   must equal the one-call generator result exactly. A sorted multiset
   comparison is not accepted anywhere in the implementation or its tests.
7. **Set identity and import order.** The predicted output set identity is
   computed over the payload digests in fixed identifier order, which is
   the identity the unchanged D-0023 importer records when the bundles
   are handed to it in that order. The `prepare-import` and
   `import-materialized` operations load the materialized directories
   with the production bundle loader, order the tasks by their fixed
   identifiers, require exactly the fixed identifier set of each stage
   (`materialized-ids`), and delegate to the unchanged
   `prepare_import_request` and `import_authorized_set`. No step depends
   on `Path.iterdir()`, filesystem creation order, or directory iteration
   order. The D-0023 loader was not modified.
8. **Operations and approval.** `prepare-materialization` prints a
   content-free request and writes nothing. `materialize-bundles` writes
   `development/` and `holdout/` bundle directories (filename = fixed
   task id, `0600` files, `0700` directories) plus a content-free
   `materialization.json` record into a **new** external directory; an
   existing destination, even empty, is `destination-exists` and is never
   overwritten. The request (schema
   [sealed-materialization-request.schema.json](../schemas/sealed-materialization-request.schema.json))
   binds the frozen commit (clean worktree required), the implementation
   digest (committed and running bytes of `sealed_materialize.py`,
   `sealed_payload.py`, `sampling.py`, and the payload schema, which must
   be the worktree files), the generator name, seed, seed rule, schedule
   rule, task-identifier rule, tasks per stage, catalog digest, the frozen
   template lists, payload version, source and predicted output
   aggregates, counts, set identities, and frozen distribution summaries.
   The approval phrase is exactly
   `I approve sealed qualification-task materialization using request sha256:{digest}`;
   the D-0023 import phrase does not authorize materialization and vice
   versa. Written bundles are re-read with the production bundle loader
   and compared with the schedule and the request before the record is
   written; any difference scrubs the new directory.
9. **Content-free output.** Requests, records, console output, and errors
   never contain task bodies, task identifiers, accepted answers, blob
   names, or paths. Failures print `BLOCKED: <reason>` only. Scenario
   template identifiers are public catalog identifiers and appear in the
   request only as the frozen template lists.
10. **Downstream unchanged.** The materialized bundles are ordinary D-0023
    input, imported into a new custody directory by the unchanged custody
    tool, and decoded by the unchanged D-0024 adapter. The materializer
    is not part of `CONTROLLER_SOURCE_PATHS`, so the custody controller
    digest is unaffected.
11. **Integrity, not confidentiality.** As under D-0023 and D-0024,
    SHA-256 digests bind bytes and do not conceal them.

**Rationale.** A sealed stage must execute the same schedule a P1 catalog
cell executes, so the only acceptable source of that schedule is the
unchanged production generator called exactly as the runner calls it. The
historical sources prove that the owner holds the frozen set, through the
exact gate and the source aggregate, but they cannot define the schedule,
because their identifier order carries no occurrence semantics. Tested
only with synthetic source entries built from the public scenario catalog,
including adversarial rejection tests for scrambled identifiers, swapped
stages, modified or substituted scenarios, grouped input, 20 copies of one
scenario, unknown identifiers, 11/9 and shifted distributions, 19 and 21
entries, duplicate identifiers, and reversed directory iteration.

## 2026-10-05 — D-0026: Candidate P2C, the controlled public-catalog qualification of evidence-grounding-v1

**This decision authorizes no inference, no provider access, no
provisioning, no model download, and no live qualification.** No
qualification result predates it. The evaluator (3.1.0), accepted answers,
thresholds, scenario catalog, system prompts, tool-description prose,
sampling rules, model, engine, precision, container, infrastructure,
candidates C1, C2, P1, and P2, the D-0023 custody controller, and the
D-0024 execution adapter are unchanged. D-0022 through D-0025 are not
edited.

**Problem.** P2 (D-0022) binds workload 2.5.0 and the
`evidence-grounding-v1` controller, but under D-0024 its development and
holdout stages execute a private sealed custody stage rather than the
public catalog schedule that P1 executes. A P2 score therefore differs
from a P1 score in two respects at once: the controller and the task
source. The effect of the controller alone cannot be isolated from the
P1/P2 pair, and the sealed stages are not yet materialized or imported.

**Decision.** Candidate **P2C** is added as the controlled public-catalog
version of P2. Its contract is:

1. **Identity.** `candidate_id` `P2C`; `workload_version` `2.5.0`;
   controller `evidence-grounding-v1`; system prompt `SYSTEM_PROMPT_V241`
   byte for byte (SHA-256
   `37b3a4fb615dc21c8d39a5301dc4318870fea3498fbed196c50bfcbe67de1bd3`);
   temperature 0.2; `top_p` 0.95; seed 20260906; `max_tokens` 1024;
   evaluator 3.1.0; task source the public catalog; no `sealed_set`; no
   custody directory. Model, engine, precision, container, and
   infrastructure pins are those of every other candidate. The identity
   digest is produced by the unchanged candidate serialization
   (`49b279fb76dfa3ee3da2dbf8bdd15c7547441faa386c14c97f7787aeff1a069a`
   at this commit); the C1, C2, P1, and P2 serializations and digests are
   byte-identical to before.
2. **P1 is the valid control.** P2C differs from P1 only in
   `candidate_id`, `workload_version`, `controller`, and the
   version-bound `evidence_refs` argument of the `recommend_remediation`
   native-tool schema that the controller requires. System prompt bytes,
   tool-description prose, scenarios, schedule, sampling, generation
   pins, model, serving, and evaluator are identical. **P2C tests the
   effect of `evidence-grounding-v1`** and nothing else.
3. **Schedule.** P2C development, holdout, and freeze use exactly the
   existing D-0019 catalog scheduling path: the frozen 6/4 template split,
   20/20/200 tasks, zero warm-up passes for development and holdout and
   one for freeze, one measured repetition, seed derivation
   `FROZEN_SEED - i - 1` for warm-up pass `i` and `FROZEN_SEED + 1` for
   the measured repetition, and the public catalog scenario bytes. The
   ordered P2C schedule equals the ordered P1 schedule at every stage.
   P2C never calls `load_sealed_stage` and never reads custody.
4. **Rejections, before any model client exists.** A `sealed_set`
   section, a `--custody-dir` argument, any workload other than 2.5.0,
   any controller other than `evidence-grounding-v1`, a different
   prompt, temperature, evaluator, seed, `top_p`, or `max_tokens`, any
   schedule-overriding or private-scenario key (`template_ids`,
   `frozen_template_id(s)`, `private_scenario(s)`, `scenarios`,
   `scenario_ids`, `task_source`, `custody_dir`), and any task count,
   warm-up, or repetition count other than the stage's are refused by
   config validation. The running code is re-checked against the frozen
   contract (`require_p2c_contract`) at the same point, and the
   assembled run specification is re-checked for catalog execution
   immediately before `OpenAICompatibleClient` is constructed. No turn
   is streamed on any of these paths.
5. **P2 is preserved.** P2 remains the sealed candidate: its development
   and holdout still require the D-0024 binding and custody directory,
   remain blocked until the private sets are materialized and imported,
   and are not deleted or repurposed. The sealed-candidate table is still
   exactly `("P2",)`.
6. **Instruments are not interchangeable.** The private sealed variants
   are a **different future instrument**. **Private-set scores cannot be
   compared with P1/P2C catalog scores**, and P2C provides **no
   blind-generalization evidence**: its tasks are the public catalog that
   candidate wording was developed against. Reports must keep P2C
   results in the controlled-catalog category and must not present them
   as holdout generalization.
7. **What success may authorize.** Successful P2C qualification may
   authorize the frozen catalog workload (workload 2.5.0 with
   `evidence-grounding-v1`) for cross-cloud comparison, only through the
   existing approval process (owner decision in this log plus the
   separate digest-bearing approval phrases). It authorizes nothing by
   itself.
8. **Provenance.** P2C receipts and validate-only reports carry a
   content-free `controlled_experiment` block (`control_candidate: P1`,
   `treatment: evidence-grounding-v1`, `task_source: catalog`,
   `blind_generalization_evidence: false`,
   `comparable_with_private_sealed_scores: false`). Other candidates'
   receipts are unchanged. Run manifests record
   `workload.task_source = {"kind": "catalog"}` with the public
   `catalog_digest`, as for every catalog cell.
9. **No new private support.** This decision adds no private scenario,
   frozen-template, or alternative task-source mechanism; the only
   mentions of such keys in the codebase are the P2C refusal list.
   Controller text (observation identifiers, eligibility codes, the
   single rejection category, and the counts-only summary) carries no
   catalog answer or scenario content.

**Rationale.** A controlled comparison needs a control that differs in
one treatment. P1 on the public catalog and P2C on the same public
catalog, same prompt bytes, same schedule, same evaluator, differ only in
the controller, so the P1/P2C pair measures the controller. The sealed P2
instrument answers a different question (generalization to tasks the
wording was never developed against) and stays separate. Tested only with
the public catalog and synthetic fixtures; no real bundle, custody
package, private result, provider, credential, inference, download,
publication system, or the secondary repository was accessed.

## 2026-10-05 — D-0027: Fixed qualification region us-iad-2 and same-session P1/P2C development control

**Decision.** The fixed Akamai qualification infrastructure region changes
from the historical `us-sea` attempt to exactly `us-iad-2`. There is no
region list, dynamic fallback, automatic retry, or provider selection.
One `g3-gpu-rtxpro6000-blackwell-1`, one run-tagged firewall, a six-hour
TTL, the $3.00/hour planning rate, and the $18 six-hour exposure are
unchanged. External Terraform state, Terraform 1.9.8, and the provider
lock are unchanged. Lifecycle, reconciliation, emergency teardown,
privacy, and approval controls are unchanged.

A `us-sea` create was refused because that plan was not available in the
selected region. Recovery verified the state empty. No resources were
billed. That attempt, its receipts, and the historical P1 evidence stay
historical. This decision does not rewrite D-0022 through D-0026 and does
not redefine those results. Advertised availability is not capacity proof.
Changing the region means the historical `us-sea` P1 is not the matched
control for a later P2C run.

P2C **development** must bind a completed, verified P1 **development**
control from the same run tag, lifecycle ledger, and resource identity,
and from the same canonical commit, region, model, engine, precision,
container and model artifacts, evaluator, catalog schedule, seeds, and
generation pins. P1 development executes first and writes a content-free
control record. P2C development authenticates that record, the P1 result,
and the ledger by digest before a model client is constructed and before
any turn is streamed. A missing, stale, failed, mismatched, substituted,
cross-run, cross-region, cross-resource, or historical control fails
closed with no provider mutation, client, endpoint contact, or result
write. P1 and P2C keep distinct run labels and approval phrases. P2C
cannot fall back to historical P1 evidence. Holdout and freeze do not
carry this binding.

The P1/P2C experimental difference stays the D-0026 surface:
`candidate_id`, `workload_version`, `controller`, and the version-bound
`evidence_refs` schema. Prompts, catalog tasks, evaluator 3.1.0, accepted
answers, thresholds, scheduling, model, engine, precision, and serving
pins are unchanged. Only a fully completed, non-stopped, verified P1
development measurement may be the control. A stopped gate, including a
project quality-floor stop, a failure record, or a missing result may not.
The control binds the P1 receipt, the terminal `qualification_completed`
event, and the frozen precision. A production firewall ledger entry may
omit region; the region binding is the authenticated instance region.
Before live provenance, P1 development validates that reconciled session:
run tag, provider check, the expected instance and firewall, provider
ids, labels, the `us-iad-2` instance region, and a valid resource
identity. The `qualification_completed` event carries the P1 run label
and config digest. Exactly one such event is authenticated with the
receipt. `verify-results` recomputes the resource identity from the
ledger.

Candidate identity digests include `region`. Moving the lock from
`us-sea` to `us-iad-2` is the only serialization change. Digests at the
previous lock, then at `us-iad-2`:

| Candidate | Previous (`us-sea`) | D-0027 (`us-iad-2`) |
| --- | --- | --- |
| C1 | `76510b8d3829f69ee8406680f7861ec38ab9d831052506c69819cf90cc3b1969` | `ce95fe585d24bd427aa8cd470f089c051eed6af3ce42ded7bea21bd3c93f7f7c` |
| C2 | `79cd85134d8c662b65e082bb3203b68e862f89f6083b69a0882022306ae01145` | `99ca5e56fd9dc37de069e23c48a08f6b95dd0256f05416eff12791092adfcba9` |
| P1 | `674280ef9090933d170a8000b4e67ca914310ea0261bc1ffcda36d7692a590d2` | `bc7d60758d21c040cc58a6b63490f7d0e7a4f26158a7da6c6286035316aa84c9` |
| P2 | `478a0ce881b22be8e5e747eafb63a6453b51540492dc1dba497bd8e28d160e44` | `20fe6cfbc084fb79d646b88a3c75ac490fad3040b1d58581b585988a9dac1ee6` |
| P2C | `49b279fb76dfa3ee3da2dbf8bdd15c7547441faa386c14c97f7787aeff1a069a` | `f9f3bb323f675674b0e5cffa84518e1007882fced1e8e71fec9275dd40885e4b` |

**This decision does not authorize** holdout, freeze, cross-cloud
inference, publication, provisioning, downloads, or live execution. Those
still require their own decisions and exact approval phrases.

**Rationale.** The failed `us-sea` attempt cannot be reused as the control
for a run in another region. A same-session digest binding is what makes
the new P1 the P2C development control. Tested with synthetic fixtures
only. No provider, credential, inference endpoint, download, private
result, custody package, publication system, or the secondary repository
was accessed.
