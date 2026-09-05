# Research Questions

## Primary question

> How much do inference-software optimization, numerical precision, and cloud
> environment affect the ability of an NVIDIA Blackwell system to satisfy
> production-like agentic-AI service-level objectives?

Decomposition:

- **RQ1a — Software.** Holding GPU, model, precision, workload, and
  measurement contract constant, how much do vLLM, TensorRT-LLM, and NVIDIA
  NIM differ in SLO-attaining throughput, latency distribution, and successful
  tasks per GPU-hour?
- **RQ1b — Precision.** Holding everything else constant, how much does
  moving from BF16 to NVFP4 change throughput, latency, energy per task,
  task-success rate, and cost per successful task on RTX PRO 6000 Blackwell?
- **RQ1c — Environment.** Holding GPU model, container, model artifact,
  serving configuration, workload, and measurement contract constant, how much
  do the three cloud environments differ on the same measures?

## Secondary question

> When the GPU, model, workload, container, and measurement contract are held
> as constant as practical, which aspects of agentic-inference performance
> remain portable across cloud environments, and which do not?

Decomposition:

- **RQ2a.** Which measures are dominated by the GPU and serving stack (and
  therefore transfer across providers), and which are dominated by host CPU,
  memory, storage, virtualization, or networking (and therefore do not)?
- **RQ2b.** How much of the observed cross-provider difference disappears
  under the controlled-resource mode (documented common CPU and system-memory
  limits) compared with the provider-native mode?
- **RQ2c.** How do provider-native economics (list price per GPU-hour, billing
  model, quota friction) change cost per successful task even when raw
  performance is similar?

## What this project will not claim

- A universal "best cloud". The design records environmental differences; it
  does not pretend they don't exist, and it does not extrapolate beyond the
  single-GPU, single-model configuration studied.
- Provider-superiority conclusions that exceed the evidence (rule 4.3 in
  [../AGENTS.md](../AGENTS.md)).
- Model-quality claims beyond the task-success criteria of the synthetic
  workload.

## Hypotheses to be tested (registered before any measurement)

These are recorded now so later analysis cannot silently move the goalposts.

1. **H1.** Serving-path optimization (vLLM → TensorRT-LLM/NIM) changes
   SLO-attaining throughput more than the choice of cloud provider does under
   controlled-resource mode.
2. **H2.** NVFP4 improves throughput and energy per task substantially with a
   measurable but bounded change in task-success rate relative to BF16.
3. **H3.** Time-to-first-token and inter-token latency are largely portable
   across providers at low concurrency; queueing behavior and tail latencies
   at higher concurrency are less portable.
4. **H4.** Provider-native economics differ more than controlled-resource
   performance: cost per successful task varies across providers mainly
   through price and billing model, not through GPU-bound performance.

Outcomes contradicting these hypotheses are reported as such; hypotheses are
never revised retroactively without a decision-log entry.
