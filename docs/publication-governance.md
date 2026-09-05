# Publication Governance

This repository is public. Genuine benchmark results are private until they
pass the release process below. This document governs *how* results may ever
become public; [results-privacy.md](results-privacy.md) governs how they are
kept private until then.

## Release principles

1. **Deliberate release only.** Genuine results reach the public repository
   only through a dedicated, separately reviewed pull request containing only
   approved and sanitized data. Results never ride along in unrelated changes.
2. **Owner approval is mandatory.** No result, summary statistic, chart, or
   prose characterization of genuine measurements is published without the
   project owner's explicit written approval on the release pull request.
3. **Organizational approval precedes publication.** If organizational
   approval for comparative publication (see
   [feasibility-report.md](feasibility-report.md), section 10) has not been
   recorded in [decision-log.md](decision-log.md), no release PR may be opened.

## Release process

1. **Validation.** The candidate results are complete per the measurement
   contract (all repetitions, manifests, and integrity checks present) and
   anomalies are investigated and documented.
2. **Review.** Methodology conformance is checked against
   [../methodology/measurement-contract.md](../methodology/measurement-contract.md);
   any methodology revisions that occurred after data collection are
   cross-checked against [decision-log.md](decision-log.md).
3. **Sanitization.** A publication script (operating on sanitized input only)
   produces the public artifact. Sanitization removes: cloud account
   identifiers, internal hostnames and IP addresses, instance identifiers,
   credentials or tokens of any kind, and any log fragments containing
   sensitive infrastructure metadata. Region names, instance *types*, list
   prices, and hardware/software versions are publishable.
4. **Limitations statement.** Every release includes its limitations: sample
   sizes, environmental differences recorded, and the boundary of what the
   evidence supports. Claims must not exceed the evidence, and
   controlled-resource results must never be mixed with provider-native
   results ([../AGENTS.md](../AGENTS.md), section 4).
5. **Release PR.** A dedicated pull request titled `Release: <scope>` contains
   only the sanitized artifacts and their documentation. The owner reviews and
   merges (squash-only).

## What may be public before any release

- Result **schemas** and documentation of the result format.
- **Synthetic** example results, clearly labeled as examples.
- Empty result-directory placeholders (`results/README.md`).
- Publication scripts that operate on sanitized input.

## What may never be public

- Raw, normalized, or intermediate genuine results that have not passed this
  process.
- Anything on the prohibited list in [../AGENTS.md](../AGENTS.md), section 6,
  regardless of approval status.
