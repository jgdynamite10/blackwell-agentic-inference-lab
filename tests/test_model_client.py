"""Deterministic mock client: typed stream events, deadline honoring, and
behavior variants (including adversarial modes)."""

from __future__ import annotations

import json
import time

import pytest
from fakes import FakeClock

from blackwell_lab.workload.model_client import (
    MOCK_BEHAVIORS,
    TOOL_CALL_PREFIX,
    DeterministicMockClient,
    GenerationSettings,
    Message,
    ModelClient,
    ModelClientError,
    ModelClientTimeout,
    StreamEvent,
)
from blackwell_lab.workload.scenarios import catalog

SETTINGS = GenerationSettings()


def conversation(scenario_id: str = "elevated-latency-001") -> list[Message]:
    scenario = catalog()[scenario_id]
    return [
        Message("system", "You are a Cloud Operations Agent."),
        Message("user", f"scenario_id: {scenario_id}\n\n{scenario.description}"),
    ]


def turn_text(client: ModelClient, messages: list[Message]) -> str:
    return "".join(e.text for e in client.stream_turn(messages, SETTINGS))


def last_call(client: ModelClient, messages: list[Message]) -> dict:
    events = list(client.stream_turn(messages, SETTINGS))
    native = [event.tool_call for event in events if event.kind == "native_tool_call"]
    assert native and native[-1] is not None
    call = native[-1]
    return {"tool": call.name, "arguments": call.arguments}


def advance_past(client: ModelClient, messages: list[Message], sequence) -> list[Message]:
    for index, step in enumerate(sequence):
        messages.append(Message("assistant", "..."))
        messages.append(
            Message(
                "tool",
                json.dumps({"tool": step["tool"], "result": {}}),
                tool_call_id=f"mock-advance-{index}",
            )
        )
    return messages


class TestTypedStreamEvents:
    def test_events_are_typed_native_tool_calls(self):
        """The mock emits a native tool-call event: no true token events and
        no usage events, so nothing downstream can mistake its replay speed
        for model token throughput."""
        events = list(DeterministicMockClient().stream_turn(conversation(), SETTINGS))
        assert events, "a turn must stream at least one event"
        assert all(isinstance(e, StreamEvent) for e in events)
        assert [e.kind for e in events] == ["native_tool_call_delta", "native_tool_call"]
        assert events[0].tool_call is None
        assert events[0].text == ""
        assert events[1].tool_call is not None
        assert events[1].tool_call.call_id.startswith("mock-")

    def test_native_call_is_not_a_token_event(self):
        events = list(DeterministicMockClient().stream_turn(conversation(), SETTINGS))
        assert all(e.kind != "token" for e in events)
        assert all(e.output_tokens is None for e in events)


class TestDeterminism:
    def test_identical_conversations_produce_identical_event_streams(self):
        client = DeterministicMockClient()
        first = list(client.stream_turn(conversation(), SETTINGS))
        second = list(client.stream_turn(conversation(), SETTINGS))
        assert first == second
        assert [e.kind for e in first] == ["native_tool_call_delta", "native_tool_call"]

    def test_two_client_instances_agree(self):
        assert list(DeterministicMockClient().stream_turn(conversation(), SETTINGS)) == list(
            DeterministicMockClient().stream_turn(conversation(), SETTINGS)
        )


class TestTurnProtocol:
    def test_first_turn_replays_reference_sequence(self):
        scenario = catalog()["elevated-latency-001"]
        call = last_call(DeterministicMockClient(), conversation())
        assert call == scenario.reference_tool_sequence[0]

    def test_terminal_submits_structured_recommendation(self):
        scenario = catalog()["elevated-latency-001"]
        client = DeterministicMockClient()
        messages = conversation()
        for step in scenario.reference_tool_sequence:
            call = last_call(client, messages)
            assert call == step
            advance_past(client, messages, [step])
        terminal = last_call(client, messages)
        assert terminal["tool"] == "recommend_remediation"
        arguments = terminal["arguments"]
        assert arguments["diagnosis_id"] in scenario.accepted_diagnoses
        assert arguments["remediation_id"] in scenario.accepted_remediations
        assert arguments["rationale"]

    def test_unknown_scenario_raises(self):
        client = DeterministicMockClient()
        bad = [Message("user", "scenario_id: not-a-scenario")]
        with pytest.raises(ModelClientError):
            list(client.stream_turn(bad, SETTINGS))


