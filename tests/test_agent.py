"""Multi-turn agent loop: submission-based timing, consumed tool delays,
error taxonomy, retries=0, deadlines, and tool traces."""

from __future__ import annotations

import time

import pytest
from fakes import FakeClock

from blackwell_lab.workload import agent as agent_module
from blackwell_lab.workload.agent import (
    DEFAULT_MAX_TURNS,
    ERROR_TAXONOMY,
    RETRIES,
    run_task,
)
from blackwell_lab.workload.model_client import DeterministicMockClient, GenerationSettings
from blackwell_lab.workload.sampling import generate_task_instances
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.tools import TOOL_LATENCY_MS, SimulatedToolbox

SETTINGS = GenerationSettings()
SCENARIO = catalog()["pod-failures-001"]

#: Fixed simulated tool total of the pod-failures reference path + terminal:
#: health 5 + logs 30 + changes 15 + runbook 10 + terminal 5 = 65 ms.
POD_FAILURES_TOOL_TOTAL_MS = 65.0


def execute(
    behavior: str = "correct",
    *,
    timeout_s: float = 30.0,
    clock=None,
    scenario=SCENARIO,
    submitted_at=None,
    **client_kwargs,
):
    clock = clock if clock is not None else FakeClock()
    client = DeterministicMockClient(behavior=behavior, **client_kwargs)
    toolbox = SimulatedToolbox(scenario, clock=clock)
    return run_task(
        scenario,
        client,
        toolbox,
        SETTINGS,
        timeout_s=timeout_s,
        clock=clock,
        submitted_at=submitted_at,
    )


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
            "agent_runtime_error",
        }
        assert agent_module.__doc__ is not None
        for category in ERROR_TAXONOMY:
            assert category in agent_module.__doc__


class TestHappyPath:
    def test_task_completes_with_structured_recommendation(self):
        execution = execute()
        assert execution.status == "completed"
        assert execution.error_category is None
        assert execution.diagnosis_id in SCENARIO.accepted_diagnoses
        assert execution.remediation_id in SCENARIO.accepted_remediations
        assert execution.rationale == SCENARIO.root_cause_summary
        assert execution.tools_used[-1] == "recommend_remediation"
        # Every reference evidence step happened, in order, before the terminal.
        expected = [step["tool"] for step in SCENARIO.reference_tool_sequence]
        assert execution.tools_used[:-1] == expected

    def test_tool_trace_is_complete(self):
        """The trace records tool, validated arguments, result, and latency
        for every invocation (evaluator evidence input; audit requirement)."""
        execution = execute()
        assert len(execution.tool_trace) == len(execution.tools_used)
        for step, trace in zip(
            SCENARIO.reference_tool_sequence, execution.tool_trace, strict=False
        ):
            assert trace.tool == step["tool"]
            assert trace.arguments == step["arguments"]
            assert isinstance(trace.result, dict) and trace.result
            assert trace.simulated_latency_ms == TOOL_LATENCY_MS[trace.tool]

    def test_task_duration_includes_the_fixed_tool_total(self):
        """Simulated tool delays are consumed, not merely recorded: on a
        virtual clock where only tools advance time, e2e equals the fixed
        tool total of the executed path exactly."""
        execution = execute()
        assert sum(execution.tool_latencies_ms) == POD_FAILURES_TOOL_TOTAL_MS
        assert execution.e2e_ms == pytest.approx(POD_FAILURES_TOOL_TOTAL_MS)

    def test_reference_scenario_85ms_fixed_total(self):
        """The canonical six-tool reference path consumes exactly 85 ms."""
        scenario = catalog()["elevated-latency-001"]
        execution = execute(scenario=scenario)
        assert execution.status == "completed"
        assert sum(execution.tool_latencies_ms) == 85.0
        assert execution.e2e_ms == pytest.approx(85.0)

    def test_turn_records_use_typed_event_semantics(self):
        execution = execute()
        assert len(execution.turns) == len(execution.tools_used)
        for turn in execution.turns:
            assert turn.ttft_ms is not None and turn.ttft_ms >= 0
            assert turn.serving_time_ms >= turn.ttft_ms
            assert turn.chunk_count >= 1
            assert turn.content_chars > 0
            # The mock has no true token events and no authoritative usage:
            # ITL and token counts are unavailable with reasons, never chunk
            # counts in disguise.
            assert turn.itl_available is False
            assert turn.inter_token_gaps_ms == ()
            assert turn.itl_unavailable_reason
            assert turn.output_tokens is None
            assert turn.tokens_unavailable_reason

    def test_wall_clock_timestamps_are_recorded_for_correlation(self):
        execution = execute()
        assert execution.submitted_at_utc.endswith("+00:00")
        assert execution.ended_at_utc >= execution.started_at_utc


