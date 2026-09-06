# Akamai Terraform — Phase 3 baseline instance

Terraform configuration for **exactly one** single-GPU NVIDIA RTX PRO 6000
Blackwell Server Edition instance, used for one owner-approved benchmark
session at a time. Everything here is Phase 3A *readiness* material: nothing
is provisioned until the owner separately authorizes Phase 3B and approves an
apply.

## Safety contract (do not weaken)

| Rule | Mechanism |
| --- | --- |
| Plan/dry-run is the default | `blackwell-cloud plan --run-tag <tag>` (the only verb the readiness workflow needs) |
| Apply requires explicit approval | `blackwell-cloud apply` refuses without the verbatim phrase `I approve creating billable Akamai resources for run <tag>` |
| Destroy requires separate approval | `blackwell-cloud destroy` refuses without the verbatim phrase `I approve deleting the exact recorded resources for run <tag>` |
| No hosted execution | apply/destroy refuse when CI/hosted-agent environment markers are present |
| Exact resource ledger | after apply, `terraform show -json` is recorded as `<run>.ledger.json` under the external `LAB_RESULTS_DIR`; teardown targets **only** ledger-recorded resources |
| Never broad cleanup | there is no sweep/cleanup verb anywhere; the orphan report is read-only |
| Single GPU only | variable validation rejects multi-GPU plan ids |
| No secrets in Git | the provider reads `LINODE_TOKEN` from the environment; state and `*.tfvars` are gitignored |
| Unique tags | every resource carries `blackwell-lab`, `run:<tag>`, `ttl-hours:<n>`, `phase:3` |

**Billing reality (cost guardrails):** Akamai bills for a GPU Linode while it
*exists on the account*, powered on or off. Powering off does **not** stop
charges; the in-instance watchdog limits runaway workload only. The session
ends by exporting and verifying results, then running the owner-approved
teardown (deletion) and the orphan report.

## Files

- `versions.tf` — Terraform `>= 1.9, < 2.0`; provider `linode/linode` pinned
  to `4.1.0`.
- `variables.tf` — validated inputs; no defaults that embed account data.
- `main.tf` — the single `linode_instance` resource and its tag set.
- `outputs.tf` — ledger inputs; the IP output is marked sensitive.
- `bootstrap/` — idempotent instance bootstrap (see below).
- `.gitignore` — state, tfvars, plans, and plugin caches never enter Git.

## Operator workflow (local, authenticated environment only)

```bash
# 0. read-only preflight: entitlement, eligible regions, account price
python scripts/preflight/check_akamai.py

# 1. write terraform.tfvars locally (never committed), e.g.:
#    run_tag            = "p3-pilot-20260907a"
#    region             = "us-ord"
#    gpu_instance_type  = "<exact plan id from the preflight output>"
#    authorized_ssh_key = "ssh-ed25519 AAAA... operator"
#    ttl_hours          = 6

# 2. always start with the plan (dry run, free)
blackwell-cloud plan --run-tag p3-pilot-20260907a

# 3. apply only with the owner's verbatim approval phrase
blackwell-cloud apply --run-tag p3-pilot-20260907a \
  --approve "I approve creating billable Akamai resources for run p3-pilot-20260907a"

# 4. bootstrap the instance (see bootstrap/), run the approved pilot,
#    export + verify results
blackwell-cloud verify-results

# 5. teardown: exact recorded resources only, then verify nothing remains
blackwell-cloud teardown-plan --run-tag p3-pilot-20260907a
blackwell-cloud destroy --run-tag p3-pilot-20260907a \
  --approve "I approve deleting the exact recorded resources for run p3-pilot-20260907a"
blackwell-cloud orphan-report --run-tag p3-pilot-20260907a
```

## Bootstrap (`bootstrap/`)

`bootstrap.sh` is **idempotent** (marker files; safe to re-run) and refuses
to serve until every pin is frozen:

1. OS assumption check (Ubuntu 24.04 LTS, the Terraform image pin).
2. NVIDIA driver (R580+ branch) and CUDA 13.x compatibility checks; exactly
   one RTX PRO 6000 GPU.
3. Container runtime (docker + nvidia-container-toolkit) with a GPU probe.
4. Serving image pulled **by immutable digest** (`VLLM_IMAGE_DIGEST`), never
   by tag alone. Candidate: vLLM `v0.28.0` (CUDA 13.0), BF16.
5. **Model artifact digest verification** — every file checked against the
   frozen `sha256sum` manifest before serving starts.
6. Serving container bound to loopback only; health (`/health`) and
   readiness (`/v1/models` lists the pinned model) checks.
7. Workload watchdog (systemd timer): powers off after idle —
   **documented as NOT a billing-control substitute**.

Pins live in `bootstrap.env` (from `bootstrap.env.example`). The pilot runs
with candidate pins; the full baseline runs only after the owner freezes the
digests observed during the successful pilot (decision D-0012).