class TestDeadline:
    def test_slow_behavior_returns_close_to_the_deadline(self):
        """A 50 ms deadline returns near 50 ms, not after the 5 s delay."""
        client = DeterministicMockClient(behavior="slow", response_delay_s=5.0)
        start = time.monotonic()
        with pytest.raises(ModelClientTimeout):
            list(client.stream_turn(conversation(), SETTINGS, deadline=time.monotonic() + 0.05))
        elapsed = time.monotonic() - start
        assert elapsed < 0.5  # documented tolerance: worst-case scheduling slack

    def test_deadline_is_honored_on_a_virtual_clock(self):
        clock = FakeClock()
        client = DeterministicMockClient(behavior="slow", response_delay_s=10.0)
        deadline = clock.monotonic() + 0.05
        with pytest.raises(ModelClientTimeout):
            list(client.stream_turn(conversation(), SETTINGS, deadline=deadline, clock=clock))
        assert clock.monotonic() == pytest.approx(deadline)

    def test_elapsed_deadline_interrupts_mid_stream(self):
        clock = FakeClock()
        client = DeterministicMockClient()
        with pytest.raises(ModelClientTimeout):
            list(
                client.stream_turn(
                    conversation(), SETTINGS, deadline=clock.monotonic() - 1.0, clock=clock
                )
            )


class TestBehaviors:
    def test_unknown_behavior_is_rejected(self):
        with pytest.raises(ValueError):
            DeterministicMockClient(behavior="chaotic")

    def test_all_documented_behaviors_construct(self):
        for behavior in MOCK_BEHAVIORS:
            DeterministicMockClient(behavior=behavior)

    def test_endpoint_error_behavior_raises_client_error(self):
        client = DeterministicMockClient(behavior="endpoint_error")
        with pytest.raises(ModelClientError):
            list(client.stream_turn(conversation(), SETTINGS))

    def test_runtime_crash_behavior_raises_unexpected_exception(self):
        client = DeterministicMockClient(behavior="runtime_crash")
        with pytest.raises(RuntimeError):
            list(client.stream_turn(conversation(), SETTINGS))

    def test_malformed_tool_call_behavior_is_unparseable(self):
        client = DeterministicMockClient(behavior="malformed_tool_call")
        text = turn_text(client, conversation())
        line = next(x for x in text.splitlines() if x.startswith(TOOL_CALL_PREFIX))
        with pytest.raises(json.JSONDecodeError):
            json.loads(line[len(TOOL_CALL_PREFIX) :])

    def test_unknown_tool_behavior_names_missing_tool(self):
        call = last_call(DeterministicMockClient(behavior="unknown_tool"), conversation())
        assert call["tool"] == "reboot_datacenter"

    def test_bad_arguments_behavior_violates_contract(self):
        call = last_call(DeterministicMockClient(behavior="bad_arguments"), conversation())
        assert call["tool"] == "query_metrics"
        assert not isinstance(call["arguments"]["metric"], str)

    def test_alternative_path_behavior_replays_the_alternative_sequence(self):
        scenario = catalog()["elevated-latency-001"]
        client = DeterministicMockClient(behavior="alternative_path")
        call = last_call(client, conversation())
        assert call == scenario.alternative_tool_sequence[0]

    def test_wrong_diagnosis_behavior_picks_distractor_id(self):
        scenario = catalog()["elevated-latency-001"]
        client = DeterministicMockClient(behavior="wrong_diagnosis")
        messages = advance_past(client, conversation(), scenario.reference_tool_sequence)
        terminal = last_call(client, messages)
        assert terminal["arguments"]["diagnosis_id"] in scenario.distractor_diagnoses

    def test_negated_keyword_rationale_still_carries_wrong_diagnosis_id(self):
        """The adversarial rationale contains the full ground-truth summary
        (negated) — but the submitted diagnosis id remains a distractor."""
        scenario = catalog()["elevated-latency-001"]
        client = DeterministicMockClient(behavior="keyword_rationale_wrong_diagnosis")
        messages = advance_past(client, conversation(), scenario.reference_tool_sequence)
        terminal = last_call(client, messages)
        assert terminal["arguments"]["diagnosis_id"] in scenario.distractor_diagnoses
        assert scenario.root_cause_summary.casefold() in (
            terminal["arguments"]["rationale"].casefold()
        )

    def test_wrong_remediation_behavior_picks_distractor(self):
        scenario = catalog()["elevated-latency-001"]
        client = DeterministicMockClient(behavior="wrong_remediation")
        messages = advance_past(client, conversation(), scenario.reference_tool_sequence)
        terminal = last_call(client, messages)
        assert terminal["arguments"]["remediation_id"] in scenario.distractor_remediations

    def test_irrelevant_queries_behavior_asks_the_wrong_questions(self):
        client = DeterministicMockClient(behavior="irrelevant_queries")
        call = last_call(client, conversation())
        assert call["tool"] == "search_logs"
        assert call["arguments"]["query"] == "unicorn sightings"

    def test_repeated_tools_behavior_repeats_one_call(self):
        client = DeterministicMockClient(behavior="repeated_tools")
        first = last_call(client, conversation())
        messages = advance_past(client, conversation(), [first])
        second = last_call(client, messages)
        assert first == second
