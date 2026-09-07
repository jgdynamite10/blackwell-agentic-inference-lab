# Cost Guardrails

Spend controls for every phase. These complement — and never override — the
safety rules in [../AGENTS.md](../AGENTS.md): no resource is created, resized,
modified, or deleted without explicit owner approval, regardless of cost.

## Hard rules

1. **Explicit approval per provisioning action.** Every instance launch names
   the provider, instance type, region, expected hourly rate, and expected
   session duration, and receives owner approval before creation.
2. **Single GPU at a time.** Phases 3–6 use exactly one single-GPU instance
   per provider, never concurrently across providers unless the owner
   explicitly approves an overlap.
3. **No idle GPUs — and know what actually stops billing.**
   - **Powering off an Akamai Linode does not stop billing.** Akamai bills
     for the service while it exists on the account, powered on or off.
     After authorized benchmark completion and verified result export to
     `LAB_RESULTS_DIR`, Akamai compute billing stops **only when the
     applicable service is deleted/removed from the account** (official
     sources: [Understanding how billing works](https://techdocs.akamai.com/cloud-computing/docs/understanding-how-billing-works),
     [Stop further billing](https://techdocs.akamai.com/cloud-computing/docs/stop-further-billing);
     retrieved 2026-09-06).
   - An in-instance shutdown watchdog **limits active workload but is not an
     Akamai spending backstop**.
   - On AWS and GCP, stopped-instance compute treatment differs from
     Akamai's, while disks, addresses, snapshots, and other resources may
     continue billing while an instance is stopped.
   - Therefore the end-of-session rule is: export and verify results, then
     **delete** the run's compute service (owner-approved), not merely stop
     it. GPU Linodes bill hourly with no monthly cap, so a forgotten
     powered-off instance is the dominant cost risk.
   - **Akamai access for this project is provided without a direct compute
     charge.** Normalized economic cost is still calculated using the
     applicable **$3/hour** planning rate (the owner-observed Seattle catalog
     base price). That rate is an economic-normalization convention for
     session summaries and planning estimates, not a claim about an invoice.
     Bootstrap fail-closed pin, key-content, and exact-package checks do not
     change that billing rule: only deletion stops charges.
4. **No reservations or commitments.** On-demand (or owner-approved Spot for
   non-measurement setup work) only. Committed-use discounts, savings plans,
   and reservations require a separate owner decision.
5. **Tag everything.** Every resource carries a `project=blackwell-lab` tag or
   label plus a `ttl` tag stating its intended teardown time.

## Budget expectations by phase

Planning estimates (details and assumptions in
[feasibility-report.md](feasibility-report.md), section 8):

| Phase | Estimate | Trigger for owner check-in |
| --- | --- | --- |
| 2 — Synthetic workload | ≈ $0 cloud | any cloud spend at all |
| 3 — Akamai baseline (full 12-cell estimate; **not authorized**) | ≈ $270–$690 before incidental costs at the owner-observed Seattle $3.00/h plan price (D-0014; public advertised starting price remains $2.50/h) | 75% of estimate consumed |
| 3B — authorized compatibility/headroom pilot only | **$25 total** / six hours maximum on one `us-sea` GPU instance plus its firewall | three elapsed hours (explicit owner decision required to continue) |
| 4 — NVIDIA optimization (**not authorized**) | ≈ $240–$600 before incidental costs at the same $3.00/h Seattle observed plan price | 75% of estimate consumed |
| 5 — Google Cloud | ≈ $280–$650 | 75% of estimate consumed |
| 6 — AWS | ≈ $250–$760 | 75% of estimate consumed |

Reaching a trigger pauses provisioning until the owner reauthorizes. The
Phase 3–6 ranges remain **planning estimates**, not authorization. Decision
D-0014 authorizes only the $25 / six-hour compatibility/headroom pilot; the
full Phase 3 estimate is not authorized. Akamai figures use the
owner-observed Seattle **$3.00/h** catalog base price and distinguish it
from the public **$2.50/h** advertised starting price. The Phase 3–4 ranges
reflect the D-0010 sample plan and the D-0012 12-cell matrix; measured-cell
hours scale with realized task latency
([feasibility-report.md](feasibility-report.md) §8).

## Automatic shutdown and orphan detection

Per-provider options are assessed in
[feasibility-report.md](feasibility-report.md), section 9. Minimum standard
for Phases 3–6:

1. **In-instance idle watchdog.** A systemd timer inside every GPU instance
   shuts the instance down after a configured idle period (no active
   benchmark process). This limits runaway *workload* only — on Akamai a
   powered-off Linode keeps billing, so the watchdog is **not** a spending
   backstop there.
2. **Provider-native controls where available.** Google Cloud instance
   schedules / max-run-duration; AWS CloudWatch low-utilization auto-stop and
   AWS Budgets alerts; Akamai has no documented native auto-stop scheduler —
   its spending control is prompt, owner-approved deletion after verified
   result export.
3. **End-of-session orphan report.** A read-only sweep lists all project-tagged
   resources (and any untagged GPU resources) at the end of each session. The
   report goes to the owner; deletions still require explicit approval, and
   teardown targets only resources recorded as created for the exact run.
4. **Post-teardown verification.** After any teardown, a read-only check
   confirms no project-created billable resources remain (instances, disks,
   addresses, snapshots).
5. **Billing alerts.** Where the account permits, billing alerts are set at
   the phase budget and at 150% of it.

## Required local-operator workflow (design contract for Phases 3+)

Future provider execution code must **default to dry-run or plan mode** and
require explicit local confirmation before any billable change. All
credentialed steps run in the owner's authenticated local environment — never
in the hosted Cloud Agent (AGENTS.md, section 3). Tooling must let a local
operator:

1. verify identity, account/project, region, quota, and estimated price;
2. generate a deployment plan;
3. review a maximum-cost estimate;
4. explicitly approve provisioning;
5. apply unique project/run tags or labels;
6. execute the benchmark;
7. export results to `LAB_RESULTS_DIR`;
8. tear down **only** resources recorded as created for the exact run
   (matched by the run's unique tags — never by broad filters);
9. verify that **no project-created billable resources remain** (including
   disks, addresses, and snapshots, which can continue billing after an
   instance stops or is deleted).

**Never implement a broad cleanup command** that could delete resources not
created by this project. Teardown operates strictly on the recorded run's
tagged resources, and **destructive teardown always requires explicit local
owner approval** — it is never automatic.

## Cost recording

Every run manifest records the instance type, region, and list price basis in
effect, so cost per successful task can be computed without consulting private
billing data. Raw provider bills and account-level cost records remain
external (never committed). At phase close, the decision log may record only
an **explicitly owner-approved sanitized summary** of phase costs — never raw
billing information.
