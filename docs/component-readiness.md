# Component readiness register

This is a **public documentation and evidence-track register**, not a
benchmark finding and not a launch authorization. Statuses here are
classifications from already-recorded public sources and project
decisions. They are not measurements.

**Conditional does not authorize genuine inference.** Every genuine
execution gate requires contract status `ready`. Conditional means the
path is recognized and unresolved conditions remain. Blocked and deferred
paths are not deadline deliverables.

No private C1/C2 outcomes, MVL-F measurements, account identifiers,
credentials, or unpublished results belong in this file. Future live
execution of any non-ready path requires a separate owner authorization
and a digest-bearing configuration.

The machine contract is
[engine-precision-contract.md](engine-precision-contract.md). Phase 4
live execution remains unauthorized (decision D-0020).

## Register

| Component/path | Status | Current boundary |
| --- | --- | --- |
| vLLM BF16, single GPU | ready | Existing validated baseline path only |
| vLLM NVFP4 | conditional/offline evidence only; live blocked | No official validated RTX PRO 6000 recipe yet |
| TensorRT-LLM BF16/NVFP4 | conditional/offline evidence only; live blocked | Exact Nemotron profile, container/build path, and native-tool compatibility unresolved |
| NVIDIA NIM BF16/W4A16/NVFP4 | conditional; strongest bounded follow-up candidate | Exact immutable image digest, runtime profile ID, embedded model identity, entitlement/terms, and parser/tool behavior require local resolution |
| NVIDIA Dynamo | blocked/deferred | Current Nemotron path uses experimental images and unsupported/unproven RTX PRO distributed topology |
| Single-node multi-GPU | conditional infrastructure evidence only; live blocked | Requires at least two GPUs plus verified model/runtime compatibility |
| Multi-node | blocked/deferred | Requires multiple nodes and verified NCCL/network/RDMA/NIXL topology |

## Evidence notes (no new research)

These notes reuse already-recorded public sources and retrieval dates
from [feasibility-report.md](feasibility-report.md). They do not add
benchmark claims.

**vLLM BF16, single GPU.** The builtin contract profile
`vllm-bf16-single-gpu` is the only core-registered ready path. It
expresses the already-authorized single-GPU vLLM BF16 serving contract.
This register does not republish any private run outcome.

**vLLM NVFP4.** Feasibility recorded NVFP4 MoE on SM120 as unverified
until tested (retrieved 2026-09-05): native kernels exist but are
opt-in, and the default path can select broken CUTLASS kernels on SM120.
Sources: [vllm#31085](https://github.com/vllm-project/vllm/issues/31085),
[vllm#33416](https://github.com/vllm-project/vllm/issues/33416),
[cutlass#3096](https://github.com/NVIDIA/cutlass/issues/3096). No
official validated RTX PRO 6000 recipe is recorded. Live cells remain
blocked.

**TensorRT-LLM BF16/NVFP4.** Feasibility recorded official RTX Pro 6000
SE FP4 support and published RTX 6000 Pro performance data for a
similar-shape MoE, retrieved with the TensorRT-LLM 1.1.0 overview and
perf overview. The exact Nemotron 3.5 Lightning profile, container or
build path, and native-tool compatibility remain unresolved. Live cells
remain blocked.

**NVIDIA NIM BF16/W4A16/NVFP4.** Feasibility recorded that a Nemotron
3.5 Lightning NIM exists, with Blackwell guidance and NGC entitlement
rules
([NIM docs](https://docs.nvidia.com/nim/large-language-models/latest/get-started/advanced/get-started-nemotron-3.5-lightning.html)).
Project entitlement, the exact immutable image digest, runtime profile
ID, embedded model identity, and parser/tool behavior still require
local resolution. This is the strongest bounded follow-up candidate and
is still conditional: it does not authorize genuine inference.

**NVIDIA Dynamo.** Phase 8 remains optional and unauthorized. No
validated RTX PRO 6000 distributed topology is recorded. The path is
blocked/deferred and is not a deadline deliverable.

**Single-node multi-GPU.** Topology `multi-gpu` is declarable. Live use
requires at least two GPUs and verified model/runtime compatibility.
Infrastructure evidence only; live cells remain blocked.

**Multi-node.** Topology `multi-node` is declarable. Live use requires
multiple nodes and verified NCCL / network / RDMA / NIXL topology. Those
checks are unresolved here. The path is blocked/deferred and is not a
deadline deliverable.
