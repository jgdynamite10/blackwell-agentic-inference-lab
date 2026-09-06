"""Latency summaries with statistically defensible percentile suppression.

Sample-count rules (decision D-0009; measurement contract §3):

- **p95 requires at least 200 observations.** With the nearest-rank method,
  n = 200 puts 10 observations at or beyond the p95 rank, the conventional
  minimum for a tail estimate that is not dominated by a handful of samples.
- **p99 requires at least 1,000 observations.** Same rule: n = 1,000 puts 10
  observations at or beyond the p99 rank.

Below the applicable minimum the percentile is **suppressed explicitly**
(``None`` / JSON ``null``) rather than fabricated; the ``count`` field makes
the suppression machine-checkable (the result schema enforces it).

Percentiles are calculated **per repetition** over that repetition's own
observations. Cross-repetition (cell-level) pooling is a separate, clearly
labeled analysis step deferred to Phase 7; it is never mixed into
per-repetition records.

Method: nearest-rank on the sorted sample — ``value = sorted[ceil(q * n)] - 1``
(1-based rank) — which is deterministic and uses only observed values (no
interpolation, so a suppressed-worthy sample can never manufacture a value
between observations).
"""

from __future__ import annotations

import math

MIN_SAMPLES_P95 = 200
MIN_SAMPLES_P99 = 1000


def percentile_nearest_rank(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile (q in (0, 1]) of an ascending-sorted sample."""
    if not sorted_values:
        raise ValueError("cannot take a percentile of an empty sample")
    if not 0.0 < q <= 1.0:
        raise ValueError(f"q must be in (0, 1], got {q}")
    rank = max(1, math.ceil(q * len(sorted_values)))
    return sorted_values[rank - 1]


def summarize_latencies(values_ms: list[float]) -> dict:
    """Builds a JSON-ready latency summary with explicit tail suppression.

    Returns a dict matching the result schema's ``latency_summary``: for an
    empty sample every statistic is ``None`` (count 0 — nothing is fabricated);
    p95 is ``None`` below 200 observations and p99 is ``None`` below 1,000.
    """
    count = len(values_ms)
    if count == 0:
        return {
            "mean": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "min": None,
            "max": None,
            "count": 0,
        }
    ordered = sorted(values_ms)
    return {
        "mean": round(sum(ordered) / count, 3),
        "p50": round(percentile_nearest_rank(ordered, 0.50), 3),
        "p90": round(percentile_nearest_rank(ordered, 0.90), 3),
        "p95": (
            round(percentile_nearest_rank(ordered, 0.95), 3) if count >= MIN_SAMPLES_P95 else None
        ),
        "p99": (
            round(percentile_nearest_rank(ordered, 0.99), 3) if count >= MIN_SAMPLES_P99 else None
        ),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
        "count": count,
    }
