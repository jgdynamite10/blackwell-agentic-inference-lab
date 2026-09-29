# TensorRT-LLM single-GPU profiles

**Offline classification only.** `tensorrt-llm-bf16-single-gpu` and
`tensorrt-llm-nvfp4-single-gpu` are conditional at most. Live execution is
blocked. Neither profile authorizes inference. No build, conversion, or
launch path exists in this repository. No benchmark finding is recorded.
Multi-GPU and multi-node remain deferred. Future execution requires
immutable artifacts, separate owner approval, and a digest-bearing
configuration.

This page is the component record for those two profiles. It does not
change the core contract, the builtin vLLM BF16 profile, or any shared
readiness register.

## Profiles

| `profile_id` | Engine | Precision | Topology | Highest status |
| --- | --- | --- | --- | --- |
| `tensorrt-llm-bf16-single-gpu` | `tensorrt-llm` | `bf16` only | `single-gpu` (1 GPU, 1 node) | `conditional` |
| `tensorrt-llm-nvfp4-single-gpu` | `tensorrt-llm` | `nvfp4` only | `single-gpu` (1 GPU, 1 node) | `conditional` |

`ready` is not a return value of either profile. `require_ready_contract`
and `require_supported_contract` reject both. A required entitlement that
is false or unknown blocks an otherwise evidence-consistent declaration.

## Official established evidence

Retrieved **2026-09-28** from public NVIDIA documentation and GitHub
release pages. No NGC login was used, and no container, engine, weight, or
layer was downloaded.

