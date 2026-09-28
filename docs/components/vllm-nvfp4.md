# vLLM NVFP4 single-GPU profile

Offline evidence classification for `vllm-nvfp4-single-gpu`. This page is
not a benchmark finding, not a launch authorization, and not permission to
run genuine inference.

The machine contract is the
[engine/precision contract](../engine-precision-contract.md). The public
register row for this path stays in
[component readiness](../component-readiness.md): conditional or offline
evidence only, with live execution blocked. This profile is stricter than
that register row. Because a contract-required artifact digest is
unpublished, evaluation is **blocked**. Blocked is within the conditional
ceiling. The profile cannot return `ready`.

## Classification

| Item | Value |
| --- | --- |
| Profile | `vllm-nvfp4-single-gpu` |
| Engine | `vllm` |
| Precision | `nvfp4` |
| Topology | `single-gpu` (1 GPU, 1 node) |
| Status | **blocked** |
| Genuine inference | refused by both execution gates |
| Launch command | none in this profile |

Live execution stays blocked until the exact RTX PRO 6000 NVFP4 path is
validated. Generic Blackwell support, SM120 compile coverage, an RTX 5090
hardware-matrix row, or a DGX Spark / H100 recipe does not supply that
validation.

No genuine inference is authorized. No benchmark finding is recorded.

## Immutable identities

Retrieved **2026-09-28** from official model-card, release, and public
registry metadata. Weights, image layers, and provider APIs were not
downloaded.

| Field | Identity | Source |
| --- | --- | --- |
| Model artifact | `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4` | [NVIDIA NVFP4 model card](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4) |
| Model revision | `bee7596271d1495f6992ae224aefde4410e816b8` | Hugging Face model API `sha` / commits for that card (commit date 2026-09-10) |
| Engine version | `0.27.1` | Model card image tag `vllm/vllm-openai:v0.27.1`; [vLLM release v0.27.1](https://github.com/vllm-project/vllm/releases/tag/v0.27.1) (published 2026-08-11) |
| Container | `docker.io/vllm/vllm-openai@sha256:c2f3b1b964e47809b722b5e75b61b1e7b39a50f70388cf2bf2418f16a9f31da2` | Docker Hub tag `v0.27.1`, **linux/amd64** image digest ([vllm/vllm-openai tags](https://hub.docker.com/r/vllm/vllm-openai/tags)). Tag metadata last updated 2026-08-11. |
| Artifact digest | **unresolved** | The model card and the model API file list do not publish one immutable artifact digest |

The linux/amd64 digest above is the platform image digest. The multi-arch
index digest `sha256:0a51ea5b4ae2dc5d81890e5173f54203d2a3ae0cfffe51b8fd2afd4391bfd967`
is a different object and is rejected.

The BF16 checkpoint is a different artifact
(`nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16`) and a different
revision. Those identities are not reused here.

## CUDA, driver, and architecture

| Fact | Evidence | What it does not prove |
| --- | --- | --- |
| Image CUDA default `13.0.3` | [v0.27.1 Dockerfile](https://github.com/vllm-project/vllm/blob/v0.27.1/docker/Dockerfile) `ARG CUDA_VERSION=13.0.3` | That this container serves NVFP4 on RTX PRO 6000 |
| Dockerfile `TORCH_CUDA_ARCH_LIST` includes `12.0` among `7.5` through `12.0` | Same Dockerfile | RTX PRO 6000 NVFP4 compatibility. `12.0` is generic compile coverage |
| CUDA 13.0 corresponds to driver branch **R580**; CUDA 13.x minor-version compatibility requires driver **>= 580** | [CUDA Toolkit release notes](https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html), retrieved 2026-09-28 | A validated RTX PRO 6000 NVFP4 host-driver pin |

The model card's supported-hardware list is NVIDIA Blackwell (DGX Spark /
GB10, GB200, GeForce RTX 5090), NVIDIA Hopper (H100, H200), and NVIDIA
Ampere via W4A16. Its hardware matrix marks GeForce RTX 5090 as having
**no published recipe**. RTX PRO 6000 is not in that list.

The card's "Local AI (RTX 5090, DGX Spark, and RTX 6000 Pro)" section
points at partner Ollama, llama.cpp, and LM Studio flows. It is not a
vLLM NVFP4 recipe for RTX PRO 6000.

## Native tool calls

The model card documents a vLLM tool parser and auto tool choice on
recipes for other GPUs, and it shows an OpenAI-compatible client example
for those backends. That documentation is not a measured result on RTX
PRO 6000 with this NVFP4 checkpoint, this revision, and this container.

Native OpenAI tool-call support for this profile is **not validated**.

## Boundaries

This profile matches only `vllm` + `nvfp4` + `single-gpu` with one GPU
and one node. The model card's own single-GPU examples are 1× DGX Spark
(GB10) or 1× H100, not RTX PRO 6000.

Multi-GPU and multi-node declarations do not match. BF16, FP8, and W4A16
do not match. A different model revision, a different container digest,
or a different engine version is blocked. A floating engine version such
as `latest` is rejected by the core contract before evaluation.

Any model artifact digest is blocked. None is the official digest,
because that digest is unpublished. Required entitlements that are false
or unknown are blocked.

`vllm-bf16-single-gpu` is a separate profile and stays the ready BF16
path. This module does not change it.

## What would have to be true before a later revision could be ready

This revision cannot become ready. A later change would need all of the
following, recorded from official or live evidence rather than inferred:

1. An official immutable NVFP4 artifact digest for
   `bee7596271d1495f6992ae224aefde4410e816b8`, pinned exactly.
2. A live RTX PRO 6000 NVFP4 run on that artifact revision, vLLM
   `0.27.1` or a newly evidenced exact engine version, and the exact
   linux/amd64 container digest under test.
3. Native OpenAI tool-call behavior checked on that same path.
4. Host driver and container CUDA observed on that path, not copied from
   the CUDA 13.0 compatibility table or from SM120 compile flags.
5. A separate owner authorization for live execution. This document does
   not grant it.

Until those exist, both genuine-execution gates refuse the cell before a
model client is used.
