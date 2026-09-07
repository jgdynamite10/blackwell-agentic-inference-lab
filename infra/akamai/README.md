# Akamai Terraform — Phase 3 baseline instance

Terraform configuration for **exactly one** single-GPU NVIDIA RTX PRO 6000
Blackwell Server Edition instance plus its run-tagged Cloud Firewall, used for
one owner-approved session at a time. Phase 3A readiness is **complete**.
Decision D-0014 authorizes **only** one bounded Phase 3B
compatibility/headroom pilot. The full 12-cell baseline remains unauthorized.
Actual apply, pilot, and destroy still require their separate exact local
approval phrases.

## Safety contract (do not weaken)

| Rule | Mechanism |
| --- | --- |
| External state only | `blackwell-cloud init` configures a **local backend outside Git** under the absolute `LAB_RESULTS_DIR`; Terraform state, `TF_DATA_DIR`, variable files, saved plans, and lifecycle records never live in the repository |
| Plan/dry-run is the default | `blackwell-cloud plan --run-tag <tag>` saves a binary plan, a redacted review copy, and SHA-256 metadata outside Git |
| Apply requires digest-bearing approval | `blackwell-cloud apply` executes **only** the exact saved plan after re-verifying its SHA-256, configuration, lock file, state, and commit; the approval phrase is verbatim: `I approve creating billable Akamai resources for run <tag> using plan sha256:<digest>` |
| Provider reconciliation required | `blackwell-cloud reconcile` must succeed before the pilot; reconciliation is clean only when state is readable, the provider API was checked, and every resource matches by typed identity (Terraform address, type, provider ID, label, project tag, run tag, and instance region). Combined numeric-ID sets are not used |
| Destroy requires provider verification first | `teardown-plan` and `destroy` refuse unless every ledger resource is verified against state **and** the provider API **before** any Terraform destroy plan is generated or applied |
| Destroy requires digest-bearing approval | The destroy approval phrase is verbatim: `I approve deleting the exact recorded resources for run <tag> using destroy plan sha256:<digest>` |
| Deletion must be confirmed | Success is reported only after read-only polling confirms every recorded provider id is gone; on timeout the operator is told billing may continue |
| No hosted execution | apply/destroy refuse when CI/hosted-agent environment markers are present |
| Exact resource ledger | after apply, reconciliation writes the ledger under `$LAB_RESULTS_DIR/infra-lifecycle/<run-tag>/`; teardown targets **only** ledger-recorded resources |
| Never broad cleanup | there is no sweep/cleanup verb; the orphan report is read-only |
| Single GPU + firewall | exactly one `linode_instance` and one `linode_firewall`; SSH only from `management_cidr`; no public model-serving port |
| No secrets in Git | credentials are supplied only through local environment variables (`LINODE_TOKEN`, `HF_TOKEN` for model fetch); they are never committed, printed, or written into Terraform state |
| Pinned Terraform CLI | **exactly Terraform 1.9.8** in `versions.tf`, CI, and lifecycle validation; other versions are refused |

**Billing reality (cost guardrails):** Akamai bills for a GPU Linode while it
*exists on the account*, powered on or off. Powering off does **not** stop
charges; the in-instance watchdog limits runaway workload only. The session
ends by exporting and verifying results, then running the owner-approved
teardown (deletion), deletion confirmation, and the orphan report. Akamai
access for this project is provided **without a direct compute charge**;
normalized economic cost is still calculated at the applicable **$3/hour**
planning rate.

## Authorized Phase 3B pilot (decision D-0014)

Sanitized owner-verified facts (no resource IDs, account IDs, IPs, or raw
API payloads):

- exact plan `g3-gpu-rtxpro6000-blackwell-1` (one RTX PRO 6000 Blackwell GPU);
- selected region `us-sea`; observed catalog base price $3.00/hour; Seattle
  had no observed regional surcharge;
- an owner-run temporary capability probe reached running and was deleted;
  it created no firewall; a later read-only check found zero matching test
  instances, firewalls, and volumes; billing is not continuing for those
  test resources;
