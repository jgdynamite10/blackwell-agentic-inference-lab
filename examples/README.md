# Synthetic Examples

**Every file in this directory is synthetic.** The values are fabricated
solely to document the run-manifest and benchmark-result formats defined in
[`schemas/`](../schemas/). They are not genuine measurements and must never be
interpreted as performance data.

Genuine results live outside the Git working tree entirely and are never
committed to this repository
([docs/results-privacy.md](../docs/results-privacy.md)). Any release of
genuine results would require the explicit owner approval process in
[docs/publication-governance.md](../docs/publication-governance.md).

Both example files set `"is_synthetic_example": true` and are validated
against the schemas by the test suite (`tests/test_schemas.py`).
