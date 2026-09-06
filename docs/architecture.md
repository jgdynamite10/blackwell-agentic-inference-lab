# Architecture

This document describes the intended system architecture. Phase 1 implements
only the repository scaffolding; components marked with their phase are built
later, subject to authorization.

## Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│ Benchmark host (single-GPU cloud instance, one per provider)        │
│                                                                     │
│  ┌────────────────────────┐    ┌─────────────────────────────────┐  │
│  │ Benchmark container    │    │ Serving container               │  │
│  │  - Cloud Ops Agent     │──▶ │  - vLLM | TensorRT-LLM | NIM    │  │
│  │    (Phase 2)           │    │    (Phases 3–4)                 │  │
│  │  - simulated tools     │    │  - Nemotron 3.5 Lightning       │  │
│  │  - scenario driver     │    │    BF16 / NVFP4                 │  │
│  │  - success evaluator   │    └─────────────────────────────────┘  │
│  └────────────┬───────────┘    ┌─────────────────────────────────┐  │
│               │                │ Telemetry (Phase 4)             │  │
│               │                │  - DCGM exporter                │  │
│               │                │  - Prometheus (+ Grafana)       │  │
│               │                │  - selected Nsight Systems      │  │
│               ▼                └─────────────────────────────────┘  │
│  run manifest + results  ────────────▶  $LAB_RESULTS_DIR (PRIVATE,  │
│                                         outside the repository)    │
└─────────────────────────────────────────────────────────────────────┘
```

## Components

### Synthetic Cloud Operations Agent (Phase 2)

An agent loop that receives deterministic incident scenarios and works them
using simulated tools: `get_service_health()`, `query_metrics()`,
`search_logs()`, `retrieve_runbook()`, `check_recent_changes()`,
`recommend_remediation()`. Tools are backed by synthetic fixture data only —
never production systems or customer information. Tool latency is simulated
deterministically so tool-execution time can be separated from model-serving
time.

### Scenario driver and evaluator (Phase 2)

Deterministic incident definitions (elevated latency, pod failures, memory
pressure, GPU saturation, storage latency, failed deployments, unhealthy
upstreams, DNS failures, rate limiting, capacity exhaustion) with
machine-checkable success criteria. Scenarios are deterministic; model outputs
are not assumed to be. Runs use fixed generation settings, record seeds when
supported, and include repeated measurements (see
[../methodology/measurement-contract.md](../methodology/measurement-contract.md)).

### Serving stack (Phases 3–4)

One serving container per configuration, pinned by digest. Proposed paths:
vLLM (the openly developed baseline engine), then TensorRT-LLM and NVIDIA
NIM. Precision:
BF16 and NVFP4, subject to the compatibility findings in
[feasibility-report.md](feasibility-report.md).

### Telemetry (Phase 4)

NVIDIA DCGM exporter → Prometheus → Grafana for GPU utilization, memory,
power, and energy. Selected Nsight Systems traces for targeted investigations
only (they are large and not part of routine runs). Telemetry is stored
privately with the run results.

### Results handling (all phases)

- The runner writes genuine results only under `$LAB_RESULTS_DIR`, which must
  resolve **outside** the repository; the runner refuses to start otherwise.
  The guard is implemented in `src/blackwell_lab/paths.py` (Phase 1).
- Every run writes a manifest conforming to
  [../schemas/run-manifest.schema.json](../schemas/run-manifest.schema.json)
  and results conforming to
  [../schemas/benchmark-result.schema.json](../schemas/benchmark-result.schema.json).
- The repository carries only schemas, synthetic examples, test fixtures,
  methodology, documentation, and code; sanitized results may be added only
  with explicit owner approval (Phase 7).

## Comparison modes

Two strictly separated modes (never mixed in analysis or reporting):

1. **Controlled-resource mode.** The serving and benchmark workload run
   under a documented common CPU and system-memory envelope that is the same
   on all three providers, defined as a **joint total across both containers
   combined** (provisional: 14 vCPUs / 100 GiB joint total, see
   [feasibility-report.md](feasibility-report.md) §7). The exact per-container
   allocation and the cgroup enforcement mechanism are frozen only after
   Phase 3 headroom validation.
2. **Provider-native mode.** Each provider's normal purchasable instance
   configuration, no artificial caps; evaluates what a customer actually
   receives, including economics.

## Host parity notes

The same GPU model across providers creates GPU parity, **not** system parity.
Every run manifest records CPU model and architecture, vCPU count, system
memory, storage type, network configuration, virtualization, driver, CUDA
version, OS, region, and any other material environmental facts, so
differences are recorded rather than hidden.

## Infrastructure-as-code (Phases 3+)

Provisioning will use Terraform with per-provider modules; state and `.tfvars`
stay outside the repository. `terraform apply`/`destroy` require explicit
owner approval per [../AGENTS.md](../AGENTS.md).

Billing-safety design constraint: on Akamai, powering off a Linode does not
stop billing — compute billing stops only when the service is deleted from
the account — and on AWS/GCP, disks, addresses, and snapshots may continue
billing while an instance is stopped. Run tooling therefore treats "export
and verify results, then owner-approved deletion of exactly the run's tagged
resources, then verify nothing billable remains" as the normal end-of-session
sequence. Automatic-shutdown, teardown, and orphan-detection controls are
described in [cost-guardrails.md](cost-guardrails.md).