- that probe was **not** a genuine benchmark and collected no serving or
  result data.

The Seattle create/delete observation is stronger evidence for `us-sea` than
an additional `us-ord` connectivity preflight. `us-ord` was only an example
and is not the selected pilot region. A saved Terraform plan verifies the
intended configuration and planned actions only. It does not prove live
capacity. Capacity is known when the provider accepts provisioning and the
instance reaches the expected running state.

Authorized envelope: provider-native only; one GPU instance plus its one
project/run-tagged firewall; six hours maximum instance lifetime; **$25
total** session ceiling; owner checkpoint at three elapsed hours.
Diagnostic cells only: interactive/1, batch-heavy/4, batch-heavy/8 (one
warm-up, one measured repetition, 20 tasks each). Pilot observations must
not be represented as comparative benchmark findings. The full 12-cell
baseline is not authorized.

## Credentials and API access

All credentials stay in the operator's local environment. They are never
committed, echoed in logs, or stored in Git.

| Workflow | API access | Typical token scope |
| --- | --- | --- |
| Authenticated preflight (`check_akamai.py`) | GET only | read-only account/plan/region visibility |
| Reconciliation, teardown identity checks, orphan report, deletion confirmation | GET only | read-only Linode + Firewall visibility |
| Terraform `apply` / `destroy` | creates/deletes the saved plan's resources | minimum Linode **and** Firewall write permissions for the one instance and its firewall |

Preflight and reconciliation never mutate provider resources. Terraform apply
and destroy require the minimum write permissions needed to execute the
reviewed saved plan — not a read-only token.

## Files

- `versions.tf` — Terraform **= 1.9.8**; provider `linode/linode` pinned to `4.1.0`; empty `backend "local" {}` for external state configuration at init.
- `.terraform.lock.hcl` — official HashiCorp Registry checksums for `darwin_arm64` (operator laptop) and `linux_amd64` (CI / Linux operators), generated with `terraform providers lock`. Lifecycle and readiness init always pass `-lockfile=readonly`. Never edit checksums by hand.
- `variables.tf` — D-0014 locks (`region=us-sea`, `gpu_instance_type=g3-gpu-rtxpro6000-blackwell-1`, `ttl_hours=6`) plus `management_cidr` (rejects `0.0.0.0/0` and `::/0`).
- `main.tf` — the single GPU instance and its run-tagged firewall (inbound DROP; SSH from management CIDR only).
- `outputs.tf` — ledger inputs including firewall id/label; instance IPv4 is sensitive.
- `bootstrap/` — idempotent instance bootstrap with pinned driver/toolkit/docker packages and digest-pinned CUDA GPU probe.
- `.gitignore` — state, tfvars, plans, and plugin caches never enter Git (state is **external**, not merely gitignored in-place).

## Authorized live-pilot sequence

The bounded Phase 3B session, when separately owner-approved, follows this
order. Steps 2–5 and 12–13 run on the owner's laptop. Steps 6–10 run on the
GPU instance after a manual private copy. No step here is a completed
benchmark.

1. **Offline pin verification** — `blackwell-cloud readiness`, lockfile
   `-lockfile=readonly` init, and review of the public candidate pins in
   `bootstrap/bootstrap.env.example` (decision D-0015). These pins are not
   yet empirically validated on the target GPU.
2. **Reviewed apply plan** — `blackwell-cloud plan` saves the binary plan
   and SHA-256 outside Git.
3. **Apply approval** — `blackwell-cloud apply` with the digest-bearing
   phrase; hosted execution is refused.
4. **Reconciliation** — `blackwell-cloud reconcile` must be clean
   (`provider_checked=true`, exact instance + firewall identity).
5. **Immediate emergency teardown-plan generation** — `blackwell-cloud
   teardown-plan` immediately after a clean reconcile, so an
   identity-verified destroy plan exists before bootstrap. Saved plans older
   than one hour are stale and must be regenerated and re-reviewed before
   destroy; do not weaken that freshness check.
