"""Provider-neutral client for an OpenAI-compatible local serving endpoint.

Phase 3A readiness code: this client is the real-benchmark counterpart of the
deterministic mock client. It streams one model turn from a **configurable,
local (loopback or private-network) OpenAI-compatible endpoint** — the vLLM
container started by the instance bootstrap — behind the exact same
:class:`~blackwell_lab.workload.model_client.ModelClient` interface the agent
loop and runner already use, so the Phase 2 timing, evaluator, accounting,
and evidence contracts are preserved unchanged.

Truthfulness rules (measurement contract; decisions D-0010/D-0011):

- **Transport chunks are never tokens.** SSE delta fragments are emitted as
  ``content_chunk`` events only. This client NEVER emits ``token`` events,
  because the OpenAI-compatible streaming protocol carries no true per-token
  timing — so inter-token latency is recorded as unavailable, never derived
  from chunk arrival times.
- **Token counts come only from authoritative usage data.** The request sets
  ``stream_options.include_usage``; the server-reported ``usage`` payload is
  surfaced as a ``usage`` event. When the endpoint reports none, downstream
  accounting records tokens as unavailable — chunk counts are never
  substituted.
- **Queue telemetry is never synthesized.** The OpenAI-compatible stream does
  not carry per-request engine queue time, so this client emits no
  ``queue_telemetry`` events. Engine-level queue metrics are collected
  separately (serving-engine metrics endpoint) only when genuinely available.
- **Deadlines are honored.** ``stream_turn`` receives the task's monotonic
  deadline; the HTTP read timeout is clamped to the remaining budget and the
  deadline is checked between stream events, raising
  :class:`~blackwell_lab.workload.model_client.ModelClientTimeout` instead of
  blocking past it.
- **Sanitized failures.** Errors carry status codes and generic causes only —
  never URLs with embedded credentials, request bodies, or raw payload text.

The endpoint must be local: loopback, RFC 1918/RFC 4193 private, or
link-local addresses only. Genuine benchmark traffic goes to the serving
container on the same host (or its private network); this client refuses
public endpoints so a misconfiguration cannot leak benchmark prompts to an
external service. An optional API key is read from a named environment
variable at request time and is never stored, printed, or echoed.
"""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Sequence

from blackwell_lab.workload.clock import SYSTEM_CLOCK, Clock
from blackwell_lab.workload.model_client import (
    GenerationSettings,
    Message,
    ModelClient,
    ModelClientError,
    ModelClientTimeout,
    NativeToolCall,
    StreamEvent,
)
from blackwell_lab.workload.native_tools import (
    TOOL_CHOICE,
    ToolCallAssembler,
    openai_tool_definitions,
)

OPENAI_CLIENT_VERSION = "2.0.0"

#: Hostnames always accepted as local.
_LOCAL_HOSTNAMES = frozenset({"localhost"})

#: Default read timeout when no task deadline applies (seconds).
DEFAULT_READ_TIMEOUT_S = 120.0

#: A transport takes (url, body_bytes, headers, timeout_s) and returns an
#: iterator of raw response lines (bytes). Injectable for offline tests.
Transport = Callable[[str, bytes, dict, float], Iterator[bytes]]


class EndpointConfigError(ValueError):
    """The endpoint configuration is unsafe or malformed."""


def validate_local_base_url(base_url: str) -> str:
    """Validates that ``base_url`` targets a local/private serving endpoint.

    Accepts http(s) URLs whose host is a loopback name/address, an RFC 1918
    or RFC 4193 private address, or a link-local address. Returns the URL
    with any trailing slash trimmed. Raises :class:`EndpointConfigError`
    otherwise (message never echoes credentials embedded in the URL).
    """
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in ("http", "https"):
        raise EndpointConfigError("endpoint base URL must use http or https")
    if parsed.username or parsed.password:
        raise EndpointConfigError(
            "endpoint base URL must not embed credentials; use the API-key "
            "environment variable instead"
        )
    host = parsed.hostname or ""
    if not host:
        raise EndpointConfigError("endpoint base URL has no host")
    if host not in _LOCAL_HOSTNAMES:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise EndpointConfigError(
                "endpoint host must be a loopback name or a literal "
                "loopback/private/link-local IP address (public or unresolved "
                "hostnames are refused: benchmark traffic never leaves the host "
                "or its private network)"
            ) from exc
        if not (address.is_loopback or address.is_private or address.is_link_local):
            raise EndpointConfigError(
                "endpoint address is public; genuine benchmark traffic must "
                "target the local serving container only"
            )
    return base_url.rstrip("/")


def _urllib_transport(url: str, body: bytes, headers: dict, timeout_s: float) -> Iterator[bytes]:
    """Default transport: a streaming POST via urllib (stdlib only)."""
    # S310: the URL was validated by validate_local_base_url (http/https to a
    # loopback or private host only) before any transport call.
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    response = urllib.request.urlopen(request, timeout=timeout_s)  # noqa: S310
    try:
        yield from response
    finally:
        response.close()


