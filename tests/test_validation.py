"""Input validation and semantic (beyond-JSON-Schema) result validation."""

from __future__ import annotations

import copy

import pytest
from fakes import FakeClock

from blackwell_lab.workload.runner import run_cell
from blackwell_lab.workload.validation import (
    ConfigError,
    SemanticValidationError,
    validate_result_semantics,
    validate_runner_config,
)

VALID_CONFIG = {
    "profile_name": "interactive",
    "concurrency": 1,
    "repetitions": 1,
    "warmup_passes": 0,
    "tasks_per_repetition": 4,
    "timeout_ms": 1000.0,
    "max_turns": 12,
    "seed": 7,
    "allowed_profiles": ("batch-heavy", "interactive"),
    "allowed_concurrency": (1, 4, 8),
}


def config(**overrides) -> dict:
    merged = dict(VALID_CONFIG)
    merged.update(overrides)
    return merged


class TestRunnerConfigValidation:
    def test_valid_config_passes(self):
        validate_runner_config(**VALID_CONFIG)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"profile_name": ""},
            {"profile_name": "   "},
            {"profile_name": "turbo"},
            {"profile_name": 42},
            {"concurrency": 0},
            {"concurrency": 3},
            {"concurrency": True},
            {"repetitions": 0},
            {"repetitions": -1},
            {"repetitions": 2.5},
            {"repetitions": True},
            {"warmup_passes": -1},
            {"tasks_per_repetition": 0},
            {"timeout_ms": 0},
            {"timeout_ms": -5},
            {"timeout_ms": "fast"},
            {"max_turns": 0},
            {"max_turns": False},
            {"seed": "lucky"},
        ],
    )
    def test_invalid_values_are_rejected(self, overrides):
        with pytest.raises(ConfigError):
            validate_runner_config(**config(**overrides))

    def test_fewer_tasks_than_concurrency_is_rejected(self):
        with pytest.raises(ConfigError, match="fewer tasks"):
            validate_runner_config(**config(concurrency=8, tasks_per_repetition=4))

    def test_none_timeout_defers_to_the_profile(self):
        validate_runner_config(**config(timeout_ms=None))

    def test_messages_are_sanitized(self):
        """Rejection messages never carry paths or environment values."""
        for overrides in ({"repetitions": -3}, {"timeout_ms": 0}, {"profile_name": "x"}):
            with pytest.raises(ConfigError) as excinfo:
                validate_runner_config(**config(**overrides))
            message = str(excinfo.value)
            assert "/" not in message
            assert "LAB_RESULTS_DIR" not in message

    def test_run_cell_applies_the_same_validation(self):
        with pytest.raises(ConfigError):
            run_cell(
                profile_name="interactive",
                concurrency=1,
                repetitions=-1,
                clock=FakeClock(),
            )


@pytest.fixture(scope="module")
def record():
    return run_cell(
        profile_name="interactive",
        concurrency=1,
        repetitions=1,
        warmup_passes=1,
        tasks_per_repetition=4,
        scenario_ids=["elevated-latency-001", "pod-failures-001"],
        clock=FakeClock(),
    )[0]


def documents(record) -> tuple[dict, dict, dict, dict | None]:
    return (
        copy.deepcopy(record.manifest),
        copy.deepcopy(record.result),
        copy.deepcopy(record.measured_observations),
        copy.deepcopy(record.warmup_observations),
    )