6. **Host bootstrap and reboot** — copy `bootstrap/` to the instance, copy
   the example to `bootstrap.env`, install the pinned GPU stack; exit 2
   means reboot and re-run.
7. **GPU/container verification** — post-reboot driver/max-CUDA checks,
   digest-pinned CUDA `nvidia-smi` probe, serving-image digest pull, and
   container CUDA runtime check.
8. **Pinned Nemotron download and digest manifest** — `fetch-model.sh`
   at the pinned Hugging Face revision; write and verify the per-file
   sha256 manifest (not present in Git).
9. **Final pilot-config digest and approval** — private config outside Git;
   `blackwell-cloud pilot` with the config SHA-256 approval phrase.
10. **Three diagnostic cells** — interactive/1, batch-heavy/4,
    batch-heavy/8 only.
11. **External result verification** — copy results back, then
    `blackwell-cloud verify-results`.
12. **Execution of the preapproved destroy plan** — `blackwell-cloud
    destroy` with the destroy-plan digest phrase. If the emergency plan
    from step 5 is stale, regenerate and re-review it first.
13. **Confirmed deletion and orphan report** — only explicit HTTP 404 is
    absence; then `orphan-report` (zero matching resources) and
    `session-summary --hourly-price 3.00`.

## Operator workflow (local, authenticated environment only)

All paths below assume an absolute external results directory:

```bash
export LAB_RESULTS_DIR=/absolute/private/path/outside/this/repository
export LINODE_TOKEN=...   # scope depends on the step; never committed or printed
```

### 0. Authenticated preflight (requires `--region`)

The public catalog check alone is **not** readiness. Authenticated preflight
uses **GET-only** API calls and requires the exact one-GPU Blackwell plan,
confirmed deployability in the selected region, deployment capability, and the
applicable regional price:

```bash
python scripts/preflight/check_akamai.py --region us-sea
# exit 0 only when every decision passed; writes a sanitized receipt under
# $LAB_RESULTS_DIR/preflight-receipts/ (path not printed)
```

Use `--public-only` for the informational catalog check without a token.

### 1. Initialize external Terraform state for the run

```bash
blackwell-cloud init --run-tag p3-pilot-20260907a
```

This configures the external local backend, external `TF_DATA_DIR`, and the
run's private lifecycle directory under
`$LAB_RESULTS_DIR/infra-lifecycle/p3-pilot-20260907a/`.

### 2. Create the external variable file (never committed)

Write `$LAB_RESULTS_DIR/infra-lifecycle/<run-tag>/terraform.tfvars`, for example:

```hcl
run_tag            = "p3-pilot-20260907a"
region             = "us-sea"
gpu_instance_type  = "g3-gpu-rtxpro6000-blackwell-1"
authorized_ssh_key = "ssh-ed25519 AAAA... operator"
management_cidr    = "203.0.113.10/32"   # operator management IP only
ttl_hours          = 6
```

### 3. Plan (dry run, free) — saved outside Git

```bash
blackwell-cloud plan --run-tag p3-pilot-20260907a
```

Review the redacted plan text and recorded SHA-256 in the run's external
lifecycle directory. The initial apply plan must create exactly
`linode_instance.gpu_baseline` and `linode_firewall.gpu_baseline`. Apply-stage
plans containing delete, replace, update, missing resources, extras, or
unrelated actions fail closed. The plan confirms intended configuration and
actions only; it is not a live-capacity guarantee.

### 4. Apply (billable) — exact saved plan only

Use a `LINODE_TOKEN` with the minimum Linode and Firewall write permissions
required to execute the saved plan (creates exactly one instance and its
firewall):

```bash
blackwell-cloud apply --run-tag p3-pilot-20260907a \
  --approve "I approve creating billable Akamai resources for run p3-pilot-20260907a using plan sha256:<digest-from-plan-output>"
```

