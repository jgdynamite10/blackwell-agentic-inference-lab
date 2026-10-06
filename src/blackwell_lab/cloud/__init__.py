"""Phase 3A cloud-readiness tooling (Akamai baseline preparation).

Everything in this package is designed to run in two strictly separated
contexts (AGENTS.md, section 3):

- **Cloud-independent** (remote/hosted Cloud Agent, CI, local dev): readiness
  validation, mocked lifecycle/preflight/endpoint/telemetry tests, plan
  rendering, teardown-plan generation from a recorded ledger. No credentials,
  no provider calls, no billable operations.
- **Owner-controlled local execution** (the owner directly or a Cursor Agent
  running on the owner's machine): authenticated read-only preflights,
  terraform plan/apply/destroy (apply and destroy each require a separate
  explicit approval phrase), the owner-approved pilot, and genuine benchmark
  execution with ``RunMode.REAL`` and an external ``LAB_RESULTS_DIR``.
"""
