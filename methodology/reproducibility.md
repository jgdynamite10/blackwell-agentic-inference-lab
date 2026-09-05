# Reproducibility

The lab's claim to credibility is that a third party with their own cloud
accounts can rerun the study and check the findings. This document defines the
pinning and provenance rules that make that possible.

## Pinning rules

1. **Model artifact.** Exact repository revision and per-file hashes recorded
   in the manifest. The artifact hash frozen in Phase 3 is reused verbatim in
   Phases 5–6.
2. **Containers.** All serving and benchmark containers referenced by
   **digest** (`repo@sha256:…`), never by mutable tag. Digests appear in
   manifests and in the frozen-baseline definition.
3. **Serving engine.** Exact version (and commit for non-release builds), plus
   the complete launch configuration: flags, environment variables, backend
   selections (e.g. MoE backend), and parallelism settings.
4. **Host software.** NVIDIA driver version, CUDA toolkit version, OS
   image/version, container runtime version.
5. **Hardware and placement.** Instance type, region (zone where applicable),
   CPU model, vCPU count, memory, storage type. Instance *identifiers* are
   private and never committed (results-privacy policy).
6. **Workload.** Workload version (Git commit of the scenario fixtures),
   profile, concurrency, generation settings, seeds where supported.
7. **This repository.** The benchmark code commit is part of every manifest.

## Run manifests

Every run writes a manifest conforming to
[../schemas/run-manifest.schema.json](../schemas/run-manifest.schema.json)
**before** measurement begins, and finalizes timing-condition fields at run
end. A run without a complete manifest is invalid. Manifests contain no
secrets, no account identifiers, and no instance identifiers — they are
designed to be publishable after review.

## Environment capture

At instance setup, a capture script records (privately, with the run data):
`nvidia-smi -q`, driver/CUDA versions, `lscpu`, `free`, block-device and
filesystem layout, container runtime info, and installed-package manifests of
the containers. Sanitized summaries feed the phase reports.

## Frozen baseline

Phase 3 produces a `baseline.lock` definition (artifact hash, container
digests, serving config, generation parameters, workload version, SLO
targets). Phases 5–6 must reproduce it byte-for-byte in configuration; any
forced deviation (e.g. a provider image lacking the pinned driver) is a
documented environmental difference in the manifest and the phase report, not
a silent substitution.

## Statistical reporting

- Five measured repetitions per cell; report median with min–max range and
  the full per-repetition values (privately; sanitized on publication).
- Latency distributions reported at p50/p90/p95/p99 plus mean.
- No selective omission: every valid repetition is reported; invalidation
  rules are in the measurement contract §7.

## Revision control

Methodology, schemas, and workload changes require a
[decision-log](../docs/decision-log.md) entry stating what changed, why, and
which results predate the change. Results collected under an older methodology
version are labeled with that version and are not silently recomputed.
