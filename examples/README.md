# Synthetic Examples

**Every file in this directory is synthetic.** The values are fabricated
solely to document the run-manifest and benchmark-result formats defined in
[`schemas/`](../schemas/). They are not genuine measurements and must never be
interpreted as performance data.

Genuine results are private until published through the process in
[docs/publication-governance.md](../docs/publication-governance.md) and are
never committed to this repository
([docs/results-privacy.md](../docs/results-privacy.md)).

Both example files set `"is_synthetic_example": true` and are validated
against the schemas by the test suite (`tests/test_schemas.py`).
