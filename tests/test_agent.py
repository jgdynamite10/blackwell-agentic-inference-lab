"""Multi-turn agent loop: happy path, error taxonomy, retries=0, timeouts."""

from __future__ import annotations

import pytest

from blackwell_lab.workload import agent as agent_module
from blackwell_lab.workload.agent import DEFAULT_MAX_TURNS, ERROR_TAXONOMY, RETRIES, run_task
from blackwell_lab.workload.model_client import DeterministicMockClient, GenerationSettings
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.tools import SimulatedToolbox

SETTINGS = GenerationSettings()
SCENARIO = catalog()["pod-failures-001"]


def execute(behavior: str = "correct", *, timeout_s: float = 30.0, **client_kwargs):
    client = DeterministicMockClient(behavior=behavior, **client_kwargs)
    toolbox = SimulatedToolbox(SCENARIO)
    return run_task(SCENARIO, client, toolbox, SETTINGS, timeout_s=timeout_s)


class TestPolicyConstants:
    def test_measurement_retry_policy_is_zero(self):
        assert RETRIES == 0

    def test_error_taxonomy_is_documented(self):
        assert set(ERROR_TAXONOMY) == {
            "endpoint_error",
            "malformed_tool_call",
            "invalid_tool_name",
            "invalid_tool_arguments",
            "no_terminal_recommendation",
            "task_timeout",
        }
        assert agent_module.__doc__ is not None
        for category in ERROR_TAXONOMY:
            assert category in agent_module.__doc__


class TestHappyPath:
    def test_task_completes_with_terminal_recommendation(self):
        execution = execute()
        assert execution.status == "completed"
        assert execution.error_category is None
        assert execution.remediation_id in SCENARIO.accepted_remediations
        assert execution.root_cause == SCENARIO.root_cause_summary
        assert execution.tools_used[-1] == "recommend_remediation"
        # Every reference evidence step happened, in order, before the terminal.
        expected = [step["tool"] for step in SCENARIO.reference_tool_sequence]
        assert execution.tools_used[:-1] == expected

    def test_timings_are_recorded(self):
        execution = execute()
        assert execution.e2e_ms > 0
        assert execution.started_at_utc.endswith("+00:00")
        assert execution.ended_at_utc >= execution.started_at_utc
        assert len(execution.turn_metrics) == len(execution.tools_used)
        for turn in execution.turn_metrics:
            assert turn.output_tokens > 1
            assert turn.serving_time_ms >= turn.ttft_ms >= 0
            assert len(turn.inter_token_gaps_ms) == turn.output_tokens - 1
        assert len(execution.tool_latencies_ms) == len(execution.tools_used)


class TestErrorTaxonomy:
    """retries=0: each failure mode fails the task visibly, once."""

    @pytest.mark.parametrize(
        ("behavior", "category"),
        [
            ("endpoint_error", "endpoint_error"),
            ("malformed_tool_call", "malformed_tool_call"),
            ("unknown_tool", "invalid_tool_name"),
            ("bad_arguments", "invalid_tool_arguments"),
            ("no_terminal", "no_terminal_recommendation"),
        ],
    )
    def test_failure_modes_map_to_categories(self, behavior, category):
        execution = execute(behavior)
        assert execution.status == "error"
        assert execution.error_category == category
        assert execution.remediation_id is None

    def test_no_terminal_stops_at_max_turns(self):
        execution = execute("no_terminal")
        assert len(execution.tools_used) == DEFAULT_MAX_TURNS

    def test_timeout_is_reported_as_timeout(self):
        execution = execute("slow", timeout_s=0.05, response_delay_s=0.2)
        assert execution.status == "timeout"
        assert execution.error_category == "task_timeout"

    def test_all_reported_categories_are_in_the_taxonomy(self):
        for behavior in (
            "endpoint_error",
            "malformed_tool_call",
            "unknown_tool",
            "bad_arguments",
            "no_terminal",
        ):
            execution = execute(behavior)
            assert execution.error_category in ERROR_TAXONOMY


class TestDeterministicReproduction:
    def test_two_runs_produce_identical_semantics(self):
        first = execute()
        second = execute()
        assert first.tools_used == second.tools_used
        assert first.root_cause == second.root_cause
        assert first.remediation_id == second.remediation_id
        assert first.status == second.status
        assert [t.output_tokens for t in first.turn_metrics] == [
            t.output_tokens for t in second.turn_metrics
        ]
