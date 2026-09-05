# Phase 1 Feasibility Report

**Date:** 2026-09-05
**Method:** Public official documentation and reputable trackers, plus
read-only credential checks in the working environment. No cloud resources
were created, modified, or deleted. No model weights or large containers were
downloaded.

Every finding below is tagged:

- **[VERIFIED]** — confirmed against an official source (linked) or observed
  directly in the working environment.
- **[ASSUMPTION]** — plausible inference not yet confirmed against an official
  source or a live account.
- **[UNRESOLVED]** — open question requiring credentials, owner action, or a
  later phase.

---

## 1. Credential and access status (this working environment)

| Provider / service | Status | Detail |
| --- | --- | --- |
| Akamai Cloud (Linode API) | **[VERIFIED] Not available** | No `linode-cli` installed and no `LINODE_TOKEN` in the environment. Account-level plan availability could not be checked. |
| Google Cloud | **[VERIFIED] Not available** | No `gcloud` installed and no application-default or service-account credentials present. Project quota could not be checked. |
| AWS | **[VERIFIED] Not available** | No `aws` CLI installed and no `AWS_*` credentials present. Service quotas and account-level pricing could not be checked. |
| GitHub | **[VERIFIED] Available (read/limited write)** | Authenticated CLI present; used for repository operations only. |
| NGC / Hugging Face | **[VERIFIED] Not available** | No `NGC_API_KEY` or `HF_TOKEN` present. Artifact access checks were limited to public pages. |

**Blocker (exact missing capability):** read-only API access to each cloud
account is required to verify (a) Akamai account onboarding/eligibility for
the limited-availability RTX PRO 6000 plan and per-region deployability,
(b) the Google Cloud project's `NVIDIA_RTX_PRO_6000_GPUS` regional quota,
(c) AWS "Running On-Demand G and VT instances" vCPU quota and live regional
capacity. The read-only preflight scripts in `scripts/preflight/` will perform
these checks once scoped read-only credentials are provided (via the variables
in `.env.example`). No credentials should ever be committed; see
[../SECURITY.md](../SECURITY.md).

---

## 2. Akamai Cloud — RTX PRO 6000 Blackwell plan

