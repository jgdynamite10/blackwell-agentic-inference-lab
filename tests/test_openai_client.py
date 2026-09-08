"""Offline tests for the OpenAI-compatible provider-neutral client.

Every test injects a fake transport: no network connection is ever attempted
(the autouse conftest guard enforces this).
"""

from __future__ import annotations

import json
import threading
import urllib.error

import pytest
from fakes import FakeClock

from blackwell_lab.workload.agent import run_task
from blackwell_lab.workload.model_client import (
    GenerationSettings,
    Message,
    ModelClientError,
    ModelClientTimeout,
    NativeToolCall,
    NativeToolCallError,
)
from blackwell_lab.workload.native_tools import TOOL_CHOICE, openai_tool_definitions
from blackwell_lab.workload.openai_client import (
    EndpointConfigError,
    OpenAICompatibleClient,
    validate_local_base_url,
)
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.tools import TOOL_SPECS, SimulatedToolbox

MESSAGES = [Message("system", "You are a test."), Message("user", "scenario_id: x")]
SETTINGS = GenerationSettings(max_tokens=64)


def sse(events: list[dict | str]) -> list[bytes]:
    lines = []
    for event in events:
        payload = event if isinstance(event, str) else json.dumps(event)
        lines.append(f"data: {payload}\n".encode())
    return lines


def content_chunk_line(text: str) -> dict:
    return {"choices": [{"delta": {"content": text}}]}


def reasoning_line(text: str) -> dict:
    return {"choices": [{"delta": {"reasoning": text}}]}


def usage_line(completion_tokens) -> dict:
    return {"choices": [], "usage": {"completion_tokens": completion_tokens}}


def tool_delta(*, index: int = 0, call_id: str = "", name: str = "", arguments: str = "") -> dict:
    fragment: dict = {"index": index}
    if call_id:
        fragment["id"] = call_id
    function: dict = {}
    if name:
        function["name"] = name
    if arguments:
        function["arguments"] = arguments
    if function:
        fragment["function"] = function
    return {"choices": [{"delta": {"tool_calls": [fragment]}}]}


def split_native_call(
    *,
    call_id: str,
    name: str,
    arguments: dict,
    piece_size: int = 3,
) -> list[dict]:
    """Realistic SSE split: id, name, then argument JSON in tiny fragments."""
    raw = json.dumps(arguments, sort_keys=True)
    events = [
        tool_delta(index=0, call_id=call_id),
        tool_delta(index=0, name=name),
    ]
    for start in range(0, len(raw), piece_size):
        events.append(tool_delta(index=0, arguments=raw[start : start + piece_size]))
    return events


def valid_health_stream(*, call_id: str = "call-health-1") -> list[dict | str]:
    return [
        *split_native_call(call_id=call_id, name="get_service_health", arguments={}),
        usage_line(9),
        "[DONE]",
    ]


class RecordingTransport:
    def __init__(self, lines: list[bytes]):
        self.lines = lines
        self.calls: list[dict] = []

    def __call__(self, url, body, headers, timeout_s):
        self.calls.append(
            {"url": url, "body": json.loads(body), "headers": headers, "timeout_s": timeout_s}
        )
        yield from self.lines


def make_client(transport, **kwargs) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        "http://127.0.0.1:8000/v1", "test-model", transport=transport, **kwargs
    )


def drain_until_error(client, messages=MESSAGES, settings=SETTINGS, **kwargs):
    events = []
    with pytest.raises(NativeToolCallError) as excinfo:
        for event in client.stream_turn(messages, settings, **kwargs):
            events.append(event)
    return events, excinfo.value


class TestEndpointValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000/v1",
            "http://localhost:8000/v1",
            "http://[::1]:8000/v1",
            "http://10.0.0.5:8000/v1",
            "http://192.168.1.20:8000/v1",
        ],
    )
    def test_local_and_private_endpoints_are_accepted(self, url):
        assert validate_local_base_url(url).startswith("http")

    @pytest.mark.parametrize(
        "url",
        [
            "http://93.184.216.34:8000/v1",  # public address
            "https://api.example/v1",  # unresolved public hostname
            "ftp://127.0.0.1/v1",  # wrong scheme
            "http:///v1",  # no host
        ],
    )
    def test_public_or_malformed_endpoints_are_refused(self, url):
        with pytest.raises(EndpointConfigError):
            validate_local_base_url(url)

    def test_embedded_credentials_are_refused_without_echoing_them(self):
        with pytest.raises(EndpointConfigError) as excinfo:
            validate_local_base_url("http://user:hunter2@127.0.0.1:8000/v1")
        assert "hunter2" not in str(excinfo.value)

    def test_empty_model_name_is_refused(self):
        with pytest.raises(EndpointConfigError):
            OpenAICompatibleClient("http://127.0.0.1:8000/v1", "  ")


