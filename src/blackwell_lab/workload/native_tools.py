"""Native OpenAI-compatible tool-call schemas, assembly, and diagnostics.

The workload's six tools are described once in :data:`TOOL_SPECS`. This
module is the only place those contracts are projected onto the OpenAI
function-calling JSON Schema used on the wire. The OpenAI-compatible client
assembles streamed ``delta.tool_calls`` fragments here; the agent loop
consumes the resulting :class:`NativeToolCall` and never parses the retired
``TOOL_CALL:`` text protocol.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from blackwell_lab.workload.model_client import NativeToolCall, NativeToolCallError, StreamEvent
from blackwell_lab.workload.tools import TOOL_SPECS, validate_tool_call

#: Identity recorded in private gpu-mode manifests so native-tool results
#: cannot be confused with run-e's custom-text TOOL_CALL protocol.
TOOL_CALL_TRANSPORT = "openai-native-tools"
TOOL_CALL_PARSER = "qwen3_coder"
REASONING_PARSER = "nemotron_v3"

#: Official NVIDIA NIM / vLLM 0.27.1 pairing for Nemotron 3.5 Lightning:
#: ``tool_choice: "auto"`` requires ``--enable-auto-tool-choice`` and
#: ``--tool-call-parser qwen3_coder``. ``required`` is not the documented
#: pairing and is known to empty or corrupt streamed arguments with this
#: parser (vLLM PR 33965; NIM tool-calling guide).
TOOL_CHOICE = "auto"

#: Descriptions projected onto the OpenAI function definitions. Argument
#: contracts remain :data:`TOOL_SPECS`.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "get_service_health": (
        "Return the current synthetic health status for one named service "
        "or, when omitted, every service in the incident."
    ),
    "query_metrics": (
        "Return a synthetic metric time series. The metric name is required; "
        "an optional window_s limits how far back points are returned."
    ),
    "search_logs": (
        "Search synthetic service logs for a query string. An optional "
        "positive limit caps the number of returned lines."
    ),
    "retrieve_runbook": (
        "Retrieve a synthetic runbook by key, including published diagnosis "
        "candidates for this incident."
    ),
    "check_recent_changes": (
        "List recent synthetic change events. An optional window_s limits "
        "how far back changes are returned."
    ),
    "recommend_remediation": (
        "Submit the terminal recommendation: one published diagnosis_id, a "
        "short rationale, and one published remediation_id."
    ),
}

_PYTHON_TO_JSON_TYPE = {str: "string", int: "integer"}

_REASONING_DELTA_KEYS = ("reasoning", "reasoning_content", "reasoning_text")


def openai_tool_definitions() -> list[dict]:
    """Deterministic OpenAI ``tools`` array for every :data:`TOOL_SPECS` entry."""
    definitions: list[dict] = []
    for name in sorted(TOOL_SPECS):
        spec = TOOL_SPECS[name]
        properties: dict[str, dict[str, str]] = {}
        for argument, python_type in {**spec["required"], **spec["optional"]}.items():
            json_type = _PYTHON_TO_JSON_TYPE.get(python_type)
            if json_type is None:
                raise ValueError(f"unsupported TOOL_SPECS type for {name}.{argument}")
            properties[argument] = {"type": json_type}
        parameters = {
            "type": "object",
            "properties": properties,
            "required": sorted(spec["required"]),
            "additionalProperties": False,
        }
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": TOOL_DESCRIPTIONS[name],
                    "parameters": parameters,
                },
            }
        )
    return definitions


def sanitized_diagnostics(
    *,
    failure_category: str,
    event_types: list[str],
    tool_call_count: int,
    name_valid: bool | None,
    arguments_json_ok: bool | None,
    arguments_schema_ok: bool | None,
    content_chars: int,
    usage_completion_tokens: int | None,
) -> dict[str, Any]:
    """Structural diagnostics only — never prompts, completions, or paths."""
    return {
        "failure_category": failure_category,
        "event_types": list(event_types),
        "tool_call_count": tool_call_count,
        "function_name_valid": name_valid,
        "arguments_json_ok": arguments_json_ok,
        "arguments_schema_ok": arguments_schema_ok,
        "content_chars": content_chars,
        "usage_completion_tokens": usage_completion_tokens,
    }


@dataclass
class ToolCallAssembler:
    """Assembles streamed native ``delta.tool_calls`` fragments by index."""

    slots: dict[int, dict[str, str]] = field(default_factory=dict)
    event_types: list[str] = field(default_factory=list)
    content_chars: int = 0
    reasoning_chars: int = 0
    usage_completion_tokens: int | None = None
    saw_legacy_tool_call: bool = False

    def note(self, event_type: str) -> None:
        if event_type not in self.event_types:
            self.event_types.append(event_type)

    def observe_usage(self, completion_tokens: int) -> None:
        self.usage_completion_tokens = completion_tokens

    def consume_delta(self, delta: dict) -> list[StreamEvent]:
        """Yield immediate timing events; accumulate tool-call fragments."""
        events: list[StreamEvent] = []
        for key in _REASONING_DELTA_KEYS:
            reasoning = delta.get(key)
            if isinstance(reasoning, str) and reasoning:
                self.note("reasoning")
                self.reasoning_chars += len(reasoning)
                events.append(StreamEvent(kind="reasoning_chunk"))
                break
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.note("content")
            self.content_chars += len(content)
            if "TOOL_CALL:" in content:
                self.saw_legacy_tool_call = True
            events.append(StreamEvent(kind="content_chunk", text=content))
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            self.note("tool_calls")
            for fragment in tool_calls:
                if not isinstance(fragment, dict):
                    continue
                raw_index = fragment.get("index", 0)
                if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                    raw_index = 0
                slot = self.slots.setdefault(raw_index, {"id": "", "name": "", "arguments": ""})
                call_id = fragment.get("id")
                if isinstance(call_id, str) and call_id:
                    slot["id"] += call_id
                function = fragment.get("function") or {}
                if isinstance(function, dict):
                    name = function.get("name")
                    if isinstance(name, str) and name:
                        slot["name"] += name
                    arguments = function.get("arguments")
                    if isinstance(arguments, str) and arguments:
                        slot["arguments"] += arguments
        return events

    def finalize(self) -> NativeToolCall | NativeToolCallError:
        """Return a complete native call or a sanitized assembly error."""
        if self.saw_legacy_tool_call and not self.slots:
            return self._error("legacy_text_tool_call", tool_call_count=0)
        if not self.slots:
            return self._error("zero_tool_calls", tool_call_count=0)
        if len(self.slots) != 1:
            return self._error("parallel_or_multiple_tool_calls", tool_call_count=len(self.slots))
        if self.content_chars and self.slots:
            # Reasoning is allowed beside a tool call; executable content
            # mixed with a native call is ambiguous and is refused.
            return self._error("mixed_text_and_tool_call", tool_call_count=1)

        slot = self.slots[next(iter(self.slots))]
        call_id = slot["id"].strip()
        name = slot["name"].strip()
        raw_arguments = slot["arguments"]
        if not call_id:
            return self._error("missing_call_id", tool_call_count=1, name_valid=bool(name))
        if not name:
            return self._error("missing_function_name", tool_call_count=1)
        name_valid = name in TOOL_SPECS
        if not name_valid:
            return self._error("unknown_tool", tool_call_count=1, name_valid=False)
        try:
            parsed = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError:
            return self._error(
                "malformed_argument_json",
                tool_call_count=1,
                name_valid=True,
                arguments_json_ok=False,
            )
        if not isinstance(parsed, dict):
            return self._error(
                "malformed_argument_json",
                tool_call_count=1,
                name_valid=True,
                arguments_json_ok=False,
            )
        try:
            validate_tool_call(name, parsed)
        except Exception:
            return self._error(
                "invalid_arguments",
                tool_call_count=1,
                name_valid=True,
                arguments_json_ok=True,
                arguments_schema_ok=False,
            )
        return NativeToolCall(call_id=call_id, name=name, arguments=parsed)

    def _error(
        self,
        failure_category: str,
        *,
        tool_call_count: int,
        name_valid: bool | None = None,
        arguments_json_ok: bool | None = None,
        arguments_schema_ok: bool | None = None,
    ) -> NativeToolCallError:
        return NativeToolCallError(
            failure_category,
            sanitized_diagnostics(
                failure_category=failure_category,
                event_types=self.event_types,
                tool_call_count=tool_call_count,
                name_valid=name_valid,
                arguments_json_ok=arguments_json_ok,
                arguments_schema_ok=arguments_schema_ok,
                content_chars=self.content_chars,
                usage_completion_tokens=self.usage_completion_tokens,
            ),
        )
