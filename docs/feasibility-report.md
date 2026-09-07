# Phase 1 Feasibility Report

**Date:** 2026-09-05
**Method:** Publicly accessible official documentation and reputable
third-party trackers (all web sources retrieved 2026-09-05 unless a different
date is noted), plus read-only environment checks in the hosted working
environment. No cloud resources were created, modified, or deleted. No model
weights or large containers were downloaded. No credentials were requested,
received, or used.

Every finding below is tagged:

- **[VERIFIED]** — confirmed against an official source (linked, with
  retrieval date) or observed directly in the working environment.
- **[ASSUMPTION]** — plausible inference or estimate not yet confirmed against
  an official source or a live account.
- **[UNVERIFIED]** — a compatibility or behavior claim that cannot be settled
  by documentation alone and requires empirical testing.
- **[UNRESOLVED]** — open question requiring owner action, local credentialed
  checks, or a later phase.

**Pricing disclaimer:** every price figure in this report is a **preliminary,
non-authoritative planning estimate**. Prices come from third-party trackers
or list pages, vary by region and over time, and require **account-level
verification by the owner's local, authenticated environment** before any
budgeting or provisioning decision.

---

## 1. Credential and access status (this working environment)

| Provider / service | Status | Detail |
| --- | --- | --- |
| Akamai Cloud (Linode API) | **[VERIFIED] Not available** | No `linode-cli` installed and no `LINODE_TOKEN` in the environment. Account-level plan availability could not be checked. |
| Google Cloud | **[VERIFIED] Not available** | No `gcloud` installed and no application-default or service-account credentials present. Project quota could not be checked. |
| AWS | **[VERIFIED] Not available** | No `aws` CLI installed and no `AWS_*` credentials present. Service quotas and account-level pricing could not be checked. |
| GitHub | **[VERIFIED] Available (read/limited write)** | Authenticated CLI present; used for repository operations only. |
| NGC / Hugging Face | **[VERIFIED] Not available** | No `NGC_API_KEY` or `HF_TOKEN` present. Artifact access checks were limited to public pages. |

**Execution boundary (project governance):** the hosted Cloud Agent must not
request, receive, discover, store, print, or use provider credentials, and
must not run credential-dependent preflight scripts
([../AGENTS.md](../AGENTS.md), section 3). The account-level checks below are
therefore **owner actions performed in the owner's authenticated local
environment**, using the read-only preflight scripts in `scripts/preflight/`:
(a) Akamai account onboarding/eligibility for the limited-availability RTX
PRO 6000 plan and per-region deployability, (b) the Google Cloud project's
`NVIDIA_RTX_PRO_6000_GPUS` regional quota, (c) AWS "Running On-Demand G and VT
instances" vCPU quota and live regional capacity. No credentials are ever
committed or pasted into chat, issues, PRs, or CI; see
[../SECURITY.md](../SECURITY.md).

---

## 2. Akamai Cloud — RTX PRO 6000 Blackwell plan