class TestSubmissionTiming:
    def test_timing_starts_at_driver_submission_not_dequeue(self):
        """Queue wait between submission and worker start is part of e2e."""
        clock = FakeClock()
        submitted = clock.monotonic()
        clock.advance(0.5)  # the task waited 500 ms for a scheduler slot
        execution = execute(clock=clock, submitted_at=submitted)
        assert execution.queue_wait_ms == pytest.approx(500.0)
        assert execution.e2e_ms == pytest.approx(500.0 + POD_FAILURES_TOOL_TOTAL_MS)

    def test_queue_wait_consumes_the_timeout_budget(self):
        clock = FakeClock()
        submitted = clock.monotonic()
        clock.advance(10.0)  # longer than the whole timeout
        execution = execute(clock=clock, submitted_at=submitted, timeout_s=5.0)
        assert execution.status == "timeout"
        assert execution.error_category == "task_timeout"


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
            ("runtime_crash", "agent_runtime_error"),
        ],
    )
    def test_failure_modes_map_to_categories(self, behavior, category):
        execution = execute(behavior)
        assert execution.status == "error"
        assert execution.error_category == category
        assert execution.remediation_id is None
        assert execution.error_category in ERROR_TAXONOMY

    def test_unexpected_exception_is_sanitized_and_contained(self):
        """A crash inside the client becomes agent_runtime_error — it never
        propagates (so one task cannot abort a repetition) and no exception
        text is retained anywhere in the record."""
        execution = execute("runtime_crash")
        assert execution.status == "error"
        assert execution.error_category == "agent_runtime_error"
        serialized = repr(execution.__dict__)
        assert "simulated unexpected client crash" not in serialized

    def test_no_terminal_stops_at_max_turns(self):
        execution = execute("no_terminal")
        assert len(execution.tools_used) == DEFAULT_MAX_TURNS


class TestTimeouts:
    def test_timeout_is_reported_as_timeout(self):
        execution = execute("slow", timeout_s=0.05, response_delay_s=0.2)
        assert execution.status == "timeout"
        assert execution.error_category == "task_timeout"

    def test_hard_timeout_returns_within_documented_tolerance(self):
        """With the real clock, a 50 ms deadline must return close to 50 ms
        (documented tolerance: 200 ms of scheduling slack), never after the
        client's arbitrary 5 s blocking delay."""
        clock_start = time.monotonic()
        client = DeterministicMockClient(behavior="slow", response_delay_s=5.0)
        toolbox = SimulatedToolbox(SCENARIO)
        execution = run_task(SCENARIO, client, toolbox, SETTINGS, timeout_s=0.05)
        elapsed_s = time.monotonic() - clock_start
        assert execution.status == "timeout"
        assert execution.error_category == "task_timeout"
        assert elapsed_s < 0.25

    def test_tool_delays_consume_the_timeout(self):
        """A timeout shorter than the consumed tool total must trip during
        evidence gathering even though the mock model itself is instant."""
        execution = execute(timeout_s=0.04)  # < 65 ms pod-failures tool total
        assert execution.status == "timeout"
        assert execution.error_category == "task_timeout"

    def test_reference_path_cannot_complete_under_an_83ms_timeout(self):
        """Regression (owner blocker 3): the 85 ms reference path fits its
        first five tools (80 ms) under an 83 ms deadline, but the 5 ms
        terminal tool crosses it. The deadline is enforced after EVERY tool,
        including the terminal one, so this is a task_timeout — never a
        completion."""
        scenario = catalog()["elevated-latency-001"]
        execution = execute(scenario=scenario, timeout_s=0.083)
        assert execution.status == "timeout"
        assert execution.error_category == "task_timeout"
        # The terminal tool ran (its latency is what crossed the deadline)
        # but its recommendation is never credited.
        assert execution.tools_used[-1] == "recommend_remediation"
        assert execution.e2e_ms == pytest.approx(85.0)
        assert execution.diagnosis_id is None
        assert execution.remediation_id is None

    def test_terminal_latency_reaching_the_deadline_exactly_is_a_timeout(self):
        """`reaches or crosses`: a deadline landing exactly on the terminal
        tool's completion instant is a timeout, not a completion. The exact
        instant is reproduced by replaying the clock's own float accumulation
        from a zero start (0.0 + timeout is always exact)."""
        scenario = catalog()["elevated-latency-001"]
        exact_deadline = 0.0
        for step in scenario.reference_tool_sequence:
            exact_deadline += TOOL_LATENCY_MS[step["tool"]] / 1000.0
        exact_deadline += TOOL_LATENCY_MS["recommend_remediation"] / 1000.0
        execution = execute(scenario=scenario, clock=FakeClock(start=0.0), timeout_s=exact_deadline)
        assert execution.status == "timeout"
        assert execution.error_category == "task_timeout"


class TestInstancePrompting:
    def test_instance_surface_variant_reaches_the_prompt(self):
        """Seeded instances inject their tracking id into the task prompt and
        their identifiers into the execution record."""
        instance = generate_task_instances([SCENARIO.scenario_id], 1, seed=7)[0]
        clock = FakeClock()
        client = DeterministicMockClient()
        toolbox = SimulatedToolbox(SCENARIO, clock=clock)
        execution = run_task(
            SCENARIO, client, toolbox, SETTINGS, timeout_s=30, clock=clock, instance=instance
        )
        assert execution.instance_id == instance.instance_id
        assert execution.instance_seed == instance.instance_seed
        assert execution.status == "completed"


class TestDeterministicReproduction:
    def test_two_runs_produce_identical_semantics(self):
        first = execute()
        second = execute()
        assert first.tools_used == second.tools_used
        assert first.diagnosis_id == second.diagnosis_id
        assert first.remediation_id == second.remediation_id
        assert first.status == second.status
        assert first.e2e_ms == second.e2e_ms
        assert [t.chunk_count for t in first.turns] == [t.chunk_count for t in second.turns]
