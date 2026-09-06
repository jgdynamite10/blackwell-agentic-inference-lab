# Publication Governance

**This repository is private and must remain private. Nothing in it — code,
documentation, methodology, or results — is published, and no publication is
scheduled or automatic.** Licensing and publication rights are under review;
no license is granted at this time.

This document governs the process that would apply **if** the project owner
ever explicitly authorizes a release of any material.
[results-privacy.md](results-privacy.md) governs how genuine results are kept
outside the repository in the meantime.

Note that a private Git branch or pull request is **not** an approved
publication process: merging material into this private repository does not
authorize its release anywhere.

## Release principles

1. **Deliberate release only.** No result, sanitized summary, chart,
   comparison, conclusion, or publication artifact may even be *committed to
   this private repository* without the owner's explicit approval, via a
   dedicated, separately reviewed pull request containing only approved and
   sanitized data. Any release *outside* the repository requires a further,
   separate, explicit owner authorization.
2. **Owner approval is mandatory at every step.** No result, summary
   statistic, chart, or prose characterization of genuine measurements is
   shared anywhere without the project owner's explicit written approval.
3. **Organizational and legal review precede any release.** If organizational
   approval for comparative publication (see
   [feasibility-report.md](feasibility-report.md), section 10) and the
   licensing/IP review have not been recorded in
   [decision-log.md](decision-log.md), no release process may begin.

## Release process

1. **Validation.** The candidate results are complete per the measurement
   contract (all repetitions, manifests, and integrity checks present) and
   anomalies are investigated and documented.
2. **Review.** Methodology conformance is checked against
   [../methodology/measurement-contract.md](../methodology/measurement-contract.md);
   any methodology revisions that occurred after data collection are
   cross-checked against [decision-log.md](decision-log.md).
3. **Sanitization (external, two-directory flow).** A **locally executed**
   sanitizer reads raw inputs **only from the external `LAB_RESULTS_DIR`**
   and writes the sanitized candidate to a **separate external staging
   directory** — raw data never enters the repository at any point in this
   flow. Sanitization removes: cloud account identifiers, project/tenant
   identifiers, internal hostnames and IP addresses, instance identifiers,
   private endpoints, signed URLs, credentials or tokens of any kind, and any
   log fragments containing sensitive infrastructure metadata. Raw provider
   bills and account-level cost records always remain external. Region names,
   instance *types*, list prices, and hardware/software versions are
   releasable if the owner approves.
4. **Staged review before Git.** The sanitized candidate in the external
   staging directory may enter Git **only after the owner's explicit review
   and approval** of that candidate. Nothing moves from `LAB_RESULTS_DIR` or
   the staging directory into the repository automatically.
5. **Limitations statement.** Every release includes its limitations: sample
   sizes, environmental differences recorded, and the boundary of what the
   evidence supports. Claims must not exceed the evidence, and
   controlled-resource results must never be mixed with provider-native
   results ([../AGENTS.md](../AGENTS.md), section 6).
6. **Release PR.** A dedicated pull request titled `Release: <scope>` contains
   only the sanitized artifacts and their documentation. The owner reviews and
   merges (squash-only). Merging still does not authorize distribution outside
   this private repository; that requires a separate explicit authorization.

## What the repository may contain before any release

- Result **schemas** and documentation of the result format.
- **Synthetic** example results, clearly labeled as examples.
- Test fixtures containing no real provider or account data.
- Empty result-directory placeholders (`results/README.md`).
- Redaction/sanitization scripts that operate on sanitized input.
- Methodology, documentation, and code.

## What the repository may never contain

- Raw, normalized, or intermediate genuine results that have not passed this
  process — genuine results live outside the Git working tree entirely.
- Anything on the prohibited list in [../AGENTS.md](../AGENTS.md), section 8,
  regardless of approval status.