class TestRequestShape:
    def test_request_sends_native_tools_auto_choice_and_no_parallel(self):
        transport = RecordingTransport(sse(valid_health_stream()))
        client = make_client(transport)
        list(client.stream_turn(MESSAGES, GenerationSettings(max_tokens=64, seed=7)))
        call = transport.calls[0]
        assert call["url"] == "http://127.0.0.1:8000/v1/chat/completions"
        assert call["body"]["stream"] is True
        assert call["body"]["stream_options"] == {"include_usage": True}
        assert call["body"]["seed"] == 7
        assert call["body"]["max_tokens"] == 64
        assert call["body"]["tools"] == openai_tool_definitions()
        assert call["body"]["tool_choice"] == TOOL_CHOICE == "auto"
        assert call["body"]["parallel_tool_calls"] is False
        names = [entry["function"]["name"] for entry in call["body"]["tools"]]
        assert names == sorted(TOOL_SPECS)
        for entry in call["body"]["tools"]:
            parameters = entry["function"]["parameters"]
            assert parameters["type"] == "object"
            assert parameters["additionalProperties"] is False
            spec = TOOL_SPECS[entry["function"]["name"]]
            assert set(parameters["properties"]) == set(spec["required"]) | set(spec["optional"])

    def test_tool_messages_use_role_tool_and_matching_id(self):
        transport = RecordingTransport(sse(valid_health_stream(call_id="call-2")))
        client = make_client(transport)
        prior = NativeToolCall("call-1", "get_service_health", {})
        messages = [
            *MESSAGES,
            Message("assistant", content="", tool_calls=(prior,)),
            Message("tool", '{"ok": true}', tool_call_id="call-1"),
        ]
        list(client.stream_turn(messages, SETTINGS))
        wire = transport.calls[0]["body"]["messages"]
        assert wire[-2]["role"] == "assistant"
        assert wire[-2]["tool_calls"][0]["id"] == "call-1"
        assert wire[-2]["tool_calls"][0]["function"]["name"] == "get_service_health"
        assert wire[-1]["role"] == "tool"
        assert wire[-1]["tool_call_id"] == "call-1"
        assert "TOOL_RESULT:" not in json.dumps(wire)

    def test_tool_result_without_id_is_refused(self):
        transport = RecordingTransport(sse(valid_health_stream()))
        client = make_client(transport)
        messages = [*MESSAGES, Message("tool", '{"ok": true}')]
        with pytest.raises(ModelClientError, match="tool_call_id"):
            list(client.stream_turn(messages, SETTINGS))

    def test_api_key_is_attached_from_the_environment_only(self, monkeypatch):
        transport = RecordingTransport(sse(valid_health_stream()))
        client = make_client(transport, api_key_env="TEST_ENDPOINT_KEY")
        monkeypatch.setenv("TEST_ENDPOINT_KEY", "synthetic-key-value")
        list(client.stream_turn(MESSAGES, SETTINGS))
        assert transport.calls[0]["headers"]["Authorization"] == "Bearer synthetic-key-value"

    def test_no_authorization_header_without_a_key(self):
        transport = RecordingTransport(sse(valid_health_stream()))
        client = make_client(transport)
        list(client.stream_turn(MESSAGES, SETTINGS))
        assert "Authorization" not in transport.calls[0]["headers"]


