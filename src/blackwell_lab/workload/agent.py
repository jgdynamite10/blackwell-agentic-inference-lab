"""Multi-turn agent loop for the synthetic Cloud Operations Agent.

The loop drives one task (one seeded instance of an incident scenario)
end-to-end: it streams typed model events, validates and executes tool calls
(recording a complete tool trace), and stops when the agent submits a
terminal recommendation, errs, or times out.

Timing semantics (decision D-0010; measurement contract §1/§2):

- **Start boundary** — the driver's actual submission instant
  (``submitted_at``, stamped when the driver enqueues the task, before any
  worker dequeues it). Queue wait inside the driver is part of task duration.
- **End boundary** — the terminal outcome stamp taken as the task record is
  completed; the runner hands that record to the evaluator synchronously and
  immediately, with no buffering in between, so the documented boundary and
  the implementation agree.
- Simulated tool delays are **consumed** through the injected clock: they
  occupy task duration, timeout budget, and wall time.
- The per-task deadline is ``submitted_at + timeout_s`` and is propagated to
  the model client, which must honor it (returning close to the deadline).

Retry policy: ``RETRIES = 0`` for measurement runs — failures are visible,
not hidden (measurement contract §7).

Error taxonomy (recorded per task and aggregated into the result record).
These are **execution errors**, disjoint from quality failure (a completed
task that fails evaluator gates is ``quality_failed`` in accounting, not an
execution error):

==============================  =================================================
Category                        Meaning
==============================  =================================================
``endpoint_error``              The model client raised a client error while
                                producing a turn.
``malformed_tool_call``         The turn's tool-call line could not be parsed
                                as JSON, or lacked the required structure.
``invalid_tool_name``           The tool call named a tool that does not exist.
``invalid_tool_arguments``      The tool exists but the arguments violate its
                                contract (missing/unexpected/ill-typed/empty).
``no_terminal_recommendation``  The agent exhausted ``max_turns`` without ever
                                calling ``recommend_remediation``.
``task_timeout``                The per-task deadline (profile-specific)
                                elapsed before a terminal recommendation.
``agent_runtime_error``         An unexpected exception in the client, a tool,
                                or the loop itself; sanitized to the category
                                only (no exception text is retained) so one
                                task cannot abort or contaminate a repetition.
==============================  =================================================
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from blackwell_lab.workload.clock import SYSTEM_CLOCK, Clock
from blackwell_lab.workload.model_client import (
    TOOL_CALL_PREFIX,
    GenerationSettings,
    Message,
    ModelClient,
    ModelClientError,
    ModelClientTimeout,
)
from blackwell_lab.workload.sampling import TaskInstance
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

#: Documented execution-error categories (see module docstring). Quality
#: failure is NOT an execution error and never appears here.
ERROR_TAXONOMY = (
    "endpoint_error",
    "malformed_tool_call",
    "invalid_tool_name",
    "invalid_tool_arguments",
    "no_terminal_recommendation",
    "task_timeout",
    "agent_runtime_error",
)

DEFAULT_MAX_TURNS = 12

#: Reason recorded whenever a client provides no true per-token timing.
ITL_UNAVAILABLE_NO_TOKEN_EVENTS = (
    "client emitted no true token events; transport chunks are not tokens"  # noqa: S105
)

#: Reason recorded whenever a client provides no authoritative usage data.
TOKENS_UNAVAILABLE_NO_USAGE = (
    "no authoritative usage data or exact-tokenizer count; chunk counts are "
    "never reported as token counts"
)


@dataclass(frozen=True)
class TurnRecord:
    """Driver-side record of one model turn (monotonic-clock durations).

    ``ttft_ms`` is measured at the first non-empty content-bearing event.
    ``inter_token_gaps_ms`` is populated only from true ``token`` events;
    when a client (like the mock) emits only transport chunks, ITL is
    unavailable and ``itl_unavailable_reason`` says why. ``output_tokens``
    comes only from authoritative ``usage`` events, never from chunk counts.
    """

    ttft_ms: float | None
    serving_time_ms: float
    chunk_count: int
    content_chars: int
    inter_token_gaps_ms: tuple[float, ...] = ()
    itl_available: bool = False
    itl_unavailable_reason: str | None = ITL_UNAVAILABLE_NO_TOKEN_EVENTS
    output_tokens: int | None = None
    tokens_unavailable_reason: str | None = TOKENS_UNAVAILABLE_NO_USAGE
    engine_queue_time_ms: float | None = None


@dataclass(frozen=True)
class ToolTrace:
    """One validated tool invocation: tool, arguments, result, latency."""

    tool: str
    arguments: dict
    result: dict
    simulated_latency_ms: float


@dataclass
class TaskExecution:
    """The complete record of one task attempt."""

    scenario_id: str
    instance_id: str = ""
    instance_seed: int | None = None
    status: str = "error"  # "completed" | "error" | "timeout"
    error_category: str | None = None
    diagnosis_id: str | None = None
    rationale: str | None = None
    remediation_id: str | None = None
    tool_trace: list[ToolTrace] = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)
    e2e_ms: float = 0.0
    queue_wait_ms: float = 0.0
    submitted_at_utc: str = ""
    started_at_utc: str = ""
    ended_at_utc: str = ""

    @property
    def tools_used(self) -> list[str]:
        return [t.tool for t in self.tool_trace]

    @property
    def tool_latencies_ms(self) -> list[float]:
        return [t.simulated_latency_ms for t in self.tool_trace]


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
        "Diagnose the incident using only your tools, then submit exactly one "
        f"recommendation via {TERMINAL_TOOL} with a diagnosis_id chosen from the "
        "published candidate list, a short rationale, and a remediation_id. "
        f"Available tools:\n{tool_lines}\n"
        "Reply each turn with a single line starting with "
        f'{TOOL_CALL_PREFIX} {{"tool": ..., "arguments": ...}}'
    )


def _task_prompt(scenario: Scenario, instance: TaskInstance | None) -> str:
    candidates = "\n".join(f"- {d}" for d in scenario.candidate_diagnoses)
    surface = f"{instance.surface_variant_text()}\n" if instance is not None else ""
    return (
        f"scenario_id: {scenario.scenario_id}\n"
        f"incident_class: {scenario.incident_class}\n"
        f"title: {scenario.title}\n"
        f"{surface}\n"
        f"{scenario.description}\n\n"
        f"Candidate diagnosis ids (submit exactly one):\n{candidates}"
    )


def run_task(
    scenario: Scenario,
    client: ModelClient,
    toolbox: SimulatedToolbox,
    settings: GenerationSettings,
    *,
    timeout_s: float,
    max_turns: int = DEFAULT_MAX_TURNS,
    clock: Clock = SYSTEM_CLOCK,
    instance: TaskInstance | None = None,
    submitted_at: float | None = None,
) -> TaskExecution:
    """Executes one task instance end-to-end and returns its record.

    ``submitted_at`` is the driver's submission instant on ``clock``'s
    monotonic timeline; when omitted (direct calls, tests) submission and
    start coincide. The deadline is ``submitted_at + timeout_s``: driver
    queue wait consumes timeout budget. All durations are monotonic-clock
    deltas; wall-clock UTC timestamps are recorded for correlation only.
    """
    started_at = clock.monotonic()
    submitted = submitted_at if submitted_at is not None else started_at
    execution = TaskExecution(
        scenario_id=scenario.scenario_id,
        instance_id=instance.instance_id if instance is not None else "",
        instance_seed=instance.instance_seed if instance is not None else None,
        status="error",
    )
    execution.submitted_at_utc = datetime.now(timezone.utc).isoformat()
    execution.started_at_utc = execution.submitted_at_utc
    execution.queue_wait_ms = max(0.0, (started_at - submitted) * 1000.0)
    deadline = submitted + timeout_s

    messages: list[Message] = [
        Message("system", _system_prompt(scenario)),
        Message("user", _task_prompt(scenario, instance)),
    ]

    def finish(status: str, error_category: str | None = None) -> TaskExecution:
        # Terminal stamp: the runner hands this record to the evaluator
        # synchronously and immediately after return (no buffering), so this
        # stamp is the documented end boundary.
        execution.status = status
        execution.error_category = error_category
        execution.e2e_ms = (clock.monotonic() - submitted) * 1000.0
        execution.ended_at_utc = datetime.now(timezone.utc).isoformat()
        return execution

    for _turn in range(max_turns):
        if clock.monotonic() >= deadline:
            return finish("timeout", "task_timeout")

        # --- one model turn, streamed and timed at the driver boundary ---
        dispatch = clock.monotonic()
        chunks: list[str] = []
        first_content_at: float | None = None
        token_times: list[float] = []
        usage_tokens: int | None = None
        engine_queue_ms: float | None = None
        try:
            events = client.stream_turn(messages, settings, deadline=deadline, clock=clock)
            for event in events:
                now = clock.monotonic()
                if event.kind in ("content_chunk", "token") and event.text:
                    if first_content_at is None:
                        first_content_at = now
                    chunks.append(event.text)
                if event.kind == "token":
                    token_times.append(now)
                elif event.kind == "usage" and event.output_tokens is not None:
                    usage_tokens = event.output_tokens
                elif event.kind == "queue_telemetry" and event.queue_time_ms is not None:
                    engine_queue_ms = event.queue_time_ms
                if now >= deadline:
                    return finish("timeout", "task_timeout")
        except ModelClientTimeout:
            return finish("timeout", "task_timeout")
        except ModelClientError:
            # retries=0: an endpoint failure fails the task visibly.
            return finish("error", "endpoint_error")
        except Exception:
            # An unexpected client exception must not abort the repetition;
            # it is recorded as a sanitized category with no exception text.
            return finish("error", "agent_runtime_error")
        turn_end = clock.monotonic()

        turn_text = "".join(chunks)
        gaps = tuple(
            (token_times[i] - token_times[i - 1]) * 1000.0 for i in range(1, len(token_times))
        )
        itl_available = len(token_times) >= 2
        execution.turns.append(
            TurnRecord(
                ttft_ms=(
                    (first_content_at - dispatch) * 1000.0 if first_content_at is not None else None
                ),
                serving_time_ms=(turn_end - dispatch) * 1000.0,
                chunk_count=len(chunks),
                content_chars=len(turn_text),
                inter_token_gaps_ms=gaps if itl_available else (),
                itl_available=itl_available,
                itl_unavailable_reason=(None if itl_available else ITL_UNAVAILABLE_NO_TOKEN_EVENTS),
                output_tokens=usage_tokens,
                tokens_unavailable_reason=(
                    None if usage_tokens is not None else TOKENS_UNAVAILABLE_NO_USAGE
                ),
                engine_queue_time_ms=engine_queue_ms,
            )
        )
        messages.append(Message("assistant", turn_text))

        if clock.monotonic() >= deadline:
            return finish("timeout", "task_timeout")

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
        except Exception:
            return finish("error", "agent_runtime_error")

        execution.tool_trace.append(
            ToolTrace(
                tool=result.tool,
                arguments=dict(call["arguments"]),
                result=result.payload,
                simulated_latency_ms=result.simulated_latency_ms,
            )
        )

        if result.tool == TERMINAL_TOOL:
            execution.diagnosis_id = call["arguments"]["diagnosis_id"]
            execution.rationale = call["arguments"]["rationale"]
            execution.remediation_id = call["arguments"]["remediation_id"]
            return finish("completed")

        # Tool execution consumed simulated latency; the deadline applies.
        if clock.monotonic() >= deadline:
            return finish("timeout", "task_timeout")

        messages.append(
            Message("tool", json.dumps({"tool": result.tool, "result": result.payload}))
        )

    return finish("error", "no_terminal_recommendation")
