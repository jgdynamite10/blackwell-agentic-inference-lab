"""Percentile rules: nearest-rank method and explicit tail suppression."""

from __future__ import annotations

import pytest

from blackwell_lab.workload.stats import (
    MIN_SAMPLES_P95,
    MIN_SAMPLES_P99,
    percentile_nearest_rank,
    summarize_latencies,
)


class TestNearestRank:
    def test_documented_minimums(self):
        assert MIN_SAMPLES_P95 == 200
        assert MIN_SAMPLES_P99 == 1000

    def test_nearest_rank_uses_only_observed_values(self):
        values = [float(v) for v in range(1, 11)]  # 1..10
        assert percentile_nearest_rank(values, 0.50) == 5.0
        assert percentile_nearest_rank(values, 0.90) == 9.0
        assert percentile_nearest_rank(values, 0.95) == 10.0
        assert percentile_nearest_rank(values, 1.00) == 10.0

    def test_single_observation(self):
        assert percentile_nearest_rank([7.0], 0.99) == 7.0

    def test_empty_sample_is_an_error(self):
        with pytest.raises(ValueError):
            percentile_nearest_rank([], 0.5)

    def test_q_bounds(self):
        with pytest.raises(ValueError):
            percentile_nearest_rank([1.0], 0.0)
        with pytest.raises(ValueError):
            percentile_nearest_rank([1.0], 1.5)


class TestSuppression:
    def test_empty_series_reports_all_null_with_count_zero(self):
        summary = summarize_latencies([])
        assert summary["count"] == 0
        for key in ("mean", "p50", "p90", "p95", "p99", "min", "max"):
            assert summary[key] is None

    def test_p95_suppressed_below_200(self):
        summary = summarize_latencies([float(v) for v in range(MIN_SAMPLES_P95 - 1)])
        assert summary["count"] == 199
        assert summary["p95"] is None
        assert summary["p99"] is None
        assert summary["p50"] is not None and summary["p90"] is not None

    def test_p95_reported_at_exactly_200(self):
        summary = summarize_latencies([float(v) for v in range(MIN_SAMPLES_P95)])
        assert summary["p95"] is not None
        assert summary["p99"] is None  # still below the p99 minimum

    def test_p99_suppressed_below_1000_and_reported_at_1000(self):
        below = summarize_latencies([float(v) for v in range(MIN_SAMPLES_P99 - 1)])
        assert below["p99"] is None
        assert below["p95"] is not None
        at_minimum = summarize_latencies([float(v) for v in range(MIN_SAMPLES_P99)])
        assert at_minimum["p99"] is not None

    def test_summary_is_deterministic_and_order_insensitive(self):
        values = [float((v * 37) % 1009) for v in range(1200)]
        assert summarize_latencies(values) == summarize_latencies(sorted(values, reverse=True))

    def test_summary_values_are_consistent(self):
        values = [float(v) for v in range(1, 1001)]
        summary = summarize_latencies(values)
        assert summary["min"] == 1.0
        assert summary["max"] == 1000.0
        assert summary["mean"] == pytest.approx(500.5)
        assert summary["p50"] == 500.0
        assert summary["p95"] == 950.0
        assert summary["p99"] == 990.0