class TestStreamParsing:
    def test_content_only_response_is_rejected_after_yielding_chunks(self):
        transport = RecordingTransport(
            sse([content_chunk_line("Hello "), content_chunk_line("world"), "[DONE]"])
        )
        events, error = drain_until_error(make_client(transport))
        assert [e.kind for e in events] == ["content_chunk", "content_chunk"]
        assert "".join(e.text for e in events) == "Hello world"
        assert all(e.kind != "token" for e in events)
        assert error.category == "zero_tool_calls"
        assert "Hello" not in str(error)
        assert "world" not in json.dumps(error.diagnostics)

    def test_fragmented_native_tool_call_assembles_by_index(self):
        arguments = {"query": "latency spike"}
        transport = RecordingTransport(
            sse(
                [
                    *split_native_call(
                        call_id="call_abc",
                        name="search_logs",
                        arguments=arguments,
                        piece_size=2,
                    ),
                    usage_line(11),
                    "[DONE]",
                ]
            )
        )
        events = list(make_client(transport).stream_turn(MESSAGES, SETTINGS))
        native = [e for e in events if e.kind == "native_tool_call"]
        assert len(native) == 1
        assert native[0].tool_call is not None
        assert native[0].tool_call.call_id == "call_abc"
        assert native[0].tool_call.name == "search_logs"
        assert native[0].tool_call.arguments == arguments
        assert any(e.kind == "usage" and e.output_tokens == 11 for e in events)

    def test_usage_event_carries_authoritative_completion_tokens(self):
        transport = RecordingTransport(sse(valid_health_stream()))
        events = list(make_client(transport).stream_turn(MESSAGES, SETTINGS))
        usage = [e for e in events if e.kind == "usage"]
        assert len(usage) == 1
        assert usage[0].output_tokens == 9

    def test_empty_deltas_comments_and_blank_lines_are_ignored(self):
        lines = [
            b"\n",
            b": keep-alive comment\n",
            *sse([{"choices": [{"delta": {}}]}, *valid_health_stream()]),
        ]
        events = list(make_client(RecordingTransport(lines)).stream_turn(MESSAGES, SETTINGS))
        assert [e.kind for e in events if e.kind != "usage"] == ["native_tool_call"]

    def test_malformed_json_payload_raises_a_sanitized_client_error(self):
        transport = RecordingTransport([b"data: {not json}\n"])
        with pytest.raises(ModelClientError) as excinfo:
            list(make_client(transport).stream_turn(MESSAGES, SETTINGS))
        assert "malformed" in str(excinfo.value)
        assert "not json" not in str(excinfo.value)

    @pytest.mark.parametrize("bad_tokens", [True, "42", None, 4.2])
    def test_non_integer_usage_counts_are_refused_never_coerced(self, bad_tokens):
        transport = RecordingTransport(sse([usage_line(bad_tokens), "[DONE]"]))
        with pytest.raises(ModelClientError) as excinfo:
            list(make_client(transport).stream_turn(MESSAGES, SETTINGS))
        assert "fabricate" in str(excinfo.value)