| Fact | Source | Retrieved |
| --- | --- | --- |
| TensorRT-LLM 1.1.0 overview lists Blackwell support as "B200, B300, GB200, GB300, RTX Pro 6000 SE with FP4 optimization" | [1.1.0 overview](https://nvidia.github.io/TensorRT-LLM/1.1.0/overview.html) | 2026-09-28 |
| TensorRT-LLM 1.1.0 performance overview includes RTX 6000 Pro Blackwell Server Edition and publishes FP4 measurements for other models, including a 1-GPU column for Qwen3 30B A3B. The listed FP4 models do not include Nemotron 3.5 Lightning. The page states that the PyTorch backend workflow used for those measurements does not require an engine to be built | [1.1.0 perf overview](https://nvidia.github.io/TensorRT-LLM/1.1.0/developer-guide/perf-overview.html) | 2026-09-28 |
| GitHub tag `v1.1.0` was published 2025-12-19. Its notes include RTX Pro 6000 test-infrastructure changes. They do not name a Nemotron 3.5 Lightning RTX PRO 6000 profile | [v1.1.0](https://github.com/NVIDIA/TensorRT-LLM/releases/tag/v1.1.0) | 2026-09-28 |
| The unversioned quantization page says quantization converts high-precision values such as BF16 to lower precision. Its hardware matrix marks NVFP4 supported on Blackwell (sm120). Its model matrix has no Nemotron 3.5 Lightning row. "latest" is not a version pin; container examples on the docs site the same day were generated from 1.3.0 pre-release trees and disagreed with each other | [quantization](https://nvidia.github.io/TensorRT-LLM/latest/features/quantization.html) | 2026-09-28 |
| Install docs name runtime image `nvcr.io/nvidia/tensorrt-llm/release` and development image `nvcr.io/nvidia/tensorrt-llm/devel`. Example tags on the fetched pages were pre-release tags (`1.3.0rc24`, `1.3.0rc26`, `1.3.0rc27`). No immutable digest was on those pages | [containers](https://nvidia.github.io/TensorRT-LLM/installation/containers.html), [install guide](https://nvidia.github.io/TensorRT-LLM/installation/installation-guide.html), [latest containers](https://nvidia.github.io/TensorRT-LLM/latest/installation/containers.html), [latest install guide](https://nvidia.github.io/TensorRT-LLM/latest/installation/installation-guide.html) | 2026-09-28 |
| `v1.2.1` (published 2026-04-20) is a non-prerelease tag. Its overview page does not name RTX Pro 6000. Its highlights are a KV-cache fix and dependency upgrades, not a Nemotron 3.5 Lightning profile | [v1.2.1](https://github.com/NVIDIA/TensorRT-LLM/releases/tag/v1.2.1), [1.2.1 overview](https://nvidia.github.io/TensorRT-LLM/1.2.1/overview.html) | 2026-09-28 |
| `v1.3.0rc28` (published 2026-09-23) is a pre-release. The first page of GitHub releases that day was pre-release tags plus `v1.2.1` | [releases](https://github.com/NVIDIA/TensorRT-LLM/releases) | 2026-09-28 |
| NVIDIA Dynamo's Nemotron 3.5 Lightning recipe marks TensorRT-LLM targets experimental. The targets name B200, H100, H200, and GB200. They do not name RTX PRO 6000 | [Dynamo recipe](https://docs.nvidia.com/dynamo/dev/recipes/nemotron-3-5-lightning) | 2026-09-28 |

The public weight-checkpoint names
`nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16` and
`nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4` are the artifact names
already recorded in [feasibility-report.md](../feasibility-report.md). This
pass did not re-query Hugging Face and did not open an NGC catalog entry.
TensorRT-LLM install pages do not state whether the release image is
keyless. Entitlement is therefore unresolved, not recorded as true or false.

SM120 NVFP4 support and RTX Pro 6000 SE FP4 wording do not establish a
functioning Nemotron 3.5 Lightning profile.

## Project interpretation

These profile ids are repository classification names. They are not NVIDIA
profile ids.

An evidence-consistent declaration uses all of the following:

- engine `tensorrt-llm`
- the profile's own precision
- topology `single-gpu` with one GPU and one node
- `identity.model_artifact` equal to the public Nemotron checkpoint for that precision
- `identity.engine_version` exactly `1.1.0`
- `identity.container_digest` shaped as `nvcr.io/nvidia/tensorrt-llm/release@sha256:<64 lowercase hex>` with no tag

That declaration is **conditional**. `1.1.0` is the versioned document set
that names RTX Pro 6000 SE FP4 support. Accepting it does not mean 1.1.0 is
a validated Nemotron pin. The checkpoint name identifies weights, not a
TensorRT-LLM conversion or engine build. The repository prefix matches the
documented runtime image; the hex is not a digest this project retrieved.
`1.2.1`, pre-release tags, a `v` prefix, the development image, a tag beside
the digest, and any other repository are outside this evidence set and are
blocked. That refusal is a closed classification rule, not a claim that
NVIDIA removed hardware support in later releases.

Floating labels such as `latest` are rejected by the core contract before
these profiles evaluate them.

## Unresolved requirements

An evidence-consistent declaration still carries every one of these:

- exact supported Nemotron 3.5 Lightning TensorRT-LLM profile
- exact conversion/build artifact and immutable digest
- exact container/runtime identity
- RTX PRO 6000 live behavior
- BF16 or NVFP4 end-to-end viability, matching the profile
- native OpenAI tool-calling compatibility
- reasoning/parser compatibility
- entitlement or registry-access requirements

## Mocked test coverage

`tests/test_engine_tensorrt_llm.py` uses synthetic revisions, hashes, and
digests. It does not call a provider, a registry, or a model client.

The tests prove discovery without the offline CLI, idempotent discovery,
order-independent classification, conditional-never-ready results for both
precisions, rejection by both execution gates, real-run refusal before a
client call, live-provenance refusal before a cell, entitlement false and
null blocking, wrong precision/version/identity/topology blocking, multi-GPU
and multi-node non-matches, floating identities failing closed, no duplicate
registration, unchanged ready `vllm-bf16-single-gpu`, and an import with no
network, subprocess, or download.

## Mandatory future live checks

None of the following were run, and none can be implied by this page:

- a named Nemotron 3.5 Lightning TensorRT-LLM profile on RTX PRO 6000 Blackwell Server Edition
- an immutable conversion or engine-build digest
- an immutable `nvcr.io/nvidia/tensorrt-llm/release` digest and the entitlement required to use it
- BF16 and NVFP4 end-to-end serving, including native OpenAI tool calls and the reasoning parser
- driver, CUDA, and observed engine-version provenance for that exact identity

A later `ready` status would be a separate change after those checks and
after owner authorization. This component does not add that status.

## Deferred topology

`multi-gpu` and `multi-node` do not match these profiles. Unnamed
declarations stay blocked as unsupported. A declaration pinned to either
profile id is blocked because the profile does not match. Single-node
multi-GPU and multi-node remain deferred.

## Authorization status

Decision D-0020 authorizes the provider-neutral contract, not live
engine/precision execution. These profiles are an offline classification
component on that contract. They do not authorize `blackwell-cloud`
mvl-baseline, qualify-agent, pilot, apply, or destroy. Phase 4 live
execution remains unauthorized until a separate digest-bearing approval
phrase exists. This page records no benchmark finding and publishes no
result.
