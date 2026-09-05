# Results

This directory is an intentional placeholder. **It contains no genuine
benchmark results, and it never will until results are explicitly approved for
publication.**

## Why is this directory empty?

The repository is public, but all genuine benchmark results — raw, normalized,
intermediate, or unpublished — remain private until they have been validated,
reviewed, sanitized, and intentionally released through the process described
in [docs/publication-governance.md](../docs/publication-governance.md).

Benchmark tooling writes genuine results to a private location **outside**
this repository, configured via the `LAB_RESULTS_DIR` environment variable
(see `.env.example`). The runner refuses to start if that location resolves
inside the repository. `.gitignore` additionally excludes this directory's
contents, but the design — not `.gitignore` — is the real boundary.

## What will eventually appear here

Only deliberately released artifacts, merged through a dedicated, separately
reviewed pull request containing exclusively approved and sanitized data:

- Sanitized, validated result sets accompanying the Phase 7 technical report.
- Their run manifests (which are designed to be publishable: no account
  identifiers, no instance identifiers, no secrets).

## Where to look meanwhile

- Result **format**: [schemas/](../schemas/) and the synthetic examples in
  [examples/](../examples/).
- Measurement methodology:
  [methodology/measurement-contract.md](../methodology/measurement-contract.md).