- **[VERIFIED — official docs, retrieved 2026-09-06]** Akamai offers NVIDIA
  RTX PRO 6000 Blackwell Server Edition GPU Linodes in **limited
  availability**; access must be requested (support/onboarding), and a $100
  deposit may be required for newer accounts. Sources:
  [onboarding doc](https://techdocs.akamai.com/cloud-computing/docs/nvidia-rtx-pro-6000-blackwell-gpu-onboarding),
  [GPU Linodes doc](https://techdocs.akamai.com/cloud-computing/docs/gpu-compute-instances).
- **[VERIFIED — current official documentation (audit of 2026-09-06)]**
  RTX PRO 6000 Blackwell plans currently cover **1–4 GPUs**, **16–64
  dedicated vCPUs**, **176–736 GB RAM**, and **96–384 GB GPU memory**, with an
  officially stated **starting price of $2.50/hour** for the one-GPU /
  16-vCPU / 176-GB-RAM / 96-GB-GPU configuration. Source:
  [GPU Linodes doc](https://techdocs.akamai.com/cloud-computing/docs/gpu-compute-instances)
  (Plans and pricing). *Transparency note:* cached copies of the
  documentation and pricing pages fetched from the hosted working environment
  on 2026-09-06 still showed an older table (1–8 GPUs, up to 128 vCPU /
  1408 GB RAM, $3.00–$3.50/hr starting price); the figures above reflect the
  current official page per the owner's audit. Decision D-0014 records the
  owner-observed Seattle catalog base price of **$3.00/hour** for plan
  `g3-gpu-rtxpro6000-blackwell-1` and uses that rate for planning estimates.
  The $2.50/hour figure remains the public advertised starting price, not
  a guaranteed future bill.
- **[VERIFIED — official page, retrieved 2026-09-06; list subject to change]**
  Limited-availability regions for the RTX PRO 6000 plan (19 data centers):
  Amsterdam, Chennai, Chicago, Frankfurt, Jakarta, London, Los Angeles,
  Madrid, Miami, Milan, Mumbai, Newark, Osaka, Paris, Seattle, Singapore,
  Stockholm, Tokyo, Toronto. Washington, DC appeared in older cached copies
  but is not included here because current official documentation does not
  support it. Verify the live
  [Product Availability](https://techdocs.akamai.com/cloud-computing/docs/how-to-choose-a-data-center#product-availability)
  list at deployment time.
- **[VERIFIED — official changelog, retrieved 2026-09-06]** Since 2026-07-01
  GPU Linodes bill hourly with **no monthly cap**; European data centers,
  Singapore, and Jakarta carry higher RTX PRO 6000 rates. Source:
  [billing changelog](https://techdocs.akamai.com/cloud-computing/changelog/jul-1-2026-new-billing-model).
- **[VERIFIED — billing safety, official docs, retrieved 2026-09-06]**
  **Powering off a Linode does not stop billing.** Akamai bills for services
  present on the account regardless of power state; compute billing stops
  only when the service is deleted/removed from the account. Sources:
  [Understanding how billing works](https://techdocs.akamai.com/cloud-computing/docs/understanding-how-billing-works),
  [Stop further billing](https://techdocs.akamai.com/cloud-computing/docs/stop-further-billing).
  See [cost-guardrails.md](cost-guardrails.md) for the teardown rules this
  implies.
- **[OBSERVED FACT — live read-only check, 2026-09-05]** The unauthenticated
  Linode types catalog (`GET /v4/linode/types`) listed 13 GPU plans — Quadro
  RTX 6000 (`g1-gpu-rtx6000-*`) and RTX 4000 Ada (`g2-gpu-rtx4000a*`) — and
  **no RTX PRO 6000 Blackwell plans**. That absence is the observed fact.
- **[INFERENCE — not yet locally verified]** The Blackwell plans being
  visible only to onboarded accounts is an inference from the catalog absence
  plus the documented limited-availability gating; it is confirmed only when
  an onboarded account's catalog shows the plan. Either way, onboarding
  (support request) is a documented prerequisite for Phase 3.
- **[SUPERSEDED for the authorized pilot — decision D-0014, 2026-09-06]**
  Account onboarding, deployable region, and Seattle catalog price for the
  exact one-GPU plan are recorded as owner-verified sanitized facts below.
  Future live capacity remains un-guaranteed.
- **[VERIFIED — owner-local sanitized observation, 2026-09-06; D-0014]**
  Exact plan `g3-gpu-rtxpro6000-blackwell-1` (one RTX PRO 6000 Blackwell
  GPU); selected pilot region `us-sea`; observed catalog base price
  **$3.00/hour**; Seattle had no observed regional surcharge. An owner-run
  temporary instance **capability probe** reached running and was
  subsequently deleted; it created no firewall; a later read-only check
  found zero matching test instances, firewalls, and volumes; billing is
  not continuing for those test resources. That probe was **not** a genuine
  benchmark and collected no model-serving or benchmark results. The direct
  Seattle create/delete observation is stronger evidence for `us-sea` than
  an additional `us-ord` connectivity preflight; `us-ord` was only an
  example and is not the selected pilot region. A saved Terraform plan
  verifies intended configuration and planned actions only; it does not
  prove live capacity. Capacity is known when provisioning is accepted and
  the instance reaches the expected running state.

## 3. Google Cloud — `g4-standard-48`

- **[VERIFIED]** G4 VMs with RTX PRO 6000 Blackwell are **generally
  available**; sizes 1/2/4/8 GPUs. Source: [Google Cloud blog](https://cloud.google.com/blog/products/compute/g4-vms-powered-by-nvidia-rtx-6000-blackwell-gpus-are-ga).
- **[VERIFIED]** G4 constraints from official docs
  ([Compute Engine GPU docs](https://docs.cloud.google.com/compute/docs/gpus/create-vm-with-gpus)):
  G4 runs only on **AMD EPYC Turin**; only certain regions/zones; **no
  Persistent Disk** (Hyperdisk/local SSD instead); no sustained-use or
  flexible committed-use discounts; capacity only via supported consumption
  options; GPU quota (`NVIDIA_RTX_PRO_6000_GPUS` per region plus global GPU
  quota) must be requested — new projects start effectively at zero.
- **[ASSUMPTION — preliminary, non-authoritative planning estimate]**
  `g4-standard-48` = 48 vCPU / 180 GB RAM / 1× 96 GB GPU at approximately
  **$4.50/hr** on-demand (third-party tracker
  [Thunder Compute](https://www.thundercompute.com/blog/google-cloud-gpu-instances),
  tracker data dated 2026-08-28, retrieved 2026-09-05). Requires account-level
  verification in the Google Cloud pricing calculator / billing for the target
  region, performed in the owner's local environment.
- **[UNRESOLVED — owner action]** Quota status and consumable regions for the
  authorized project; whether on-demand (vs Spot/reservation/Flex-start) is
  available for `g4-standard-48` in the target region.

## 4. AWS — EC2 G7e

- **[VERIFIED]** G7e instances (RTX PRO 6000 Blackwell Server Edition) became
  generally available **2026-01-20**, initially in us-east-1 and us-east-2,
  with Intel Emerald Rapids CPUs; purchasable On-Demand, Savings Plans, Spot,
  and Dedicated. Source: [AWS News Blog](https://aws.amazon.com/blogs/aws/announcing-amazon-ec2-g7e-instances-accelerated-by-nvidia-rtx-pro-6000-blackwell-server-edition-gpus/).
- **[VERIFIED]** Official size table (single-GPU options):
  `g7e.2xlarge` 8 vCPU / 64 GiB, `g7e.4xlarge` 16 vCPU / 128 GiB,
  `g7e.8xlarge` 32 vCPU / 256 GiB — each 1× 96 GB GPU, 1.9 TB local NVMe.
  Same source.
- **[ASSUMPTION — preliminary, non-authoritative planning estimate]**
  On-demand pricing (third-party trackers
  [Vantage](https://instances.vantage.sh/aws/ec2/g7e.4xlarge) and
  [aws-pricing.com](https://aws-pricing.com/g7e.4xlarge.html), retrieved
  2026-09-05; US regions): `g7e.4xlarge` ≈ **$4.00/hr**, `g7e.8xlarge` ≈
  **$5.27/hr**; availability reported in roughly 11 regions. Requires
  account-level verification via the AWS console or Pricing API in the owner's
  local environment (note: `scripts/preflight/check_aws.py` does **not**
  query the Pricing API and makes no pricing claims).
- **[UNRESOLVED — owner action]** The account's "Running On-Demand G and VT
  instances" vCPU quota (new accounts frequently need an increase to run even
  one 16-vCPU GPU instance), and live regional capacity.

### 4.1 `g7e.4xlarge` vs `g7e.8xlarge` headroom analysis (candidate selection, not final)

Model memory analysis (see section 5 for artifact facts):

- NVFP4 weights ≈ 30B params × ~0.5 B/param + overhead ≈ **~20 GB**
  [ASSUMPTION based on parameter count].
- BF16 weights ≈ 30B × 2 B ≈ **~60 GB** [ASSUMPTION based on parameter
  count]; fits the 96 GB GPU with roughly 30 GB left for KV cache and
  activations at moderate context lengths.
- Host-side needs: model download/load buffer, serving-engine runtime,
  benchmark container, telemetry, OS page cache.

Finding: `g7e.4xlarge` (16 vCPU / 128 GiB) is the **candidate** AWS instance
size — it is not a final selection. On paper it appears sufficient for every
proposed single-GPU serving path (128 GiB exceeds the ~60 GB BF16 artifact
plus estimated runtime overhead), and it matches Akamai's 16-vCPU 1-GPU plan
far better than the 8xlarge does, which strengthens the controlled-resource
design. `g7e.8xlarge` is **retained as a possible fallback** if measured
memory and CPU headroom (for example TensorRT-LLM engine-build peaks, NIM
profile caching, or telemetry overhead) proves the 4xlarge inadequate.
**[ASSUMPTION — the final size selection is made only from measured headroom
during the Phase 3 baseline and the Phase 6 AWS preflight.]**

## 5. Model artifacts — NVIDIA Nemotron 3.5 Lightning 30B-A3B

- **[VERIFIED]** Both artifacts exist on Hugging Face:
  [`nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16`](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16)
  and
  [`nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4`](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4),
  released 2026-08-11. Architecture: MoE hybrid (Mamba-2 + MoE + Attention),
  30B total / 3B active parameters, context up to 1M tokens.
- **[VERIFIED]** Model license: **OpenMDW-1.1** (Linux Foundation permissive
  model license) on both variants — permissive, royalty-free, no field-of-use
  restrictions, no restrictions on outputs
  ([license text](https://github.com/OpenMDW/OpenMDW/blob/main/1.1/LICENSE.OpenMDW-1.1),
  retrieved 2026-09-05). The license permits redistribution of
  quantized/derived artifacts.
- **Important distinction:** OpenMDW-1.1 governs the **third-party model
  artifacts only**. It says nothing about this repository, whose original
  source code and documentation are licensed under Apache-2.0 (see
  [../LICENSE](../LICENSE)). Genuine benchmark datasets and results are not
  included in the Apache-2.0 grant, and any release of experiment artifacts
  still requires the owner's explicit approval regardless of what the model
  license permits.
- **[VERIFIED]** NVIDIA's model cards list supported hardware as Blackwell
  (DGX Spark/GB10, GB200, GeForce RTX 5090), Hopper (H100/H200), and Ampere
  via W4A16. Recommended sampling: temperature 1.0, top_p 0.95. Reasoning mode
  toggles via chat template.
- **[ASSUMPTION]** RTX PRO 6000 Blackwell **Server Edition (SM120)** is not
  explicitly named on the model card, but it shares the SM120 architecture
  with the listed RTX 5090; the model is expected to run. This must be
  validated in Phase 3 before the baseline is frozen.
- **[ASSUMPTION]** The Hugging Face repositories appear publicly downloadable
  under OpenMDW-1.1 (no gate observed on the public pages); confirm at
  download time in Phase 3.

## 6. Serving-stack compatibility

### vLLM (proposed baseline)

- **[ASSUMPTION]** BF16 serving of this model on RTX-class Blackwell is
  expected to be standard vLLM territory, but has not been tested by this
  project.
- **[UNVERIFIED — explicitly unverified until tested]** **NVFP4 MoE on
  SM120** under vLLM: community and upstream reports (all retrieved
  2026-09-05) indicate native kernels exist but are currently **opt-in**
  (`--moe-backend flashinfer_b12x`, a CUDA 13.0 toolkit matching the PyTorch
  CUDA runtime, driver R580+), and that the default/auto path can select
  broken CUTLASS kernels on SM120. Sources:
  [vllm#31085](https://github.com/vllm-project/vllm/issues/31085),
  [vllm#33416](https://github.com/vllm-project/vllm/issues/33416),
  [cutlass#3096](https://github.com/NVIDIA/cutlass/issues/3096). None of this
  has been reproduced by this project; treat vLLM NVFP4 support on SM120 as
  **unverified until empirically tested on the target hardware**.
- **Implication:** the NVFP4-on-vLLM cell is provisionally feasible but
  fragile; the exact vLLM version, FlashInfer version, and flags must be
  pinned in the run manifest, and the baseline must not be frozen until a
  working pin is validated. BF16-on-vLLM is the safest Phase 3 baseline
  candidate, itself still subject to validation.

### TensorRT-LLM

- **[VERIFIED]** TensorRT-LLM officially supports "RTX Pro 6000 SE with FP4
  optimization" and publishes RTX 6000 Pro Blackwell Server Edition
  performance data, including a similar-shape MoE (Qwen3 30B A3B). Sources:
  [TensorRT-LLM overview](https://nvidia.github.io/TensorRT-LLM/1.1.0/overview.html),
  [perf overview](https://nvidia.github.io/TensorRT-LLM/developer-guide/perf-overview.html).
- **[ASSUMPTION]** Nemotron 3.5 Lightning's hybrid Mamba-2+MoE architecture is
  supported by a current TensorRT-LLM release (NVIDIA's NVFP4 model card
  references TensorRT-LLM recipes; the exact version pin is unverified).

### NVIDIA NIM

- **[VERIFIED]** A Nemotron 3.5 Lightning NIM exists with official
  documentation, including Blackwell guidance (NVFP4 profile preferred on
  Blackwell; known FlashInfer autotune startup issue on some R595 drivers with
  documented workarounds, e.g. `--no-enable-flashinfer-autotune` or
  `--moe-backend triton`). Source:
  [NIM docs](https://docs.nvidia.com/nim/large-language-models/latest/get-started/advanced/get-started-nemotron-3.5-lightning.html).
- **[VERIFIED]** NGC access: most NIM containers/models need no key; an **NGC
  Personal API key** is required for Production Branch (PB) models; governing
  terms must be accepted on NGC before first download. Production-branch
  commercial deployment requires an **NVIDIA AI Enterprise license**.
- **[UNRESOLVED — owner action]** NIM **entitlement** for this project
  remains unresolved: whether this research requires a Production Branch
  model / NVIDIA AI Enterprise license or can use the feature-branch NIM, and
  what the owner's NGC account is entitled to. Owner decision plus an NGC
  account check in the owner's local environment.

### Driver / CUDA baseline

- **[VERIFIED]** SM120 NVFP4 paths require driver **R580+** and **CUDA
  13.0+** for the `compute_120f` feature set; NIM docs additionally recommend
  R580 over at least one R595 driver for a known MoE autotune defect.
- **[ASSUMPTION]** A single container base (CUDA 13.0.x, driver R580-series
  hosts) can serve all three serving paths across all three providers. Driver
  versions offered by provider images may differ and must be recorded per run.

## 7. Controlled-resource envelope (proposed, not final)

Host inventory across the three single-GPU candidates:

| Provider | Instance | vCPU | RAM | CPU |
| --- | --- | --- | --- | --- |
| Akamai | 1-GPU RTX PRO 6000 plan | 16 (dedicated) | 176 GB | [UNRESOLVED — record in Phase 3] |
| Google | `g4-standard-48` | 48 | 180 GB | AMD EPYC Turin |
| AWS | `g7e.4xlarge` | 16 | 128 GiB | Intel Emerald Rapids |

**Provisional envelope (single definition used project-wide):** the
14-vCPU/100-GiB value is a provisional **joint total across the serving and
benchmark workload combined** — not 14 vCPUs / 100 GiB independently for each
container. It leaves ≥2 vCPUs and ≥24 GiB on the smallest host
(`g7e.4xlarge`) for OS, telemetry, and the driver stack. Rationale: 16 vCPU
is the binding CPU constraint (Akamai and AWS), 128 GiB the binding memory
constraint (AWS); ~60 GB BF16 weights + serving runtime fit within 100 GiB
with margin.

**Not final:** per the exit criteria, the exact allocation between the two
containers and the cgroup enforcement mechanism will be frozen **only after
Phase 3 headroom validation** empirically confirms adequate headroom for
model loading, serving, telemetry, and benchmark execution. CPU
**architecture** differences (AMD Turin vs Intel Emerald Rapids vs Akamai's
host CPU) cannot be equalized — they are recorded, not hidden.

## 8. Approximate costs

See [cost-guardrails.md](cost-guardrails.md) for controls. Estimates use the
pricing marked [ASSUMPTION] above; **all figures are preliminary,
non-authoritative planning estimates — not quotes — and require account-level
verification** in the owner's local environment before budgeting decisions.

| Phase | Basis | Estimate |
| --- | --- | --- |
| Phase 2 (synthetic workload) | No GPU; local dev + GitHub Actions CI (small usage; Actions minutes are free once the repository is public) | **≈ $0 cloud** |
| Phase 3 (Akamai baseline, **full 12-cell estimate — not authorized**) | ~90–230 GPU-h (12 cells — both comparison modes per D-0012 — plus setup) × **$3.00/h** owner-observed Seattle catalog base price (D-0014; public advertised starting price remains $2.50/h; higher in EU/Singapore/Jakarta) + storage/egress | **≈ $270–$690** before incidental costs |
| Phase 3B authorized pilot only | ≤ 6 GPU-h on one `g3-gpu-rtxpro6000-blackwell-1` in `us-sea` | **$25 total ceiling** (six hours × $3.00/h ≈ $18 before incidentals; not authorization of the full Phase 3 estimate) |
| Phase 4 (optimization, **not authorized**) | ~80–200 GPU-h × **$3.00/h** Seattle observed plan price (same advertised-vs-observed distinction) | **≈ $240–$600** before incidental costs |
| Phase 5 (Google Cloud) | ~60–140 GPU-h (12 cells: both comparison modes) × ~$4.50/h + Hyperdisk/local SSD | **≈ $280–$650** |
| Phase 6 (AWS) | ~60–140 GPU-h × ~$4.00/h (4xlarge) or ~$5.27/h (8xlarge) + EBS | **≈ $250–$760** |
| Storage/network (all) | model artifact downloads (~80 GB/provider), result egress | **≈ $10–$50 total** |

Run-hour estimates assume setup/validation ≈ 15–25 h, a short pilot ≈ 2–4 h,
plus measured-cell time under the **D-0010 sample plan** (200 balanced task
instances per measured repetition; 5 measured repetitions + 1 warm-up pass
per cell). Measured wall time per cell is approximately
`200 × mean_task_latency × 6 passes ÷ concurrency`, so cell hours are
dominated by realized task latency, which is unknown before measurement.
**[ASSUMPTION]** Planning ranges above use mean task latencies of ~15–45 s
(interactive) and ~60–180 s (batch-heavy, bounded by the 300 s SLO and 600 s
timeout), giving ≈ 70–200 measured GPU-h for the **twelve** Phase 3 baseline
cells (D-0012: six cells in each of the two comparison modes, run serially
on the same single instance — the mode factor doubles measured hours, not
instance count). A **short Phase 3 pilot must revalidate realized task
latency and re-derive the budget before full measurement**, and the
cost-guardrails check-in triggers pause provisioning on overrun. Akamai
planning figures now use the **owner-observed Seattle catalog base price of
$3.00/h** (decision D-0014). The public advertised starting price remains
$2.50/h. These remain **estimates, not guaranteed future billing**. The
authorized Phase 3B pilot is a separate $25 / six-hour ceiling and does
**not** authorize the full Phase 3 estimate.

**Billing-duration caveat (all providers):** GPU-hour cost accrues for the
entire life of the *service*, not just active benchmarking. On Akamai,
powering off a Linode does **not** stop billing — the instance must be
deleted/removed from the account to stop charges. On AWS and GCP,
stopped-instance compute treatment differs from Akamai's, but disks,
addresses, snapshots, and other attached resources may continue billing while
the instance is stopped. Estimates above therefore assume prompt deletion of
the compute service (after verified result export to `LAB_RESULTS_DIR`) at
the end of each session, with teardown following the rules in
[cost-guardrails.md](cost-guardrails.md).

## 9. Automatic shutdown, teardown, and orphan detection (options)

**Critical billing-safety facts (official Akamai docs, retrieved 2026-09-06;
sources: [Understanding how billing works](https://techdocs.akamai.com/cloud-computing/docs/understanding-how-billing-works),
[Stop further billing](https://techdocs.akamai.com/cloud-computing/docs/stop-further-billing)):**

1. **Powering off an Akamai Linode does not stop billing.** Billing continues
   while the service exists on the account, powered on or off.
2. After authorized benchmark completion and verified result export to
   `LAB_RESULTS_DIR`, Akamai compute billing stops **only when the applicable
   service is deleted/removed from the account**.
3. An in-instance shutdown watchdog **limits active workload but is not an
   Akamai spending backstop** — a powered-off Linode keeps billing.
4. On AWS and GCP, stopped-instance compute treatment differs from Akamai's,
   while disks, addresses, snapshots, and other resources may continue
   billing while an instance is stopped.

Per-provider options:

- **Akamai:** no native instance auto-stop scheduler is documented. An
  in-instance idle watchdog (systemd timer) limits runaway *workload* only;
  the actual spending control is prompt, owner-approved **deletion** of the
  Linode after verified result export, plus a read-only sweep listing GPU
  Linodes older than their TTL tag. [Watchdog approach: ASSUMPTION.]
- **Google Cloud:** native instance schedules and
  `max-run-duration`/auto-delete options (Spot and Flex-start); budget alerts
  via Cloud Billing. Stopped instances still bill for attached Hyperdisk/SSD
  and reserved addresses. [VERIFIED — documented Compute Engine features.]
- **AWS:** CloudWatch low-utilization auto-stop, AWS Budgets actions,
  `InstanceInitiatedShutdownBehavior`; stopped instances still bill for EBS
  volumes, Elastic IPs, and snapshots; orphan sweep via read-only
  `describe-instances` filtered by project tag. [VERIFIED — documented AWS
  features.]

Teardown rules (binding; see [cost-guardrails.md](cost-guardrails.md)):
teardown deletes **only** resources recorded as created for the exact run
(matched by the run's unique tags); destructive teardown always requires
explicit local owner approval; and post-teardown verification must confirm
that **no project-created billable resources remain** on the account. No
teardown tooling is implemented or executed in Phase 1.

## 10. Ownership and publication approvals

- **[RESOLVED — owner decision D-0007, 2026-09-06]** The owner has determined
  that this project is independently owned and that no external publication
  authorization is required. The repository's source and methodology are
  published under Apache-2.0 (see [decision-log.md](decision-log.md)).
- **[STILL REQUIRED]** Every release of genuine benchmark results remains
  gated by the class B process in
  [publication-governance.md](publication-governance.md): separate explicit
  owner approval identifying the exact files and scope. Before Phase 7
  publication of cross-provider comparisons, reverify that the planned
  content complies with each provider's terms of service, and keep every
  claim within the evidence.

## 11. Summary of unresolved questions (blockers and owner actions)

1. Owner-run local preflight checks for Google Cloud and AWS (see
   section 1): the read-only scripts in `scripts/preflight/` verify
   account-level availability and pricing from the owner's authenticated
   local environment. The hosted Cloud Agent does not receive credentials
   and does not run these checks. Akamai plan, Seattle region, and catalog
   base price for the authorized pilot are recorded in decision D-0014.
2. ~~Akamai limited-availability onboarding / Seattle price for the exact
   one-GPU plan~~ — recorded as owner-verified sanitized facts in D-0014
   (`g3-gpu-rtxpro6000-blackwell-1`, `us-sea`, $3.00/h catalog base, no
   observed Seattle surcharge). A saved Terraform plan verifies intended
   configuration and planned actions only; it does not prove live capacity.
   Capacity is known when provisioning is accepted and the instance reaches
   the expected running state.
3. Google Cloud `NVIDIA_RTX_PRO_6000_GPUS` quota and consumable regions.
4. AWS G-instance vCPU quota and G7e regional capacity.
5. Whether the NIM path requires a Production Branch model / NVIDIA AI
   Enterprise license for this research.
6. ~~Organizational approval for company-resource use and comparative
   publication~~ — resolved by owner decision D-0007 (independent ownership;
   see section 10). Result releases still require per-release owner approval.
7. Empirical validation (Phase 3) of: Nemotron 3.5 Lightning on RTX PRO 6000
   Server Edition, the vLLM NVFP4 pin, and the controlled-resource envelope.
