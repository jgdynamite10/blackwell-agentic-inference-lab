# Blackwell Agentic Inference Lab

A research lab with a documented and reproducible methodology for measuring
how inference software, numerical precision, and cloud environment affect the
ability of an NVIDIA RTX PRO 6000 Blackwell Server Edition system to satisfy
production-like agentic-AI service-level objectives (SLOs).

**This repository is private and must remain private.** Do not change its
visibility, mirror it, or publish any branch, pull request, artifact, result,
or document from it without the project owner's explicit authorization.

**Status:** Phase 1 — repository foundation and feasibility. No benchmarks
have been run. No results exist, and any future genuine results are stored
outside this Git repository entirely (see below).

## Research questions

**Primary.** How much do inference-software optimization, numerical precision,
and cloud environment affect the ability of an NVIDIA Blackwell system to
satisfy production-like agentic-AI service-level objectives?

**Secondary.** When the GPU, model, workload, container, and measurement
contract are held as constant as practical, which aspects of agentic-inference
performance remain portable across cloud environments, and which do not?

This project does not attempt to identify a universal cloud winner. It aims to
determine where agentic-AI performance, reliability, and economics originate.
See [docs/research-questions.md](docs/research-questions.md).

## What will be measured

A synthetic **Cloud Operations Agent** (no production systems, no customer
data) works through deterministic incident scenarios using simulated tools.
The benchmark measures outcome-level quantities such as successful tasks per
GPU-hour, SLO-attaining throughput, end-to-end task completion time, time to
first token, inter-token latency, GPU utilization and power, error rates, and
cost per successful task. The full measurement contract is in
[methodology/measurement-contract.md](methodology/measurement-contract.md).

## Planned technical scope

| Element | Plan |
| --- | --- |
| GPU | NVIDIA RTX PRO 6000 Blackwell Server Edition, 96 GB, single GPU |
| Clouds | Akamai Cloud (baseline), then Google Cloud G4 and Amazon EC2 G7e (replication) |
| Model | NVIDIA Nemotron 3.5 Lightning 30B-A3B (BF16 and NVFP4, subject to compatibility) |
| Serving | vLLM baseline; TensorRT-LLM and NVIDIA NIM as subsequent paths |
| Telemetry | NVIDIA DCGM, Prometheus, Grafana; selected Nsight Systems profiling |
| Optional | NVIDIA Dynamo multi-GPU extension (Phase 8, only if justified) |

The [feasibility report](docs/feasibility-report.md) distinguishes verified
facts from assumptions, estimates, and inferences, and time-sensitive claims
(pricing, availability, compatibility) are reverified before execution rather
than treated as settled.

## Repository layout

```
AGENTS.md                    Canonical project rules (safety, integrity, scope)
docs/                        Charter, roadmap, architecture, feasibility, governance
methodology/                 Measurement contract, experiment matrix, workload, reproducibility
schemas/                     JSON Schemas for run manifests and benchmark results
examples/                    Synthetic example manifest and result files (NOT real data)
src/blackwell_lab/           Minimal Python package (schema validation, results-path guard)
tests/                       Automated tests
scripts/preflight/           Read-only cloud feasibility checks (run locally by the operator)
results/                     Placeholder only — genuine results are never committed here
```

## Results privacy

**All genuine benchmark results remain external to Git and private.** The
repository contains only schemas, synthetic examples clearly labeled as such,
and documentation of the result format. Benchmark tooling writes genuine
results to a location outside the repository (`LAB_RESULTS_DIR`), fails closed
if that variable is unset for a real run, and refuses to run if the location
resolves inside the repository — including through symlinks. Nothing is
published without the project owner's explicit approval. See
[docs/results-privacy.md](docs/results-privacy.md) and
[docs/publication-governance.md](docs/publication-governance.md).

## Getting started (development)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```

## Project governance

- [AGENTS.md](AGENTS.md) — permanent safety and research-integrity rules.
- [docs/roadmap.md](docs/roadmap.md) — the eight project phases. Work stops at
  the end of the currently authorized phase.
- [docs/cost-guardrails.md](docs/cost-guardrails.md) — spend controls.
- [SECURITY.md](SECURITY.md) — reporting and secret-handling policy.
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute.

## Licensing

Copyright ownership, publication authorization, and licensing for this project
are under review. No open-source software or documentation license is granted
at this time. An approved license may be added only after the required review
and explicit authorization.

## Disclaimer

No benchmark findings exist or have been released. Nothing in this repository
should be interpreted as a performance or superiority claim about any cloud
provider, GPU, model, or serving stack.
