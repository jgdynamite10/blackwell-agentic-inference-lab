"""Provider-neutral model-client interface and deterministic mock client.

The agent loop and benchmark runner depend only on the :class:`ModelClient`
interface. Phase 3+ will add clients that stream from a real serving endpoint
(vLLM / TensorRT-LLM / NIM) behind the same interface; Phase 2 ships only the
:class:`DeterministicMockClient`, which is **fully offline**: no model
download, no GPU, no external API, and no network connection anywhere.

Typed stream events
-------------------
A turn is streamed as :class:`StreamEvent` values, which distinguish:

- ``content_chunk`` — a transport text chunk. Chunks are **not** tokens: one
  chunk may carry many model tokens. The mock client deliberately emits
  multi-word chunks so nothing downstream can conflate the two.
- ``token`` — a true per-token event with per-token timing. Only clients with
  genuine token-granular streaming emit these; the mock client never does.
- ``usage`` — authoritative token counts (server-reported usage or an exact
  tokenizer). Token counts come only from these events; network chunks are
  never counted as tokens. The mock client emits none.
- ``queue_telemetry`` — optional serving-engine queue time reported by the
  engine itself (e.g. vLLM metrics). The mock client emits none.

TTFT is measured at the first meaningful model-output event: content,
reasoning output, or a native tool-call delta. Inter-token latency exists
only when true ``token`` events are present.

Deadlines
---------
``stream_turn`` receives an optional monotonic ``deadline`` and must honor it:
implementations return (or raise :class:`ModelClientTimeout`) close to the
deadline instead of blocking arbitrarily.

Turn protocol
-------------
Each turn must produce exactly one native OpenAI-compatible tool call
(``StreamEvent.kind == "native_tool_call"``). The retired ``TOOL_CALL:``
text protocol is never accepted. Calling the terminal tool
``recommend_remediation`` (with ``diagnosis_id``, ``rationale``, and
``remediation_id``) ends the task. Anything unparseable is a malformed
tool call and, with the measurement retry policy of ``retries=0``, fails
the task visibly (measurement contract §7).
"""

from __future__ import annotations

import abc
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from blackwell_lab.workload.clock import SYSTEM_CLOCK, Clock
from blackwell_lab.workload.scenarios import Scenario, catalog

MOCK_CLIENT_VERSION = "4.0.0"

#: Retired text protocol. Kept only so tests and sanitizers can detect and
#: reject it. Production clients never assemble a turn from this prefix.
TOOL_CALL_PREFIX = "TOOL_CALL:"

#: Words per transport chunk emitted by the mock client. Deliberately > 1 so
#: any code path equating chunks with tokens is caught by tests.
MOCK_WORDS_PER_CHUNK = 3


