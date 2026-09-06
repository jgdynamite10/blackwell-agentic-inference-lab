"""Offline tests for the OpenAI-compatible provider-neutral client.

Every test injects a fake transport: no network connection is ever attempted
(the autouse conftest guard enforces this).
"""

from __future__ import annotations

import json
import urllib.error

import pytest
from fakes import FakeClock

from blackwell_lab.workload.agent import run_task
from blackwell_lab.workload.model_client import (
    GenerationSettings,
    Message,
    ModelClientError,
    ModelClientTimeout,
)
from blackwell_lab.workload.openai_client import (
    EndpointConfigError,
    OpenAICompatibleClient,
    validate_local_base_url,
)
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.tools import SimulatedToolbox

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


def usage_line(completion_tokens) -> dict:
    return {"choices": [], "usage": {"completion_tokens": completion_tokens}}


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
    def test_request_enables_streaming_with_usage_and_seed(self):
        transport = RecordingTransport(sse([content_chunk_line("hello"), "[DONE]"]))
        client = make_client(transport)
        list(client.stream_turn(MESSAGES, GenerationSettings(max_tokens=64, seed=7)))
        call = transport.calls[0]
        assert call["url"] == "http://127.0.0.1:8000/v1/chat/completions"
        assert call["body"]["stream"] is True
        assert call["body"]["stream_options"] == {"include_usage": True}
        assert call["body"]["seed"] == 7
        assert call["body"]["max_tokens"] == 64

    def test_tool_messages_map_to_user_role_with_tool_result_prefix(self):
        transport = RecordingTransport(sse(["[DONE]"]))
        client = make_client(transport)
        messages = [*MESSAGES, Message("tool", '{"tool": "search_logs"}')]
        list(client.stream_turn(messages, SETTINGS))
        wire = transport.calls[0]["body"]["messages"]
        assert wire[-1]["role"] == "user"
        assert wire[-1]["content"].startswith("TOOL_RESULT: ")

    def test_api_key_is_attached_from_the_environment_only(self, monkeypatch):
        transport = RecordingTransport(sse(["[DONE]"]))
        client = make_client(transport, api_key_env="TEST_ENDPOINT_KEY")
        monkeypatch.setenv("TEST_ENDPOINT_KEY", "synthetic-key-value")
        list(client.stream_turn(MESSAGES, SETTINGS))
        assert transport.calls[0]["headers"]["Authorization"] == "Bearer synthetic-key-value"

    def test_no_authorization_header_without_a_key(self):
        transport = RecordingTransport(sse(["[DONE]"]))
        client = make_client(transport)
        list(client.stream_turn(MESSAGES, SETTINGS))
        assert "Authorization" not in transport.calls[0]["headers"]


class TestStreamParsing:
    def test_content_deltas_become_content_chunks_never_tokens(self):
        transport = RecordingTransport(
            sse([content_chunk_line("Hello "), content_chunk_line("world"), "[DONE]"])
        )
        events = list(make_client(transport).stream_turn(MESSAGES, SETTINGS))
        assert [e.kind for e in events] == ["content_chunk", "content_chunk"]
        assert "".join(e.text for e in events) == "Hello world"
        assert all(e.kind != "token" for e in events)

    def test_usage_event_carries_authoritative_completion_tokens(self):
        transport = RecordingTransport(sse([content_chunk_line("hi"), usage_line(42), "[DONE]"]))
        events = list(make_client(transport).stream_turn(MESSAGES, SETTINGS))
        usage = [e for e in events if e.kind == "usage"]
        assert len(usage) == 1
        assert usage[0].output_tokens == 42

    def test_empty_deltas_comments_and_blank_lines_are_ignored(self):
        lines = [
            b"\n",
            b": keep-alive comment\n",
            *sse([{"choices": [{"delta": {}}]}, content_chunk_line("x"), "[DONE]"]),
        ]
        events = list(make_client(RecordingTransport(lines)).stream_turn(MESSAGES, SETTINGS))
        assert [e.kind for e in events] == ["content_chunk"]

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
        transport = RecordingTransport(sse(["[DONE]"]))
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
        transport = RecordingTransport(sse(["[DONE]"]))
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

    def test_run_task_completes_with_usage_backed_token_counts(self):
        scenario = next(iter(catalog().values()))
        terminal_call = {
            "tool": "recommend_remediation",
            "arguments": {
                "diagnosis_id": scenario.accepted_diagnoses[0],
                "rationale": "test rationale",
                "remediation_id": scenario.accepted_remediations[0],
            },
        }
        turn_text = f"Submitting.\nTOOL_CALL: {json.dumps(terminal_call)}"

        def transport(url, body, headers, timeout_s):
            yield from sse([content_chunk_line(turn_text), usage_line(17), "[DONE]"])

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
        turn = execution.turns[0]
        assert turn.output_tokens == 17
        assert turn.tokens_unavailable_reason is None
        # Transport chunks are never tokens: no ITL from chunk timing.
        assert turn.itl_available is False
        assert turn.inter_token_gaps_ms == ()
