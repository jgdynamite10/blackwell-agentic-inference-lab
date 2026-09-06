"""Benchmark runner: owner-approved profiles, bounded closed-loop scheduling,
truthful concurrency accounting, sample design, mock validity, prompt atomic
private persistence of raw observations, and safe CLI output."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest
from fakes import FakeClock

from blackwell_lab.paths import RESULTS_DIR_ENV_VAR
from blackwell_lab.schemas import (
    validate_benchmark_result,
    validate_run_manifest,
    validate_task_observations,
)
from blackwell_lab.workload.model_client import (
    DeterministicMockClient,
    GenerationSettings,
    Message,
    ModelClient,
)
from blackwell_lab.workload.runner import (
    CONCURRENCY_LEVELS,
    DEFAULT_REPETITIONS,
    DEFAULT_TASKS_PER_REPETITION,
    DEFAULT_WARMUP_PASSES,
    PROFILES,
    SCHEDULER_DESCRIPTION,
    main,
    run_cell,
)
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.tools import SimulatedToolbox
from blackwell_lab.workload.validation import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git") or "/usr/bin/git"

FAST_SCENARIOS = ["elevated-latency-001", "pod-failures-001"]


def fast_cell(**overrides):
    kwargs = {
        "profile_name": "interactive",
        "concurrency": 1,
        "repetitions": 2,
        "warmup_passes": 1,
        "tasks_per_repetition": 4,
        "scenario_ids": FAST_SCENARIOS,
        "clock": FakeClock(),
    }
    kwargs.update(overrides)
    return run_cell(**kwargs)


class TestProfiles:
    def test_both_documented_profiles_exist(self):
        assert set(PROFILES) == {"interactive", "batch-heavy"}

    def test_owner_approved_slo_targets_and_timeouts(self):
        """Decision D-0010: these exact values are owner-approved."""
        interactive = PROFILES["interactive"]
        assert interactive.slo_task_ms == 60_000.0
        assert interactive.slo_ttft_ms == 2_500.0
        assert interactive.task_timeout_ms == 120_000.0
        batch = PROFILES["batch-heavy"]
        assert batch.slo_task_ms == 300_000.0
        assert batch.slo_ttft_ms is None
        assert batch.task_timeout_ms == 600_000.0

    def test_profiles_differ_in_context_size_output_budget_timeout_and_slo(self):
        interactive, batch = PROFILES["interactive"], PROFILES["batch-heavy"]
        assert interactive.log_limit < batch.log_limit
        assert interactive.metric_window_s < batch.metric_window_s
        assert interactive.max_tokens < batch.max_tokens
        assert interactive.task_timeout_ms < batch.task_timeout_ms
        assert interactive.slo_task_ms < batch.slo_task_ms

    def test_profiles_measurably_produce_the_context_size_difference(self):
        """The documented context difference is real: the batch-heavy log
        limit returns measurably more log context for one identical query."""
        scenario = catalog()["elevated-latency-001"]
        lines_by_profile = {}
        for name, profile in PROFILES.items():
            toolbox = SimulatedToolbox(
                scenario,
                log_limit=profile.log_limit,
                metric_window_s=profile.metric_window_s,
                clock=FakeClock(),
            )
            payload = toolbox.execute("search_logs", {"query": "e"}).payload
            lines_by_profile[name] = len(payload["lines"])
        assert lines_by_profile["interactive"] == PROFILES["interactive"].log_limit
        assert lines_by_profile["batch-heavy"] > lines_by_profile["interactive"]

    def test_defaults_match_the_measurement_contract(self):
        assert DEFAULT_REPETITIONS == 5
        assert DEFAULT_WARMUP_PASSES >= 1
        assert DEFAULT_TASKS_PER_REPETITION == 200
        assert CONCURRENCY_LEVELS == (1, 4, 8)

    def test_scheduler_description_makes_no_exactness_claim(self):
        assert "bounded closed-loop" in SCHEDULER_DESCRIPTION
        assert "exactly" not in SCHEDULER_DESCRIPTION.replace("not claimed to be exactly", "")

    def test_unknown_profile_concurrency_and_scenarios_are_rejected(self):
        with pytest.raises(ConfigError):
            fast_cell(profile_name="turbo")
        with pytest.raises(ConfigError):
            fast_cell(concurrency=3)
        with pytest.raises(ConfigError):
            fast_cell(scenario_ids=["nope"])

    def test_fewer_tasks_than_concurrency_is_rejected(self):
        with pytest.raises(ConfigError):
            fast_cell(concurrency=4, tasks_per_repetition=2)


class TestCellExecution:
    def test_repetitions_and_task_counts(self):
        records = fast_cell()
        assert len(records) == 2
        for index, record in enumerate(records, start=1):
            timing = record.manifest["timing_conditions"]
            assert timing["repetition_index"] == index
            assert timing["repetitions_planned"] == 2
            assert record.result["tasks"]["attempted"] == 4
            assert len(record.measured_observations["observations"]) == 4

    def test_instances_are_balanced_across_templates(self):
        record = fast_cell(repetitions=1)[0]
        counts: dict[str, int] = {}
        for observation in record.measured_observations["observations"]:
            counts[observation["template_id"]] = counts.get(observation["template_id"], 0) + 1
        assert counts == {"elevated-latency-001": 2, "pod-failures-001": 2}

    def test_repetitions_use_distinct_seeds_never_byte_identical(self):
        """Decision D-0010: byte-identical repetitions are never presented as
        independent cases — every repetition draws its own seeded instances."""
        first, second = fast_cell()
        assert first.result["sample_design"]["seed"] != second.result["sample_design"]["seed"]
        first_ids = [o["instance_seed"] for o in first.measured_observations["observations"]]
        second_ids = [o["instance_seed"] for o in second.measured_observations["observations"]]
        assert first_ids != second_ids

    def test_sample_design_records_the_honest_identity_counts(self):
        record = fast_cell(repetitions=1)[0]
        sample = record.result["sample_design"]
        assert sample["tasks_per_repetition"] == 4
        assert sample["total_attempts"] == 4
        assert sample["unique_template_count"] == 2
        assert sample["unique_instance_count"] == 4
        assert "unique_template_count" in sample["quality_independence_note"]

    def test_warmup_is_retained_separately_and_excluded_from_measurement(self):
        with_warmup = fast_cell(warmup_passes=2, repetitions=1)[0]
        without_warmup = fast_cell(warmup_passes=0, repetitions=1)[0]
        assert with_warmup.result["tasks"] == without_warmup.result["tasks"]
        assert with_warmup.warmup_observations is not None
        assert with_warmup.warmup_observations["phase"] == "warmup"
        assert len(with_warmup.warmup_observations["observations"]) == 8  # 2 passes x 4
        assert with_warmup.result["observations"]["warmup_count"] == 8
        assert with_warmup.result["observations"]["measured_count"] == 4
        assert without_warmup.warmup_observations is None

    def test_deterministic_reproduction(self):
        first = fast_cell(repetitions=1)[0]
        second = fast_cell(repetitions=1)[0]
        assert first.result["tasks"] == second.result["tasks"]
        assert [o["status"] for o in first.measured_observations["observations"]] == [
            o["status"] for o in second.measured_observations["observations"]
        ]

    def test_full_default_scenario_set_covers_all_ten_templates(self):
        record = fast_cell(repetitions=1, scenario_ids=None, tasks_per_repetition=10)[0]
        templates = {o["template_id"] for o in record.measured_observations["observations"]}
        assert templates == set(catalog())


class TestTaskAccounting:
    def test_all_successes_sum_exactly(self):
        tasks = fast_cell(repetitions=1)[0].result["tasks"]
        assert tasks["attempted"] == 4
        assert tasks["succeeded"] == 4
        assert (
            tasks["attempted"]
            == tasks["succeeded"] + tasks["quality_failed"] + tasks["errored"] + tasks["timed_out"]
        )

    def test_quality_failure_is_not_an_execution_error(self):
        """wrong_diagnosis completes the task but fails the diagnosis gate:
        it must land in quality_failed, never in the error taxonomy."""
        record = fast_cell(
            repetitions=1, client=DeterministicMockClient(behavior="wrong_diagnosis")
        )[0]
        tasks = record.result["tasks"]
        assert tasks["succeeded"] == 0
        assert tasks["quality_failed"] == 4
        assert tasks["errored"] == 0
        assert record.result["errors"]["taxonomy"] == {}
        assert record.result["errors"]["error_rate"] == 0.0

    def test_execution_errors_land_in_the_taxonomy(self):
        record = fast_cell(
            repetitions=1, client=DeterministicMockClient(behavior="malformed_tool_call")
        )[0]
        tasks = record.result["tasks"]
        assert tasks["errored"] == 4
        assert tasks["quality_failed"] == 0
        assert record.result["errors"]["taxonomy"] == {"malformed_tool_call": 4}
        assert record.result["errors"]["error_rate"] == 1.0

    def test_runtime_crash_is_sanitized_and_contained(self):
        """One crashing task never aborts the repetition; it is accounted as
        agent_runtime_error with no raw exception text anywhere."""
        record = fast_cell(repetitions=1, client=DeterministicMockClient(behavior="runtime_crash"))[
            0
        ]
        assert record.result["errors"]["taxonomy"] == {"agent_runtime_error": 4}
        assert "Traceback" not in json.dumps(record.measured_observations)

    def test_mixed_statuses_still_sum_exactly(self):
        record = fast_cell(
            repetitions=1, client=DeterministicMockClient(behavior="missing_evidence")
        )[0]
        tasks = record.result["tasks"]
        assert (
            tasks["attempted"]
            == tasks["succeeded"] + tasks["quality_failed"] + tasks["errored"] + tasks["timed_out"]
        )
        assert tasks["quality_failed"] == 4


class _GateClient(ModelClient):
    """Correct mock client that blocks each task's first turn on a barrier:
    the barrier releases only when `parties` tasks are in flight at once."""

    version = "3.0.0-test"

    def __init__(self, parties: int) -> None:
        self._inner = DeterministicMockClient()
        self._barrier = threading.Barrier(parties, timeout=30)

    def stream_turn(self, messages: Sequence[Message], settings: GenerationSettings, **kwargs):
        if not any(m.role == "tool" for m in messages):  # first turn of a task
            self._barrier.wait()
        yield from self._inner.stream_turn(messages, settings, **kwargs)


class _ProbeClient(ModelClient):
    """Correct mock client that records the maximum simultaneous turns."""

    version = "3.0.0-test"

    def __init__(self) -> None:
        self._inner = DeterministicMockClient()
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def stream_turn(self, messages: Sequence[Message], settings: GenerationSettings, **kwargs):
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            yield from self._inner.stream_turn(messages, settings, **kwargs)
        finally:
            with self._lock:
                self._active -= 1


class TestConcurrencyTruthfulness:
    def test_requested_concurrency_is_actually_achieved(self):
        """Barrier-controlled client: the repetition can only finish if four
        tasks were genuinely in flight simultaneously."""
        record = fast_cell(
            repetitions=1,
            warmup_passes=0,
            concurrency=4,
            tasks_per_repetition=4,
            client=_GateClient(parties=4),
        )[0]
        concurrency = record.result["concurrency"]
        assert concurrency["requested"] == 4
        assert concurrency["achieved_max"] == 4
        assert record.result["tasks"]["succeeded"] == 4

    def test_in_flight_work_never_exceeds_the_requested_bound(self):
        probe = _ProbeClient()
        record = fast_cell(
            repetitions=1,
            warmup_passes=0,
            concurrency=4,
            tasks_per_repetition=8,
            client=probe,
        )[0]
        assert probe.max_active <= 4
        concurrency = record.result["concurrency"]
        assert concurrency["achieved_max"] <= concurrency["requested"]
        assert concurrency["mean_in_flight"] <= concurrency["requested"]

    def test_sequential_execution_reports_max_one(self):
        record = fast_cell(repetitions=1)[0]
        concurrency = record.result["concurrency"]
        assert concurrency["requested"] == 1
        assert concurrency["achieved_max"] == 1
        assert 0.0 <= concurrency["mean_in_flight"] <= 1.0

    def test_submission_is_stamped_at_slot_claim_not_preloaded(self):
        """Regression (owner blocker 1): three 85 ms elevated-latency tasks at
        concurrency 1 under a 100 ms timeout must ALL complete. A genuine
        bounded closed-loop scheduler stamps each submission only when its
        slot becomes available, so unslotted tasks consume no timeout budget;
        pre-stamping every task at enqueue would time out tasks 2 and 3."""
        record = fast_cell(
            repetitions=1,
            warmup_passes=0,
            concurrency=1,
            tasks_per_repetition=3,
            scenario_ids=["elevated-latency-001"],
            timeout_ms=100.0,
        )[0]
        tasks = record.result["tasks"]
        assert tasks["attempted"] == 3
        assert tasks["succeeded"] == 3
        assert tasks["timed_out"] == 0
        observations = record.measured_observations["observations"]
        offsets = [o["submitted_offset_ms"] for o in observations]
        # Submission offsets advance rather than all being zero: each task is
        # submitted only after the previous 85 ms task released the slot.
        assert offsets[0] < offsets[1] < offsets[2]
        assert offsets[1] >= 85.0
        assert offsets[2] >= 170.0
        for observation in observations:
            assert observation["e2e_ms"] == pytest.approx(85.0)
            assert observation["queue_wait_ms"] == pytest.approx(0.0)


class TestTimingSemantics:
    def test_cell_wall_time_includes_the_85ms_fixed_tool_total(self):
        """Reference path (elevated-latency): 85 ms of simulated tool delay
        per task must appear in host-clock task time and cell wall time."""
        record = fast_cell(
            repetitions=1,
            warmup_passes=0,
            scenario_ids=["elevated-latency-001"],
            tasks_per_repetition=2,
        )[0]
        diagnostics = record.result["mock_diagnostics"]
        assert diagnostics["host_clock_task_completion_ms"]["min"] >= 85.0
        assert diagnostics["wall_time_s"] >= 2 * 0.085

    def test_observations_record_submission_based_timing(self):
        record = fast_cell(repetitions=1, warmup_passes=0)[0]
        for observation in record.measured_observations["observations"]:
            assert observation["submitted_offset_ms"] <= observation["started_offset_ms"]
            assert observation["started_offset_ms"] <= observation["ended_offset_ms"]
            assert observation["e2e_ms"] == pytest.approx(
                observation["ended_offset_ms"] - observation["submitted_offset_ms"], abs=0.01
            )


@pytest.fixture(scope="module")
def record():
    return run_cell(
        profile_name="batch-heavy",
        concurrency=1,
        repetitions=1,
        warmup_passes=1,
        tasks_per_repetition=4,
        scenario_ids=FAST_SCENARIOS,
        clock=FakeClock(),
    )[0]


class TestOutputDocuments:
    def test_all_documents_are_schema_valid(self, record):
        validate_run_manifest(record.manifest)
        validate_benchmark_result(record.result)
        validate_task_observations(record.measured_observations)
        validate_task_observations(record.warmup_observations)

    def test_mock_execution_mode_is_explicit(self, record):
        assert record.manifest["execution_mode"] == "mock"
        assert record.result["execution_mode"] == "mock"
        assert record.manifest["serving"]["engine"] == "mock"
        # is_synthetic_example marks committed example FILES, not the mode.
        assert record.manifest["is_synthetic_example"] is False

    def test_mock_manifest_fabricates_no_gpu_model_or_container_facts(self, record):
        assert "model" not in record.manifest
        assert "container_digest" not in record.manifest["serving"]
        for field in ("gpu_model", "gpu_count", "gpu_memory_gb", "driver_version", "cuda_version"):
            assert field not in record.manifest["host"]
        assert record.manifest["cloud"]["comparison_mode"] == "not-applicable"

    def test_catalog_digest_is_a_workload_field(self, record):
        assert record.manifest["workload"]["catalog_digest"].startswith("sha256:")

    def test_mock_mode_claims_no_performance_latency(self, record):
        latency = record.result["latency_ms"]
        for measure in (
            "task_completion",
            "time_to_first_token",
            "inter_token",
            "queue_time",
            "model_serving_time",
        ):
            assert latency[measure]["available"] is False
            assert latency[measure]["reason"]
        # Simulated tool latencies are defined values — the one truthful series.
        assert latency["tool_execution_time"]["available"] is True

    def test_mock_mode_claims_no_token_throughput(self, record):
        throughput = record.result["throughput"]
        assert throughput["available"] is False
        assert "tokens/sec" in throughput["reason"]

    def test_mock_mode_claims_no_slo_attainment(self, record):
        slo = record.result["slo"]
        assert slo["slo_attaining_tasks"] is None
        assert slo["attainment_unavailable_reason"]
        assert slo["slo_attaining_throughput_per_gpu_hour"] is None
        assert slo["latency_target_ms"] == 300_000.0
        assert slo["ttft_target_ms"] is None
        assert slo["quality_threshold"] == 1.0

    def test_gpu_telemetry_is_unavailable_with_reason_not_fabricated(self, record):
        gpu = record.result["gpu"]
        assert gpu["telemetry_available"] is False
        assert "no GPU" in gpu["unavailable_reason"]
        assert "utilization_mean_pct" not in gpu
        assert record.result["economics"]["gpu_hours"] == 0.0
        assert record.result["economics"]["cost_per_successful_task_usd"] is None

    def test_host_clock_timings_live_only_in_mock_diagnostics(self, record):
        diagnostics = record.result["mock_diagnostics"]
        assert "NOT benchmark performance" in diagnostics["note"]
        assert diagnostics["host_clock_task_completion_ms"]["count"] == 4


class TestPersistence:
    @pytest.fixture()
    def persisted(self, tmp_path):
        external = tmp_path / "lab-results"
        external.mkdir()
        records = fast_cell(repetitions=2, results_dir=external)
        return external, records

    def test_no_results_dir_means_no_persistence(self):
        record = fast_cell(repetitions=1)[0]
        assert record.written_files == ()
        assert record.result["observations"]["persisted"] is False
        assert "measured_file" not in record.result["observations"]

    def test_files_are_written_externally_and_referenced_safely(self, persisted):
        external, records = persisted
        target = external / "synthetic-mock-runs"
        # Repetition 1: observations + warm-up observations + manifest + result.
        assert len(records[0].written_files) == 4
        assert len(records[1].written_files) == 3  # warm-up attaches to rep 1 only
        for record in records:
            for name in record.written_files:
                assert "/" not in name and not name.startswith(".")
                assert (target / name).is_file()

    def test_written_documents_validate_and_hashes_match(self, persisted):
        external, records = persisted
        target = external / "synthetic-mock-runs"
        for record in records:
            observations = record.result["observations"]
            assert observations["persisted"] is True
            payload = (target / observations["measured_file"]).read_bytes()
            assert hashlib.sha256(payload).hexdigest() == observations["measured_sha256"]
            validate_task_observations(json.loads(payload))
            manifest_doc = json.loads((target / f"{record.run_id}.manifest.json").read_bytes())
            validate_run_manifest(manifest_doc)
            result_doc = json.loads((target / f"{record.run_id}.result.json").read_bytes())
            validate_benchmark_result(result_doc)

    def test_warmup_observations_persist_separately(self, persisted):
        external, records = persisted
        target = external / "synthetic-mock-runs"
        observations = records[0].result["observations"]
        warmup_payload = (target / observations["warmup_file"]).read_bytes()
        assert hashlib.sha256(warmup_payload).hexdigest() == observations["warmup_sha256"]
        warmup_doc = json.loads(warmup_payload)
        assert warmup_doc["phase"] == "warmup"
        assert observations["warmup_file"] != observations["measured_file"]

    def test_writes_are_atomic_no_temporary_files_remain(self, persisted):
        external, _ = persisted
        target = external / "synthetic-mock-runs"
        leftovers = [p.name for p in target.iterdir() if ".tmp" in p.name]
        assert leftovers == []

    def test_private_file_permissions_where_supported(self, persisted):
        external, records = persisted
        target = external / "synthetic-mock-runs"
        assert stat.S_IMODE(target.stat().st_mode) == 0o700
        for name in records[0].written_files:
            assert stat.S_IMODE((target / name).stat().st_mode) == 0o600

    def test_run_writes_nothing_into_the_repository(self, tmp_path):
        before = subprocess.run(
            [GIT, "-C", str(REPO_ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        fast_cell(repetitions=1, results_dir=tmp_path / "external")
        after = subprocess.run(
            [GIT, "-C", str(REPO_ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert before == after


class TestCli:
    def test_list_scenarios(self, capsys):
        assert main(["--list-scenarios"]) == 0
        out = capsys.readouterr().out
        assert "elevated-latency-001" in out
        assert len(out.strip().splitlines()) == 10

    def test_smoke_run_without_persistence(self, capsys, monkeypatch):
        monkeypatch.delenv(RESULTS_DIR_ENV_VAR, raising=False)
        code = main(
            [
                "--profile",
                "interactive",
                "--concurrency",
                "1",
                "--repetitions",
                "1",
                "--warmup-passes",
                "1",
                "--tasks-per-repetition",
                "1",
                "--scenario",
                "rate-limiting-001",
            ]
        )
        assert code == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["persistence"]["enabled"] is False
        assert summary["persistence"]["file_count"] == 0
        assert summary["repetitions"][0]["succeeded"] == 1

    def test_smoke_run_with_external_persistence_prints_no_absolute_paths(
        self, capsys, monkeypatch, tmp_path
    ):
        external = tmp_path / "external-results"
        monkeypatch.setenv(RESULTS_DIR_ENV_VAR, str(external))
        code = main(
            [
                "--profile",
                "batch-heavy",
                "--concurrency",
                "1",
                "--repetitions",
                "1",
                "--warmup-passes",
                "0",
                "--tasks-per-repetition",
                "2",
                "--scenario",
                "gpu-saturation-001",
                "--scenario",
                "capacity-exhaustion-001",
            ]
        )
        assert code == 0
        out = capsys.readouterr().out
        # Absolute private paths are never printed: relative names only.
        assert str(external) not in out
        assert str(tmp_path) not in out
        summary = json.loads(out)
        assert summary["persistence"]["enabled"] is True
        assert summary["persistence"]["file_count"] == 3
        for name in summary["persistence"]["files"]:
            assert "/" not in name
            assert (external / "synthetic-mock-runs" / name).is_file()

    def test_cli_refuses_repository_interior_persistence(self, capsys, monkeypatch):
        monkeypatch.setenv(RESULTS_DIR_ENV_VAR, str(REPO_ROOT / "results"))
        code = main(
            [
                "--profile",
                "interactive",
                "--repetitions",
                "1",
                "--warmup-passes",
                "0",
                "--scenario",
                "storage-latency-001",
            ]
        )
        assert code == 3
        captured = capsys.readouterr()
        assert "refusing to persist" in captured.err

    @pytest.mark.parametrize(
        "argv",
        [
            ["--profile", "interactive", "--repetitions", "-1"],
            ["--profile", "interactive", "--repetitions", "0"],
            ["--profile", "interactive", "--warmup-passes", "-1"],
            ["--profile", "interactive", "--max-turns", "0"],
            ["--profile", "interactive", "--tasks-per-repetition", "0"],
            ["--profile", "interactive", "--concurrency", "4", "--tasks-per-repetition", "2"],
        ],
    )
    def test_invalid_inputs_exit_nonzero_with_sanitized_message(
        self, argv, capsys, monkeypatch, tmp_path
    ):
        """Every rejected input exits 2 with a sanitized message: no absolute
        paths, no environment values (validation runs before any work)."""
        monkeypatch.setenv(RESULTS_DIR_ENV_VAR, str(tmp_path))
        assert main(argv) == 2
        err = capsys.readouterr().err
        assert err.startswith("error:")
        assert str(tmp_path) not in err
