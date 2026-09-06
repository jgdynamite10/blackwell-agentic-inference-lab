"""Provider-neutral model-client interface and deterministic mock client.

The agent loop and benchmark runner depend only on the :class:`ModelClient`
interface. Phase 3+ will add clients that stream from a real serving endpoint
(vLLM / TensorRT-LLM / NIM) behind the same interface; Phase 2 ships only the
:class:`DeterministicMockClient`, which is **fully offline**: no model
download, no GPU, no external API, and no network connection anywhere.

Turn protocol
-------------
A model turn is streamed as text tokens. The final line of the turn must be:

    ``TOOL_CALL: {"tool": "<name>", "arguments": {...}}``

Calling the terminal tool ``recommend_remediation`` ends the task. Anything
unparseable is a malformed tool call and, with the measurement retry policy of
``retries=0``, fails the task visibly (measurement contract §7).
"""

from __future__ import annotations

import abc
import json
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from blackwell_lab.workload.scenarios import Scenario, catalog

MOCK_CLIENT_VERSION = "2.0.0"

TOOL_CALL_PREFIX = "TOOL_CALL:"


@dataclass(frozen=True)
class Message:
    """One conversation message. ``role`` is system, user, assistant, or tool."""

    role: str
    content: str


@dataclass(frozen=True)
class GenerationSettings:
    """Fixed generation settings recorded in the run manifest."""

    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 1024
    seed: int | None = 20260906
    reasoning_mode: bool | None = None


class ModelClientError(Exception):
    """The serving endpoint (or its stand-in) failed to produce a turn."""


class ModelClient(abc.ABC):
    """Provider-neutral streaming interface for one model turn."""

    #: Identifier recorded in the run manifest (engine field).
    name: str = "abstract"
    version: str = "0.0.0"

    @abc.abstractmethod
    def stream_turn(
        self, messages: Sequence[Message], settings: GenerationSettings
    ) -> Iterator[str]:
        """Streams the tokens of one assistant turn for the given conversation.

        Implementations must be thread-safe: the runner issues concurrent
        tasks against a single client instance.
        """


def _tokenize(text: str) -> list[str]:
    """Splits text into whitespace-preserving tokens so the driver observes a
    realistic multi-token stream (TTFT + inter-token gaps)."""
    tokens: list[str] = []
    for word in text.split(" "):
        tokens.append(word + " ")
    if tokens:
        tokens[-1] = tokens[-1].rstrip(" ")
    return tokens


#: Behaviors the mock client can exhibit, used to exercise every branch of the
#: agent loop and evaluator in tests. "correct" is the default benchmark
#: behavior; the others deterministically simulate failure modes.
MOCK_BEHAVIORS = (
    "correct",
    "wrong_root_cause",
    "wrong_remediation",
    "partial_evidence",
    "malformed_tool_call",
    "unknown_tool",
    "bad_arguments",
    "no_terminal",
    "endpoint_error",
    "slow",
)


class DeterministicMockClient(ModelClient):
    """Deterministic, offline stand-in for a serving endpoint.

    For each scenario it replays the scenario's reference tool sequence and
    then submits the ground-truth recommendation via the terminal tool. The
    turn index is derived from the conversation itself (the number of tool
    messages), so the client is stateless and thread-safe. Two identical
    conversations always produce identical token streams.

    ``behavior`` selects a deterministic failure mode for tests (see
    :data:`MOCK_BEHAVIORS`). ``response_delay_s`` sleeps before the first
    token only when behavior is ``slow``, to exercise timeout handling.
    """

    name = "mock"
    version = MOCK_CLIENT_VERSION

    def __init__(self, behavior: str = "correct", response_delay_s: float = 0.0) -> None:
        if behavior not in MOCK_BEHAVIORS:
            raise ValueError(f"unknown mock behavior: {behavior!r}")
        self._behavior = behavior
        self._response_delay_s = response_delay_s
        self._catalog = catalog()

    def stream_turn(
        self, messages: Sequence[Message], settings: GenerationSettings
    ) -> Iterator[str]:
        if self._behavior == "endpoint_error":
            raise ModelClientError("simulated endpoint failure (mock behavior)")
        if self._behavior == "slow" and self._response_delay_s > 0:
            time.sleep(self._response_delay_s)

        scenario = self._scenario_for(messages)
        turn_index = sum(1 for m in messages if m.role == "tool")
        text = self._turn_text(scenario, turn_index)
        yield from _tokenize(text)

    # -- deterministic turn construction ---------------------------------

    def _scenario_for(self, messages: Sequence[Message]) -> Scenario:
        for message in messages:
            if message.role != "user":
                continue
            for line in message.content.splitlines():
                if line.startswith("scenario_id:"):
                    scenario_id = line.split(":", 1)[1].strip()
                    if scenario_id in self._catalog:
                        return self._catalog[scenario_id]
        raise ModelClientError("mock client could not identify the scenario from the prompt")

    def _turn_text(self, scenario: Scenario, turn_index: int) -> str:
        sequence = list(scenario.reference_tool_sequence)
        if self._behavior == "partial_evidence":
            # Keep only the first two evidence steps, then recommend early.
            sequence = sequence[:2]

        if self._behavior == "no_terminal":
            # Never recommend: keep polling health forever (agent enforces
            # max_turns and reports no_terminal_recommendation).
            call = {"tool": "get_service_health", "arguments": {}}
            return self._render(scenario, f"Re-checking service health (turn {turn_index}).", call)

        if turn_index < len(sequence):
            call = sequence[turn_index]
            if turn_index == 0:
                if self._behavior == "malformed_tool_call":
                    return (
                        "Investigating the incident now.\n"
                        f'{TOOL_CALL_PREFIX} {{"tool": "get_service_health", '
                        '"arguments": {{unclosed'
                    )
                if self._behavior == "unknown_tool":
                    call = {"tool": "reboot_datacenter", "arguments": {}}
                elif self._behavior == "bad_arguments":
                    call = {"tool": "query_metrics", "arguments": {"metric": 12345}}
            reasoning = (
                f"Step {turn_index + 1}: consulting {call['tool']} to narrow down the "
                f"cause of the {scenario.incident_class} incident."
            )
            return self._render(scenario, reasoning, call)

        # Evidence gathering complete: submit the terminal recommendation.
        root_cause = scenario.root_cause_summary
        remediation = scenario.accepted_remediations[0]
        if self._behavior == "wrong_root_cause":
            root_cause = "Transient network blip; no specific cause identified."
        if self._behavior == "wrong_remediation":
            remediation = scenario.distractor_remediations[0]
        call = {
            "tool": "recommend_remediation",
            "arguments": {"root_cause": root_cause, "remediation_id": remediation},
        }
        summary = (
            "Evidence gathered is consistent with a single root cause; submitting "
            "the remediation recommendation."
        )
        return self._render(scenario, summary, call)

    @staticmethod
    def _render(scenario: Scenario, reasoning: str, call: dict) -> str:
        return f"{reasoning}\n{TOOL_CALL_PREFIX} {json.dumps(call, sort_keys=True)}"
