# Decision Log

Append-only record of project decisions, methodology revisions, and incidents.
Every entry states the date, the decision, the rationale, and — for
methodology changes — which results (if any) predate the change.
Methodology may not be revised after examining results without an entry here
([../AGENTS.md](../AGENTS.md), section 4).

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

**Decision.** Propose 14 vCPUs / 100 GiB RAM as the common container envelope,
derived from the smallest candidate host (`g7e.4xlarge`: 16 vCPU / 128 GiB).
**Provisional** — finalized only after Phase 3 empirically confirms headroom
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
