# Synthetic Examples

**Every file in this directory is synthetic.** The values are fabricated
solely to document the run-manifest, benchmark-result, and engine-contract
formats defined in [`schemas/`](../schemas/). They are not genuine
measurements and must never be interpreted as performance data.

Genuine results live outside the Git working tree entirely and are never
committed to this repository
([docs/results-privacy.md](../docs/results-privacy.md)). Any release of
genuine results would require the explicit owner approval process in
[docs/publication-governance.md](../docs/publication-governance.md).

The run-manifest and benchmark-result examples set
`"is_synthetic_example": true`. The engine-contract example is a
synthetic declaration only (no launch, no credentials) and is validated
against [`schemas/engine-contract.schema.json`](../schemas/engine-contract.schema.json).
All examples are validated by the test suite (`tests/test_schemas.py`).
