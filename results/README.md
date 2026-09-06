# Results

This directory is an intentional placeholder. **It contains no genuine
benchmark results, and none may be added without the project owner's explicit
approval after validation and review.**

## Why is this directory empty?

All genuine benchmark results — raw, normalized, intermediate, or processed —
live **outside the Git working tree entirely** and remain private. Although
this repository's source and methodology are public, public visibility does
**not** authorize publication of genuine benchmark data. Any result release
happens only after validation, review, sanitization, and the owner's explicit
approval identifying the exact files and scope, through the process described
in [docs/publication-governance.md](../docs/publication-governance.md).

Benchmark tooling writes genuine results to an external location configured
via the `LAB_RESULTS_DIR` environment variable (see `.env.example`). The
runner fails closed if that variable is unset for a real run and refuses to
start if the location resolves inside the repository (including via symlinks).
`.gitignore` additionally excludes this directory's contents, but it is a
defense-in-depth control only — the design, not `.gitignore`, is the boundary.

## What could eventually appear here

Only owner-approved artifacts, merged through a dedicated, separately reviewed
pull request containing exclusively approved and sanitized data:

- Sanitized, validated result sets accompanying the Phase 7 technical report.
- Their run manifests (which are designed to be sanitization-friendly: no
  account identifiers, no instance identifiers, no secrets).

## Where to look meanwhile

- Result **format**: [schemas/](../schemas/) and the synthetic examples in
  [examples/](../examples/).
- Measurement methodology:
  [methodology/measurement-contract.md](../methodology/measurement-contract.md).