class TestNativeRejections:
    def test_legacy_tool_call_text_is_refused(self):
        text = 'Investigating.\nTOOL_CALL: {"tool": "get_service_health", "arguments": {}}'
        events, error = drain_until_error(
            make_client(RecordingTransport(sse([content_chunk_line(text), "[DONE]"])))
        )
        assert error.category == "legacy_text_tool_call"
        assert "TOOL_CALL" not in str(error)
        assert "Investigating" not in json.dumps(error.diagnostics)
        assert events[0].kind == "content_chunk"

    def test_missing_call_id_is_rejected(self):
        _events, error = drain_until_error(
            make_client(
                RecordingTransport(
                    sse(
                        [
                            tool_delta(name="get_service_health", arguments="{}"),
                            "[DONE]",
                        ]
                    )
                )
            )
        )
        assert error.category == "missing_call_id"
        assert error.diagnostics["function_name_valid"] is True

    def test_missing_function_name_is_rejected(self):
        _events, error = drain_until_error(
            make_client(
                RecordingTransport(sse([tool_delta(call_id="call-x", arguments="{}"), "[DONE]"]))
            )
        )
        assert error.category == "missing_function_name"

    def test_malformed_argument_json_is_rejected(self):
        _events, error = drain_until_error(
            make_client(
                RecordingTransport(
                    sse(
                        [
                            tool_delta(
                                call_id="call-x",
                                name="search_logs",
                                arguments='{"query":',
                            ),
                            "[DONE]",
                        ]
                    )
                )
            )
        )
        assert error.category == "malformed_argument_json"
        assert error.diagnostics["arguments_json_ok"] is False
        assert '{"query":' not in json.dumps(error.diagnostics)

    def test_incomplete_argument_json_is_rejected(self):
        _events, error = drain_until_error(
            make_client(
                RecordingTransport(
                    sse(
                        [
                            tool_delta(
                                call_id="call-x",
                                name="search_logs",
                                arguments='{"query": "latency"',
                            ),
                            "[DONE]",
                        ]
                    )
                )
            )
        )
        assert error.category == "malformed_argument_json"

    def test_unknown_tool_is_rejected(self):
        _events, error = drain_until_error(
            make_client(
                RecordingTransport(
                    sse(
                        [
                            *split_native_call(
                                call_id="call-x",
                                name="reboot_datacenter",
                                arguments={},
                            ),
                            "[DONE]",
                        ]
                    )
                )
            )
        )
        assert error.category == "unknown_tool"
        assert error.diagnostics["function_name_valid"] is False
        assert "reboot_datacenter" not in json.dumps(error.diagnostics)

    def test_wrong_argument_type_is_rejected(self):
        _events, error = drain_until_error(
            make_client(
                RecordingTransport(
                    sse(
                        [
                            *split_native_call(
                                call_id="call-x",
                                name="query_metrics",
                                arguments={"metric": 12345},
                            ),
                            "[DONE]",
                        ]
                    )
                )
            )
        )
        assert error.category == "invalid_arguments"
        assert error.diagnostics["arguments_schema_ok"] is False

    def test_missing_required_argument_is_rejected(self):
        _events, error = drain_until_error(
            make_client(
                RecordingTransport(
                    sse(
                        [
                            *split_native_call(
                                call_id="call-x",
                                name="search_logs",
                                arguments={},
                            ),
                            "[DONE]",
                        ]
                    )
                )
            )
        )
        assert error.category == "invalid_arguments"

    def test_extra_argument_is_rejected(self):
        _events, error = drain_until_error(
            make_client(
                RecordingTransport(
                    sse(
                        [
                            *split_native_call(
                                call_id="call-x",
                                name="get_service_health",
                                arguments={"unexpected": "nope"},
                            ),
                            "[DONE]",
                        ]
                    )
                )
            )
        )
        assert error.category == "invalid_arguments"

    def test_two_tool_calls_are_rejected(self):
        _events, error = drain_until_error(
            make_client(
                RecordingTransport(
                    sse(
                        [
                            tool_delta(
                                index=0,
                                call_id="a",
                                name="get_service_health",
                                arguments="{}",
                            ),
                            tool_delta(
                                index=1,
                                call_id="b",
                                name="search_logs",
                                arguments='{"query":"x"}',
                            ),
                            "[DONE]",
                        ]
                    )
                )
            )
        )
        assert error.category == "parallel_or_multiple_tool_calls"
        assert error.diagnostics["tool_call_count"] == 2

    def test_reasoning_then_valid_tool_call_succeeds(self):
        transport = RecordingTransport(
            sse(
                [
                    reasoning_line("private chain of thought that must not persist"),
                    *split_native_call(call_id="call-r", name="get_service_health", arguments={}),
                    usage_line(4),
                    "[DONE]",
                ]
            )
        )
        events = list(make_client(transport).stream_turn(MESSAGES, SETTINGS))
        kinds = [e.kind for e in events]
        assert kinds[0] == "reasoning_chunk"
        assert "native_tool_call" in kinds
        native = next(e for e in events if e.kind == "native_tool_call")
        assert native.tool_call is not None
        assert native.tool_call.name == "get_service_health"
        serialized = json.dumps([e.__dict__ for e in events], default=str)
        assert "private chain of thought" not in serialized

    def test_mixed_content_and_tool_call_is_rejected(self):
        _events, error = drain_until_error(
            make_client(
                RecordingTransport(
                    sse(
                        [
                            content_chunk_line("I will call a tool now."),
                            *split_native_call(
                                call_id="call-x",
                                name="get_service_health",
                                arguments={},
                            ),
                            "[DONE]",
                        ]
                    )
                )
            )
        )
        assert error.category == "mixed_text_and_tool_call"


