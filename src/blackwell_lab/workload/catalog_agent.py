"""Workload 2.9.0 agent loop, frozen from D-0034 before extending catalog coverage.

The turn loop is unchanged. Its prompt/rendering/controller dispatch selects
the full-catalog contract. Keeping this module separate preserves the W5/W6
renderer closure bytes and identities; it is not a new agent framework.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from blackwell_lab.workload.agent import (
    _OUTPUT_EVENT_KINDS,
    _TTFT_EVENT_KINDS,
    DEFAULT_MAX_TURNS,
    DIRECT_EVIDENCE_REQUIRED,
    SYSTEM_CLOCK,
    TERMINAL_TOOL,
    Clock,
    GenerationSettings,
    InvalidToolArgumentsError,
    InvalidToolNameError,
    Message,
    ModelClient,
    ModelClientError,
    ModelClientTimeout,
    NativeToolCall,
    NativeToolCallError,
    Scenario,
    SimulatedToolbox,
    TaskExecution,
    TaskInstance,
    ToolTrace,
    _completed_turn_record,
    _error_category_for_native,
    system_prompt,
    task_prompt,
)
from blackwell_lab.workload.catalog_runtime import build_controller
from blackwell_lab.workload.public_metadata.catalog_v2 import (
    STRUCTURED_FIELD_EXPLANATION,
    hypothesis_prompt,
)
from blackwell_lab.workload.tools import workflow_terminal_schema_is_correctable

# Workload 2.10.0 keeps the 2.4.1 prompt and adds the literal search_logs
# tokens the public evaluator already accepts. The sentence names no
# diagnosis, remediation, or scenario. It lives here, not in agent.py, so
# the W5/W6 renderer closure and identity digests stay pinned.
SEARCH_GUIDANCE_V210 = (
    "search_logs matches literal substrings. When gathering log evidence, "
    "include a literal query token the incident supports. The accepted literal "
    "tokens are: servfail, search domain, readiness, migration, startup, and dep-8102."
)
# Workload 2.11.0 keeps the 2.10.0 prompt and requires exactly one native
# tool call on every turn. The sentence names no diagnosis, remediation,
# or scenario.
SINGLE_CALL_GUIDANCE_V211 = (
    "Each turn must contain exactly one native tool call. Do not answer with "
    "prose alone, and do not emit more than one tool call in the same turn."
)


def _system_prompt(scenario, workload_version=None):
    prompt = system_prompt(scenario, "2.8.0")
    if workload_version == "2.10.0":
        return f"{prompt} {SEARCH_GUIDANCE_V210}"
    if workload_version == "2.11.0":
        return f"{prompt} {SEARCH_GUIDANCE_V210} {SINGLE_CALL_GUIDANCE_V211}"
    return prompt


def _task_prompt(scenario, instance, workload_version=None):
    return (
        task_prompt(scenario, instance, "2.7.0")
        + "\n\n"
        + STRUCTURED_FIELD_EXPLANATION
        + hypothesis_prompt(scenario)
    )


def _workflow_can_correct_terminal(controller, name, workload_version):
    return controller is not None and workflow_terminal_schema_is_correctable(name, "2.8.0")


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
    evidence_context: str | None = None,
) -> TaskExecution:
    """Executes one task instance end-to-end and returns its record.

    ``submitted_at`` is the driver's submission instant on ``clock``'s
    monotonic timeline; when omitted (direct calls, tests) submission and
    start coincide. The deadline is ``submitted_at + timeout_s``: driver
    queue wait consumes timeout budget. All durations are monotonic-clock
    deltas; wall-clock UTC timestamps are recorded for correlation only.

    ``evidence_context`` is the private context from which workload 2.5.0
    observation IDs are derived. It defaults to a fresh random token per
    task execution, so IDs are never shared across tasks, repetitions,
    scheduler workers, or re-executions of the same instance. The workload
    contract and its controller binding are resolved **before** the client
    is used; an unknown version never reaches the model.
    """
    from blackwell_lab.workload.native_tools import require_workload_version

    executed_version = require_workload_version(settings.workload_version)
    if executed_version not in {"2.9.0", "2.10.0", "2.11.0"}:
        from blackwell_lab.workload.validation import ConfigError

        raise ConfigError("the catalog agent requires workload 2.9.0, 2.10.0, or 2.11.0")
    controller = build_controller(executed_version, evidence_context or uuid.uuid4().hex)
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
        Message("system", _system_prompt(scenario, executed_version)),
        Message("user", _task_prompt(scenario, instance, executed_version)),
    ]

    def finish(status: str, error_category: str | None = None) -> TaskExecution:
        # Terminal stamp: the runner hands this record to the evaluator
        # synchronously and immediately after return (no buffering), so this
        # stamp is the documented end boundary.
        execution.status = status
        execution.error_category = error_category
        execution.e2e_ms = (clock.monotonic() - submitted) * 1000.0
        execution.ended_at_utc = datetime.now(timezone.utc).isoformat()
        if controller is not None:
            # Each controller family records its sanitized counts in its own
            # observation field (``evidence_grounding`` for 2.5.0,
            # ``workflow_control`` for 2.6.x), so existing documents and
            # schemas are untouched.
            summary_field = getattr(controller, "summary_field", "evidence_grounding")
            setattr(execution, summary_field, controller.summary())
        return execution

    note_turn = getattr(controller, "note_turn", None)
    for _turn in range(max_turns):
        if clock.monotonic() >= deadline:
            return finish("timeout", "task_timeout")
        if note_turn is not None:
            # Generic remaining-turn accounting for the workflow controller;
            # it adds no message and no tool call (one native call per turn).
            note_turn(_turn, max_turns)

        # --- one model turn, streamed and timed at the driver boundary ---
        dispatch = clock.monotonic()
        chunks: list[str] = []
        output_events = 0
        first_output_at: float | None = None
        token_times: list[float] = []
        usage_tokens: int | None = None
        engine_queue_ms: float | None = None
        native_calls: list[NativeToolCall] = []

        try:
            events = client.stream_turn(messages, settings, deadline=deadline, clock=clock)
            for event in events:
                now = clock.monotonic()
                if event.kind in _TTFT_EVENT_KINDS and first_output_at is None:
                    if event.kind != "token" or event.text:
                        first_output_at = now
                if event.kind in _OUTPUT_EVENT_KINDS:
                    output_events += 1
                if event.kind in ("content_chunk", "token") and event.text:
                    chunks.append(event.text)
                if event.kind == "native_tool_call" and event.tool_call is not None:
                    native_calls.append(event.tool_call)
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
        except NativeToolCallError as exc:
            execution.turns.append(
                _completed_turn_record(
                    dispatch=dispatch,
                    first_output_at=first_output_at,
                    turn_end=clock.monotonic(),
                    output_events=output_events,
                    content_chars=len("".join(chunks)),
                    token_times=token_times,
                    usage_tokens=usage_tokens,
                    engine_queue_ms=engine_queue_ms,
                )
            )
            execution.tool_call_diagnostics = exc.diagnostics
            return finish("error", _error_category_for_native(exc.category))
        except ModelClientError:
            # retries=0: an endpoint failure fails the task visibly.
            return finish("error", "endpoint_error")
        except Exception:
            # An unexpected client exception must not abort the repetition;
            # it is recorded as a sanitized category with no exception text.
            return finish("error", "agent_runtime_error")
        execution.turns.append(
            _completed_turn_record(
                dispatch=dispatch,
                first_output_at=first_output_at,
                turn_end=clock.monotonic(),
                output_events=output_events,
                content_chars=len("".join(chunks)),
                token_times=token_times,
                usage_tokens=usage_tokens,
                engine_queue_ms=engine_queue_ms,
            )
        )

        if clock.monotonic() >= deadline:
            return finish("timeout", "task_timeout")

        # Native tool calls only. The retired TOOL_CALL text protocol is
        # never accepted, even if content happens to contain that prefix.
        if len(native_calls) != 1:
            assembled_text = "".join(chunks)
            execution.tool_call_diagnostics = {
                "failure_category": (
                    "legacy_text_tool_call" if "TOOL_CALL:" in assembled_text else "zero_tool_calls"
                )
                if not native_calls
                else "parallel_or_multiple_tool_calls",
                "event_types": ["content"] if assembled_text else [],
                "tool_call_count": len(native_calls),
                "function_name_valid": None,
                "arguments_json_ok": None,
                "arguments_schema_ok": None,
                "content_chars": len(assembled_text),
                "usage_completion_tokens": usage_tokens,
            }
            return finish("error", "malformed_tool_call")
        call = native_calls[0]
        messages.append(
            Message("assistant", content="", tool_calls=(call,)),
        )
        try:
            result = toolbox.execute(call.name, call.arguments, workload_version=executed_version)
        except InvalidToolNameError:
            return finish("error", "invalid_tool_name")
        except InvalidToolArgumentsError:
            if not _workflow_can_correct_terminal(controller, call.name, executed_version):
                return finish("error", "invalid_tool_arguments")
            # Structurally invalid terminal arguments never run the handler
            # and are never accepted. The controller returns a typed
            # rejection and the loop continues under the same max_turns.
            verdict = controller.validate_terminal(call.arguments)
            if verdict.accepted:
                return finish("error", "invalid_tool_arguments")
            payload = controller.rejection_payload(verdict)
            latency_ms = toolbox.consume_latency(call.name)
            execution.tool_trace.append(
                ToolTrace(
                    tool=call.name,
                    arguments=dict(call.arguments),
                    result=payload,
                    simulated_latency_ms=latency_ms,
                )
            )
            if clock.monotonic() >= deadline:
                return finish("timeout", "task_timeout")
            messages.append(
                Message(
                    "tool",
                    content=json.dumps(payload, sort_keys=True),
                    tool_call_id=call.call_id,
                )
            )
            continue
        except Exception:
            return finish("error", "agent_runtime_error")

        payload = result.payload
        terminal_accepted = result.tool == TERMINAL_TOOL
        if controller is not None:
            try:
                if result.tool == TERMINAL_TOOL:
                    # Provenance and structure only; the verdict never
                    # names a reference or a reason.
                    verdict = controller.validate_terminal(call.arguments)
                    terminal_accepted = verdict.accepted
                    if not verdict.accepted:
                        rejection_payload = getattr(controller, "rejection_payload", None)
                        payload = (
                            rejection_payload(verdict)
                            if rejection_payload is not None
                            else verdict.tool_payload()
                        )
                else:
                    payload = controller.annotate(payload, controller.record(result.tool, payload))
            except Exception:
                return finish("error", "agent_runtime_error")

        execution.tool_trace.append(
            ToolTrace(
                tool=result.tool,
                arguments=dict(call.arguments),
                result=payload,
                simulated_latency_ms=result.simulated_latency_ms,
            )
        )

        # The deadline is enforced after EVERY tool, including the terminal
        # one: tool latency that reaches or crosses the deadline is a
        # task_timeout, never a completion.
        if clock.monotonic() >= deadline:
            return finish("timeout", "task_timeout")

        if terminal_accepted:
            execution.diagnosis_id = call.arguments["diagnosis_id"]
            execution.rationale = call.arguments["rationale"]
            execution.remediation_id = call.arguments["remediation_id"]
            return finish("completed")

        # A rejected terminal attempt returns the generic failure payload as
        # an ordinary tool message and the loop continues under the SAME
        # max_turns budget: no extra turn, retry, or hidden call is added.
        messages.append(
            Message(
                "tool",
                content=json.dumps(payload, sort_keys=True),
                tool_call_id=call.call_id,
            )
        )

    if controller is not None:
        mark_exhausted = getattr(controller, "mark_exhausted", None)
        if mark_exhausted is not None:
            mark_exhausted()
        exhaustion_category = getattr(controller, "exhaustion_error_category", None)
        if exhaustion_category is not None:
            # Workflow controller: a clear terminal failure that distinguishes
            # "never tried" from "tried and was rejected".
            return finish("error", exhaustion_category)
        if controller.rejected_terminal_attempts > 0:
            return finish("error", DIRECT_EVIDENCE_REQUIRED)
    return finish("error", "no_terminal_recommendation")
