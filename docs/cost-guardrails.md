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
3. **No idle GPUs.** GPU instances are stopped or destroyed at the end of
   every working session. All three target providers bill GPU instances
   hourly with no monthly cap, so idle time is the dominant cost risk.
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
| 3 — Akamai baseline | ≈ $130–$200 | 75% of estimate consumed |
| 4 — NVIDIA optimization | ≈ $180–$300 | 75% of estimate consumed |
| 5 — Google Cloud | ≈ $190–$290 | 75% of estimate consumed |
| 6 — AWS | ≈ $170–$330 | 75% of estimate consumed |

Reaching a trigger pauses provisioning until the owner reauthorizes.

## Automatic shutdown and orphan detection

Per-provider options are assessed in
[feasibility-report.md](feasibility-report.md), section 9. Minimum standard
for Phases 3–6:

1. **In-instance idle watchdog.** A systemd timer inside every GPU instance
   shuts the instance down after a configured idle period (no active benchmark
   process), as a provider-independent backstop.
2. **Provider-native controls where available.** Google Cloud instance
   schedules / max-run-duration; AWS CloudWatch low-utilization auto-stop and
   AWS Budgets alerts; Akamai relies on the watchdog plus manual checks.
3. **End-of-session orphan report.** A read-only sweep lists all project-tagged
   resources (and any untagged GPU resources) at the end of each session. The
   report goes to the owner; deletions still require explicit approval.
4. **Billing alerts.** Where the account permits, billing alerts are set at
   the phase budget and at 150% of it.

## Cost recording

Every run manifest records the instance type, region, and list price basis in
effect, so cost per successful task can be computed without consulting private
billing data. Actual billed totals per phase are recorded privately and
summarized in the decision log at phase close.
