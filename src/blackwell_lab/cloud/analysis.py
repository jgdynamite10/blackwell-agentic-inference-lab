"""Read-only analysis of an existing qualification result (decision D-0031).

``blackwell-cloud analyze-qualification`` explains *why* a development
qualification scored the way it did without touching a provider, an
endpoint, a model client, or a stream, and without modifying any source
artifact. It reads one persisted ``*.observations.json`` under
``LAB_RESULTS_DIR/qualification-runs/<label>/``, re-applies the public
binary evaluator to every recorded task, and reports:

* the successful incidents (plain-English catalog title, accepted diagnosis
  and remediation identifiers, and the tool path that produced the
  evidence);
* for every task, which gates failed, as evaluator gate identifiers;
* aggregate gate-failure counts, distinguishing task *completion* (a
  terminal recommendation was produced) from *correctness* (every gate
  passed);
* an explicit statement that the 0.40 development floor means 40 percent.

Every input path component is validated before any file access, and the
optional written report lands only under the external private results
directory. The rendered output carries no private paths, raw prompts, model
output, rationale text, tool payloads, search queries, or receipt digests.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from blackwell_lab.cloud import qualification
from blackwell_lab.cloud.artifacts import write_private_json
from blackwell_lab.workload.agent import TaskExecution, ToolTrace
from blackwell_lab.workload.evaluator import EVALUATOR_VERSION, evaluate
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.validation import ConfigError

ANALYSIS_FAMILY = "qualification-analysis"
ANALYSIS_SCHEMA_VERSION = "1.0.0"
FLOOR_NOTE = (
    "The development quality floor 0.40 is a fraction: it means 40 percent of "
    "measured tasks (eight of twenty) must pass every gate. It does not mean 0.4 percent."
)


class AnalysisError(RuntimeError):
    """Fail-closed analysis refusal (content-free)."""


def _safe_label(run_label: str, candidate_id: str, stage: str) -> str:
    try:
        qualification.require_safe_run_label(run_label)
    except ConfigError as exc:
        raise AnalysisError("qualification run label is malformed") from exc
    if candidate_id not in qualification.ALL_AUTHORIZED_CANDIDATES:
        raise AnalysisError(qualification.UNKNOWN_CANDIDATE_MESSAGE)
    if stage not in qualification.AUTHORIZED_STAGES:
        raise AnalysisError("qualification stage must be development, holdout, or freeze")
    try:
        return qualification.output_label(run_label, stage, candidate_id)
    except (ConfigError, qualification.QualificationError) as exc:
        raise AnalysisError("composed qualification output label is unsafe") from exc


def result_directory(results_dir: Path, label: str) -> Path:
    """The cell directory, proven to lie directly inside the qualification family.

    The label has already passed the run-label regex, so it cannot carry a
    separator or traversal component; the resolve check is a second,
    independent guard against symlinked escapes.
    """
    family = results_dir / qualification.QUALIFICATION_ARTIFACT_FAMILY
    cell = family / label
    if cell.is_symlink():
        raise AnalysisError("qualification result directory must not be a symlink")
    if not cell.is_dir():
        raise AnalysisError("no qualification result directory exists for this label")
    try:
        resolved = cell.resolve(strict=True)
        family_resolved = family.resolve(strict=True)
    except OSError as exc:
        raise AnalysisError("qualification result directory is unreadable") from exc
    if resolved.parent != family_resolved or resolved.name != label:
        raise AnalysisError("qualification result directory escapes the results directory")
    return cell


def _observations(cell: Path) -> list[dict]:
    files = sorted(path for path in cell.glob("*.observations.json") if path.is_file())
    if len(files) != 1:
        raise AnalysisError("expected exactly one observations file in the result directory")
    if files[0].is_symlink():
        raise AnalysisError("observations file must not be a symlink")
    try:
        payload = json.loads(files[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisError("observations file is unreadable") from exc
    observations = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(observations, list) or not observations:
        raise AnalysisError("observations file carries no task observations")
    return observations


def _execution(observation: dict, scenario) -> TaskExecution:
    trace = [
        ToolTrace(
            tool=str(entry.get("tool", "")),
            arguments=dict(entry.get("arguments") or {}),
            result=dict(entry.get("result") or {}),
            simulated_latency_ms=int(entry.get("simulated_latency_ms") or 0),
        )
        for entry in observation.get("tool_trace") or []
        if isinstance(entry, dict)
    ]
    terminal = observation.get("terminal") or {}
    return TaskExecution(
        scenario_id=scenario.scenario_id,
        instance_id=str(observation.get("instance_id", "")),
        status=str(observation.get("status", "")),
        error_category=observation.get("error_category"),
        diagnosis_id=terminal.get("diagnosis_id"),
        rationale=None,
        remediation_id=terminal.get("remediation_id"),
        tool_trace=trace,
    )


def _task_row(observation: dict, scenario) -> dict[str, Any]:
    execution = _execution(observation, scenario)
    evaluation = evaluate(scenario, execution)
    gates = {gate.gate_id: gate.passed for gate in evaluation.gates}
    completed = execution.status == "completed"
    trace = execution.tool_trace
    searches = [entry for entry in trace if entry.tool == "search_logs"]
    zero_match = sum(1 for entry in searches if int(entry.result.get("total_matches") or 0) == 0)
    usable_search = sum(1 for entry in searches if int(entry.result.get("total_matches") or 0) > 0)
    runbooks = [entry for entry in trace if entry.tool == "retrieve_runbook"]
    correct_runbook = any(entry.arguments.get("key") in scenario.runbooks for entry in runbooks)
    returned: set[str] = set()
    for entry in runbooks:
        if entry.result.get("found") is True:
            runbook = entry.result.get("runbook") or {}
            returned.update(str(item) for item in runbook.get("remediation_ids") or [])
    remediation_in_runbook = (
        execution.remediation_id in returned if execution.remediation_id else None
    )
    failed_gates = sorted(gate_id for gate_id, passed in gates.items() if not passed)
    if not completed:
        outcome = "errored"
    elif evaluation.success:
        outcome = "passed"
    else:
        outcome = "completed_but_failed_gates"
    return {
        "task_index": observation.get("task_index"),
        "template_id": scenario.scenario_id,
        "incident_class": scenario.incident_class,
        "title": scenario.title,
        "outcome": outcome,
        "completed": completed,
        "correct": bool(evaluation.success),
        "error_category": observation.get("error_category"),
        "failed_gates": failed_gates,
        "diagnosis_correct": gates.get("diagnosis"),
        "remediation_correct": gates.get("remediation"),
        "diagnosis_id": execution.diagnosis_id if completed else None,
        "remediation_id": execution.remediation_id if completed else None,
        "tool_path": [entry.tool for entry in trace],
        "turns": len(observation.get("turns") or []),
        "zero_match_searches": zero_match,
        "usable_searches": usable_search,
        "correct_runbook_retrieved": correct_runbook,
        "remediation_in_retrieved_runbook": remediation_in_runbook,
    }


def analyze(
    *,
    results_dir: Path,
    run_label: str,
    candidate_id: str,
    stage: str = qualification.STAGE_DEVELOPMENT,
    write_report: bool = False,
) -> dict[str, Any]:
    """Analyze one persisted qualification cell. Read-only on the source."""
    label = _safe_label(run_label, candidate_id, stage)
    cell = result_directory(results_dir, label)
    observations = _observations(cell)
    scenarios = catalog()
    rows: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, dict):
            raise AnalysisError("observations file carries a malformed task observation")
        template_id = observation.get("template_id")
        scenario = scenarios.get(str(template_id))
        if scenario is None:
            raise AnalysisError("observation references a template outside the public catalog")
        rows.append(_task_row(observation, scenario))

    passed = [row for row in rows if row["correct"]]
    completed = [row for row in rows if row["completed"]]
    gate_failures: Counter[str] = Counter()
    for row in rows:
        gate_failures.update(row["failed_gates"])
    spec = qualification.stage_spec(stage)
    floor = spec["quality_floor"]
    tasks = len(rows)
    report = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "kind": "qualification-analysis",
        "read_only": True,
        "provider_contacted": False,
        "model_client_constructed": False,
        "evaluator_version": EVALUATOR_VERSION,
        "candidate_id": candidate_id,
        "stage": stage,
        "run_label": label,
        "tasks": tasks,
        "completion": {
            "completed_tasks": len(completed),
            "errored_tasks": tasks - len(completed),
            "error_categories": dict(
                Counter(str(row["error_category"]) for row in rows if not row["completed"])
            ),
            "note": (
                "Completion means a terminal recommendation was produced; it is not correctness."
            ),
        },
        "correctness": {
            "passed_tasks": len(passed),
            "completed_but_failed_gates": len(completed) - len(passed),
            "aggregate_quality": (len(passed) / tasks) if tasks else 0.0,
            "quality_floor": floor,
            "quality_floor_percent": (round(floor * 100) if floor is not None else None),
            "floor_met": (len(passed) / tasks >= floor) if (tasks and floor is not None) else None,
            "note": FLOOR_NOTE,
        },
        "successful_incidents": [
            {
                "task_index": row["task_index"],
                "title": row["title"],
                "incident_class": row["incident_class"],
                "diagnosis_id": row["diagnosis_id"],
                "remediation_id": row["remediation_id"],
                "evidence_path": row["tool_path"],
            }
            for row in passed
        ],
        "failed_gates_by_task": [
            {
                "task_index": row["task_index"],
                "title": row["title"],
                "outcome": row["outcome"],
                "error_category": row["error_category"],
                "failed_gates": row["failed_gates"],
            }
            for row in rows
            if not row["correct"]
        ],
        "aggregate_gate_failures": dict(sorted(gate_failures.items())),
        "workflow_signals": {
            "tasks_with_zero_match_search": sum(1 for r in rows if r["zero_match_searches"]),
            "zero_match_searches_total": sum(r["zero_match_searches"] for r in rows),
            "tasks_with_no_usable_search": sum(1 for r in rows if not r["usable_searches"]),
            "tasks_without_correct_runbook": sum(
                1 for r in rows if not r["correct_runbook_retrieved"]
            ),
            "remediation_outside_retrieved_runbook": sum(
                1 for r in rows if r["remediation_in_retrieved_runbook"] is False
            ),
            "diagnosis_gate_failures_among_completed": sum(
                1 for r in completed if r["diagnosis_correct"] is False
            ),
            "remediation_gate_failures_among_completed": sum(
                1 for r in completed if r["remediation_correct"] is False
            ),
            "turn_count_distribution": dict(sorted(Counter(r["turns"] for r in rows).items())),
        },
        "failures_by_template": {
            template: {
                "attempted": sum(1 for r in rows if r["template_id"] == template),
                "passed": sum(1 for r in rows if r["template_id"] == template and r["correct"]),
                "failed_gates": dict(
                    Counter(
                        gate
                        for r in rows
                        if r["template_id"] == template
                        for gate in r["failed_gates"]
                    )
                ),
            }
            for template in sorted({r["template_id"] for r in rows})
        },
        "tasks_detail": rows,
    }
    if write_report:
        target = results_dir / ANALYSIS_FAMILY / label / "analysis.json"
        write_private_json(target, report)
        report["report_written"] = True
    return report


def render(report: dict[str, Any], *, fmt: str = "text") -> str:
    if fmt == "json":
        return json.dumps(report, indent=2, sort_keys=False)
    lines: list[str] = []
    lines.append(
        f"Qualification analysis: candidate {report['candidate_id']} {report['stage']} "
        f"({report['tasks']} tasks; evaluator {report['evaluator_version']}; read-only)"
    )
    completion = report["completion"]
    correctness = report["correctness"]
    lines.append(
        f"Completion: {completion['completed_tasks']} completed, "
        f"{completion['errored_tasks']} errored {completion['error_categories'] or ''}".rstrip()
    )
    lines.append(
        f"Correctness: {correctness['passed_tasks']} passed every gate, "
        f"{correctness['completed_but_failed_gates']} completed but failed a gate; "
        f"aggregate quality {correctness['aggregate_quality']:.2f} vs floor "
        f"{correctness['quality_floor']} ({correctness['quality_floor_percent']}%): "
        f"{'met' if correctness['floor_met'] else 'NOT met'}"
    )
    lines.append(FLOOR_NOTE)
    lines.append("")
    lines.append("Successful incidents:")
    for item in report["successful_incidents"]:
        lines.append(
            f"  [{item['task_index']:>2}] {item['title']} -> diagnosis {item['diagnosis_id']}, "
            f"remediation {item['remediation_id']}; evidence path "
            + " > ".join(item["evidence_path"])
        )
    if not report["successful_incidents"]:
        lines.append("  (none)")
    lines.append("")
    lines.append("Failed gates by task:")
    for item in report["failed_gates_by_task"]:
        suffix = f" [{item['error_category']}]" if item["error_category"] else ""
        lines.append(
            f"  [{item['task_index']:>2}] {item['title']}: {item['outcome']}{suffix}; "
            f"failed gates: {', '.join(item['failed_gates']) or '-'}"
        )
    lines.append("")
    lines.append("Aggregate gate failures:")
    for gate, count in report["aggregate_gate_failures"].items():
        lines.append(f"  {gate}: {count}")
    signals = report["workflow_signals"]
    lines.append("")
    lines.append("Workflow signals:")
    for key, value in signals.items():
        lines.append(f"  {key}: {value}")
    return "\n".join(lines)
