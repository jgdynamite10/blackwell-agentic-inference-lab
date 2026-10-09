# D-0034 holdout exposure

This note records what was already exposed before any W5/W6 run. It is not
a claim that the remaining holdout templates are unseen.

- Pinned implementation base: `8888927f2f5e853ea792ca947eaad12f7c017880`.
  D-0034 is local on branch `d0034-structured-evidence` and is not merged.
- Evidence contract revision: public evidence 1.0.0, normalization policy
  1.0.0, `workflow-controller-v3`. Digests are the D-0034 entry in
  `docs/decision-log.md`.
- Review reports already exposed `pod-failures-001` and
  `rate-limiting-001`, including candidate and accepted-diagnosis
  associations, distinguishing log content, and distractor acceptance.
  Those reports mentioned a missing environment variable versus an
  upstream-ledger outage, and rate-limit configuration versus client
  abuse. This revision does not repeat those lines and does not use them
  to tune a rule or a claim.
- During design review the catalog was read for development public log
  and health fields. Source inspection also included previously exposed
  holdout material. An absolute claim that no answer-bearing material was
  visible is not supportable.
- Annotation inputs for the development overlay were public candidate
  ids, raw logs, and returned health. The new matcher modules do not read
  `accepted_diagnoses`, evaluator predicates, or holdout records.
- Policy and claims were not revised from holdout answers or from the
  3/10 canary score. The 0.40 and 0.50 floors are unchanged.
- `validation_scope=public-catalog-pre-exposed-holdout`.
  `blind_generalization_evidence=false`.
- Holdout and freeze execution for W5/W6 is refused until a separate
  answer-withheld annotation exists. That refusal is not a schedule change.
  A future unseen claim would need a separately authorized fresh evaluation
  set. Creating one is outside this revision.
