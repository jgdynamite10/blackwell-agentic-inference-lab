# Blackwell Agentic Inference Lab

An open-source research project with documented and reproducible methodology
for measuring agentic AI inference outcomes on NVIDIA RTX PRO 6000 Blackwell
Server Edition systems across cloud environments — how inference software,
numerical precision, and cloud environment affect the ability of such a system
to satisfy production-like agentic-AI service-level objectives (SLOs).

**Canonical repository:**
[https://github.com/jgdynamite10/blackwell-agentic-inference-lab](https://github.com/jgdynamite10/blackwell-agentic-inference-lab)
— all development, issues, pull requests, releases, security reporting, and
documentation live here. The repository at
`github.com/jgdynamite/blackwell-agentic-inference-lab` is a non-synchronized
redirect only and will be archived.

**Status:** Phase 1 — repository foundation and feasibility.

- **No genuine benchmarks have been run yet.** No results exist.
- **All current example files are synthetic** and clearly labeled as such.
- **Pricing figures are preliminary, non-authoritative planning estimates**
  requiring account-level verification.
- **Provider capacity and quota must be verified locally** by the operator.
- **Genuine results never go into the Git working tree** — they live outside
  the repository entirely (see below).

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

## Results privacy (public source, private results)

The source code and methodology in this repository are public, but **all
genuine benchmark results remain external to Git and private** — public
repository visibility does not authorize publication of genuine benchmark
data. The repository contains only schemas, synthetic examples clearly
labeled as such, and documentation of the result format. Benchmark tooling
writes genuine results to a location outside the repository
(`LAB_RESULTS_DIR`), fails closed if that variable is unset for a real run,
and refuses to run if the location resolves inside the repository — including
through symlinks. Credentials, account identifiers, project IDs, ARNs,
private endpoints, provider bills, and infrastructure metadata are likewise
never committed. Only explicitly approved sanitized summaries may later be
published. See [docs/results-privacy.md](docs/results-privacy.md) and
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

- Unless otherwise identified, original source code and documentation in this
  repository are licensed under the
  [Apache License 2.0](LICENSE) (see also [NOTICE](NOTICE)).
- Third-party software and model artifacts retain their respective licenses.
- OpenMDW-1.1 applies only to the identified NVIDIA model artifacts, not to
  this repository.
- Genuine benchmark datasets and results are **not** included in the
  Apache-2.0 grant unless an approved release explicitly states otherwise.

## Disclaimer

This is an independent project. The findings and views are the author's own
and do not represent Akamai Technologies, AWS, Google Cloud, NVIDIA, or any
other provider. Product and company names are the property of their respective
owners. References do not imply sponsorship or endorsement.

No benchmark findings exist or have been released. Nothing in this repository
should be interpreted as a performance or superiority claim about any cloud
provider, GPU, model, or serving stack, and no provider-superiority claim may
ever exceed the evidence.