class TestResultSemantics:
    def check(self, manifest, result, measured, warmup):
        validate_result_semantics(
            manifest, result, measured_observations=measured, warmup_observations=warmup
        )

    def test_generated_documents_pass(self, record):
        self.check(*documents(record))

    def test_task_accounting_must_sum_exactly(self, record):
        manifest, result, measured, warmup = documents(record)
        result["tasks"]["succeeded"] += 1
        with pytest.raises(SemanticValidationError, match="sum exactly"):
            self.check(manifest, result, measured, warmup)

    def test_success_rate_must_match_counts(self, record):
        manifest, result, measured, warmup = documents(record)
        result["tasks"]["success_rate"] = 0.5
        with pytest.raises(SemanticValidationError, match="success_rate"):
            self.check(manifest, result, measured, warmup)

    def test_run_ids_must_match(self, record):
        manifest, result, measured, warmup = documents(record)
        result["run_id"] = "22222222-2222-2222-2222-222222222222"
        with pytest.raises(SemanticValidationError):
            self.check(manifest, result, measured, warmup)

    def test_observation_file_run_id_must_match(self, record):
        manifest, result, measured, warmup = documents(record)
        measured["run_id"] = "22222222-2222-2222-2222-222222222222"
        with pytest.raises(SemanticValidationError, match="observation file run_id"):
            self.check(manifest, result, measured, warmup)

    def test_observation_counts_must_match(self, record):
        manifest, result, measured, warmup = documents(record)
        measured["observations"] = measured["observations"][:-1]
        with pytest.raises(SemanticValidationError, match="observation count"):
            self.check(manifest, result, measured, warmup)

    def test_warmup_phase_label_is_checked(self, record):
        manifest, result, measured, warmup = documents(record)
        assert warmup is not None
        warmup["phase"] = "measured"
        with pytest.raises(SemanticValidationError, match="phase"):
            self.check(manifest, result, measured, warmup)

    def test_manifest_ref_must_be_safe_and_owned_by_the_run(self, record):
        manifest, result, measured, warmup = documents(record)
        result["manifest_ref"] = "../outside.manifest.json"
        with pytest.raises(SemanticValidationError, match="safe relative"):
            self.check(manifest, result, measured, warmup)
        result["manifest_ref"] = "someone-elses-run.manifest.json"
        with pytest.raises(SemanticValidationError, match="this run's manifest"):
            self.check(manifest, result, measured, warmup)

    def test_unknown_error_taxonomy_categories_are_rejected(self, record):
        manifest, result, measured, warmup = documents(record)
        result["errors"]["taxonomy"]["quality_failure"] = 1
        with pytest.raises(SemanticValidationError, match="unknown execution-error"):
            self.check(manifest, result, measured, warmup)

    def test_taxonomy_must_account_for_errored_and_timed_out(self, record):
        manifest, result, measured, warmup = documents(record)
        result["errors"]["taxonomy"]["endpoint_error"] = 5
        with pytest.raises(SemanticValidationError, match="taxonomy"):
            self.check(manifest, result, measured, warmup)

    def test_concurrency_claims_must_be_bounded(self, record):
        manifest, result, measured, warmup = documents(record)
        result["concurrency"]["achieved_max"] = result["concurrency"]["requested"] + 1
        with pytest.raises(SemanticValidationError, match="achieved_max"):
            self.check(manifest, result, measured, warmup)

    def test_mean_in_flight_cannot_exceed_requested(self, record):
        manifest, result, measured, warmup = documents(record)
        result["concurrency"]["mean_in_flight"] = result["concurrency"]["requested"] + 0.5
        with pytest.raises(SemanticValidationError, match="mean_in_flight"):
            self.check(manifest, result, measured, warmup)

    def test_sample_design_must_agree_with_the_manifest(self, record):
        manifest, result, measured, warmup = documents(record)
        result["sample_design"]["tasks_per_repetition"] = 9999
        with pytest.raises(SemanticValidationError, match="sample_design"):
            self.check(manifest, result, measured, warmup)

    def test_unique_instances_cannot_exceed_attempts(self, record):
        manifest, result, measured, warmup = documents(record)
        result["sample_design"]["unique_instance_count"] = (
            result["sample_design"]["total_attempts"] + 1
        )
        with pytest.raises(SemanticValidationError, match="unique_instance_count"):
            self.check(manifest, result, measured, warmup)

    def test_observation_hash_format_is_enforced_when_persisted(self, record, tmp_path):
        persisted = run_cell(
            profile_name="interactive",
            concurrency=1,
            repetitions=1,
            warmup_passes=0,
            tasks_per_repetition=2,
            scenario_ids=["elevated-latency-001"],
            clock=FakeClock(),
            results_dir=tmp_path,
        )[0]
        manifest = copy.deepcopy(persisted.manifest)
        result = copy.deepcopy(persisted.result)
        result["observations"]["measured_sha256"] = "NOT-A-HASH"
        with pytest.raises(SemanticValidationError, match="sha256"):
            validate_result_semantics(manifest, result)
