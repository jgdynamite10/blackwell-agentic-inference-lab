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
