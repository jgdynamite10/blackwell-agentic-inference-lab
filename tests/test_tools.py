"""The six simulated tools: determinism, fixture binding, validation, and
clock-consumed latencies."""

from __future__ import annotations

import pytest
from fakes import FakeClock

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
def clock():
    return FakeClock()


@pytest.fixture()
def toolbox(clock):
    return SimulatedToolbox(SCENARIO, log_limit=10, metric_window_s=900, clock=clock)


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

    def test_reference_path_fixed_tool_total_is_85_ms(self):
        """5 + 20 + 30 + 15 + 10 + 5 = 85 ms (documented fixed total)."""
        assert sum(TOOL_LATENCY_MS.values()) == 85.0

    def test_terminal_tool_takes_structured_arguments(self):
        spec = TOOL_SPECS["recommend_remediation"]
        assert set(spec["required"]) == {"diagnosis_id", "rationale", "remediation_id"}


class TestLatencyConsumption:
    """Simulated tool latency is consumed through the clock, so it occupies
    task duration, timeout budget, and wall time — not merely recorded."""

    def test_each_tool_advances_the_clock_by_its_documented_latency(self, clock, toolbox):
        for name, arguments in [
            ("get_service_health", {}),
            ("query_metrics", {"metric": "latency_p99_ms"}),
            ("search_logs", {"query": "audit"}),
            ("retrieve_runbook", {"key": "zephyr-cart"}),
            ("check_recent_changes", {}),
        ]:
            before = clock.monotonic()
            result = toolbox.execute(name, arguments)
            elapsed_ms = (clock.monotonic() - before) * 1000.0
            assert elapsed_ms == pytest.approx(TOOL_LATENCY_MS[name])
            assert result.simulated_latency_ms == TOOL_LATENCY_MS[name]


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

    def test_empty_required_string_is_rejected(self, toolbox):
        with pytest.raises(InvalidToolArgumentsError):
            toolbox.execute("search_logs", {"query": "   "})
        with pytest.raises(InvalidToolArgumentsError):
            toolbox.execute(
                "recommend_remediation",
                {"diagnosis_id": "", "rationale": "r", "remediation_id": "x"},
            )

    def test_boolean_where_integer_required_is_rejected(self, toolbox):
        with pytest.raises(InvalidToolArgumentsError):
            toolbox.execute("search_logs", {"query": "audit", "limit": True})
        with pytest.raises(InvalidToolArgumentsError):
            toolbox.execute("check_recent_changes", {"window_s": False})


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

    def test_get_service_health_filters_by_service(self, toolbox):
        everything = toolbox.execute("get_service_health", {})
        assert set(everything.payload["services"]) == set(SCENARIO.health)
        one = toolbox.execute("get_service_health", {"service": "zephyr-cart"})
        assert set(one.payload["services"]) == {"zephyr-cart"}
        unknown = toolbox.execute("get_service_health", {"service": "no-such-svc"})
        assert unknown.payload["services"]["no-such-svc"] == {"status": "unknown-service"}

    def test_query_metrics_applies_profile_window(self, clock):
        compact = SimulatedToolbox(SCENARIO, metric_window_s=900, clock=clock)
        verbose = SimulatedToolbox(SCENARIO, metric_window_s=3600, clock=clock)
        compact_points = compact.execute("query_metrics", {"metric": "latency_p99_ms"})
        verbose_points = verbose.execute("query_metrics", {"metric": "latency_p99_ms"})
        assert len(compact_points.payload["points"]) < len(verbose_points.payload["points"])

    def test_query_metrics_reports_unknown_metric(self, toolbox):
        result = toolbox.execute("query_metrics", {"metric": "no_such_metric"})
        assert result.payload["found"] is False
        assert "latency_p99_ms" in result.payload["available"]

    def test_search_logs_matches_case_insensitively_and_limits(self, clock):
        compact = SimulatedToolbox(SCENARIO, log_limit=1, clock=clock)
        result = compact.execute("search_logs", {"query": "AUDIT"})
        assert result.payload["total_matches"] >= 2
        assert len(result.payload["lines"]) == 1
        explicit = compact.execute("search_logs", {"query": "AUDIT", "limit": 5})
        assert len(explicit.payload["lines"]) <= 5

    def test_profile_log_limits_surface_different_context_sizes(self, clock):
        interactive = SimulatedToolbox(SCENARIO, log_limit=10, clock=clock)
        batch = SimulatedToolbox(SCENARIO, log_limit=50, clock=clock)
        query = {"query": "request completed"}
        assert len(batch.execute("search_logs", query).payload["lines"]) > len(
            interactive.execute("search_logs", query).payload["lines"]
        )

    def test_retrieve_runbook_hit_publishes_diagnosis_candidates(self, toolbox):
        """Candidate diagnosis ids are available through tool evidence, so the
        model is never asked to guess a hidden string."""
        hit = toolbox.execute("retrieve_runbook", {"key": "zephyr-cart"})
        assert hit.payload["found"] is True
        assert "remediation_ids" in hit.payload["runbook"]
        assert hit.payload["diagnosis_candidates"] == list(SCENARIO.candidate_diagnoses)
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
            {
                "diagnosis_id": "some-diagnosis",
                "rationale": "because of the evidence",
                "remediation_id": "some-action",
            },
        )
        assert result.payload == {
            "acknowledged": True,
            "diagnosis_id": "some-diagnosis",
            "rationale": "because of the evidence",
            "remediation_id": "some-action",
        }
