"""Read-only qualification analysis CLI (decision D-0031).

Adversarial path tests prove the command cannot read outside the authorized
result directory; read-only tests prove the source is never modified; the
offline tests prove no provider, endpoint, client, or stream is touched; and
the sanitization tests prove no private payload reaches the output.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
from pathlib import Path

import pytest
from fakes import FakeClock
from test_evidence_grounding import runbook, search, terminal

from blackwell_lab.cloud import analysis
from blackwell_lab.cloud.analysis import FLOOR_NOTE, AnalysisError, analyze, render
from blackwell_lab.cloud.cli import main
from blackwell_lab.workload import openai_client
from blackwell_lab.workload.agent import TaskExecution
from blackwell_lab.workload.evaluator import evaluate
from blackwell_lab.workload.model_client import GenerationSettings
from blackwell_lab.workload.runner import TaskOutcome, _observation_document
from blackwell_lab.workload.sampling import generate_task_instances
from blackwell_lab.workload.scenarios import catalog

RUN_LABEL = "qual-x"
LABEL = "qual-x-p1-development"
SECRET_RATIONALE = "RATIONALE-SECRET-TOKEN"  # noqa: S105 - a sentinel, not a credential
SECRET_QUERY = "QUERY-SECRET-TOKEN"  # noqa: S105 - a sentinel, not a credential


def _execution(scenario_id: str, *, good: bool) -> TaskExecution:
    from test_evidence_grounding import run

    scenario = catalog()[scenario_id]
    steps = (
        [*scenario.reference_tool_sequence, terminal(scenario=scenario, omit_refs=True)]
        if good
        else [
            runbook(scenario.affected_service),
            search(SECRET_QUERY),
            terminal(scenario=scenario, omit_refs=True),
        ]
    )
    execution, _ = run(
        steps,
        scenario_id=scenario_id,
        settings=GenerationSettings(workload_version="2.4.1"),
    )
    execution.rationale = SECRET_RATIONALE
    return execution


def _write_cell(results_dir: Path, *, label: str = LABEL, good: int = 2, bad: int = 2) -> Path:
    scenario_ids = list(catalog())[: good + bad]
    outcomes = []
    for index, scenario_id in enumerate(scenario_ids):
        execution = _execution(scenario_id, good=index < good)
        instance = generate_task_instances([scenario_id], 1, 11 + index)[0]
        execution.instance_id = instance.instance_id
        execution.instance_seed = instance.instance_seed
        outcomes.append(
            TaskOutcome(
                task_index=index,
                instance=instance,
                execution=execution,
                evaluation=evaluate(catalog()[scenario_id], execution),
                submitted_offset_ms=0.0,
            )
        )
    document = _observation_document(
        run_id="00000000-0000-4000-8000-000000000000",
        repetition_index=1,
        phase="measured",
        outcomes=tuple(outcomes),
    )
    cell = results_dir / "qualification-runs" / label
    cell.mkdir(parents=True)
    path = cell / "run.observations.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def results(tmp_path, monkeypatch):
    external = tmp_path / "external"
    monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
    return external


class TestReport:
    def test_completion_versus_correctness_and_floor_wording(self, results):
        _write_cell(results)
        report = analyze(results_dir=results, run_label=RUN_LABEL, candidate_id="P1")
        assert report["tasks"] == 4
        assert report["completion"]["completed_tasks"] == 4
        assert report["correctness"]["passed_tasks"] == 2
        assert report["correctness"]["completed_but_failed_gates"] == 2
        assert report["correctness"]["quality_floor"] == 0.40
        assert report["correctness"]["quality_floor_percent"] == 40
        assert "40 percent" in report["correctness"]["note"]
        assert "0.4 percent" in FLOOR_NOTE
        assert len(report["successful_incidents"]) == 2
        assert all(item["title"] for item in report["successful_incidents"])
        assert all(item["evidence_path"] for item in report["successful_incidents"])
        assert all(item["failed_gates"] for item in report["failed_gates_by_task"])
        assert all(
            gate.startswith("evidence:")
            for item in report["failed_gates_by_task"]
            for gate in item["failed_gates"]
        )
        assert sum(report["aggregate_gate_failures"].values()) == sum(
            len(item["failed_gates"]) for item in report["failed_gates_by_task"]
        )
        text = render(report)
        assert "Completion:" in text and "Correctness:" in text
        assert FLOOR_NOTE in text

    def test_output_is_sanitized(self, results):
        path = _write_cell(results)
        report = analyze(results_dir=results, run_label=RUN_LABEL, candidate_id="P1")
        for fmt in ("text", "json"):
            blob = render(report, fmt=fmt)
            assert SECRET_RATIONALE not in blob
            assert SECRET_QUERY not in blob
            assert str(results) not in blob and str(path) not in blob
            assert "sha256" not in blob.lower() or "evaluator" in blob


class TestReadOnlyAndOffline:
    def test_source_bytes_and_mtime_are_untouched_and_report_lands_in_the_private_dir(
        self, results
    ):
        path = _write_cell(results)
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        report = analyze(
            results_dir=results, run_label=RUN_LABEL, candidate_id="P1", write_report=True
        )
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        written = results / "qualification-analysis" / LABEL / "analysis.json"
        assert written.is_file() and report["report_written"] is True
        assert oct(written.stat().st_mode & 0o777) == "0o600"
        assert sorted(p.name for p in path.parent.iterdir()) == ["run.observations.json"]

    def test_no_client_endpoint_or_socket_activity(self, results, monkeypatch, capsys):
        _write_cell(results)

        def forbidden(*_a, **_k):
            raise AssertionError("a model client must never be constructed")

        monkeypatch.setattr(openai_client.OpenAICompatibleClient, "__init__", forbidden)
        monkeypatch.setattr(socket, "socket", forbidden)
        code = main(
            [
                "analyze-qualification",
                "--run-label",
                RUN_LABEL,
                "--candidate",
                "P1",
                "--format",
                "json",
            ]
        )
        assert code == 0
        report = json.loads(capsys.readouterr().out)
        assert report["provider_contacted"] is False
        assert report["model_client_constructed"] is False

    def test_module_imports_no_provider_or_client_code(self):
        import inspect

        source = inspect.getsource(analysis)
        for forbidden in ("openai_client", "requests", "urllib", "socket", "provenance", "linode"):
            assert forbidden not in source, forbidden


class TestAdversarialPaths:
    @pytest.mark.parametrize(
        "label",
        [
            "../other",
            "/abs/path",
            "qual x",
            "QUAL",
            "a",
            "x" * 60,
            "qual-x/..",
            "qual-x\x00",
        ],
    )
    def test_malformed_labels_are_rejected_before_any_file_access(
        self, results, label, monkeypatch
    ):
        touched: list[str] = []
        real_is_dir = Path.is_dir

        def spy(self):
            touched.append(str(self))
            return real_is_dir(self)

        monkeypatch.setattr(Path, "is_dir", spy)
        with pytest.raises(AnalysisError, match=r"malformed|unsafe|no qualification result"):
            analyze(results_dir=results, run_label=label, candidate_id="P1")
        if label != "a":
            assert touched == []

    def test_unknown_candidate_and_stage_are_rejected(self, results):
        with pytest.raises(AnalysisError, match="candidate"):
            analyze(results_dir=results, run_label=RUN_LABEL, candidate_id="P9")
        with pytest.raises(AnalysisError, match="stage"):
            analyze(results_dir=results, run_label=RUN_LABEL, candidate_id="P1", stage="pilot")

    def test_symlinked_result_directory_is_refused(self, results, tmp_path):
        outside = tmp_path / "outside"
        _write_cell(outside)
        family = results / "qualification-runs"
        family.mkdir(parents=True)
        os.symlink(outside / "qualification-runs" / LABEL, family / LABEL)
        with pytest.raises(AnalysisError, match="symlink"):
            analyze(results_dir=results, run_label=RUN_LABEL, candidate_id="P1")

    def test_symlinked_artifact_family_is_refused_before_any_read(
        self, results, tmp_path, monkeypatch
    ):
        outside = tmp_path / "outside"
        source = _write_cell(outside)
        results.mkdir(parents=True)
        os.symlink(outside / "qualification-runs", results / "qualification-runs")
        opened: list[str] = []
        real_read = Path.read_text

        def spy(self, *args, **kwargs):
            opened.append(self.name)
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", spy)
        with pytest.raises(AnalysisError, match=r"symlink|escapes"):
            analyze(results_dir=results, run_label=RUN_LABEL, candidate_id="P1")
        assert opened == []
        assert source.read_bytes()  # source remains; the analyzer did not need it

    def test_nested_symlink_escapes_are_refused_before_any_read(
        self, results, tmp_path, monkeypatch
    ):
        path = _write_cell(results)
        secret = tmp_path / "secret.observations.json"
        secret.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.unlink()
        path.symlink_to(Path(os.path.relpath(secret, path.parent)))
        opened: list[str] = []
        real_read = Path.read_text

        def spy(self, *args, **kwargs):
            opened.append(self.name)
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", spy)
        with pytest.raises(AnalysisError, match=r"symlink|escapes"):
            analyze(results_dir=results, run_label=RUN_LABEL, candidate_id="P1")
        assert opened == []

        family_target = tmp_path / "family-target"
        _write_cell(family_target)
        hop = tmp_path / "hop"
        hop.symlink_to(family_target / "qualification-runs")
        family = results / "qualification-runs"
        shutil.rmtree(family)
        family.symlink_to(hop)
        opened.clear()
        with pytest.raises(AnalysisError, match=r"symlink|escapes"):
            analyze(results_dir=results, run_label=RUN_LABEL, candidate_id="P1")
        assert opened == []

    def test_symlinked_observations_file_is_refused(self, results, tmp_path):
        outside = tmp_path / "outside"
        source = _write_cell(outside)
        cell = results / "qualification-runs" / LABEL
        cell.mkdir(parents=True)
        os.symlink(source, cell / "run.observations.json")
        with pytest.raises(AnalysisError, match="symlink"):
            analyze(results_dir=results, run_label=RUN_LABEL, candidate_id="P1")

    def test_missing_cell_and_multiple_observation_files_are_refused(self, results):
        with pytest.raises(AnalysisError, match="no qualification result directory"):
            analyze(results_dir=results, run_label=RUN_LABEL, candidate_id="P1")
        path = _write_cell(results)
        (path.parent / "second.observations.json").write_text("{}", encoding="utf-8")
        with pytest.raises(AnalysisError, match="exactly one observations file"):
            analyze(results_dir=results, run_label=RUN_LABEL, candidate_id="P1")

    def test_cli_refuses_results_dir_inside_the_repository(self, monkeypatch, capsys):
        repo = Path(__file__).resolve().parents[1]
        monkeypatch.setenv("LAB_RESULTS_DIR", str(repo / "results"))
        code = main(["analyze-qualification", "--run-label", RUN_LABEL, "--candidate", "P1"])
        assert code != 0
        assert "inside the repository" in capsys.readouterr().err
        assert not (repo / "results" / "qualification-runs").exists()

    def test_observation_outside_the_catalog_is_refused(self, results):
        path = _write_cell(results)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["observations"][0]["template_id"] = "private-scenario-999"
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(AnalysisError, match="outside the public catalog"):
            analyze(results_dir=results, run_label=RUN_LABEL, candidate_id="P1")


def test_fake_clock_is_available():
    assert FakeClock() is not None
