# Akamai Terraform — Phase 3 baseline instance

Terraform configuration for **exactly one** single-GPU NVIDIA RTX PRO 6000
Blackwell Server Edition instance plus its run-tagged Cloud Firewall, used for
one owner-approved benchmark session at a time. Everything here is Phase 3A
*readiness* material: nothing is provisioned until the owner separately
authorizes Phase 3B and approves an apply.

## Safety contract (do not weaken)

| Rule | Mechanism |
| --- | --- |
| External state only | `blackwell-cloud init` configures a **local backend outside Git** under the absolute `LAB_RESULTS_DIR`; Terraform state, `TF_DATA_DIR`, variable files, saved plans, and lifecycle records never live in the repository |
| Plan/dry-run is the default | `blackwell-cloud plan --run-tag <tag>` saves a binary plan, a redacted review copy, and SHA-256 metadata outside Git |
| Apply requires digest-bearing approval | `blackwell-cloud apply` executes **only** the exact saved plan after re-verifying its SHA-256, configuration, lock file, state, and commit; the approval phrase is verbatim: `I approve creating billable Akamai resources for run <tag> using plan sha256:<digest>` |
| Provider reconciliation required | `blackwell-cloud reconcile` must succeed with a read-only `LINODE_TOKEN` before the pilot; reconciliation is clean only when state is readable, the provider API was checked, and state matches provider observations exactly |
| Destroy requires provider verification first | `teardown-plan` and `destroy` refuse unless every ledger resource is verified against state **and** the read-only provider API **before** any Terraform destroy plan is generated or applied |
| Destroy requires digest-bearing approval | The destroy approval phrase is verbatim: `I approve deleting the exact recorded resources for run <tag> using destroy plan sha256:<digest>` |
| Deletion must be confirmed | Success is reported only after read-only polling confirms every recorded provider id is gone; on timeout the operator is told billing may continue |
| No hosted execution | apply/destroy refuse when CI/hosted-agent environment markers are present |
| Exact resource ledger | after apply, reconciliation writes the ledger under `LAB_RESULTS_DIR`; teardown targets **only** ledger-recorded resources |
| Never broad cleanup | there is no sweep/cleanup verb; the orphan report is read-only |
| Single GPU + firewall | exactly one `linode_instance` and one `linode_firewall`; SSH only from `management_cidr`; no public model-serving port |
| No secrets in Git | the provider reads `LINODE_TOKEN` from the environment; state and `*.tfvars` are gitignored and stored externally |
| Pinned Terraform CLI | **exactly Terraform 1.9.8** in `versions.tf`, CI, and lifecycle validation; other versions are refused |

**Billing reality (cost guardrails):** Akamai bills for a GPU Linode while it
*exists on the account*, powered on or off. Powering off does **not** stop
charges; the in-instance watchdog limits runaway workload only. The session
ends by exporting and verifying results, then running the owner-approved
teardown (deletion), deletion confirmation, and the orphan report.

## Files

- `versions.tf` — Terraform **= 1.9.8**; provider `linode/linode` pinned to `4.1.0`; empty `backend "local" {}` for external state configuration at init.
- `variables.tf` — validated inputs including `management_cidr` (rejects `0.0.0.0/0` and `::/0`).
- `main.tf` — the single GPU instance and its run-tagged firewall (inbound DROP; SSH from management CIDR only).
- `outputs.tf` — ledger inputs including firewall id/label; instance IPv4 is sensitive.
- `bootstrap/` — idempotent instance bootstrap with pinned driver/toolkit/docker packages and digest-pinned CUDA GPU probe.
- `.gitignore` — state, tfvars, plans, and plugin caches never enter Git (state is **external**, not merely gitignored in-place).

## Operator workflow (local, authenticated environment only)

All paths below assume an absolute external results directory:

```bash
export LAB_RESULTS_DIR=/absolute/private/path/outside/this/repository
export LINODE_TOKEN=...   # read-only scope; never committed or printed
```

### 0. Authenticated preflight (requires `--region`)

The public catalog check alone is **not** readiness. Authenticated readiness
requires the exact one-GPU Blackwell plan, confirmed deployability in the
selected region, deployment capability, and the applicable regional price:

```bash
python scripts/preflight/check_akamai.py --region us-ord
# exit 0 only when every decision passed; writes a sanitized receipt under
# $LAB_RESULTS_DIR/preflight-receipts/ (path not printed)
```

Use `--public-only` for the informational catalog check without a token.

### 1. Initialize external Terraform state for the run

```bash
blackwell-cloud init --run-tag p3-pilot-20260907a
```

This configures the external local backend, external `TF_DATA_DIR`, and the
run's private lifecycle directory under `$LAB_RESULTS_DIR`.

### 2. Create the external variable file (never committed)

Write `$LAB_RESULTS_DIR/lifecycle/<run-tag>/terraform.tfvars`, for example:

```hcl
run_tag            = "p3-pilot-20260907a"
region             = "us-ord"
gpu_instance_type  = "<exact plan id from authenticated preflight>"
authorized_ssh_key = "ssh-ed25519 AAAA... operator"
management_cidr    = "203.0.113.10/32"   # operator management IP only
ttl_hours          = 6
```

### 3. Plan (dry run, free) — saved outside Git

```bash
blackwell-cloud plan --run-tag p3-pilot-20260907a
```

Review the redacted plan text and recorded SHA-256 in the run's external
lifecycle directory. Apply-stage plans containing delete, replace, update, or
unrelated actions fail closed.

### 4. Apply (billable) — exact saved plan only

```bash
blackwell-cloud apply --run-tag p3-pilot-20260907a \
  --approve "I approve creating billable Akamai resources for run p3-pilot-20260907a using plan sha256:<digest-from-plan-output>"
```

A pending recovery record is written before apply; reconciliation runs after
every attempt. The pilot remains blocked until reconciliation reports clean.

### 5. Reconcile with the provider API

```bash
blackwell-cloud reconcile --run-tag p3-pilot-20260907a
```

Reconciliation is clean only when Terraform state is readable, the provider
API was successfully checked, state and provider observations agree exactly,
and no untracked or missing resource exists. Without a read-only token the
ledger records `reconciled=false` and retains any pending record.

### 6. Bootstrap the instance

Copy `bootstrap/` to the instance, copy `bootstrap.env.example` to
`bootstrap.env`, fill in **every** pinned package version, NVIDIA repository
URL/list entry, digest-pinned CUDA probe image, and model digest manifest, then:

```bash
/opt/bwlab-bootstrap/bootstrap.sh
# exit 2 means reboot required, then re-run bootstrap.sh
/opt/bwlab-bootstrap/fetch-model.sh   # HF_TOKEN from environment only
```

Bootstrap refuses empty version pins and never installs “latest”. The GPU
probe uses a digest-pinned NVIDIA CUDA image and runs `nvidia-smi` inside the
container to verify exactly one RTX PRO 6000 Blackwell GPU.

### 7. Short pilot (provider-native only)

```bash
blackwell-cloud pilot \
  --run-tag p3-pilot-20260907a \
  --run-label pilot-a \
  --config /path/to/pilot.json \
  --approve "I approve the short Akamai pilot for run pilot-a"
```

The pilot requires: no pending lifecycle operation; `reconciled=true`;
`provider_checked=true`; exactly one instance and one firewall in the ledger;
and live provenance re-verified **immediately before every cell** (observations
from the first cell are never reused). Controlled-resource mode is rejected
until joint cgroup enforcement is implemented.

### 8. Verify exported results

```bash
blackwell-cloud verify-results --subdir real-runs
```

An empty results directory is a failure when a pilot result is required.

### 9. Teardown — identity verified before any destroy plan

```bash
blackwell-cloud teardown-plan --run-tag p3-pilot-20260907a
# requires read-only LINODE_TOKEN; verifies ledger vs state vs provider API
# BEFORE generating the saved destroy plan

blackwell-cloud destroy --run-tag p3-pilot-20260907a \
  --approve "I approve deleting the exact recorded resources for run p3-pilot-20260907a using destroy plan sha256:<digest-from-teardown-plan>"
```

Deletion is confirmed by polling recorded provider ids. On timeout, billing
may continue — escalate and retry.

### 10. Orphan report and session cost summary

```bash
blackwell-cloud orphan-report --run-tag p3-pilot-20260907a
blackwell-cloud session-summary --run-tag p3-pilot-20260907a --hourly-price 3.50
```

The orphan report is read-only and never deletes anything.

## Bootstrap (`bootstrap/`)

`bootstrap.sh` is **idempotent** (marker files; safe to re-run) and refuses
to mutate the host until every package version, repository pin, and digest is
frozen:

1. OS assumption check (Ubuntu 24.04 LTS).
2. Pinned NVIDIA driver + Container Toolkit install (exact package versions;
   pinned apt repository key/list); reboot and post-reboot validation.
3. Pinned Docker runtime install (exact package version).
4. Digest-pinned NVIDIA CUDA probe image: `nvidia-smi` runs **inside** the
   container; verifies exactly one RTX PRO 6000 Blackwell GPU.
5. Serving image pulled **by immutable digest** (`VLLM_IMAGE_DIGEST`).
6. Container CUDA runtime validated separately from the driver's max CUDA.
7. Model artifact digest verification before serving.
8. Serving bound to loopback only; health and readiness checks.
9. Workload watchdog — **NOT a billing-control substitute**.

Pins live in `bootstrap.env` (from `bootstrap.env.example`). vLLM v0.28.0 is
an unvalidated candidate; the model card's recipe uses v0.27.1. The pilot
freezes the version (decision D-0012).
