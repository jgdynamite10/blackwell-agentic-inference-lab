"""Multi-turn agent loop for the synthetic Cloud Operations Agent.

The loop drives one task (one incident scenario) end-to-end: it streams model
turns, validates and executes tool calls, and stops when the agent submits a
terminal recommendation, errs, or times out.

Retry policy: ``RETRIES = 0`` for measurement runs — failures are visible, not
hidden (measurement contract §7).

Error taxonomy (recorded per task and aggregated into the result record):

==============================  =================================================
Category                        Meaning
==============================  =================================================
``endpoint_error``              The model client raised while producing a turn.
``malformed_tool_call``         The turn's tool-call line could not be parsed
                                as JSON, or lacked the required structure.
``invalid_tool_name``           The tool call named a tool that does not exist.
``invalid_tool_arguments``      The tool exists but the arguments violate its
                                contract (missing/unexpected/ill-typed).
``no_terminal_recommendation``  The agent exhausted ``max_turns`` without ever
                                calling ``recommend_remediation``.
``task_timeout``                The per-task deadline (profile-specific)
                                elapsed before a terminal recommendation.
==============================  =================================================
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from blackwell_lab.workload.model_client import (
    TOOL_CALL_PREFIX,
    GenerationSettings,
    Message,
    ModelClient,
    ModelClientError,
)
from blackwell_lab.workload.scenarios import Scenario
from blackwell_lab.workload.tools import (
    TERMINAL_TOOL,
    TOOL_SPECS,
    InvalidToolArgumentsError,
    InvalidToolNameError,
    SimulatedToolbox,
)

#: Retry policy for measurement runs (measurement contract §7).
RETRIES = 0

#: Documented error categories (see module docstring).
ERROR_TAXONOMY = (
    "endpoint_error",
    "malformed_tool_call",
    "invalid_tool_name",
    "invalid_tool_arguments",
    "no_terminal_recommendation",
    "task_timeout",
)

DEFAULT_MAX_TURNS = 12


@dataclass(frozen=True)
class TurnMetrics:
    """Client-side timings for one model turn (monotonic-clock durations)."""

    ttft_ms: float
    inter_token_gaps_ms: tuple[float, ...]
    serving_time_ms: float
    output_tokens: int


@dataclass
class TaskExecution:
    """The complete record of one task attempt."""

    scenario_id: str
    status: str  # "completed" | "error" | "timeout"
    error_category: str | None = None
    root_cause: str | None = None
    remediation_id: str | None = None
    tools_used: list[str] = field(default_factory=list)
    turn_metrics: list[TurnMetrics] = field(default_factory=list)
    tool_latencies_ms: list[float] = field(default_factory=list)
    e2e_ms: float = 0.0
    started_at_utc: str = ""
    ended_at_utc: str = ""


def _parse_tool_call(turn_text: str) -> dict:
    """Extracts and validates the structure of the turn's tool-call line.

    Raises ``ValueError`` (mapped to ``malformed_tool_call``) if the line is
    missing, is not valid JSON, or lacks the required shape.
    """
    call_lines = [line for line in turn_text.splitlines() if line.startswith(TOOL_CALL_PREFIX)]
    if not call_lines:
        raise ValueError("turn contains no TOOL_CALL line")
    if len(call_lines) > 1:
        raise ValueError("turn contains more than one TOOL_CALL line")
    raw = call_lines[0][len(TOOL_CALL_PREFIX) :].strip()
    try:
        call = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"tool call is not valid JSON: {exc.msg}") from exc
    if not isinstance(call, dict) or "tool" not in call or "arguments" not in call:
        raise ValueError('tool call must be an object with "tool" and "arguments"')
    if not isinstance(call["tool"], str):
        raise ValueError("tool name must be a string")
    return call


def _system_prompt(scenario: Scenario) -> str:
    tool_lines = "\n".join(f"- {name}" for name in TOOL_SPECS)
    return (
        "You are a Cloud Operations Agent working a synthetic incident. "
        "Diagnose the root cause using only your tools, then submit exactly one "
        f"remediation via {TERMINAL_TOOL}. Available tools:\n{tool_lines}\n"
        "Reply each turn with a single line starting with "
        f'{TOOL_CALL_PREFIX} {{"tool": ..., "arguments": ...}}'
    )


def _task_prompt(scenario: Scenario) -> str:
    return (
        f"scenario_id: {scenario.scenario_id}\n"
        f"incident_class: {scenario.incident_class}\n"
        f"title: {scenario.title}\n\n"
        f"{scenario.description}"
    )


def run_task(
    scenario: Scenario,
    client: ModelClient,
    toolbox: SimulatedToolbox,
    settings: GenerationSettings,
    *,
    timeout_s: float,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> TaskExecution:
    """Executes one incident scenario end-to-end and returns its record.

    All durations are monotonic-clock deltas; wall-clock UTC timestamps are
    recorded for correlation only (measurement contract §1).
    """
    execution = TaskExecution(scenario_id=scenario.scenario_id, status="error")
    execution.started_at_utc = datetime.now(timezone.utc).isoformat()
    task_start = time.monotonic()
    deadline = task_start + timeout_s

    messages: list[Message] = [
        Message("system", _system_prompt(scenario)),
        Message("user", _task_prompt(scenario)),
    ]

    def finish(status: str, error_category: str | None = None) -> TaskExecution:
        execution.status = status
        execution.error_category = error_category
        execution.e2e_ms = (time.monotonic() - task_start) * 1000.0
        execution.ended_at_utc = datetime.now(timezone.utc).isoformat()
        return execution

    for _turn in range(max_turns):
        if time.monotonic() >= deadline:
            return finish("timeout", "task_timeout")

        # --- one model turn, streamed and timed at the driver boundary ---
        dispatch = time.monotonic()
        token_times: list[float] = []
        chunks: list[str] = []
        try:
            for token in client.stream_turn(messages, settings):
                token_times.append(time.monotonic())
                chunks.append(token)
                if token_times[-1] >= deadline:
                    return finish("timeout", "task_timeout")
        except ModelClientError:
            # retries=0: an endpoint failure fails the task visibly.
            return finish("error", "endpoint_error")
        turn_end = time.monotonic()

        if time.monotonic() >= deadline:
            return finish("timeout", "task_timeout")

        turn_text = "".join(chunks)
        ttft_ms = ((token_times[0] - dispatch) * 1000.0) if token_times else 0.0
        gaps = tuple(
            (token_times[i] - token_times[i - 1]) * 1000.0 for i in range(1, len(token_times))
        )
        execution.turn_metrics.append(
            TurnMetrics(
                ttft_ms=ttft_ms,
                inter_token_gaps_ms=gaps,
                serving_time_ms=(turn_end - dispatch) * 1000.0,
                output_tokens=len(token_times),
            )
        )
        messages.append(Message("assistant", turn_text))

        # --- parse, validate, and execute the tool call (retries=0) ---
        try:
            call = _parse_tool_call(turn_text)
        except ValueError:
            return finish("error", "malformed_tool_call")
        try:
            result = toolbox.execute(call["tool"], call["arguments"])
        except InvalidToolNameError:
            return finish("error", "invalid_tool_name")
        except InvalidToolArgumentsError:
            return finish("error", "invalid_tool_arguments")

        execution.tools_used.append(result.tool)
        execution.tool_latencies_ms.append(result.simulated_latency_ms)

        if result.tool == TERMINAL_TOOL:
            execution.root_cause = call["arguments"]["root_cause"]
            execution.remediation_id = call["arguments"]["remediation_id"]
            return finish("completed")

        messages.append(
            Message("tool", json.dumps({"tool": result.tool, "result": result.payload}))
        )

    return finish("error", "no_terminal_recommendation")
