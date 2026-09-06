# Publication Governance

This project distinguishes **two publication classes** with entirely different
rules. Making the repository public covers class A only; it does **not**
authorize class B.

## Class A — Repository publication (public)

- Source code, schemas, synthetic examples, documentation, and methodology in
  this repository are public under the **Apache License 2.0** (see
  [../LICENSE](../LICENSE) and [../NOTICE](../NOTICE)), per owner decision
  D-0007 in [decision-log.md](decision-log.md).
- Normal repository changes continue through reviewed pull requests and CI —
  no special release process applies to ordinary code and documentation.
- The canonical public repository is
  `jgdynamite10/blackwell-agentic-inference-lab`; the personal
  `jgdynamite/blackwell-agentic-inference-lab` repository is a
  non-synchronized redirect that will be archived.

## Class B — Benchmark-result publication (restricted)

- **Genuine raw and processed results remain external and private**, in the
  external `LAB_RESULTS_DIR` location
  ([results-privacy.md](results-privacy.md)). Public repository visibility
  changes nothing about this.
- **Raw provider bills and provider-account information remain private.**
- **No automatic result-publication process exists.** Every benchmark-result
  release requires separate, explicit owner approval, and that approval must
  identify the **exact files and scope** approved.

## Release principles (class B)

1. **Deliberate release only.** No genuine result, sanitized summary, chart,
   comparison, conclusion, or publication artifact may be committed to this
   repository without the owner's explicit approval, via a dedicated,
   separately reviewed pull request containing only approved and sanitized
   data.
2. **Owner approval is mandatory at every step.** No result, summary
   statistic, chart, or prose characterization of genuine measurements is
   shared anywhere without the project owner's explicit written approval
   identifying the exact files and scope approved.
3. **Claims must not exceed the evidence** — in particular, no
   provider-superiority claim may exceed what the released data supports.

## Release process (class B)

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
   only the sanitized artifacts and their documentation, matching exactly the
   files and scope named in the owner's approval. The owner reviews and merges
   (squash-only).

## What the repository may contain before any result release

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
