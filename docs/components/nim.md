# NVIDIA NIM component profile (offline)

**Offline classification only.** This page records an offline reading of
public NVIDIA documentation. It is not a benchmark finding, not a measured
result, and not permission to run a model.

**Live execution blocked.** No NIM profile registered here is `ready`.
The genuine execution gate accepts only `ready`.

**Conditional does not authorize inference. Conditional never authorizes genuine inference.** A conditional result means the declaration matches the documented surface and unresolved conditions remain. It must not reach client construction, inference, measurement, or result creation.

**No launch path exists.** This component does not contain a container
pull, a server start, a bootstrap step, a Kubernetes manifest, or a
Terraform change.

**No benchmark finding is recorded.** Nothing on this page is a latency,
throughput, quality, or cost measurement.

**Future live execution requires separately reviewed immutable identities,
entitlement verification, and explicit owner approval.** A catalog digest,
a support-matrix row, or a conditional classification is not that
approval. Phase 4 live execution remains unauthorized until the project
owner gives the separate digest-bearing approval for that execution.

## Registered profiles

Automatic discovery registers exactly these three profiles. Each one owns
`engine=nim`, topology `single-gpu`, and engine version `2.0.13` for one
precision, and only for this model family and container repository:

- model artifact: `nvidia/nemotron-3.5-lightning`
- container identity:
  `nvcr.io/nim/nvidia/nemotron-3.5-lightning-30b-a3b@sha256:<64 lowercase hex>`

Any 64-character lowercase hex digest in that repository satisfies the
shape. No specific digest is an adopted execution pin. The classification
is `conditional` when that surface matches and required entitlements are
not failed. It is `blocked` when the model, container repository, version,
precision, or topology is outside the surface, or when a required
entitlement is false or unknown. Conditional never authorizes genuine
inference. No declaration evaluates to `ready`.

| Profile ID | Precision | Topology | Engine version | Classification |
| --- | --- | --- | --- | --- |
| `nim-nemotron35-bf16-single-gpu` | `bf16` | `single-gpu` | `2.0.13` | conditional, or blocked |
| `nim-nemotron35-w4a16-single-gpu` | `w4a16` | `single-gpu` | `2.0.13` | conditional, or blocked |
| `nim-nemotron35-nvfp4-single-gpu` | `nvfp4` | `single-gpu` | `2.0.13` | conditional, or blocked |

FP8 is not registered. The NIM 2.0.13 support matrix does not list an FP8
profile for this model. Multi-GPU and multi-node are not registered.

## Official facts

Retrieved **2026-09-28**. Each row is limited to what that page stated.
Nothing below was filled in by guessing a digest, runtime profile, or
entitlement.