class TestFailureMapping:
    def test_http_error_is_reported_as_status_code_only(self):
        def transport(url, body, headers, timeout_s):
            raise urllib.error.HTTPError(url, 500, "internal", None, None)
            yield  # pragma: no cover

        with pytest.raises(ModelClientError) as excinfo:
            list(make_client(transport).stream_turn(MESSAGES, SETTINGS))
        assert str(excinfo.value) == "endpoint returned HTTP 500"

    def test_connection_failure_is_generic(self):
        def transport(url, body, headers, timeout_s):
            raise urllib.error.URLError("connection refused by 127.0.0.1")
            yield  # pragma: no cover

        with pytest.raises(ModelClientError) as excinfo:
            list(make_client(transport).stream_turn(MESSAGES, SETTINGS))
        assert str(excinfo.value) == "endpoint unreachable (connection failed)"

    def test_socket_timeout_below_the_deadline_is_a_client_error(self):
        clock = FakeClock()

        def transport(url, body, headers, timeout_s):
            raise TimeoutError("read timed out")
            yield  # pragma: no cover

        client = make_client(transport)
        with pytest.raises(ModelClientError) as excinfo:
            list(
                client.stream_turn(
                    MESSAGES, SETTINGS, deadline=clock.monotonic() + 60.0, clock=clock
                )
            )
        assert not isinstance(excinfo.value, ModelClientTimeout)


class TestDeadlines:
    def test_elapsed_deadline_raises_before_any_request_is_sent(self):
        transport = RecordingTransport(sse(valid_health_stream()))
        client = make_client(transport)
        clock = FakeClock()
        with pytest.raises(ModelClientTimeout):
            list(
                client.stream_turn(
                    MESSAGES, SETTINGS, deadline=clock.monotonic() - 0.001, clock=clock
                )
            )
        assert transport.calls == []  # nothing was sent

    def test_read_timeout_is_clamped_to_the_remaining_budget(self):
        transport = RecordingTransport(sse(valid_health_stream()))
        client = make_client(transport, read_timeout_s=120.0)
        clock = FakeClock()
        list(client.stream_turn(MESSAGES, SETTINGS, deadline=clock.monotonic() + 5.0, clock=clock))
        assert transport.calls[0]["timeout_s"] <= 5.0

    def test_deadline_crossing_mid_stream_raises_timeout(self):
        clock = FakeClock()
        deadline = clock.monotonic() + 1.0

        def transport(url, body, headers, timeout_s):
            for piece in ("a", "b", "c"):
                clock.sleep(0.6)  # 2nd line arrives past the deadline
                yield f"data: {json.dumps(content_chunk_line(piece))}\n".encode()

        client = make_client(transport)
        with pytest.raises(ModelClientTimeout):
            list(client.stream_turn(MESSAGES, SETTINGS, deadline=deadline, clock=clock))

    def test_socket_timeout_past_the_deadline_is_a_model_client_timeout(self):
        clock = FakeClock()
        deadline = clock.monotonic() + 1.0

        def transport(url, body, headers, timeout_s):
            clock.sleep(2.0)
            raise TimeoutError("read timed out")
            yield  # pragma: no cover

        client = make_client(transport)
        with pytest.raises(ModelClientTimeout):
            list(client.stream_turn(MESSAGES, SETTINGS, deadline=deadline, clock=clock))


