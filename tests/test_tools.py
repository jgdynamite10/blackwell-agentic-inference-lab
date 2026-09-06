"""The six simulated tools: determinism, fixture binding, and validation."""

from __future__ import annotations

import pytest

from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.tools import (
    TOOL_LATENCY_MS,
    TOOL_SPECS,
    InvalidToolArgumentsError,
    InvalidToolNameError,
    SimulatedToolbox,
)

SCENARIO = catalog()["elevated-latency-001"]


@pytest.fixture()
def toolbox():
    return SimulatedToolbox(SCENARIO, log_limit=10, metric_window_s=900)


class TestRegistry:
    def test_all_six_tools_are_registered_with_latencies(self):
        expected = {
            "get_service_health",
            "query_metrics",
            "search_logs",
            "retrieve_runbook",
            "check_recent_changes",
            "recommend_remediation",
        }
        assert set(TOOL_SPECS) == expected
        assert set(TOOL_LATENCY_MS) == expected
        assert all(latency > 0 for latency in TOOL_LATENCY_MS.values())


class TestValidation:
    def test_unknown_tool_is_rejected(self, toolbox):
        with pytest.raises(InvalidToolNameError):
            toolbox.execute("reboot_datacenter", {})

    def test_missing_required_argument_is_rejected(self, toolbox):
        with pytest.raises(InvalidToolArgumentsError):
            toolbox.execute("query_metrics", {})

    def test_unexpected_argument_is_rejected(self, toolbox):
        with pytest.raises(InvalidToolArgumentsError):
            toolbox.execute("get_service_health", {"hostname": "x"})

    def test_wrong_argument_type_is_rejected(self, toolbox):
        with pytest.raises(InvalidToolArgumentsError):
            toolbox.execute("search_logs", {"query": 42})

    def test_non_dict_arguments_are_rejected(self, toolbox):
        with pytest.raises(InvalidToolArgumentsError):
            toolbox.execute("search_logs", ["query"])


class TestToolBehavior:
    def test_responses_are_deterministic_functions_of_scenario_and_query(self, toolbox):
        for name, arguments in [
            ("get_service_health", {"service": "zephyr-cart"}),
            ("query_metrics", {"metric": "latency_p99_ms"}),
            ("search_logs", {"query": "audit"}),
            ("retrieve_runbook", {"key": "zephyr-cart"}),
            ("check_recent_changes", {}),
        ]:
            first = toolbox.execute(name, arguments)
            second = toolbox.execute(name, arguments)
            assert first == second, name
            assert first.simulated_latency_ms == TOOL_LATENCY_MS[name]

    def test_get_service_health_filters_by_service(self, toolbox):
        everything = toolbox.execute("get_service_health", {})
        assert set(everything.payload["services"]) == set(SCENARIO.health)
        one = toolbox.execute("get_service_health", {"service": "zephyr-cart"})
        assert set(one.payload["services"]) == {"zephyr-cart"}
        unknown = toolbox.execute("get_service_health", {"service": "no-such-svc"})
        assert unknown.payload["services"]["no-such-svc"] == {"status": "unknown-service"}

    def test_query_metrics_applies_profile_window(self):
        compact = SimulatedToolbox(SCENARIO, metric_window_s=900)
        verbose = SimulatedToolbox(SCENARIO, metric_window_s=3600)
        compact_points = compact.execute("query_metrics", {"metric": "latency_p99_ms"})
        verbose_points = verbose.execute("query_metrics", {"metric": "latency_p99_ms"})
        assert len(compact_points.payload["points"]) < len(verbose_points.payload["points"])

    def test_query_metrics_reports_unknown_metric(self, toolbox):
        result = toolbox.execute("query_metrics", {"metric": "no_such_metric"})
        assert result.payload["found"] is False
        assert "latency_p99_ms" in result.payload["available"]

    def test_search_logs_matches_case_insensitively_and_limits(self):
        compact = SimulatedToolbox(SCENARIO, log_limit=1)
        result = compact.execute("search_logs", {"query": "AUDIT"})
        assert result.payload["total_matches"] >= 2
        assert len(result.payload["lines"]) == 1
        explicit = compact.execute("search_logs", {"query": "AUDIT", "limit": 5})
        assert len(explicit.payload["lines"]) <= 5

    def test_profile_log_limits_surface_different_context_sizes(self):
        interactive = SimulatedToolbox(SCENARIO, log_limit=10)
        batch = SimulatedToolbox(SCENARIO, log_limit=50)
        query = {"query": "request completed"}
        assert len(batch.execute("search_logs", query).payload["lines"]) > len(
            interactive.execute("search_logs", query).payload["lines"]
        )

    def test_retrieve_runbook_hit_and_miss(self, toolbox):
        hit = toolbox.execute("retrieve_runbook", {"key": "zephyr-cart"})
        assert hit.payload["found"] is True
        assert "remediation_ids" in hit.payload["runbook"]
        miss = toolbox.execute("retrieve_runbook", {"key": "nonexistent"})
        assert miss.payload["found"] is False
        assert miss.payload["available"] == ["zephyr-cart"]

    def test_check_recent_changes_window_filter(self, toolbox):
        all_changes = toolbox.execute("check_recent_changes", {})
        assert len(all_changes.payload["changes"]) == 2
        recent = toolbox.execute("check_recent_changes", {"window_s": 3600})
        assert [c["change_id"] for c in recent.payload["changes"]] == ["cfg-2041"]

    def test_recommend_remediation_echoes_submission(self, toolbox):
        result = toolbox.execute(
            "recommend_remediation",
            {"root_cause": "cause text", "remediation_id": "some-action"},
        )
        assert result.payload == {
            "acknowledged": True,
            "root_cause": "cause text",
            "remediation_id": "some-action",
        }