| Source | Supported fact | Unresolved implication |
| --- | --- | --- |
| [NIM for LLM and VLM 2.0.13 support matrix](https://docs.nvidia.com/nim/large-language-models/2.0.13/reference/support-matrix.html), `nvidia/nemotron-3.5-lightning` profile table | BF16 TP=1 PP=1 requires 66 GB per GPU, minimum 1 GPU, Ampere or newer (SM 8.0+). W4A16 TP=1 PP=1 requires 32 GB per GPU, minimum 1 GPU, Ampere or newer (SM 8.0+). NVFP4 TP=1 PP=1 requires 30 GB per GPU, minimum 1 GPU, Blackwell or newer (SM 10.0+). The same precisions are also listed at higher tensor-parallel sizes. | A minimum-VRAM floor is not a measurement of this workload's KV cache, concurrency, or context. Empirical single-GPU memory viability stays unresolved. |
| Same support matrix, verified-GPU table for this model | `NVIDIA-RTX-PRO-6000-Blackwell-Server-Edition` is listed at 96 GB under "Blackwell (SM 10.0) — supports NVFP4, W4A16, BF16" with "All profiles". The page states that only GPUs in the verified tables have been tested end-to-end for this model at this NIM version, and that VRAM floors do not include headroom for large KV caches. | The named SKU row is the hardware evidence for this path. A generic Blackwell, B200, or GB200 row is not that evidence. This repository has not repeated the live check, so RTX PRO 6000 live compatibility stays unresolved. |
| Same support matrix, optimized deployment profiles | Two optimized IDs are published: NVFP4 TP=1 on `NVIDIA-B200`, profile `4816da181f28c435aaa2efa0e2302309c28f2c08c9534070e89f67854a521d72`; and `NVFP4 (W4A16) + FP8` TP=1 on `NVIDIA-H200`, profile `1e34732516864e06c8dad271edd1d19b986c4c010b9d7e3adcb8b113df58819b`. | Neither ID is an RTX PRO 6000 single-GPU BF16, W4A16, or NVFP4 profile. The exact NIM runtime profile ID for this path is unresolved. Those IDs must not be reused for it. |
| [NGC tags for `nemotron-3.5-lightning-30b-a3b`](https://catalog.ngc.nvidia.com/orgs/nim/nvidia/containers/nemotron-3.5-lightning-30b-a3b/latest/tags) | Tag `2.0.13` lists linux/amd64 digest `sha256:4f93f7e217ca05954bb34d3ccabbb3080f419e0ee5e1c0053f70debc00eb97e8` (created 2026-09-17) and a different linux/arm64 digest. Tag `latest` lists the same pair. Tag `2.0.9-variant` lists a different linux/amd64 digest, `sha256:ddb4442fe147ee7ba751861ebb1f8cf9186cc34cfac5ff2c88d3c33ef5a4f2b6`. | The catalog text was not confirmed with a registry manifest request, and no layer was downloaded. `latest` is a floating alias of the same digest on this date. The immutable linux/amd64 digest is not an adopted execution pin. |
| [NGC container overview](https://catalog.ngc.nvidia.com/orgs/nim/nvidia/containers/nemotron-3.5-lightning-30b-a3b) | The container packages NVIDIA-Nemotron-3.5-Lightning-30B-A3B. The model pull method is "Automatic (embedded in container)". Use is governed by the NVIDIA Software License Agreement and the Product-Specific Terms for AI Products. The page says downloading or using the container agrees to the linked licenses. Operating system: Linux. Multinode support: No. The page also says the container exposes industry-standard OpenAI-compatible APIs. Its stated NGC release date is 08/10/2026, while the tags page dates tag `2.0.13` to 2026-09-17. | No embedded revision or artifact digest is on the page. Account entitlement and license acceptance are not shown. OpenAI-compatible API wording is not proof of native tool-calling for workload 2.4.0. The 08/10/2026 date is not a statement that tag `2.0.13` was published that day. NGC's sentence that container components are ready for commercial or non-commercial use is license wording, not engine-contract status `ready`. |
| [NIM offerings, documentation set 2.0.13](https://docs.nvidia.com/nim/large-language-models/2.0.13/about-nim-llm/nim-offerings.html) | NIM is available at no charge under the applicable license terms and is not covered by NVIDIA Enterprise support. NIM Certified Production Branch requires an active NVIDIA AI Enterprise subscription. The certified-model list is on the NGC catalog. | The page does not say which offering this image tag belongs to, and it does not show this account's entitlement. A required entitlement that is false or unknown is blocked. |
| [Get Started with Nemotron 3.5 Lightning](https://docs.nvidia.com/nim/large-language-models/latest/get-started/advanced/get-started-nemotron-3.5-lightning.html) | On 2026-09-28 the `latest` URL presented document set 2.0.10, and its pull example named tag `2.0.9-variant`. The same fetch recorded parser guidance for that guide (`nemotron_v3`, `qwen3_coder`, and auto tool choice). The versioned URL `.../2.0.13/get-started/advanced/get-started-nemotron-3.5-lightning.html` returned page not found. | Parser and tool-calling text tied to tag `2.0.9-variant` is not evidence for tag `2.0.13`. Reasoning and parser compatibility with workload 2.4.0 stays unresolved. Native OpenAI tool-calling behavior on the 2.0.13 image stays unresolved. |

## Project interpretations

These are this repository's classifications. They are not additional NVIDIA
claims.

- The three precisions above have applicable single-GPU evidence on the
  NIM 2.0.13 support matrix, including the named RTX PRO 6000 Blackwell
  Server Edition row. They are registered as separate stable profile IDs.
- A matching, well-formed declaration is conditional. Required entitlement
  false or unknown is blocked by the shared contract finalizer. Unsupported
  version, precision, and topology are blocked.
- The profile owns engine version `2.0.13`, model family
  `nvidia/nemotron-3.5-lightning`, and container repository
  `nvcr.io/nim/nvidia/nemotron-3.5-lightning-30b-a3b` with a
  `sha256:<64 lowercase hex>` digest. A different model artifact, container
  repository, or engine version does not match these profiles.
- The catalog linux/amd64 digest is recorded as a retrieved fact and is
  not compared, accepted, or frozen by the profile. Exact digest adoption,
  the embedded model revision, the embedded artifact digest, account
  entitlement, the runtime profile ID, workload 2.4.0 native-tool
  compatibility, and live RTX PRO 6000 behavior remain unresolved.
- B200, GB200, GB300, and generic "Blackwell or newer" wording do not
  identify this SKU. The verified-GPU row that names
  `NVIDIA-RTX-PRO-6000-Blackwell-Server-Edition` is the hardware fact used
  here, and it still leaves live compatibility unresolved.
- Workload 2.4.0 native tool transport is unchanged. This component does
  not claim that NIM 2.0.13 implements it.

## Unresolved requirements

Every conditional result carries all of the following:

1. Immutable linux/amd64 NIM image digest, adopted as a reviewed execution
   pin rather than a floating tag.
2. Exact NIM runtime profile ID for this GPU and precision.
3. Embedded model revision and artifact digest.
4. NGC/NIM entitlement and license acceptance for the account that would
   run the image.
5. Account-visible runtime profile, as that account's `list-model-profiles`
   output would show it.
6. RTX PRO 6000 Blackwell Server Edition live compatibility on the adopted
   image.
7. Native OpenAI tool-calling behavior.
8. Reasoning and parser compatibility with workload 2.4.0.
9. Live health, model listing, and provenance.
10. Empirical single-GPU memory viability at the workload's context and
    concurrency.

## Future live checks

A later, separately authorized execution would still have to resolve every
item above on the owner's local, credentialed machine. That work is not
implemented here. It would need:

- a reviewed immutable linux/amd64 image identity, distinct from tag
  `latest`;
- the runtime profile ID actually visible for the RTX PRO 6000 Blackwell
  Server Edition and the selected precision;
- the embedded model revision and artifact digest observed from that image;
- entitlement and license acceptance checked without placing credentials
  in this repository;
- live health, model listing, and provenance observations;
- a tool-calling and reasoning-parser check against workload 2.4.0;
- a memory check on one GPU at the workload's settings;
- an explicit owner approval that names the reviewed identities.

Until those exist, the classification stays conditional or blocked, and
the execution gate refuses the cell.