class TestAgentLoopIntegration:
    """The client preserves the Phase 2 agent-loop contracts end to end."""

    def test_three_sequential_native_turns_with_role_tool_ids(self):
        scenario = catalog()["elevated-latency-001"]
        calls = [
            NativeToolCall("id-0", "get_service_health", {}),
            NativeToolCall("id-1", "search_logs", {"query": "timeout"}),
            NativeToolCall(
                "id-2",
                "recommend_remediation",
                {
                    "diagnosis_id": scenario.accepted_diagnoses[0],
                    "rationale": "test rationale",
                    "remediation_id": scenario.accepted_remediations[0],
                },
            ),
        ]
        recorded: list[dict] = []

        def transport(url, body, headers, timeout_s):
            payload = json.loads(body)
            recorded.append(payload)
            turn = len(recorded) - 1
            call = calls[turn]
            yield from sse(
                [
                    *split_native_call(
                        call_id=call.call_id,
                        name=call.name,
                        arguments=call.arguments,
                        piece_size=4,
                    ),
                    usage_line(17),
                    "[DONE]",
                ]
            )

        client = make_client(transport)
        clock = FakeClock()
        execution = run_task(
            scenario,
            client,
            SimulatedToolbox(scenario, clock=clock),
            SETTINGS,
            timeout_s=60.0,
            clock=clock,
        )
        assert execution.status == "completed"
        assert execution.tools_used == [
            "get_service_health",
            "search_logs",
            "recommend_remediation",
        ]
        assert [turn.output_tokens for turn in execution.turns] == [17, 17, 17]
        assert execution.turns[0].tokens_unavailable_reason is None
        assert execution.turns[0].itl_available is False
        assert recorded[1]["messages"][-2]["role"] == "assistant"
        assert recorded[1]["messages"][-2]["tool_calls"][0]["id"] == "id-0"
        assert recorded[1]["messages"][-1] == {
            "role": "tool",
            "tool_call_id": "id-0",
            "content": recorded[1]["messages"][-1]["content"],
        }
        assert recorded[2]["messages"][-1]["tool_call_id"] == "id-1"
        assert all(body["tools"] == openai_tool_definitions() for body in recorded)
        assert all(body["tool_choice"] == "auto" for body in recorded)
        assert all(body["parallel_tool_calls"] is False for body in recorded)

    def test_ttft_starts_on_the_first_tool_call_delta(self):
        clock = FakeClock()

        def transport(url, body, headers, timeout_s):
            clock.sleep(0.012)
            yield from sse(
                [
                    *split_native_call(
                        call_id="ttft-1",
                        name="recommend_remediation",
                        arguments={
                            "diagnosis_id": catalog()["elevated-latency-001"].accepted_diagnoses[0],
                            "rationale": "ttft",
                            "remediation_id": catalog()[
                                "elevated-latency-001"
                            ].accepted_remediations[0],
                        },
                    ),
                    usage_line(3),
                    "[DONE]",
                ]
            )

        scenario = catalog()["elevated-latency-001"]
        execution = run_task(
            scenario,
            make_client(transport),
            SimulatedToolbox(scenario, clock=clock),
            SETTINGS,
            timeout_s=60.0,
            clock=clock,
        )
        assert execution.status == "completed"
        assert execution.turns[0].ttft_ms == pytest.approx(12.0)

    def test_legacy_text_response_fails_the_task_without_raw_output(self):
        scenario = catalog()["elevated-latency-001"]
        call = {
            "tool": "recommend_remediation",
            "arguments": {
                "diagnosis_id": scenario.accepted_diagnoses[0],
                "rationale": "secret rationale text",
                "remediation_id": scenario.accepted_remediations[0],
            },
        }
        turn_text = f"Submitting.\nTOOL_CALL: {json.dumps(call)}"

        def transport(url, body, headers, timeout_s):
            yield from sse([content_chunk_line(turn_text), usage_line(17), "[DONE]"])

        clock = FakeClock()
        execution = run_task(
            scenario,
            make_client(transport),
            SimulatedToolbox(scenario, clock=clock),
            SETTINGS,
            timeout_s=60.0,
            clock=clock,
        )
        assert execution.status == "error"
        assert execution.error_category == "malformed_tool_call"
        assert execution.tool_call_diagnostics is not None
        assert execution.tool_call_diagnostics["failure_category"] == "legacy_text_tool_call"
        blob = json.dumps(execution.tool_call_diagnostics)
        assert "secret rationale text" not in blob
        assert "TOOL_CALL" not in blob
        assert "Submitting" not in blob

    def test_concurrency_isolation_between_requests(self):
        barrier = threading.Barrier(2, timeout=5)
        results: dict[str, str] = {}

        def transport_for(name: str, call_id: str):
            def transport(url, body, headers, timeout_s):
                barrier.wait()
                yield from sse(
                    [
                        *split_native_call(call_id=call_id, name=name, arguments={}),
                        "[DONE]",
                    ]
                )

            return transport

        def worker(label: str, name: str, call_id: str) -> None:
            events = list(make_client(transport_for(name, call_id)).stream_turn(MESSAGES, SETTINGS))
            native = next(e for e in events if e.kind == "native_tool_call")
            assert native.tool_call is not None
            results[label] = native.tool_call.name

        threads = [
            threading.Thread(target=worker, args=("a", "get_service_health", "iso-a")),
            threading.Thread(target=worker, args=("b", "check_recent_changes", "iso-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert results == {"a": "get_service_health", "b": "check_recent_changes"}
