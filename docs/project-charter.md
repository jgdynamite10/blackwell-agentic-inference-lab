# Project Charter — Blackwell Agentic Inference Lab

## Purpose

Build a reproducible research lab that determines where agentic-AI
performance, reliability, and economics originate when serving a modern
open-weight model on a single NVIDIA RTX PRO 6000 Blackwell Server Edition
GPU across three public clouds.

## Objectives

1. Quantify how much **inference-software optimization** (vLLM vs
   TensorRT-LLM vs NVIDIA NIM), **numerical precision** (BF16 vs NVFP4), and
   **cloud environment** (Akamai Cloud, Google Cloud, AWS) each affect the
   ability to satisfy production-like agentic-AI service-level objectives.
2. Identify which aspects of agentic-inference performance are **portable**
   across cloud environments when the GPU, model, workload, container, and
   measurement contract are held as constant as practical — and which are not.
3. Produce outcome-level measures a service owner actually cares about:
   successful tasks per GPU-hour, SLO-attaining throughput, and cost per
   successful task — not only microbenchmark latency numbers.

## Explicit non-goals

- This project is **not** intended to identify a universal cloud winner.
- It does not benchmark multi-GPU scale-out (except the optional Phase 8
  Dynamo investigation).
- It does not evaluate model quality beyond the task-success criteria needed
  to score the synthetic workload.
- It does not touch production systems or customer data in any form.

## Stakeholders

| Role | Responsibility |
| --- | --- |
| Project owner | Authorizes phases, approves all cloud-resource actions, reviews and merges pull requests, approves any publication of results |
| Contributors / agents | Execute the currently authorized phase within the rules in [AGENTS.md](../AGENTS.md) |

## Guiding constraints

1. **Safety first.** No cloud resource is created, resized, modified, or
   deleted without explicit owner approval. See [AGENTS.md](../AGENTS.md).
2. **Public source, private results.** The repository's source and
   methodology are public (Apache-2.0), but genuine results stay outside the
   Git working tree entirely, and no genuine result is released without
   explicit owner approval identifying the exact files and scope. See
   [results-privacy.md](results-privacy.md).
3. **Phase discipline.** Work proceeds through the phases in
   [roadmap.md](roadmap.md) and stops at the end of the authorized phase.
4. **Evidence discipline.** Feasibility and analysis documents must
   distinguish verified facts, assumptions, and unresolved questions.
5. **Budget discipline.** Spend controls in
   [cost-guardrails.md](cost-guardrails.md) apply to every phase.

## Success criteria (project level)

- A benchmark whose methodology, schemas, workload definitions, and tooling
  are documented and reproducible — such that an authorized party with their
  own cloud accounts could rerun the study and check the findings.
- Cross-cloud findings that clearly separate controlled-resource comparisons
  from provider-native comparisons, and that document every material
  environmental difference.
- A technical report (Phase 7) containing only validated, reviewed, sanitized
  results. Whether, where, and in what scope any genuine result is released
  remains a separate, per-release owner decision; no result release is
  scheduled or automatic.

## Key documents

- [research-questions.md](research-questions.md)
- [roadmap.md](roadmap.md)
- [architecture.md](architecture.md)
- [feasibility-report.md](feasibility-report.md)
- [../methodology/measurement-contract.md](../methodology/measurement-contract.md)
- [../methodology/experiment-matrix.md](../methodology/experiment-matrix.md)
