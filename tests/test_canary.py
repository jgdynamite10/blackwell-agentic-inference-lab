"""Ten-task diagnostic canary (decision D-0031).

Covers balance (one task per incident class), fixed-seed determinism,
disjointness from every official schedule, the shared 0.40 floor applied as
four of ten, the diagnostic-only verdict, that a canary can never mint or
authenticate a development control, its own approval phrase, config
refusals, the untouched twenty-task gate, and the offline CLI paths.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from test_evidence_grounding import (
    RUN_TAG,
    _FakeRecord,
    _observed,
    _ready_ledger,
    qualification_config_dict,
)

from blackwell_lab.cloud import canary, cli, lifecycle, provenance, qualification, realbench
from blackwell_lab.cloud.canary import (
    CANARY_ARTIFACT_FAMILY,
    CANARY_CANDIDATES,
    CANARY_MIN_PASSES,
    CANARY_QUALITY_FLOOR,
    CANARY_SEED,
    CANARY_TASKS,
    QualificationError,
    approval_phrase,
    canary_schedule,
    evaluate_canary,
    require_approval,
    require_canary_balance,
    require_canary_contract,
    require_canary_disjoint,
    validate_canary_config,
)
from blackwell_lab.cloud.cli import main
from blackwell_lab.workload.evaluator import EVALUATOR_VERSION
from blackwell_lab.workload.sampling import generate_task_instances
from blackwell_lab.workload.tools import TERMINAL_TOOL
from blackwell_lab.workload.validation import ConfigError

COMMIT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _canary_config(candidate="W1"):
    config = qualification_config_dict(candidate, "development")
    config["workflow"] = "canary-agent"
    config["tasks_per_repetition"] = CANARY_TASKS
    config.pop("development_control", None)
    return config


def _outcomes(passes: int) -> list[dict]:
    templates = list(canary.canary_template_ids())
    return [
        {
            "template_id": templates[index],
            "success": index < passes,
            "evaluation": {"success": index < passes, "evaluator_version": EVALUATOR_VERSION},
            "error_category": None,
            "e2e_ms": 1_000.0,
            "ttft_ms": 200.0,
            "turns": [{"ttft_ms": 200.0}],
            "tool_trace": [{"tool": TERMINAL_TOOL}],
        }
        for index in range(CANARY_TASKS)
    ]


class TestSchedule:
    def test_contract_balance_and_determinism(self):
        spec = require_canary_contract()
        assert spec["tasks"] == CANARY_TASKS == 10
        assert spec["quality_floor"] == CANARY_QUALITY_FLOOR == 0.40
        assert spec["min_passes"] == CANARY_MIN_PASSES == 4
        assert spec["diagnostic_only"] is True
        first = canary_schedule()
        second = canary_schedule()
        assert [i.instance_seed for i in first] == [i.instance_seed for i in second]
        assert [i.template_id for i in first] == list(canary.canary_template_ids())
        assert len({i.template_id for i in first}) == 10
        require_canary_balance(first)

    def test_unbalanced_or_reordered_schedules_are_refused(self):
        schedule = canary_schedule()
        with pytest.raises(QualificationError, match="exactly 10 tasks"):
            require_canary_balance(schedule[:9])
        with pytest.raises(QualificationError, match="one task per incident class"):
            require_canary_balance([schedule[0], *schedule[:9]])
        with pytest.raises(QualificationError, match="deterministic catalog order"):
            require_canary_balance(list(reversed(schedule)))

    def test_disjoint_from_every_official_schedule(self):
        require_canary_disjoint()
        canary_keys = {(i.template_id, i.instance_seed, i.tracking_id) for i in canary_schedule()}
        for stage in qualification.AUTHORIZED_STAGES:
            spec = qualification.stage_spec(stage)
            for seed in (
                qualification.MEASURED_REPETITION_SEED,
                *(qualification.FROZEN_SEED - i - 1 for i in range(spec["warmup_passes"])),
            ):
                official = {
                    (i.template_id, i.instance_seed, i.tracking_id)
                    for i in generate_task_instances(spec["template_ids"], spec["tasks"], seed)
                }
                assert not canary_keys & official, stage

    def test_an_overlapping_seed_is_refused(self, monkeypatch):
        monkeypatch.setattr(canary, "CANARY_SEED", qualification.MEASURED_REPETITION_SEED)
        with pytest.raises(QualificationError, match=r"overlaps|differ"):
            require_canary_disjoint()

    def test_seed_is_fixed_and_documented(self):
        assert CANARY_SEED == 20261007
        assert CANARY_SEED not in {
            qualification.FROZEN_SEED,
            qualification.MEASURED_REPETITION_SEED,
            qualification.FROZEN_SEED - 1,
        }


class TestVerdict:
    def _metrics(self, passes: int):
        return qualification.compute_qualification_metrics(
            _outcomes(passes), provenance_ok=True, verification_ok=True
        )

    @pytest.mark.parametrize("passes", range(0, 4))
    def test_fewer_than_four_stops(self, passes):
        verdict = evaluate_canary(self._metrics(passes))
        assert verdict["stopped"] is True and verdict["continue"] is False
        assert verdict["passed_tasks"] == passes
        assert verdict["next_step"].startswith("STOP")

    @pytest.mark.parametrize("passes", range(4, 11))
    def test_four_or_more_allows_a_separate_official_approval_only(self, passes):
        verdict = evaluate_canary(self._metrics(passes))
        assert verdict["stopped"] is False and verdict["continue"] is True
        assert verdict["authorizes_qualification"] is False
        assert verdict["creates_development_control"] is False
        assert verdict["official_gate"] == {"tasks": 20, "quality_floor": 0.40, "min_passes": 8}
        assert "eight of twenty" in verdict["next_step"]
        assert "authorizes nothing" in verdict["next_step"]

    def test_floor_is_forty_percent(self):
        verdict = evaluate_canary(self._metrics(4))
        assert verdict["quality_floor"] == 0.40
        assert verdict["quality_floor_percent"] == 40

    def test_wrong_task_count_is_refused(self):
        metrics = qualification.compute_qualification_metrics(
            _outcomes(10)[:9], provenance_ok=True, verification_ok=True
        )
        with pytest.raises(QualificationError, match="exactly 10"):
            evaluate_canary(metrics)

    def test_official_gate_untouched(self):
        assert qualification.DEVELOPMENT_TASKS == 20
        assert qualification.DEVELOPMENT_QUALITY_FLOOR == 0.40
        assert qualification.stage_spec("development")["tasks"] == 20


class TestApprovalAndConfig:
    def test_canary_phrase_is_distinct_from_the_qualification_phrase(self):
        digest = "0" * 64
        phrase = approval_phrase(RUN_TAG, "run-a", "W1", digest)
        assert "diagnostic" in phrase and "authorizes no qualification" in phrase
        assert phrase != qualification.approval_phrase(RUN_TAG, "run-a", "W1", digest)
        require_approval(
            phrase, run_tag=RUN_TAG, run_label="run-a", candidate_id="W1", config_sha256=digest
        )
        with pytest.raises(QualificationError):
            require_approval(
                qualification.approval_phrase(RUN_TAG, "run-a", "W1", digest),
                run_tag=RUN_TAG,
                run_label="run-a",
                candidate_id="W1",
                config_sha256=digest,
            )
        with pytest.raises(QualificationError):
            require_approval(
                None, run_tag=RUN_TAG, run_label="run-a", candidate_id="W1", config_sha256=digest
            )

    def test_candidates_are_public_catalog_only(self):
        assert CANARY_CANDIDATES == ("C1", "C2", "P1", "P2C", "W1", "W2")
        with pytest.raises(ConfigError, match="public-catalog"):
            validate_canary_config(_canary_config("W1"), candidate_id="P2")

    def test_config_refuses_control_sealed_and_schedule_keys(self):
        for key in ("development_control", "sealed_set", "template_ids", "stage"):
            config = _canary_config()
            config[key] = {"x": 1}
            with pytest.raises(ConfigError, match="refuses keys"):
                validate_canary_config(config, candidate_id="W1")
        config = _canary_config()
        config["tasks_per_repetition"] = 20
        with pytest.raises(ConfigError, match="tasks_per_repetition must equal 10"):
            validate_canary_config(config, candidate_id="W1")
        config = _canary_config()
        config["workflow"] = "qualify-agent"
        with pytest.raises(ConfigError, match="workflow must equal canary-agent"):
            validate_canary_config(config, candidate_id="W1")
        validate_canary_config(_canary_config("W2"), candidate_id="W2")
        validate_canary_config(_canary_config("P1"), candidate_id="P1")


@pytest.fixture
def ready(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
    monkeypatch.setattr(cli, "_git_head", lambda: COMMIT)
    monkeypatch.setattr(cli, "_tree_clean", lambda: True)
    external = tmp_path / "external"
    monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
    paths = lifecycle.lifecycle_paths(external, RUN_TAG)
    lifecycle.write_private_json(paths.ledger_path, _ready_ledger())
    monkeypatch.setattr(
        provenance, "verify_live_provenance", lambda **kwargs: _observed(full_host=True)
    )
    return external


def _argv(path, *, candidate="W1", approve=None, validate_only=False):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    argv = [
        "canary-agent",
        "--run-tag",
        RUN_TAG,
        "--run-label",
        "canary-a",
        "--candidate",
        candidate,
        "--config",
        str(path),
    ]
    if validate_only:
        return [*argv, "--validate-only"]
    return [*argv, "--approve", approve or approval_phrase(RUN_TAG, "canary-a", candidate, digest)]


class TestCli:
    def _write(self, tmp_path, config):
        path = tmp_path / "canary.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_validate_only_executes_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path / "external"))
        path = self._write(tmp_path, _canary_config("W2"))
        assert main(_argv(path, candidate="W2", validate_only=True)) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["executed"] is False and report["endpoint_contacted"] is False
        assert report["schedule"] == {
            "tasks": 10,
            "seed": CANARY_SEED,
            "templates": 10,
            "quality_floor": 0.40,
            "min_passes": 4,
        }
        assert report["authorizes_qualification"] is False
        assert report["creates_development_control"] is False

    def test_qualification_phrase_does_not_authorize_a_canary(self, ready, tmp_path, capsys):
        path = self._write(tmp_path, _canary_config())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        wrong = qualification.approval_phrase(RUN_TAG, "canary-a", "W1", digest)
        assert main(_argv(path, approve=wrong)) == 1
        assert "approval phrase" in capsys.readouterr().err
        assert not (ready / CANARY_ARTIFACT_FAMILY).exists()

    @pytest.mark.parametrize("passes,expected_code", [(3, 1), (4, 0), (10, 0)])
    def test_canary_runs_offline_and_mints_no_control(
        self, ready, tmp_path, monkeypatch, capsys, passes, expected_code
    ):
        specs = []

        def fake_run(spec, *_args, **_kwargs):
            specs.append(spec)
            return [_FakeRecord(_outcomes(passes))]

        monkeypatch.setattr(realbench, "run_real_cell", fake_run)
        path = self._write(tmp_path, _canary_config("W1"))
        assert main(_argv(path)) == expected_code
        receipt = json.loads(capsys.readouterr().out)
        spec = specs[0]
        assert spec.tasks_per_repetition == 10
        assert spec.seed == CANARY_SEED
        assert spec.warmup_passes == 0
        assert spec.artifact_family == CANARY_ARTIFACT_FAMILY
        assert tuple(spec.template_ids) == canary.canary_template_ids()
        assert spec.run_label == "canary-a-w1-canary"
        assert spec.controller == "workflow-controller-v1"
        assert receipt["kind"] == "diagnostic-canary"
        assert receipt["verdict"]["passed_tasks"] == passes
        assert receipt["stopped"] is (passes < 4)
        assert receipt["authorizes_qualification"] is False
        assert receipt["creates_development_control"] is False
        assert receipt["qualification_environment"]["ok"] is True
        assert (ready / CANARY_ARTIFACT_FAMILY / "canary-a-w1-canary-receipt.json").is_file()
        # Nothing lands in the qualification family and no control exists.
        assert not (ready / "qualification-runs").exists()
        assert not list(ready.rglob("*-control.json"))
        session = json.loads(
            (ready / "infra-lifecycle" / RUN_TAG / "session.json").read_text(encoding="utf-8")
        )
        events = [e["event"] for e in session["events"]]
        assert events[0] == "canary_started"
        assert events[-1] == ("canary_stopped" if passes < 4 else "canary_completed")
        assert "qualification_completed" not in events

    def test_a_canary_receipt_cannot_serve_as_a_development_control(
        self, ready, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            realbench, "run_real_cell", lambda *a, **k: [_FakeRecord(_outcomes(10))]
        )
        path = self._write(tmp_path, _canary_config("W1"))
        assert main(_argv(path)) == 0
        from blackwell_lab.cloud.matched_control import W1_W2_PAIR, audit_matched_controls

        assert audit_matched_controls(ready) == []
        config = qualification_config_dict("W2", "development")
        config["development_control"] = {
            "schema_version": "1.0.0",
            "run_tag": RUN_TAG,
            "w1_run_label": "canary-a",
            "canonical_commit": COMMIT,
            "region": "us-ord",
            "config_sha256": "0" * 64,
            "result_sha256": "0" * 64,
            "control_record_sha256": "0" * 64,
            "resource_identity_sha256": "0" * 64,
            "ledger_sha256": "0" * 64,
        }
        from blackwell_lab.cloud.matched_control import authenticate_matched_development_control

        with pytest.raises(QualificationError):
            authenticate_matched_development_control(
                config, results_dir=ready, run_tag=RUN_TAG, p2c_run_label="qual-w2", pair=W1_W2_PAIR
            )

    def test_wrong_task_count_from_the_runner_stops_with_a_failure_record(
        self, ready, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            realbench, "run_real_cell", lambda *a, **k: [_FakeRecord(_outcomes(10)[:9])]
        )
        path = self._write(tmp_path, _canary_config("W1"))
        assert main(_argv(path)) == 1
        assert "exactly 10" in capsys.readouterr().err
        assert (ready / CANARY_ARTIFACT_FAMILY / "canary-a-failure.json").is_file()