- **[VERIFIED]** Akamai offers NVIDIA RTX PRO 6000 Blackwell Server Edition
  GPU Linodes in **limited availability**; access must be requested (support
  ticket onboarding). Source: [Akamai onboarding doc](https://techdocs.akamai.com/cloud-computing/docs/nvidia-rtx-pro-6000-blackwell-gpu-onboarding).
- **[VERIFIED]** Plans range 1–8 GPUs, 16–128 dedicated vCPUs, 176–1408 GB
  RAM, 96–768 GB VRAM. Regions (limited availability): Amsterdam, Chennai,
  Chicago, Frankfurt, Jakarta, London, Los Angeles, Madrid, Miami, Mumbai,
  Milan, Newark, Osaka, Paris, Seattle, Singapore, Stockholm, Tokyo, Toronto,
  Washington DC. Source: [GPU Linodes doc](https://techdocs.akamai.com/cloud-computing/docs/gpu-compute-instances).
- **[VERIFIED]** Since 2026-07-01 GPU Linodes bill hourly with **no monthly
  cap**; European data centers, Singapore, and Jakarta carry higher RTX PRO
  6000 rates. Source: [billing changelog](https://techdocs.akamai.com/cloud-computing/changelog/jul-1-2026-new-billing-model).
- **[ASSUMPTION]** The 1-GPU plan is approximately 16 vCPU / 176 GB RAM at
  roughly **$3.00 per GPU-hour** in US/APAC regions (third-party tracker
  [ComputePrices](https://computeprices.com/providers/akamai-cloud/gpus/rtx-pro-6000),
  2026-08-05). Confirm against Akamai's official pricing page and the account.
- **[VERIFIED — live read-only check, 2026-09-05]** The unauthenticated public
  Linode types catalog (`GET /v4/linode/types`) lists 13 GPU plans — Quadro
  RTX 6000 (`g1-gpu-rtx6000-*`) and RTX 4000 Ada (`g2-gpu-rtx4000a*`) — but
  **no RTX PRO 6000 Blackwell plans**. This is consistent with the
  limited-availability gating: the Blackwell plans appear to be exposed only
  to onboarded accounts. Account onboarding is therefore a hard prerequisite
  for Phase 3.
- **[UNRESOLVED]** Whether the authorized account is already onboarded for the
  limited-availability plan, and which specific regions are deployable for it.
  Requires a read-only account check (`scripts/preflight/check_akamai.py` with
  a read-only `LINODE_TOKEN`) or an owner confirmation.

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
- **[ASSUMPTION]** `g4-standard-48` = 48 vCPU / 180 GB RAM / 1× 96 GB GPU at
  approximately **$4.50/hr** on-demand (third-party tracker, 2026-08-28).
  Confirm region-specific pricing in the pricing calculator.
- **[UNRESOLVED]** Quota status and consumable regions for the authorized
  project; whether on-demand (vs Spot/reservation/Flex-start) is available for
  `g4-standard-48` in the target region.

## 4. AWS — EC2 G7e

- **[VERIFIED]** G7e instances (RTX PRO 6000 Blackwell Server Edition) became
  generally available **2026-01-20**, initially in us-east-1 and us-east-2,
  with Intel Emerald Rapids CPUs; purchasable On-Demand, Savings Plans, Spot,
  and Dedicated. Source: [AWS News Blog](https://aws.amazon.com/blogs/aws/announcing-amazon-ec2-g7e-instances-accelerated-by-nvidia-rtx-pro-6000-blackwell-server-edition-gpus/).
- **[VERIFIED]** Official size table (single-GPU options):
  `g7e.2xlarge` 8 vCPU / 64 GiB, `g7e.4xlarge` 16 vCPU / 128 GiB,
  `g7e.8xlarge` 32 vCPU / 256 GiB — each 1× 96 GB GPU, 1.9 TB local NVMe.
  Same source.
- **[ASSUMPTION]** On-demand pricing (third-party trackers, US regions):
  `g7e.4xlarge` ≈ **$4.00/hr**, `g7e.8xlarge` ≈ **$5.27/hr**; availability has
  expanded to roughly 11 regions. Confirm with the AWS Pricing API once
  read-only credentials exist.
- **[UNRESOLVED]** The account's "Running On-Demand G and VT instances" vCPU
  quota (new accounts frequently need an increase to run even one 16-vCPU
  GPU instance), and live regional capacity.

### 4.1 `g7e.4xlarge` vs `g7e.8xlarge` headroom analysis

Model memory analysis (see section 5 for artifact facts):

- NVFP4 weights ≈ 30B params × ~0.5 B/param + overhead ≈ **~20 GB**
  [ASSUMPTION based on parameter count].
- BF16 weights ≈ 30B × 2 B ≈ **~60 GB** [ASSUMPTION based on parameter
  count]; fits the 96 GB GPU with roughly 30 GB left for KV cache and
  activations at moderate context lengths.
- Host-side needs: model download/load buffer, serving-engine runtime,
  benchmark container, telemetry, OS page cache.

Finding: `g7e.4xlarge` (16 vCPU / 128 GiB) **appears sufficient** for every
proposed single-GPU serving path — 128 GiB comfortably exceeds the ~60 GB
BF16 artifact plus runtime overhead — and it matches Akamai's 16-vCPU 1-GPU
plan far better than the 8xlarge does, which strengthens the
controlled-resource design. `g7e.8xlarge` remains the fallback if Phase 3
reveals higher-than-expected host-memory pressure (for example TensorRT-LLM
engine-build peaks or NIM profile caching). **[ASSUMPTION — must be validated
empirically in Phase 3/6; do not finalize until then.]**

## 5. Model artifacts — NVIDIA Nemotron 3.5 Lightning 30B-A3B

- **[VERIFIED]** Both artifacts exist on Hugging Face:
  [`nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16`](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16)
  and
  [`nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4`](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4),
  released 2026-08-11. Architecture: MoE hybrid (Mamba-2 + MoE + Attention),
  30B total / 3B active parameters, context up to 1M tokens.
- **[VERIFIED]** License: **OpenMDW-1.1** (Linux Foundation permissive model
  license) on both variants — permissive, royalty-free, no field-of-use
  restrictions, no restrictions on outputs
  ([license text](https://github.com/OpenMDW/OpenMDW/blob/main/1.1/LICENSE.OpenMDW-1.1)).
  Redistribution of quantized/derived artifacts is permitted, which supports
  publishing exact experiment artifacts if desired.
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

- **[VERIFIED]** BF16 serving on RTX-class Blackwell is standard vLLM
  territory. For **NVFP4 MoE on SM120**, native kernels exist but are
  currently **opt-in**: `--moe-backend flashinfer_b12x`, with a CUDA 13.0
  toolkit matching the PyTorch CUDA runtime and driver R580+. The default/auto
  path has been reported to select broken CUTLASS kernels on SM120. Sources:
  [vllm#31085](https://github.com/vllm-project/vllm/issues/31085),
  [vllm#33416](https://github.com/vllm-project/vllm/issues/33416),
  [cutlass#3096](https://github.com/NVIDIA/cutlass/issues/3096).
- **Implication:** the NVFP4-on-vLLM cell is feasible but fragile; the exact
  vLLM version, FlashInfer version, and flags must be pinned in the run
  manifest, and the baseline must not be frozen until a working pin is
  validated. BF16-on-vLLM is the safest Phase 3 baseline candidate.

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
- **[UNRESOLVED]** Whether this research requires a PB model / NVAIE license
  or can use the feature-branch NIM. Owner decision plus NGC account check.

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

**Proposed envelope:** benchmark + serving containers jointly limited to
**14 vCPUs and 100 GiB RAM** (cgroup limits via container runtime), leaving
≥2 vCPUs and ≥24 GiB on the smallest host (`g7e.4xlarge`) for OS, telemetry,
and the driver stack. Rationale: 16 vCPU is the binding CPU constraint
(Akamai and AWS), 128 GiB the binding memory constraint (AWS); ~60 GB BF16
weights + serving runtime fit within 100 GiB with margin.

**Not final:** per the exit criteria, this envelope is finalized only after
Phase 3 empirically confirms adequate headroom for model loading, serving,
telemetry, and benchmark execution. CPU **architecture** differences (AMD
Turin vs Intel Emerald Rapids vs Akamai's host CPU) cannot be equalized —
they are recorded, not hidden.

## 8. Approximate costs

See [cost-guardrails.md](cost-guardrails.md) for controls. Estimates use the
pricing marked [ASSUMPTION] above; all are order-of-magnitude planning
figures, not quotes.

| Phase | Basis | Estimate |
| --- | --- | --- |
| Phase 2 (synthetic workload) | No GPU; local dev + free public-repo CI | **≈ $0 cloud** |
| Phase 3 (Akamai baseline) | ~40–60 GPU-h × ~$3.00/h + storage/egress | **≈ $130–$200** |
| Phase 4 (optimization) | ~60–100 GPU-h × ~$3.00/h (more cells: precisions × serving paths) | **≈ $180–$300** |
| Phase 5 (Google Cloud) | ~40–60 GPU-h × ~$4.50/h + Hyperdisk/local SSD | **≈ $190–$290** |
| Phase 6 (AWS) | ~40–60 GPU-h × ~$4.00/h (4xlarge) or ~$5.27/h (8xlarge) + EBS | **≈ $170–$330** |
| Storage/network (all) | model artifact downloads (~80 GB/provider), result egress | **≈ $10–$50 total** |

Run-hour estimates assume: setup/validation ≈ 15–25 h, measured cells
(2 profiles × 3 concurrency × 5 repetitions plus warm-ups) ≈ 15–30 h, and
retry margin. Instances are stopped/destroyed between sessions (hourly
billing on all three providers makes idle time the main cost risk).

## 9. Automatic shutdown and orphan detection (options)

- **Akamai:** no native instance auto-stop scheduler; use in-instance idle
  watchdog (systemd timer that shuts down after N idle minutes) plus a
  read-only sweep script listing GPU Linodes older than a TTL tag. [VERIFIED
  that no first-party scheduler is documented; watchdog approach ASSUMPTION.]
- **Google Cloud:** native support for instance schedules and
  `max-run-duration` / auto-delete on Spot and Flex-start; budget alerts via
  Cloud Billing. [VERIFIED — documented Compute Engine features.]
- **AWS:** EC2 instance auto-stop via CloudWatch alarm on low utilization,
  AWS Budgets actions, and instance `InstanceInitiatedShutdownBehavior`;
  orphan sweep via read-only `describe-instances` filtered by project tag.
  [VERIFIED — documented AWS features.]
- All phases: every resource carries a project tag/label; a read-only orphan
  report runs at the end of each session and anything unexpected is reported
  to the owner before any deletion (deletion itself requires approval).

## 10. Organizational approvals

- **[UNRESOLVED — owner action]** Whether organizational approval is needed to
  use company resources (the Akamai account) and to publish comparative
  findings involving Akamai Cloud, Google Cloud, and AWS. Publishing
  cross-provider benchmark comparisons may implicate provider terms of
  service and employer policy; obtain a written go/no-go before Phase 7. The
  repository's publication-governance process
  ([publication-governance.md](publication-governance.md)) blocks any release
  until this approval is recorded.

## 11. Summary of unresolved questions (blockers and owner actions)

1. Read-only credentials for Akamai, Google Cloud, and AWS (see section 1) so
   the preflight scripts can verify account-level availability, quota, and
   pricing.
2. Akamai limited-availability onboarding status for the RTX PRO 6000 plan.
3. Google Cloud `NVIDIA_RTX_PRO_6000_GPUS` quota and consumable regions.
4. AWS G-instance vCPU quota and G7e regional capacity.
5. Whether the NIM path requires a Production Branch model / NVIDIA AI
   Enterprise license for this research.
6. Organizational approval for company-resource use and comparative
   publication.
7. Empirical validation (Phase 3) of: Nemotron 3.5 Lightning on RTX PRO 6000
   Server Edition, the vLLM NVFP4 pin, and the controlled-resource envelope.
