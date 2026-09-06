"""Deterministic mock client: interface conformance and behavior variants."""

from __future__ import annotations

import json

import pytest

from blackwell_lab.workload.model_client import (
    MOCK_BEHAVIORS,
    TOOL_CALL_PREFIX,
    DeterministicMockClient,
    GenerationSettings,
    Message,
    ModelClient,
    ModelClientError,
)
from blackwell_lab.workload.scenarios import catalog

SETTINGS = GenerationSettings()


def conversation(scenario_id: str = "elevated-latency-001") -> list[Message]:
    scenario = catalog()[scenario_id]
    return [
        Message("system", "You are a Cloud Operations Agent."),
        Message("user", f"scenario_id: {scenario_id}\n\n{scenario.description}"),
    ]


def last_call(client: ModelClient, messages: list[Message]) -> dict:
    text = "".join(client.stream_turn(messages, SETTINGS))
    line = [x for x in text.splitlines() if x.startswith(TOOL_CALL_PREFIX)][-1]
    return json.loads(line[len(TOOL_CALL_PREFIX) :])


class TestDeterminism:
    def test_identical_conversations_produce_identical_token_streams(self):
        client = DeterministicMockClient()
        first = list(client.stream_turn(conversation(), SETTINGS))
        second = list(client.stream_turn(conversation(), SETTINGS))
        assert first == second
        assert len(first) > 1  # a genuine multi-token stream

    def test_two_client_instances_agree(self):
        assert list(DeterministicMockClient().stream_turn(conversation(), SETTINGS)) == list(
            DeterministicMockClient().stream_turn(conversation(), SETTINGS)
        )


class TestTurnProtocol:
    def test_first_turn_replays_reference_sequence(self):
        scenario = catalog()["elevated-latency-001"]
        call = last_call(DeterministicMockClient(), conversation())
        assert call == scenario.reference_tool_sequence[0]

    def test_turn_index_derived_from_tool_messages(self):
        scenario = catalog()["elevated-latency-001"]
        client = DeterministicMockClient()
        messages = conversation()
        for step in scenario.reference_tool_sequence:
            call = last_call(client, messages)
            assert call == step
            messages.append(Message("assistant", "..."))
            messages.append(Message("tool", json.dumps({"tool": step["tool"], "result": {}})))
        terminal = last_call(client, messages)
        assert terminal["tool"] == "recommend_remediation"
        assert terminal["arguments"]["remediation_id"] in scenario.accepted_remediations
        for keyword in scenario.root_cause_keywords:
            assert keyword.casefold() in terminal["arguments"]["root_cause"].casefold()

    def test_unknown_scenario_raises(self):
        client = DeterministicMockClient()
        bad = [Message("user", "scenario_id: not-a-scenario")]
        with pytest.raises(ModelClientError):
            list(client.stream_turn(bad, SETTINGS))


class TestBehaviors:
    def test_unknown_behavior_is_rejected(self):
        with pytest.raises(ValueError):
            DeterministicMockClient(behavior="chaotic")

    def test_all_documented_behaviors_construct(self):
        for behavior in MOCK_BEHAVIORS:
            DeterministicMockClient(behavior=behavior)

    def test_endpoint_error_behavior_raises(self):
        client = DeterministicMockClient(behavior="endpoint_error")
        with pytest.raises(ModelClientError):
            list(client.stream_turn(conversation(), SETTINGS))

    def test_malformed_tool_call_behavior_is_unparseable(self):
        client = DeterministicMockClient(behavior="malformed_tool_call")
        text = "".join(client.stream_turn(conversation(), SETTINGS))
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

    def test_wrong_remediation_behavior_picks_distractor(self):
        scenario = catalog()["elevated-latency-001"]
        client = DeterministicMockClient(behavior="wrong_remediation")
        messages = conversation()
        for step in scenario.reference_tool_sequence:
            messages.append(Message("assistant", "..."))
            messages.append(Message("tool", json.dumps({"tool": step["tool"], "result": {}})))
        terminal = last_call(client, messages)
        assert terminal["arguments"]["remediation_id"] in scenario.distractor_remediations