A pending recovery record is written before apply; reconciliation runs after
every attempt. If reconciliation is not clean — including when the provider
lookup fails after a successful apply — a dirty recovery ledger is written,
the pending record is retained, and the pilot remains blocked.

### 5. Reconcile with the provider API

Reconciliation uses **GET-only** provider calls:

```bash
blackwell-cloud reconcile --run-tag p3-pilot-20260907a
```

Reconciliation is clean only when Terraform state is readable, the provider
API was successfully checked, exactly one instance and one firewall are
present, and every resource matches by typed identity (Terraform address,
resource type, provider ID, label, project tag, run tag, and instance
region). Any missing, extra, duplicate, conflicting, or unverified resource
leaves `reconciled=false` and blocks the pilot. Without a token, or when the
provider lookup fails, the ledger records `reconciled=false`, writes a
sanitized recovery explanation, and retains any pending record.

### 5b. Immediate emergency teardown-plan (before bootstrap)

As soon as reconciliation is clean, generate the identity-verified destroy
plan so an emergency teardown path exists before anyone copies bootstrap
material or starts serving:

```bash
blackwell-cloud teardown-plan --run-tag p3-pilot-20260907a
```

This is a reviewed saved plan, not a deletion. It still requires a later
digest-bearing destroy approval. Plans older than one hour are stale.

## Where each step runs

Lifecycle and Terraform operations (`init`, `plan`, `apply`, `reconcile`,
`teardown-plan`, `destroy`, orphan report, session summary) run on the
**owner's laptop**. Bootstrap, local serving, live provenance, and
`blackwell-cloud pilot` run on the **GPU instance**, because instance
metadata, Docker, `nvidia-smi`, and the loopback serving endpoint are
instance-local.

This diagnostic pilot uses a **manual private transfer**. Do not add a
handoff, archive, or synchronization subsystem for this step.

1. On the laptop, after a clean reconcile, securely copy only the approved
   pilot config and a **read-only** ledger snapshot to an external private
   directory on the GPU instance.
2. Never copy Terraform state, `terraform.tfvars`, saved plans, or provider
   credentials onto the instance.
3. On the instance, check out and install the exact canonical commit that
   produced the reviewed apply.
4. Run bootstrap, serving, provenance, and the pilot on the instance.
5. Securely copy results and pilot session records back to the laptop's
   `LAB_RESULTS_DIR`.
6. Validate those artifacts on the laptop (`blackwell-cloud verify-results`)
   before normal teardown.

Result-transfer failure must **never** prevent an identity-verified,
digest-approved emergency teardown. If results cannot be copied back, still
run `teardown-plan` / `destroy` from the laptop against the recorded ledger.

### 6. Bootstrap the instance

Copy `bootstrap/` to the instance, copy `bootstrap.env.example` to
`bootstrap.env`, and fill in **every** pinned package version, NVIDIA
repository URL/list entry, and digest-pinned CUDA probe image.

**GPU stack first (may require reboot):**

```bash
/opt/bwlab-bootstrap/bootstrap.sh
# exit 2 means reboot required, then re-run bootstrap.sh
```

**Model acquisition (after the GPU stack is validated):**

```bash
export HF_TOKEN=...   # environment only; never an argument or file
/opt/bwlab-bootstrap/fetch-model.sh
```

**Final bootstrap (serving requires the populated model and digest manifest):**

```bash
/opt/bwlab-bootstrap/bootstrap.sh
```

If you run bootstrap once before `fetch-model.sh` to install the GPU stack,
you **must re-run** `bootstrap.sh` afterward so model verification, serving
image checks, and readiness run against the populated artifact.

Bootstrap refuses empty version pins and never installs “latest”. The GPU
probe uses a digest-pinned NVIDIA CUDA image and runs `nvidia-smi` inside the
container to verify exactly one RTX PRO 6000 Blackwell GPU.

### 7. Short pilot (provider-native only)

Run this on the GPU instance after the manual private transfer. The config
path must be an existing absolute path outside the repository. The approval
phrase names the run tag, run label, and SHA-256 of the config bytes:

```bash
blackwell-cloud pilot \
  --run-tag p3-pilot-20260907a \
  --run-label pilot-a \
  --config /absolute/private/path/outside/the/repository/pilot.json \
  --approve "I approve the short Akamai pilot for run p3-pilot-20260907a (pilot-a) using config sha256:<digest-of-those-config-bytes>"
```

The pilot requires: the locked D-0014 envelope (provider-native, BF16,
expected RTX PRO 6000 Blackwell GPU, exactly interactive/1, batch-heavy/4,
and batch-heavy/8, one warm-up, one measured repetition, 20 tasks per cell);
no pending lifecycle operation; `reconciled=true`; `provider_checked=true`;
exactly one instance and one firewall in the ledger; and live provenance
re-verified **immediately before every cell** (observations from the first
cell are never reused). Config, approval, and ledger checks run before any
model request, telemetry, or measurement. The full 12-cell baseline remains
disabled.

### 8. Verify exported results

```bash
blackwell-cloud verify-results --subdirectory real-runs
```

An empty results directory is a failure when a pilot result is required.

### 9. Teardown — identity verified before any destroy plan

Teardown identity checks use **GET-only** provider calls. Destroy executes
the saved plan and requires write permissions:

```bash
blackwell-cloud teardown-plan --run-tag p3-pilot-20260907a
# verifies ledger vs state vs provider API BEFORE generating the saved destroy plan

blackwell-cloud destroy --run-tag p3-pilot-20260907a \
  --approve "I approve deleting the exact recorded resources for run p3-pilot-20260907a using destroy plan sha256:<digest-from-teardown-plan>"
```

Deletion is confirmed by polling recorded provider ids (GET only). On timeout,
billing may continue — escalate and retry.

### 10. Orphan report and session cost summary

