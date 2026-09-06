"""The six simulated tools of the Cloud Operations Agent.

Tool responses are **deterministic functions of (scenario, query)**. Tool
latencies are simulated with the fixed, documented values below — they are
recorded as tool-execution time but never slept, so tool time is separable
from model-serving time (measurement contract §2) and offline runs stay fast
and reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from blackwell_lab.workload.scenarios import Scenario

#: Fixed simulated latency per tool, in milliseconds (documented values; see
#: methodology/workload-definition.md). These are recorded as the
#: tool-execution time of every invocation of that tool.
TOOL_LATENCY_MS: dict[str, float] = {
    "get_service_health": 5.0,
    "query_metrics": 20.0,
    "search_logs": 30.0,
    "retrieve_runbook": 10.0,
    "check_recent_changes": 15.0,
    "recommend_remediation": 5.0,
}

#: Argument contracts: name -> (required args, optional args), each mapping
#: argument name to the accepted Python type(s).
TOOL_SPECS: dict[str, dict[str, dict[str, type | tuple[type, ...]]]] = {
    "get_service_health": {"required": {}, "optional": {"service": str}},
    "query_metrics": {"required": {"metric": str}, "optional": {"window_s": int}},
    "search_logs": {"required": {"query": str}, "optional": {"limit": int}},
    "retrieve_runbook": {"required": {"key": str}, "optional": {}},
    "check_recent_changes": {"required": {}, "optional": {"window_s": int}},
    "recommend_remediation": {
        "required": {"root_cause": str, "remediation_id": str},
        "optional": {},
    },
}

#: The terminal tool: calling it ends the task with the agent's recommendation.
TERMINAL_TOOL = "recommend_remediation"


class ToolError(Exception):
    """Base class for tool-invocation failures."""


class InvalidToolNameError(ToolError):
    """The requested tool does not exist."""


class InvalidToolArgumentsError(ToolError):
    """The tool exists but the arguments violate its contract."""


@dataclass(frozen=True)
class ToolResult:
    """The outcome of one simulated tool invocation."""

    tool: str
    payload: dict[str, Any]
    simulated_latency_ms: float


def validate_tool_call(name: str, arguments: dict[str, Any]) -> None:
    """Validates a tool call against the tool registry, raising on violation."""
    spec = TOOL_SPECS.get(name)
    if spec is None:
        raise InvalidToolNameError(f"unknown tool: {name!r}")
    if not isinstance(arguments, dict):
        raise InvalidToolArgumentsError(f"{name}: arguments must be an object")
    allowed = {**spec["required"], **spec["optional"]}
    for arg in arguments:
        if arg not in allowed:
            raise InvalidToolArgumentsError(f"{name}: unexpected argument {arg!r}")
    for arg in spec["required"]:
        if arg not in arguments:
            raise InvalidToolArgumentsError(f"{name}: missing required argument {arg!r}")
    for arg, value in arguments.items():
        expected = allowed[arg]
        if not isinstance(value, expected):
            raise InvalidToolArgumentsError(
                f"{name}: argument {arg!r} has invalid type {type(value).__name__}"
            )


class SimulatedToolbox:
    """Executes the six simulated tools against one scenario's fixtures.

    ``log_limit`` and ``metric_window_s`` come from the workload profile
    (interactive surfaces compact context; batch-heavy surfaces more), so both
    profiles draw from the same catalog with identical success criteria.
    """

    def __init__(
        self,
        scenario: Scenario,
        *,
        log_limit: int = 10,
        metric_window_s: int = 900,
    ) -> None:
        self._scenario = scenario
        self._log_limit = log_limit
        self._metric_window_s = metric_window_s

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        validate_tool_call(name, arguments)
        handler = getattr(self, f"_tool_{name}")
        payload = handler(**arguments)
        return ToolResult(tool=name, payload=payload, simulated_latency_ms=TOOL_LATENCY_MS[name])

    # -- tool implementations (deterministic functions of scenario + query) --

    def _tool_get_service_health(self, service: str | None = None) -> dict[str, Any]:
        health = self._scenario.health
        if service is not None:
            return {"services": {service: health.get(service, {"status": "unknown-service"})}}
        return {"services": health}

    def _tool_query_metrics(self, metric: str, window_s: int | None = None) -> dict[str, Any]:
        series = self._scenario.metrics.get(metric)
        if series is None:
            return {"metric": metric, "found": False, "available": sorted(self._scenario.metrics)}
        window = window_s if window_s is not None else self._metric_window_s
        points = [p for p in series["points"] if p["t_offset_s"] >= series_end(series) - window]
        return {"metric": metric, "found": True, "unit": series["unit"], "points": points}

    def _tool_search_logs(self, query: str, limit: int | None = None) -> dict[str, Any]:
        effective_limit = limit if limit is not None else self._log_limit
        needle = query.casefold()
        matches = [line for line in self._scenario.logs if needle in line["message"].casefold()]
        return {
            "query": query,
            "total_matches": len(matches),
            "lines": matches[:effective_limit],
        }

    def _tool_retrieve_runbook(self, key: str) -> dict[str, Any]:
        runbook = self._scenario.runbooks.get(key)
        if runbook is None:
            return {"key": key, "found": False, "available": sorted(self._scenario.runbooks)}
        return {"key": key, "found": True, "runbook": runbook}

    def _tool_check_recent_changes(self, window_s: int | None = None) -> dict[str, Any]:
        changes = self._scenario.recent_changes
        if window_s is not None:
            changes = [c for c in changes if abs(c["t_offset_s"]) <= window_s]
        return {"changes": list(changes)}

    def _tool_recommend_remediation(self, root_cause: str, remediation_id: str) -> dict[str, Any]:
        return {
            "acknowledged": True,
            "root_cause": root_cause,
            "remediation_id": remediation_id,
        }


def series_end(series: dict[str, Any]) -> int:
    """The last time offset in a metric series (deterministic)."""
    return max(p["t_offset_s"] for p in series["points"])