@dataclass(frozen=True)
class NativeToolCall:
    """One complete native function call after stream assembly."""

    call_id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Message:
    """One conversation message. ``role`` is system, user, assistant, or tool."""

    role: str
    content: str = ""
    tool_calls: tuple[NativeToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class GenerationSettings:
    """Fixed generation settings recorded in the run manifest."""

    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 1024
    seed: int | None = 20260906
    reasoning_mode: bool | None = None


@dataclass(frozen=True)
class StreamEvent:
    """One typed event of a streamed model turn (see module docstring)."""

    kind: str
    # "content_chunk" | "reasoning_chunk" | "native_tool_call" | "token" |
    # "usage" | "queue_telemetry"
    text: str = ""
    output_tokens: int | None = None  # usage events only
    queue_time_ms: float | None = None  # queue_telemetry events only
    tool_call: NativeToolCall | None = None


class ModelClientError(Exception):
    """The serving endpoint (or its stand-in) failed to produce a turn."""


class NativeToolCallError(ModelClientError):
    """A streamed native tool call could not be assembled or validated.

    The exception text is the sanitized failure category only. Structural
    diagnostics live on :attr:`diagnostics` and must never include raw
    prompts, completions, reasoning, or private paths.
    """

    def __init__(self, category: str, diagnostics: dict) -> None:
        super().__init__(category)
        self.category = category
        self.diagnostics = diagnostics


class ModelClientTimeout(ModelClientError):
    """The turn's deadline elapsed before the turn completed."""


class ModelClient(abc.ABC):
    """Provider-neutral streaming interface for one model turn."""

    #: Identifier recorded in the run manifest (engine field).
    name: str = "abstract"
    version: str = "0.0.0"

    @abc.abstractmethod
    def stream_turn(
        self,
        messages: Sequence[Message],
        settings: GenerationSettings,
        *,
        deadline: float | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> Iterator[StreamEvent]:
        """Streams the typed events of one assistant turn.

        ``deadline`` is a monotonic-clock instant (``clock.monotonic()``
        units); implementations must not block meaningfully past it — they
        raise :class:`ModelClientTimeout` (or return) close to the deadline.

        Implementations must be thread-safe: the runner issues concurrent
        tasks against a single client instance.
        """


def _chunk(text: str, words_per_chunk: int = MOCK_WORDS_PER_CHUNK) -> list[str]:
    """Splits text into multi-word transport chunks (chunks are NOT tokens)."""
    words = text.split(" ")
    chunks: list[str] = []
    for i in range(0, len(words), words_per_chunk):
        piece = " ".join(words[i : i + words_per_chunk])
        if i + words_per_chunk < len(words):
            piece += " "
        chunks.append(piece)
    return chunks


#: Behaviors the mock client can exhibit, used to exercise every branch of the
#: agent loop and evaluator in tests. "correct" is the default benchmark
#: behavior; the others deterministically simulate failure/adversarial modes.
MOCK_BEHAVIORS = (
    "correct",
    "alternative_path",
    "wrong_diagnosis",
    "wrong_remediation",
    "keyword_rationale_wrong_diagnosis",
    "irrelevant_queries",
    "repeated_tools",
    "missing_evidence",
    "malformed_tool_call",
    "unknown_tool",
    "bad_arguments",
    "no_terminal",
    "endpoint_error",
    "slow",
    "runtime_crash",
)


class DeterministicMockClient(ModelClient):
    """Deterministic, offline stand-in for a serving endpoint.

    For each scenario it replays an evidence tool sequence and then submits a
    terminal recommendation. The turn index is derived from the conversation
    itself (the number of tool messages), so the client is stateless and
    thread-safe. Two identical conversations always produce identical event
    streams.

    The mock emits a single ``native_tool_call`` event per turn (except the
    retired-text ``malformed_tool_call`` behavior). It has no true token
    timing and no authoritative tokenizer, so ITL and token counts stay
    unavailable for mock runs — mock Python replay speed is never model
    tokens/sec.

    ``behavior`` selects a deterministic failure/adversarial mode for tests
    (see :data:`MOCK_BEHAVIORS`). ``response_delay_s`` delays the first event
    only when behavior is ``slow``, to exercise timeout handling; the delay
    honors the turn deadline and raises :class:`ModelClientTimeout` at the
    deadline instead of blocking past it.
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
        self,
        messages: Sequence[Message],
        settings: GenerationSettings,
        *,
        deadline: float | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> Iterator[StreamEvent]:
        if self._behavior == "endpoint_error":
            raise ModelClientError("simulated endpoint failure (mock behavior)")
        if self._behavior == "runtime_crash":
            raise RuntimeError("simulated unexpected client crash (mock behavior)")
        if self._behavior == "slow" and self._response_delay_s > 0:
            self._sleep_until(clock, self._response_delay_s, deadline)

        scenario = self._scenario_for(messages)
        turn_index = sum(1 for m in messages if m.role == "tool")
        if self._behavior == "malformed_tool_call" and turn_index == 0:
            # Emit the retired text protocol only: the agent/client must
            # refuse it and never fall back to parsing TOOL_CALL lines.
            text = (
                "Investigating the incident now.\n"
                f'{TOOL_CALL_PREFIX} {{"tool": "get_service_health", '
                '"arguments": {{unclosed'
            )
            for piece in _chunk(text):
                if deadline is not None and clock.monotonic() >= deadline:
                    raise ModelClientTimeout("turn deadline elapsed mid-stream")
                yield StreamEvent(kind="content_chunk", text=piece)
            return
        call = self._turn_call(scenario, turn_index)
        if deadline is not None and clock.monotonic() >= deadline:
            raise ModelClientTimeout("turn deadline elapsed mid-stream")
        yield StreamEvent(
            kind="native_tool_call",
            tool_call=NativeToolCall(
                call_id=f"mock-{scenario.scenario_id}-t{turn_index}",
                name=call["tool"],
                arguments=dict(call["arguments"]),
            ),
        )

    @staticmethod
    def _sleep_until(clock: Clock, delay_s: float, deadline: float | None) -> None:
        """Sleeps ``delay_s`` but never meaningfully past ``deadline``."""
        if deadline is None:
            clock.sleep(delay_s)
            return
        remaining = deadline - clock.monotonic()
        if delay_s < remaining:
            clock.sleep(delay_s)
            return
        if remaining > 0:
            clock.sleep(remaining)
        raise ModelClientTimeout("turn deadline elapsed during simulated delay")

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

    def _evidence_sequence(self, scenario: Scenario) -> list[dict]:
        if self._behavior == "alternative_path":
            return list(scenario.alternative_tool_sequence)
        if self._behavior == "missing_evidence":
            # Gather almost nothing, then recommend early.
            return list(scenario.reference_tool_sequence)[:1]
        if self._behavior == "irrelevant_queries":
            # The right tools asked the wrong questions: none of these calls
            # can satisfy an evidence predicate (arguments/results constrained).
            return [
                {"tool": "search_logs", "arguments": {"query": "unicorn sightings"}},
                {"tool": "query_metrics", "arguments": {"metric": "no_such_metric"}},
                {"tool": "check_recent_changes", "arguments": {"window_s": 1}},
                {"tool": "retrieve_runbook", "arguments": {"key": "nonexistent-runbook"}},
            ]
        if self._behavior == "repeated_tools":
            # Repeating one evidencing call must not accumulate credit for
            # the other mandatory predicates.
            step = next(
                (s for s in scenario.reference_tool_sequence if s["tool"] == "search_logs"),
                {"tool": "get_service_health", "arguments": {}},
            )
            return [step, step, step, step]
        return list(scenario.reference_tool_sequence)

    def _turn_call(self, scenario: Scenario, turn_index: int) -> dict:
        sequence = self._evidence_sequence(scenario)

        if self._behavior == "no_terminal":
            return {"tool": "get_service_health", "arguments": {}}

        if turn_index < len(sequence):
            call = sequence[turn_index]
            if turn_index == 0:
                if self._behavior == "unknown_tool":
                    return {"tool": "reboot_datacenter", "arguments": {}}
                if self._behavior == "bad_arguments":
                    return {"tool": "query_metrics", "arguments": {"metric": 12345}}
            return call

        diagnosis_id = scenario.accepted_diagnoses[0]
        rationale = scenario.root_cause_summary
        remediation_id = scenario.accepted_remediations[0]
        if self._behavior == "wrong_diagnosis":
            diagnosis_id = scenario.distractor_diagnoses[0]
        elif self._behavior == "keyword_rationale_wrong_diagnosis":
            diagnosis_id = scenario.distractor_diagnoses[0]
            rationale = f"It is not the case that: {scenario.root_cause_summary}"
        if self._behavior == "wrong_remediation":
            remediation_id = scenario.distractor_remediations[0]
        return {
            "tool": "recommend_remediation",
            "arguments": {
                "diagnosis_id": diagnosis_id,
                "rationale": rationale,
                "remediation_id": remediation_id,
            },
        }

    def _turn_text(self, scenario: Scenario, turn_index: int) -> str:
        sequence = self._evidence_sequence(scenario)

        if self._behavior == "no_terminal":
            # Never recommend: keep polling health forever (agent enforces
            # max_turns and reports no_terminal_recommendation).
            call = {"tool": "get_service_health", "arguments": {}}
            return self._render(f"Re-checking service health (turn {turn_index}).", call)

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
            return self._render(reasoning, call)

        # Evidence gathering complete: submit the terminal recommendation.
        diagnosis_id = scenario.accepted_diagnoses[0]
        rationale = scenario.root_cause_summary
        remediation_id = scenario.accepted_remediations[0]
        if self._behavior == "wrong_diagnosis":
            diagnosis_id = scenario.distractor_diagnoses[0]
        elif self._behavior == "keyword_rationale_wrong_diagnosis":
            # Adversarial: a rationale stuffed with (negated) ground-truth
            # wording must not rescue a wrong diagnosis id.
            diagnosis_id = scenario.distractor_diagnoses[0]
            rationale = f"It is not the case that: {scenario.root_cause_summary}"
        if self._behavior == "wrong_remediation":
            remediation_id = scenario.distractor_remediations[0]
        call = {
            "tool": "recommend_remediation",
            "arguments": {
                "diagnosis_id": diagnosis_id,
                "rationale": rationale,
                "remediation_id": remediation_id,
            },
        }
        summary = (
            "Evidence gathered is consistent with a single root cause; submitting "
            "the remediation recommendation."
        )
        return self._render(summary, call)

    @staticmethod
    def _render(reasoning: str, call: dict) -> str:
        return f"{reasoning}\n{TOOL_CALL_PREFIX} {json.dumps(call, sort_keys=True)}"