```bash
blackwell-cloud orphan-report --run-tag p3-pilot-20260907a
blackwell-cloud session-summary --run-tag p3-pilot-20260907a --hourly-price 3.00
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
7. Model artifact digest verification before serving (requires `fetch-model.sh`
   to have populated the artifact and manifest first).
8. Serving bound to loopback only; health and readiness checks.
9. Workload watchdog — **NOT a billing-control substitute**.

Pins live in `bootstrap.env` (from `bootstrap.env.example`). Decision D-0015
records the offline-resolved **vLLM v0.27.1** candidate (the NVIDIA model
card's named recipe). Those pins are not empirically validated on the target
GPU, and the baseline is not frozen until the pilot succeeds.

## Candidate pin evidence (offline, 2026-09-07)

Public metadata only. No model weights or container layers were downloaded.
Candidate values are **not** a completed pilot and **not** a frozen baseline.

| Component | Exact version or digest | Target platform | Official source | Retrieved | Status | Live validation still required |
| --- | --- | --- | --- | --- | --- | --- |
| NVIDIA driver package | `nvidia-driver-580-server=580.173.02-0ubuntu0.24.04.1` | Ubuntu 24.04 `linux/amd64` | [Ubuntu noble package](https://packages.ubuntu.com/noble/amd64/nvidia-driver-580-server) | 2026-09-07 | Official package metadata | Running driver after reboot; `nvidia-smi` on RTX PRO 6000 Blackwell Server Edition |
| NVIDIA Container Toolkit | `1.20.0-1` | Ubuntu 24.04 `linux/amd64` | [NVIDIA install guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html); [amd64 Packages](https://nvidia.github.io/libnvidia-container/stable/deb/amd64/Packages) | 2026-09-07 | Official docs + repo index | `nvidia-ctk` + GPU probe after install |
| NVIDIA repo key | `https://nvidia.github.io/libnvidia-container/gpgkey` | Ubuntu apt | [NVIDIA install guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) | 2026-09-07 | Official ASCII-armored PGP key | apt signed-by install on the host |
| NVIDIA repo list | `deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://nvidia.github.io/libnvidia-container/stable/deb/amd64 /` | Ubuntu 24.04 `linux/amd64` | Official list file with documented `signed-by` transform; `$(ARCH)` resolved to `amd64` | 2026-09-07 | Official repo list | apt-get update on the host |
| Docker package | `docker.io=29.1.3-0ubuntu3~24.04.2` | Ubuntu 24.04 `linux/amd64` | [Ubuntu noble package](https://packages.ubuntu.com/noble/amd64/docker.io) | 2026-09-07 | Official package metadata | docker service + NVIDIA runtime on the host |
| CUDA GPU-probe image tag | `nvcr.io/nvidia/cuda:13.0.0-base-ubuntu24.04` | `linux/amd64` host | NVIDIA NGC / Docker Hub `nvidia/cuda` | 2026-09-07 | Official public tag | `nvidia-smi` inside the digest-pinned probe |
| CUDA GPU-probe index digest | `sha256:6e43a6b02e5f16e4a715953be8b29c40acbe84aed11a61357ef0fc899967fbd9` | multi-platform index | NGC and Docker Hub manifest-list APIs (identical) | 2026-09-07 | Registry `Docker-Content-Digest`; no layers downloaded | None beyond using the amd64 manifest on the host |
| CUDA GPU-probe `linux/amd64` digest | `sha256:cf5ab24c3f5040a0ea5931658874c2158e606bf740f006809aa4bb9d3334016b` | `linux/amd64` | Same manifest APIs | 2026-09-07 | Platform manifest digest | Probe container on the GPU host |
| Required container CUDA | `13.0` | vLLM image userland | [v0.27.1 `docker/versions.json`](https://github.com/vllm-project/vllm/blob/v0.27.1/docker/versions.json) `CUDA_VERSION=13.0.3` | 2026-09-07 | Official Dockerfile pin | Exact `torch.version.cuda` string inside the digest-pinned image |
| vLLM image tag | `docker.io/vllm/vllm-openai:v0.27.1` | GPU host pulls `linux/amd64` | [NVIDIA model card](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16); [vLLM recipes](https://recipes.vllm.ai/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16); [v0.27.1 release](https://github.com/vllm-project/vllm/releases/tag/v0.27.1) | 2026-09-07 | Named NVIDIA recipe; not chosen because newer | Serve BF16 Nemotron on RTX PRO 6000 Blackwell Server Edition |
| vLLM index digest | `sha256:0a51ea5b4ae2dc5d81890e5173f54203d2a3ae0cfffe51b8fd2afd4391bfd967` | multi-platform index | Docker Hub registry manifest list | 2026-09-07 | Registry `Docker-Content-Digest`; no layers downloaded | None beyond using the amd64 manifest on the host |
| vLLM `linux/amd64` digest | `sha256:c2f3b1b964e47809b722b5e75b61b1e7b39a50f70388cf2bf2418f16a9f31da2` | `linux/amd64` | Docker Hub platform manifest | 2026-09-07 | Platform manifest digest | Image pull + engine version on the GPU host |
| Nemotron BF16 revision | `a9904d24bcc1d289a1950fa9d2b978c47cf903b9` | Hugging Face repo | [Public model API](https://huggingface.co/api/models/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16) | 2026-09-07 | Official `sha` field | Authorized live download + per-file digest manifest |
| Model per-file digest manifest | unresolved (path only) | instance-local | Generated by `fetch-model.sh` at download time | — | Not computable without the weights | Live download, `sha256sum` manifest, bootstrap verification |
| Minimum driver branch | R580 | host | NVIDIA CUDA 13.0 release notes (`>=580.65.06`); project SM120 notes | 2026-09-07 | Official CUDA 13 driver floor | Observed `nvidia-smi` driver after reboot |

vLLM **v0.28.0** was evaluated and not selected: its default image is CUDA
13.0 and its release notes mention additional SM12x work, but the NVIDIA
model card and official vLLM recipes name **v0.27.1** for this BF16
checkpoint. Newer is not a selection reason.
