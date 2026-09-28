# Engine / precision contract (Phase 4A)

Decision **D-0020** authorizes this contract only. It is provider-neutral,
credential-free, and is **not** a launch command. Live engine/precision
execution, model downloads, container pulls, and component-specific launch
verbs remain unauthorized.

The machine-readable schema is
[../schemas/engine-contract.schema.json](../schemas/engine-contract.schema.json).
The Python package is `blackwell_lab.engines`.

## Contract version

`ENGINE_CONTRACT_VERSION` is **1.0.0**.

## Required declaration

A declaration must name all of the following. Missing or floating values
fail closed before inference.

| Field | Meaning | Exact-version rule |
| --- | --- | --- |
| `engine` | Serving-engine identity | Closed set: `vllm`, `tensorrt-llm`, `nim` |
| `precision` | Numerical precision | Closed set: `bf16`, `nvfp4`, `fp8`, `w4a16` |
| `topology` | Accelerator layout | `single-gpu` (1 GPU, 1 node), `multi-gpu` (≥2 GPUs, 1 node), or `multi-node` (≥2 nodes) |
| `identity.model_artifact` | Model artifact name | Non-empty; not a floating label |
| `identity.model_revision` | Immutable model revision | 40-character lowercase hex commit |
| `identity.model_artifact_hash` | Content hash | `sha256:` / `sha512:` / `blake3:` plus the matching hex digest |
| `identity.container_digest` | Immutable container | `repo@sha256:<64 hex>` — tags such as `:latest` are refused |
| `identity.engine_version` | Exact engine version | Non-empty; not `latest`, `nightly`, `stable`, `current`, `main`, `master`, or `head` |

Optional fields:

- `profile_id` — pin evaluation to one registered profile. An unknown id
  fails closed.
- `entitlements[]` — capability or license prerequisites. A required
  entitlement that is `false` or `unknown` (`satisfied: null`) blocks the
  declaration.

## Readiness

Evaluation returns exactly one of:

| Status | Meaning |
| --- | --- |
| `ready` | A registered profile matches and required entitlements are satisfied |
| `conditional` | A registered profile matches with stated conditions; inference is not blocked by this contract |
| `blocked` | Missing support, unknown profile, entitlement failure, or no matching profile |

`require_supported_contract` raises `EngineContractError` only when the
status is `blocked`. Conditional declarations may proceed so a later
component can finish its own checks. No profile match is blocked.

## Builtin authorized profile

The core registers exactly one profile:

| `profile_id` | Engine | Precision | Topology |
| --- | --- | --- | --- |
| `vllm-bf16-single-gpu` | `vllm` | `bf16` | `single-gpu` |

This profile recognizes the already-authorized vLLM + BF16 + single-GPU
path. It does **not** tighten existing MVL or qualification pins and does
not launch a server. Any well-formed vLLM BF16 single-GPU identity is
accepted, including test fixtures that use engine `0.28.0`.

TensorRT-LLM, NIM, NVFP4, FP8, W4A16, multi-GPU, and multi-node remain
blocked until a component module registers a matching profile.

## Fail-closed provenance

Shared genuine-run paths call the contract before a client is used:

- `RealRunSpec` validation calls `require_real_spec_contract`.
- Live provenance appends an `EngineContractError` as a mismatch and
  refuses the cell.

Existing vLLM BF16 manifests that omit `serving.topology` and
`serving.engine_profile_id` remain schema-valid. New genuine gpu-mode
manifests emit those optional fields when the contract evaluates.

## Extension points for component agents

Component agents must not edit the core contract, each other, or the
shared integration files owned by this change. They register by adding a
module under `src/blackwell_lab/engines/components/` and calling
`register_engine_profile` at import time.

1. Implement the `EngineProfile` protocol (`profile_id`, `engine`,
   `precision`, `topologies`, `matches`, `evaluate`).
2. Place the module in `engines/components/` so
   `load_registered_components()` discovers it with `pkgutil`. The core
   never names those modules.
3. Return `ready`, `conditional`, or `blocked`. Do not download artifacts,
   call a provider API, or add a launch command in this phase.
4. Keep existing vLLM BF16, C1/C2, workload 2.4.0, evaluator 3.1.0,
   thresholds, lifecycle, Terraform, and publication policy unchanged.

The offline CLI is:

```bash
blackwell-cloud engine-contract --list
blackwell-cloud engine-contract --config examples/example-engine-contract.json
```

`blackwell-cloud readiness` validates the synthetic example contract
without credentials or provider access.