class OpenAICompatibleClient(ModelClient):
    """Streams one turn from an OpenAI-compatible ``/v1/chat/completions``.

    Stateless per request (thread-safe: the runner issues concurrent tasks
    against one instance). Each request carries the deterministic OpenAI
    ``tools`` collection. The first streamed tool-call fragment yields a
    privacy-safe ``native_tool_call_delta`` timing event (no identifiers or
    arguments). Fragments are then assembled into exactly one executable
    ``native_tool_call``; tool results are returned as ``role=tool``
    messages with the matching ``tool_call_id``. The retired ``TOOL_CALL:``
    text protocol is never accepted.
    """

    name = "openai-compatible"
    version = OPENAI_CLIENT_VERSION

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key_env: str | None = None,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        transport: Transport | None = None,
    ) -> None:
        if not model or not model.strip():
            raise EndpointConfigError("a served model name is required")
        if read_timeout_s <= 0:
            raise EndpointConfigError("read_timeout_s must be > 0")
        self._base_url = validate_local_base_url(base_url)
        self._model = model
        self._api_key_env = api_key_env
        self._read_timeout_s = read_timeout_s
        self._transport = transport or _urllib_transport

    @property
    def chat_completions_url(self) -> str:
        return f"{self._base_url}/chat/completions"

    def stream_turn(
        self,
        messages: Sequence[Message],
        settings: GenerationSettings,
        *,
        deadline: float | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> Iterator[StreamEvent]:
        timeout_s = self._read_timeout_s
        if deadline is not None:
            remaining = deadline - clock.monotonic()
            if remaining <= 0:
                raise ModelClientTimeout("turn deadline elapsed before the request was sent")
            timeout_s = min(timeout_s, remaining)

        body = json.dumps(self._request_body(messages, settings)).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        api_key = os.environ.get(self._api_key_env) if self._api_key_env else None
        if api_key:
            # Attached to the request only; never stored, printed, or echoed.
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            lines = self._transport(self.chat_completions_url, body, headers, timeout_s)
            yield from self._parse_stream(lines, deadline=deadline, clock=clock)
        except (ModelClientTimeout, ModelClientError):
            raise
        except urllib.error.HTTPError as exc:
            # Sanitized: status code only, never the response body or URL.
            raise ModelClientError(f"endpoint returned HTTP {exc.code}") from None
        except TimeoutError:
            if deadline is not None and clock.monotonic() >= deadline:
                raise ModelClientTimeout("turn deadline elapsed waiting on the endpoint") from None
            raise ModelClientError("endpoint read timed out below the task deadline") from None
        except (urllib.error.URLError, ConnectionError, OSError):
            raise ModelClientError("endpoint unreachable (connection failed)") from None

    # -- request/stream details ------------------------------------------

    def _request_body(self, messages: Sequence[Message], settings: GenerationSettings) -> dict:
        body: dict = {
            "model": self._model,
            "messages": [self._wire_message(m) for m in messages],
            "temperature": settings.temperature,
            "top_p": settings.top_p,
            "max_tokens": settings.max_tokens,
            "stream": True,
            # Authoritative token counts: the final stream chunk carries usage.
            "stream_options": {"include_usage": True},
            "tools": openai_tool_definitions(),
            # Official NIM/vLLM 0.27.1 pairing for Nemotron 3.5 Lightning.
            "tool_choice": TOOL_CHOICE,
            "parallel_tool_calls": False,
        }
        if settings.seed is not None:
            body["seed"] = settings.seed
        return body

    @staticmethod
    def _wire_message(message: Message) -> dict:
        if message.role == "tool":
            if not message.tool_call_id:
                raise ModelClientError("tool result is missing its tool_call_id")
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
        if message.role == "assistant" and message.tool_calls:
            return {
                "role": "assistant",
                "content": message.content or None,
                "tool_calls": [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, sort_keys=True),
                        },
                    }
                    for call in message.tool_calls
                ],
            }
        if message.role in ("system", "user", "assistant"):
            return {"role": message.role, "content": message.content}
        raise ModelClientError(f"unsupported message role: {message.role!r}")

    def _parse_stream(
        self,
        lines: Iterator[bytes],
        *,
        deadline: float | None,
        clock: Clock,
    ) -> Iterator[StreamEvent]:
        assembler = ToolCallAssembler()
        for raw in lines:
            if deadline is not None and clock.monotonic() >= deadline:
                raise ModelClientTimeout("turn deadline elapsed mid-stream")
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                raise ModelClientError(
                    "endpoint sent a malformed stream payload (not valid JSON)"
                ) from None
            if not isinstance(event, dict):
                raise ModelClientError("endpoint sent a malformed stream payload (not an object)")

            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                if isinstance(delta, dict):
                    yield from assembler.consume_delta(delta)

            usage = event.get("usage")
            if isinstance(usage, dict):
                completion_tokens = usage.get("completion_tokens")
                if isinstance(completion_tokens, bool) or not isinstance(completion_tokens, int):
                    raise ModelClientError(
                        "endpoint usage payload is malformed (completion_tokens "
                        "is not an integer); refusing to fabricate token counts"
                    )
                assembler.observe_usage(completion_tokens)
                yield StreamEvent(kind="usage", output_tokens=completion_tokens)

        assembled = assembler.finalize()
        if isinstance(assembled, NativeToolCall):
            yield StreamEvent(kind="native_tool_call", tool_call=assembled)
            return
        raise assembled
