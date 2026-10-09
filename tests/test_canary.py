"""Ten-task diagnostic canary (decision D-0031).

Covers the development-only schedule (six templates, ten tasks, holdout
excluded), fixed-seed determinism, disjointness from every official
schedule, the shared 0.40 floor applied as four of ten, the diagnostic-only
verdict, that a canary can never mint or authenticate a development control,
its own approval phrase, config refusals, the untouched twenty-task gate,
and the offline CLI paths.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import replace

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
from blackwell_lab.cloud.qualification import DEVELOPMENT_TEMPLATE_IDS, HOLDOUT_TEMPLATE_IDS
from blackwell_lab.cloud.realbench import (
    ALLOWED_ARTIFACT_FAMILIES,
    RealRunSpec,
    build_real_manifest,
)
from blackwell_lab.schemas import validate_run_manifest
from blackwell_lab.workload.evaluator import EVALUATOR_VERSION
from blackwell_lab.workload.model_client import GenerationSettings
from blackwell_lab.workload.sampling import generate_task_instances, sample_design_summary
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
    templates = [instance.template_id for instance in canary.canary_schedule()]
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
        assert [i.template_id for i in first] == list(canary.canary_template_sequence())
        assert len(first) == CANARY_TASKS == 10
        assert len({i.template_id for i in first}) == 6
        require_canary_balance(first)

    def test_development_templates_only_with_round_robin_extras(self):
        schedule = canary_schedule()
        template_ids = [instance.template_id for instance in schedule]
        assert len(schedule) == 10
        assert tuple(canary.canary_template_ids()) == DEVELOPMENT_TEMPLATE_IDS
        assert set(template_ids) == set(DEVELOPMENT_TEMPLATE_IDS)
        assert len(set(template_ids)) == 6
        assert set(template_ids).isdisjoint(HOLDOUT_TEMPLATE_IDS)
        assert not any(instance.template_id in HOLDOUT_TEMPLATE_IDS for instance in schedule)
        counts = Counter(template_ids)
        assert [counts[template_id] for template_id in DEVELOPMENT_TEMPLATE_IDS] == [
            2,
            2,
            2,
            2,
            1,
            1,
        ]
        assert template_ids == [
            DEVELOPMENT_TEMPLATE_IDS[index % 6] for index in range(CANARY_TASKS)
        ]
        official = qualification.stage_spec("development")
        assert official["tasks"] == 20
        assert official["template_ids"] == DEVELOPMENT_TEMPLATE_IDS
        assert qualification.stage_spec("holdout")["template_ids"] == HOLDOUT_TEMPLATE_IDS

    def test_no_holdout_template_or_instance(self):
        schedule = canary_schedule()
        canary_keys = {(i.template_id, i.instance_seed, i.tracking_id) for i in schedule}
        assert len(canary_keys) == 10
        holdout = qualification.stage_spec("holdout")
        for seed in (
            qualification.MEASURED_REPETITION_SEED,
            *(qualification.FROZEN_SEED - index - 1 for index in range(holdout["warmup_passes"])),
        ):
            official = generate_task_instances(holdout["template_ids"], holdout["tasks"], seed)
            official_keys = {(i.template_id, i.instance_seed, i.tracking_id) for i in official}
            assert not canary_keys & official_keys
            assert not {i.template_id for i in official} & {i.template_id for i in schedule}
        bad = list(schedule)
        bad[0] = replace(bad[0], template_id=HOLDOUT_TEMPLATE_IDS[0])
        with pytest.raises(QualificationError, match="holdout template"):
            require_canary_balance(bad)

    def test_unbalanced_or_reordered_schedules_are_refused(self):
        schedule = canary_schedule()
        with pytest.raises(QualificationError, match="exactly 10 tasks"):
            require_canary_balance(schedule[:9])
        with pytest.raises(QualificationError, match="frozen-split round-robin"):
            require_canary_balance([schedule[0], *schedule[:9]])
        with pytest.raises(QualificationError, match="development-template round-robin"):
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
        assert CANARY_CANDIDATES == ("C1", "C2", "P1", "P2C", "W1", "W2", "W3", "W4", "W5", "W6")
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
            "templates": 6,
            "template_source": "development",
            "holdout_excluded": True,
            "allocation": canary.CANARY_ALLOCATION,
            "quality_floor": 0.40,
            "min_passes": 4,
        }
        assert set(canary.canary_template_ids()).isdisjoint(HOLDOUT_TEMPLATE_IDS)
        assert report["authorizes_qualification"] is False
        assert report["creates_development_control"] is False

    def test_w5_validate_only_preflights_the_development_overlay(
        self, tmp_path, monkeypatch, capsys
    ):
        from blackwell_lab.workload.public_metadata import evidence_contract

        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path / "external"))
        config = _canary_config("W5")
        config["evidence_contract"] = evidence_contract()
        path = self._write(tmp_path, config)
        assert main(_argv(path, candidate="W5", validate_only=True)) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["executed"] is False
        assert report["model_client_constructed"] is False
        assert report["endpoint_contacted"] is False
        assert report["candidate_id"] == "W5"
        assert report["workload_version"] == "2.8.0"
        assert report["controller"] == "workflow-controller-v3"
        assert report["schedule"]["tasks"] == 10
        assert report["schedule"]["holdout_excluded"] is True
        assert report["schedule"]["template_source"] == "development"
        for stage in ("holdout", "freeze"):
            spec = qualification.stage_spec(stage)
            with pytest.raises(ConfigError, match="development catalog only"):
                qualification.require_w_catalog_execution(
                    "W5",
                    stage=stage,
                    template_ids=spec["template_ids"],
                    sealed_set=None,
                    sealed_tasks=None,
                )

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
        assert spec.seed == canary.CANARY_BASE_SEED
        assert spec.seed + 1 == CANARY_SEED
        assert receipt["schedule"]["seed"] == CANARY_SEED
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
            "region": "ca-central",
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


def _canary_run_spec(**overrides) -> RealRunSpec:
    """A spec the real validator accepts, shaped like the canary CLI."""
    defaults = dict(
        profile_name="interactive",
        concurrency=1,
        comparison_mode="provider-native",
        instance_type="g3-gpu-rtxpro6000-blackwell-1",
        region="ca-central",
        list_price_usd_per_hour=3.0,
        price_source_date="2026-09-18",
        model={
            "artifact": "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16",
            "revision": "0123456789abcdef0123456789abcdef01234567",
            "artifact_hash": "sha256:" + "ab" * 32,
            "precision": "bf16",
        },
        engine="vllm",
        engine_version="0.27.1",
        container_digest="docker.io/vllm/vllm-openai@sha256:" + "cd" * 32,
        repetitions=1,
        warmup_passes=0,
        tasks_per_repetition=CANARY_TASKS,
        seed=canary.CANARY_BASE_SEED,
        run_label="canary-a-w1-canary",
        generation=GenerationSettings(
            temperature=0.2,
            top_p=0.95,
            seed=qualification.FROZEN_SEED,
            reasoning_mode=True,
            workload_version="2.6.0",
        ),
        template_ids=canary.canary_template_ids(),
        artifact_family=CANARY_ARTIFACT_FAMILY,
        workload_version="2.6.0",
        controller="workflow-controller-v1",
    )
    defaults.update(overrides)
    return RealRunSpec(**defaults)


class _GuardClient:
    def __init__(self) -> None:
        self.used = False

    def stream_turn(self, *args, **kwargs):
        self.used = True
        raise AssertionError("model client was used")


class _Sampler:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class TestRealCanaryValidation:
    def test_canary_family_passes_real_validation_and_invalid_families_do_not(self):
        assert CANARY_ARTIFACT_FAMILY == "canary-runs"
        assert ALLOWED_ARTIFACT_FAMILIES == frozenset(
            {"real-runs", "qualification-runs", "canary-runs"}
        )
        profile = realbench._validate_spec(_canary_run_spec())
        assert profile.name == "interactive"
        client = _GuardClient()
        for family in ("not-a-family", "canary-runs-extra", "real-runs-canary", ""):
            with pytest.raises(ConfigError, match="artifact_family"):
                realbench._validate_spec(_canary_run_spec(artifact_family=family))
            with pytest.raises(ConfigError, match="artifact_family"):
                realbench.run_real_cell(
                    _canary_run_spec(artifact_family=family),
                    client,
                    host={},
                    sampler_factory=_Sampler,
                )
        assert client.used is False

    def test_runner_executes_the_declared_measured_schedule(self, tmp_path, monkeypatch):
        external = tmp_path / "external"
        external.mkdir()
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        captured: dict[str, object] = {}

        def capture_pass(*, instances, **kwargs):
            captured["instances"] = list(instances)
            captured["client"] = kwargs.get("client")
            raise RuntimeError("stop-before-inference")

        monkeypatch.setattr(realbench, "_run_pass", capture_pass)
        spec = _canary_run_spec()
        realbench._validate_spec(spec)
        declared = canary.require_measured_schedule(spec)
        client = _GuardClient()
        with pytest.raises(RuntimeError, match="stop-before-inference"):
            realbench.run_real_cell(
                spec,
                client,
                host={"gpu_model": "synthetic"},
                sampler_factory=_Sampler,
                results_dir=external,
            )
        executed = captured["instances"]
        assert client.used is False
        assert captured["client"] is client

        def keys(rows):
            return [(item.template_id, item.instance_seed, item.tracking_id) for item in rows]

        assert keys(executed) == keys(declared) == keys(canary_schedule())
        measured_seed = spec.seed + 1
        assert measured_seed == CANARY_SEED == 20261007
        design = sample_design_summary(executed, measured_seed)
        receipt = canary.sanitized_receipt(
            run_label=spec.run_label,
            candidate_id="W1",
            config_sha256="0" * 64,
            identity_digest="1" * 64,
            verdict=canary.evaluate_canary(
                qualification.compute_qualification_metrics(
                    _outcomes(4), provenance_ok=True, verification_ok=True
                )
            ),
            files=[],
        )
        assert receipt["schedule"]["seed"] == design["seed"] == CANARY_SEED
        assert receipt["schedule"]["tasks"] == design["tasks_per_repetition"] == 10
        assert receipt["schedule"]["templates"] == design["unique_template_count"] == 6
        assert receipt["schedule"]["template_source"] == "development"
        assert receipt["schedule"]["holdout_excluded"] is True
        assert receipt["schedule"]["allocation"] == canary.CANARY_ALLOCATION
        assert set(item.template_id for item in executed) == set(DEVELOPMENT_TEMPLATE_IDS)
        assert set(item.template_id for item in executed).isdisjoint(HOLDOUT_TEMPLATE_IDS)
        profile = realbench._validate_spec(spec)
        manifest = build_real_manifest(
            spec=spec,
            profile=profile,
            run_id="canary-schedule-check",
            host={
                "operating_system": "Ubuntu 24.04 LTS",
                "cpu_model": "Synthetic Test CPU",
                "vcpu_count": 16,
                "system_memory_gib": 176.0,
                "gpu_model": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
                "gpu_count": 1,
                "gpu_memory_gb": 96.0,
                "driver_version": "580.65.06",
                "driver_max_cuda_version": "13.0",
            },
            scenario_count=len(spec.template_ids),
            repetition_index=1,
            started_at_utc="2026-10-08T16:00:00+00:00",
            ended_at_utc="2026-10-08T16:00:01+00:00",
        )
        validate_run_manifest(manifest)
        assert manifest["workload"]["tasks_per_repetition"] == receipt["schedule"]["tasks"] == 10
        assert manifest["workload"]["scenario_count"] == receipt["schedule"]["templates"] == 6
        assert (external / "canary-runs" / spec.run_label).is_dir()
        assert not (external / "qualification-runs").exists()
        assert not (external / "real-runs").exists()

    def test_executed_instances_are_disjoint_from_every_official_schedule(
        self, tmp_path, monkeypatch
    ):
        external = tmp_path / "external"
        external.mkdir()
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        captured: list = []

        def capture_pass(*, instances, **kwargs):
            captured.extend(instances)
            raise RuntimeError("stop-before-inference")

        monkeypatch.setattr(realbench, "_run_pass", capture_pass)
        with pytest.raises(RuntimeError, match="stop-before-inference"):
            realbench.run_real_cell(
                _canary_run_spec(),
                _GuardClient(),
                host={"gpu_model": "synthetic"},
                sampler_factory=_Sampler,
                results_dir=external,
            )
        executed_keys = {(i.template_id, i.instance_seed, i.tracking_id) for i in captured}
        assert len(executed_keys) == CANARY_TASKS
        for stage in qualification.AUTHORIZED_STAGES:
            stage_spec = qualification.stage_spec(stage)
            seeds = [qualification.MEASURED_REPETITION_SEED]
            seeds.extend(
                qualification.FROZEN_SEED - index - 1
                for index in range(int(stage_spec["warmup_passes"]))
            )
            for seed in seeds:
                official = {
                    (i.template_id, i.instance_seed, i.tracking_id)
                    for i in generate_task_instances(
                        stage_spec["template_ids"], stage_spec["tasks"], seed
                    )
                }
                assert not executed_keys & official, (stage, seed)

    def test_invalid_family_is_refused_before_the_client_is_constructed(
        self, ready, tmp_path, monkeypatch, capsys
    ):
        constructed: list[str] = []

        class BoomClient:
            def __init__(self, *args, **kwargs):
                constructed.append("client")
                raise AssertionError("client constructed")

        monkeypatch.setattr(
            "blackwell_lab.workload.openai_client.OpenAICompatibleClient", BoomClient
        )
        monkeypatch.setattr(canary, "CANARY_ARTIFACT_FAMILY", "bogus-runs")
        path = tmp_path / "canary.json"
        path.write_text(json.dumps(_canary_config("W1")), encoding="utf-8")
        assert main(_argv(path)) == 1
        assert constructed == []
        assert "artifact_family" in capsys.readouterr().err

    def test_holdout_templates_are_refused_before_the_client_is_constructed(
        self, ready, tmp_path, monkeypatch, capsys
    ):
        constructed: list[str] = []

        class BoomClient:
            def __init__(self, *args, **kwargs):
                constructed.append("client")
                raise AssertionError("client constructed")

        monkeypatch.setattr(
            "blackwell_lab.workload.openai_client.OpenAICompatibleClient", BoomClient
        )
        monkeypatch.setattr(canary, "canary_template_ids", lambda: HOLDOUT_TEMPLATE_IDS)
        path = tmp_path / "canary.json"
        path.write_text(json.dumps(_canary_config("W1")), encoding="utf-8")
        assert main(_argv(path)) == 1
        assert constructed == []
        assert "holdout template" in capsys.readouterr().err

    def test_a_holdout_spec_is_refused_before_any_client_is_used(self):
        spec = _canary_run_spec(template_ids=HOLDOUT_TEMPLATE_IDS)
        client = _GuardClient()
        with pytest.raises(QualificationError, match="six development templates"):
            canary.require_measured_schedule(spec)
        assert client.used is False
