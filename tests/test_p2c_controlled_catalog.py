"""Adversarial offline tests for candidate P2C (decision D-0026).

P2C is the controlled public-catalog version of P2: workload 2.5.0 and the
``evidence-grounding-v1`` controller, executed on exactly the D-0019
catalog schedule that P1 executes. These tests prove, offline and with no
custody package, that:

- P2C differs from P1 only in ``candidate_id``, ``workload_version``,
  ``controller``, and the version-bound ``evidence_refs`` terminal schema;
- P2C and P1 share the ordered development, holdout, and freeze schedules,
  warm-up configuration, seed derivation, and scenario bytes;
- P2C performs zero custody reads and refuses sealed input;
- every invalid P2C binding fails before ``OpenAICompatibleClient`` is
  constructed and before any turn is streamed;
- P2, C1, C2, P1, the prompt, the evaluator, and the thresholds are unchanged.
"""

from __future__ import annotations

import builtins
import dataclasses
import hashlib
import inspect
import io
import json
import os
import re
from pathlib import Path

import pytest
from fakes import FakeClock
from sealed_fixtures import placeholder_binding, write_synthetic_custody
from test_evidence_grounding import (
    COMMIT,
    FROZEN_ACCEPTED_ANSWERS_SHA256,
    FROZEN_C1_IDENTITY_SHA256,
    FROZEN_C2_IDENTITY_SHA256,
    FROZEN_P1_IDENTITY_SHA256,
    FROZEN_SYSTEM_PROMPT_V241_SHA256,
    RUN_TAG,
    SCENARIO_ID,
    ScriptedClient,
    _install_offline_endpoint,
    _observed,
    _qual_argv,
    _ready_ledger,
    cite_all,
    health,
    qualification_config_dict,
    search,
)
from test_qualification import _accepted_answers_payload, _catalog_answer_tokens, _differing_paths

from blackwell_lab.cloud import cli, lifecycle, provenance, qualification, realbench
from blackwell_lab.cloud import sealed_binding as sealed_binding_module
from blackwell_lab.cloud.cli import main
from blackwell_lab.cloud.qualification import (
    AUTHORIZED_CANDIDATES,
    AUTHORIZED_STAGES,
    CANDIDATE_CONTROLLERS,
    CANDIDATE_P1,
    CANDIDATE_P2,
    CANDIDATE_P2C,
    CANDIDATE_WORKLOAD_VERSIONS,
    CATALOG_CANDIDATES,
    DEVELOPMENT_QUALITY_FLOOR,
    FROZEN_MAX_TOKENS,
    FROZEN_SEED,
    FROZEN_TOP_P,
    HOLDOUT_QUALITY_FLOOR,
    MAX_INTERACTIVE_E2E_P95_MS,
    MAX_INTERACTIVE_TTFT_P95_MS,
    MAX_INVALID_ARGUMENT_RATE,
    MAX_INVALID_TOOL_NAME_RATE,
    MAX_REQUEST_INFERENCE_ERROR_RATE,
    MAX_TIMEOUTS,
    MIN_VALID_NATIVE_TOOL_CALL_RATE,
    P2C_CONTRACT,
    P2C_CONTROL_CANDIDATE,
    P2C_CONTROLLER,
    P2C_FORBIDDEN_CONFIG_KEYS,
    P2C_TEMPERATURE,
    P2C_WORKLOAD_VERSION,
    PRODUCTION_LIKE_MIN_AGGREGATE,
    PRODUCTION_LIKE_MIN_SCENARIO,
    STUDY_ENTRY_MIN_AGGREGATE,
    STUDY_ENTRY_MIN_SCENARIO,
    UNKNOWN_CANDIDATE_MESSAGE,
    QualificationError,
    approval_phrase,
    candidate_controller,
    candidate_identity_digest,
    candidate_temperature,
    candidate_workload_version,
    experimental_behavior_fields,
    experimental_configuration_digest,
    frozen_candidate_fields,
    p2c_experiment_record,
    require_p2c_catalog_execution,
    require_p2c_config,
    require_p2c_contract,
    require_p2c_runtime,
    sanitized_receipt,
    serialize_candidate,
    stage_schedule,
    stage_spec,
    validate_authorized_qualification_config,
)
from blackwell_lab.cloud.realbench import RealRunSpec
from blackwell_lab.cloud.sealed_binding import (
    SEALED_CANDIDATES,
    SealedSetBinding,
    SealedSetError,
    binding_from_config,
    requires_sealed_set,
    resolve_sealed_stage,
)
from blackwell_lab.cloud.sealed_payload import scenario_document
from blackwell_lab.sealed_sets import custody
from blackwell_lab.workload import agent as agent_module
from blackwell_lab.workload import evaluator as evaluator_module
from blackwell_lab.workload import evidence as evidence_module
from blackwell_lab.workload.agent import (
    SYSTEM_PROMPT_V240,
    SYSTEM_PROMPT_V241,
    run_task,
    system_prompt,
)
from blackwell_lab.workload.evaluator import EVALUATOR_VERSION
from blackwell_lab.workload.evidence import (
    CONTROLLER_EVIDENCE_GROUNDING_V1,
    OBSERVATION_ID_FIELD,
    WORKLOAD_CONTROLLERS,
)
from blackwell_lab.workload.model_client import GenerationSettings
from blackwell_lab.workload.native_tools import openai_tool_definitions, tool_descriptions
from blackwell_lab.workload.openai_client import OpenAICompatibleClient
from blackwell_lab.workload.sampling import generate_task_instances
from blackwell_lab.workload.scenarios import WORKLOAD_VERSION, catalog, catalog_digest
from blackwell_lab.workload.tools import TERMINAL_TOOL, SimulatedToolbox, tool_specs
from blackwell_lab.workload.validation import ConfigError

# Identity digests this change must not move (C1/C2/P1 from D-0021/D-0022,
# P2 as serialized at the D-0025 base commit) and the new P2C identity
# produced by the unchanged candidate serialization.
FROZEN_P2_IDENTITY_SHA256 = "478a0ce881b22be8e5e747eafb63a6453b51540492dc1dba497bd8e28d160e44"
FROZEN_P2C_IDENTITY_SHA256 = "49b279fb76dfa3ee3da2dbf8bdd15c7547441faa386c14c97f7787aeff1a069a"
P2C_MINUS_P1 = ["candidate_id", "controller", "workload_version"]
_OBSERVATION_ID_RE = re.compile(r"^obs-[0-9a-f]{24}$")


def _instance_tuples(instances) -> list[tuple]:
    return [
        (
            item.template_id,
            item.instance_id,
            item.instance_seed,
            item.tracking_id,
            item.reported_minute,
        )
        for item in instances
    ]


def _scenario_bytes(scenarios_by_id: dict) -> dict[str, bytes]:
    return {
        scenario_id: json.dumps(
            scenario_document(scenario), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        for scenario_id, scenario in scenarios_by_id.items()
    }


def _write_config(tmp_path: Path, candidate: str, stage: str, name: str = "cfg") -> Path:
    config = qualification_config_dict(candidate, stage)
    results = os.environ.get("LAB_RESULTS_DIR")
    if candidate == "P2C" and stage == "development" and results:
        ledger = Path(results) / "infra-lifecycle" / RUN_TAG / "ledger.json"
        if ledger.is_file():
            from blackwell_lab.cloud.matched_control import (
                install_verified_p1_development_control,
            )

            install_verified_p1_development_control(
                Path(results),
                config,
                run_tag=RUN_TAG,
                p1_run_label="qual-p1",
                canonical_commit=COMMIT,
            )
    path = tmp_path / f"{name}-{candidate.lower()}-{stage}.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _argv(path: Path, *, candidate: str, stage: str, custody_dir=None, extra=()):
    phrase = approval_phrase(
        RUN_TAG, "qual-a", candidate, hashlib.sha256(path.read_bytes()).hexdigest()
    )
    return _qual_argv(
        path, candidate=candidate, stage=stage, approve=phrase, custody_dir=custody_dir
    ) + list(extra)


@pytest.fixture
def ready_cli(tmp_path, monkeypatch):
    """Offline CLI: ledger present, provenance stubbed, clean canonical commit."""
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


class _StopAfterSchedule(RuntimeError):
    """Raised by the schedule recorder once the measured pass is scheduled."""


def _record_schedules(monkeypatch, sink: dict, *, passes: int):
    """Record the exact ordered instances the production runner hands to
    ``_run_pass`` for every pass of the cell, then stop before any turn is
    streamed. Warm-up passes return an empty pass so the runner proceeds to
    schedule the measured repetition; the warm-up document validation is
    bypassed for that reason only."""
    from blackwell_lab.workload.runner import RepetitionData

    def recording(*, instances, scenarios_by_id, settings, **kwargs):
        sink.setdefault("passes", []).append(_instance_tuples(instances))
        sink["scenarios"] = _scenario_bytes(scenarios_by_id)
        sink["settings"] = settings
        if len(sink["passes"]) >= passes:
            raise _StopAfterSchedule()
        return RepetitionData(
            outcomes=(), wall_time_s=0.0, achieved_max_concurrency=0, mean_in_flight=0.0
        )

    monkeypatch.setattr(realbench, "_run_pass", recording)
    monkeypatch.setattr(realbench, "validate_task_observations", lambda document: None)


def _spy_client(monkeypatch):
    from blackwell_lab.workload import openai_client

    constructed: list[object] = []
    streamed: list[object] = []

    class SpyClient(OpenAICompatibleClient):
        def __init__(self, *args, **kwargs):
            constructed.append(self)
            super().__init__(*args, **kwargs)

        def stream_turn(self, *args, **kwargs):
            streamed.append(args)
            raise AssertionError("stream_turn must never run in this test")

    monkeypatch.setattr(openai_client, "OpenAICompatibleClient", SpyClient)
    return constructed, streamed


# --- contract and identity --------------------------------------------------------------


class TestP2CContract:
    def test_p2c_is_authorized_and_bound_to_workload_250_and_the_controller(self):
        assert CANDIDATE_P2C == "P2C"
        assert AUTHORIZED_CANDIDATES == ("C1", "C2", "P1", "P2", "P2C")
        assert CATALOG_CANDIDATES == ("C1", "C2", "P1", "P2C")
        assert CANDIDATE_P2 not in CATALOG_CANDIDATES
        assert CANDIDATE_WORKLOAD_VERSIONS["P2C"] == P2C_WORKLOAD_VERSION == "2.5.0"
        assert CANDIDATE_CONTROLLERS["P2C"] == P2C_CONTROLLER == "evidence-grounding-v1"
        assert candidate_workload_version("P2C") == "2.5.0"
        assert candidate_controller("P2C") == CONTROLLER_EVIDENCE_GROUNDING_V1
        assert WORKLOAD_CONTROLLERS["2.5.0"] == CONTROLLER_EVIDENCE_GROUNDING_V1
        assert candidate_temperature("P2C") == P2C_TEMPERATURE == 0.2
        assert P2C_CONTROL_CANDIDATE == CANDIDATE_P1 == "P1"
        assert "P2C" in UNKNOWN_CANDIDATE_MESSAGE
        assert P2C_CONTRACT == {
            "candidate_id": "P2C",
            "workload_version": "2.5.0",
            "controller": "evidence-grounding-v1",
            "system_prompt_sha256": FROZEN_SYSTEM_PROMPT_V241_SHA256,
            "temperature": 0.2,
            "top_p": 0.95,
            "seed": 20260906,
            "max_tokens": 1024,
            "evaluator_version": "3.1.0",
            "task_source": "catalog",
            "control_candidate": "P1",
        }
        assert require_p2c_contract() == P2C_CONTRACT

    def test_p2c_identity_digest_comes_from_the_normal_serialization(self):
        fields = frozen_candidate_fields("P2C")
        assert fields["region"] == "us-sea"
        assert fields["candidate_id"] == "P2C"
        assert fields["workload_version"] == "2.5.0"
        assert fields["controller"] == "evidence-grounding-v1"
        assert fields["generation"] == {
            "temperature": 0.2,
            "top_p": FROZEN_TOP_P,
            "max_tokens": FROZEN_MAX_TOKENS,
            "seed": FROZEN_SEED,
            "reasoning_mode": True,
        }
        assert "system_prompt" not in fields and "sealed_set" not in fields
        serialized = serialize_candidate("P2C")
        assert serialized == json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
        assert candidate_identity_digest("P2C") == hashlib.sha256(serialized).hexdigest()
        assert candidate_identity_digest("P2C") == FROZEN_P2C_IDENTITY_SHA256
        assert candidate_identity_digest("P2C") != experimental_configuration_digest("P2C")
        digests = {candidate_identity_digest(c) for c in AUTHORIZED_CANDIDATES}
        assert len(digests) == 5

    def test_c1_c2_p1_p2_identities_match_the_region_lock(self):
        assert candidate_identity_digest("C1") == FROZEN_C1_IDENTITY_SHA256
        assert candidate_identity_digest("C2") == FROZEN_C2_IDENTITY_SHA256
        assert candidate_identity_digest("P1") == FROZEN_P1_IDENTITY_SHA256
        assert candidate_identity_digest("P2") == FROZEN_P2_IDENTITY_SHA256
        for candidate in ("C1", "C2", "P1"):
            assert "controller" not in frozen_candidate_fields(candidate)
        assert frozen_candidate_fields("P2")["controller"] == "evidence-grounding-v1"

    def test_p2c_differs_from_p1_only_in_identity_version_and_controller(self):
        p1 = experimental_behavior_fields("P1")
        p2c = experimental_behavior_fields("P2C")
        assert _differing_paths(p1, p2c) == P2C_MINUS_P1
        assert _differing_paths(frozen_candidate_fields("P1"), frozen_candidate_fields("P2C")) == (
            P2C_MINUS_P1
        )
        assert p1["system_prompt"] == p2c["system_prompt"] == SYSTEM_PROMPT_V241
        assert p1["generation"] == p2c["generation"]
        assert p1["model"] == p2c["model"]
        assert p1["serving"] == p2c["serving"]
        # P2C versus P2: the candidate id is the only serialized difference;
        # the execution difference (catalog versus sealed input) is a
        # property of the stage binding, never of the identity record.
        assert _differing_paths(experimental_behavior_fields("P2"), p2c) == ["candidate_id"]

    def test_prompt_tool_text_and_terminal_schema_are_the_only_model_visible_surface(self):
        p1_version = candidate_workload_version("P1")
        p2c_version = candidate_workload_version("P2C")
        frozen = "37b3a4fb615dc21c8d39a5301dc4318870fea3498fbed196c50bfcbe67de1bd3"
        assert FROZEN_SYSTEM_PROMPT_V241_SHA256 == frozen == P2C_CONTRACT["system_prompt_sha256"]
        for scenario in catalog().values():
            prompt = system_prompt(scenario, p2c_version)
            assert prompt == system_prompt(scenario, p1_version) == SYSTEM_PROMPT_V241
            assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == frozen
        assert hashlib.sha256(SYSTEM_PROMPT_V241.encode("utf-8")).hexdigest() == frozen
        assert SYSTEM_PROMPT_V241 != SYSTEM_PROMPT_V240
        assert tool_descriptions(p2c_version) == tool_descriptions(p1_version)
        p1_tools = {t["function"]["name"]: t for t in openai_tool_definitions(p1_version)}
        p2c_tools = {t["function"]["name"]: t for t in openai_tool_definitions(p2c_version)}
        assert set(p1_tools) == set(p2c_tools)
        assert _differing_paths(p1_tools, p2c_tools) == [
            f"{TERMINAL_TOOL}.function.parameters.properties.evidence_refs",
            f"{TERMINAL_TOOL}.function.parameters.required",
        ]
        p1_specs = tool_specs(p1_version)
        p2c_specs = tool_specs(p2c_version)
        for name in p1_specs:
            if name == TERMINAL_TOOL:
                continue
            assert p1_specs[name] == p2c_specs[name]

    def test_evaluator_thresholds_scenarios_and_catalog_are_unchanged(self):
        assert EVALUATOR_VERSION == "3.1.0" == P2C_CONTRACT["evaluator_version"]
        assert hashlib.sha256(_accepted_answers_payload()).hexdigest() == (
            FROZEN_ACCEPTED_ANSWERS_SHA256
        )
        assert WORKLOAD_VERSION == "2.3.0"
        assert catalog_digest().startswith("sha256:")
        assert (DEVELOPMENT_QUALITY_FLOOR, HOLDOUT_QUALITY_FLOOR) == (0.40, 0.50)
        assert (STUDY_ENTRY_MIN_AGGREGATE, STUDY_ENTRY_MIN_SCENARIO) == (0.70, 0.40)
        assert (PRODUCTION_LIKE_MIN_AGGREGATE, PRODUCTION_LIKE_MIN_SCENARIO) == (0.90, 0.80)
        assert MIN_VALID_NATIVE_TOOL_CALL_RATE == 0.99
        assert MAX_INVALID_TOOL_NAME_RATE == 0.0
        assert MAX_INVALID_ARGUMENT_RATE == 0.01
        assert MAX_REQUEST_INFERENCE_ERROR_RATE == 0.01
        assert MAX_TIMEOUTS == 0
        assert MAX_INTERACTIVE_TTFT_P95_MS == 2_500.0
        assert MAX_INTERACTIVE_E2E_P95_MS == 60_000.0
        for stage in AUTHORIZED_STAGES:
            spec = stage_spec(stage)
            assert set(spec["template_ids"]) <= set(catalog())
        assert stage_spec("development")["tasks"] == 20
        assert stage_spec("holdout")["tasks"] == 20
        assert stage_spec("freeze")["tasks"] == 200

    @pytest.mark.parametrize(
        ("mutate", "problem"),
        [
            (
                lambda mp: mp.setitem(
                    agent_module.SYSTEM_PROMPTS_BY_VERSION, "2.5.0", SYSTEM_PROMPT_V240
                ),
                "system_prompt",
            ),
            (lambda mp: mp.setattr(evaluator_module, "EVALUATOR_VERSION", "3.2.0"), "evaluator"),
            (lambda mp: mp.setattr(qualification, "FROZEN_SEED", 1), "seed"),
            (lambda mp: mp.setattr(qualification, "FROZEN_TOP_P", 0.9), "top_p"),
            (lambda mp: mp.setattr(qualification, "FROZEN_MAX_TOKENS", 512), "max_tokens"),
            (lambda mp: mp.setattr(qualification, "P2C_TEMPERATURE", 1.0), "temperature"),
            (
                lambda mp: mp.setattr(sealed_binding_module, "SEALED_CANDIDATES", ("P2", "P2C")),
                "task_source",
            ),
        ],
    )
    def test_contract_drift_fails_closed(self, monkeypatch, mutate, problem):
        mutate(monkeypatch)
        with pytest.raises(QualificationError, match=f"P2C contract drift: .*{problem}"):
            require_p2c_contract()
        with pytest.raises((ConfigError, QualificationError)):
            require_p2c_config(qualification_config_dict("P2C"), candidate_id="P2C")

    def test_controller_table_drift_fails_closed(self, monkeypatch):
        monkeypatch.setitem(qualification.CANDIDATE_CONTROLLERS, "P2C", None)
        with pytest.raises(ConfigError, match="binding mismatch"):
            candidate_controller("P2C")
        with pytest.raises(ConfigError, match="binding mismatch"):
            require_p2c_contract()
        monkeypatch.setitem(qualification.CANDIDATE_WORKLOAD_VERSIONS, "P2C", "2.4.1")
        # Workload 2.4.1 with no controller is a consistent binding, so the
        # contract check itself must catch the drift.
        with pytest.raises(QualificationError, match="drift: workload_version, controller"):
            require_p2c_contract()


# --- ordered schedule equality ------------------------------------------------------------


class TestP2CSchedule:
    @pytest.mark.parametrize("stage", AUTHORIZED_STAGES)
    def test_stage_schedule_is_the_production_derivation(self, stage):
        spec = stage_spec(stage)
        schedule = stage_schedule(stage)
        assert schedule["template_ids"] == tuple(spec["template_ids"])
        assert schedule["tasks"] == spec["tasks"]
        assert schedule["seed"] == FROZEN_SEED == 20260906
        assert len(schedule["warmup"]) == spec["warmup_passes"]
        assert len(schedule["measured"]) == spec["repetitions"] == 1
        assert schedule["measured_seeds"] == (FROZEN_SEED + 1,)
        expected_warmup = () if stage != "freeze" else (FROZEN_SEED - 1,)
        assert schedule["warmup_seeds"] == expected_warmup
        assert schedule["measured"][0] == list(
            generate_task_instances(spec["template_ids"], spec["tasks"], FROZEN_SEED + 1)
        )
        assert len(schedule["measured"][0]) == spec["tasks"]
        if stage == "freeze":
            assert schedule["warmup"][0] == list(
                generate_task_instances(spec["template_ids"], spec["tasks"], FROZEN_SEED - 1)
            )
            assert schedule["warmup"][0] != schedule["measured"][0]

    @pytest.mark.parametrize("stage", AUTHORIZED_STAGES)
    def test_p2c_and_p1_runner_schedules_are_identical_in_order(
        self, stage, ready_cli, tmp_path, monkeypatch, capsys
    ):
        """The production runner's own pass schedule, captured at ``_run_pass``
        for P1 and P2C, is the same ordered sequence for every pass."""
        recorded: dict[str, dict] = {}
        specs: dict[str, RealRunSpec] = {}
        real_run = realbench.run_real_cell

        def recording_run(spec, client, **kwargs):
            specs[spec.run_label] = spec
            assert kwargs.get("sealed_tasks") is None
            return real_run(spec, client, **kwargs)

        monkeypatch.setattr(realbench, "run_real_cell", recording_run)
        _install_offline_endpoint(monkeypatch)
        expected_pass_count = stage_spec(stage)["warmup_passes"] + 1
        for candidate in ("P1", "P2C"):
            sink: dict = {}
            _record_schedules(monkeypatch, sink, passes=expected_pass_count)
            path = _write_config(tmp_path, candidate, stage)
            assert main(_argv(path, candidate=candidate, stage=stage)) == 1
            assert "BLOCKED" in capsys.readouterr().err
            recorded[candidate] = sink
        p1, p2c = recorded["P1"], recorded["P2C"]
        schedule = stage_schedule(stage)
        expected_passes = [
            _instance_tuples(p) for p in (*schedule["warmup"], *schedule["measured"])
        ]
        assert p1["passes"] == p2c["passes"] == expected_passes
        assert len(p1["passes"]) == stage_spec(stage)["warmup_passes"] + 1
        assert len(p1["passes"][-1]) == stage_spec(stage)["tasks"]
        assert p1["scenarios"] == p2c["scenarios"] == _scenario_bytes(catalog())
        assert set(p1["scenarios"]) == set(catalog())
        # Settings differ only by the executed workload contract.
        assert dataclasses.replace(p1["settings"], workload_version=None) == dataclasses.replace(
            p2c["settings"], workload_version=None
        )
        assert p1["settings"].workload_version == "2.4.1"
        assert p2c["settings"].workload_version == "2.5.0"
        assert p1["settings"].seed == p2c["settings"].seed == 20260906
        assert p1["settings"].max_tokens == p2c["settings"].max_tokens == 1024
        # The assembled run specifications differ only in label, workload, controller.
        p1_spec = dataclasses.asdict(specs[f"qual-a-p1-{stage}"])
        p2c_spec = dataclasses.asdict(specs[f"qual-a-p2c-{stage}"])
        assert _differing_paths(p1_spec, p2c_spec) == [
            "controller",
            "generation.workload_version",
            "run_label",
            "workload_version",
        ]
        assert p2c_spec["sealed_set"] is None and p1_spec["sealed_set"] is None
        assert p2c_spec["template_ids"] == tuple(stage_spec(stage)["template_ids"])
        assert p2c_spec["warmup_passes"] == stage_spec(stage)["warmup_passes"]
        assert p2c_spec["seed"] == 20260906
        assert p2c_spec["controller"] == "evidence-grounding-v1" and p1_spec["controller"] is None

    def test_freeze_schedule_is_two_hundred_tasks_with_one_warmup(self):
        schedule = stage_schedule("freeze")
        assert len(schedule["measured"][0]) == 200
        assert len(schedule["warmup"]) == 1 and len(schedule["warmup"][0]) == 200
        assert schedule["template_ids"] == tuple(catalog())
        development, holdout = stage_schedule("development"), stage_schedule("holdout")
        assert len(development["measured"][0]) == len(holdout["measured"][0]) == 20
        assert set(i.template_id for i in development["measured"][0]).isdisjoint(
            i.template_id for i in holdout["measured"][0]
        )


# --- zero custody access -----------------------------------------------------------------


class TestP2CZeroCustody:
    def test_requires_no_sealed_set_at_any_stage(self):
        assert SEALED_CANDIDATES == ("P2",)
        for stage in AUTHORIZED_STAGES:
            assert requires_sealed_set("P2C", stage) is False
            assert requires_sealed_set("P1", stage) is False
            config = qualification_config_dict("P2C", stage)
            assert "sealed_set" not in config
            validate_authorized_qualification_config(config, candidate_id="P2C", stage=stage)
            assert binding_from_config(config, candidate_id="P2C", stage=stage) is None
            assert (
                resolve_sealed_stage(
                    config, candidate_id="P2C", stage=stage, custody_dir=None, repo=Path("/repo")
                )
                is None
            )
        assert requires_sealed_set("P2", "development") is True
        assert requires_sealed_set("P2", "holdout") is True

    def test_p2c_development_executes_the_catalog_with_zero_custody_reads(
        self, ready_cli, tmp_path, monkeypatch, capsys
    ):
        external = ready_cli
        # A real synthetic custody package exists on disk; P2C must never
        # touch it even though it is adjacent and readable.
        custody_root = tmp_path / "custody"
        write_synthetic_custody(custody_root, commit=COMMIT)
        custody_calls: list[str] = []

        def forbidden(name):
            def _fail(*args, **kwargs):
                custody_calls.append(name)
                raise AssertionError(f"{name} must not run for P2C")

            return _fail

        for module, name in (
            (sealed_binding_module, "load_sealed_stage"),
            (sealed_binding_module, "_load"),
            (custody, "load_stage_index"),
            (custody, "read_stage_blob"),
            (custody, "validate_public_manifest"),
            (custody, "require_external_directory"),
            (custody, "load_bundle_directory"),
        ):
            monkeypatch.setattr(module, name, forbidden(name))
        resolved: list[object] = []

        def observing_resolve(config, **kwargs):
            result = resolve_sealed_stage(config, **kwargs)
            resolved.append(result)
            return result

        monkeypatch.setattr(sealed_binding_module, "resolve_sealed_stage", observing_resolve)
        opened: list[str] = []
        real_open = builtins.open
        real_io_open = io.open
        real_os_open = os.open

        def spy_open(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        def spy_io_open(file, *args, **kwargs):
            opened.append(str(file))
            return real_io_open(file, *args, **kwargs)

        def spy_os_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_os_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", spy_open)
        monkeypatch.setattr(io, "open", spy_io_open)
        monkeypatch.setattr(os, "open", spy_os_open)
        specs: list[RealRunSpec] = []
        sealed_seen: list[object] = []
        real_run = realbench.run_real_cell

        def recording_run(spec, client, **kwargs):
            specs.append(spec)
            sealed_seen.append(kwargs.get("sealed_tasks"))
            return real_run(spec, client, **kwargs)

        monkeypatch.setattr(realbench, "run_real_cell", recording_run)
        _install_offline_endpoint(monkeypatch)
        path = _write_config(tmp_path, "P2C", "development")
        assert main(_argv(path, candidate="P2C", stage="development")) == 0
        assert custody_calls == []
        assert not [p for p in opened if p.startswith(str(custody_root))]
        assert resolved == [None]
        assert sealed_seen == [None]
        spec = specs[0]
        assert spec.sealed_set is None
        assert spec.template_ids == tuple(stage_spec("development")["template_ids"])
        assert spec.workload_version == "2.5.0" and spec.controller == "evidence-grounding-v1"
        assert spec.generation.temperature == 0.2 and spec.generation.seed == 20260906
        assert spec.tasks_per_repetition == 20 and spec.warmup_passes == 0
        assert spec.artifact_family == "qualification-runs"
        report = json.loads(capsys.readouterr().out)
        assert report["candidate_id"] == "P2C"
        assert report["workload_version"] == "2.5.0"
        assert report["controller"] == "evidence-grounding-v1"
        assert report["candidate_identity_sha256"] == FROZEN_P2C_IDENTITY_SHA256
        assert "sealed_set" not in report
        assert report["controlled_experiment"] == p2c_experiment_record()
        assert report["controlled_experiment"]["control_candidate"] == "P1"
        assert report["controlled_experiment"]["blind_generalization_evidence"] is False
        assert report["matched_control"]["region"] == "us-sea"
        assert report["matched_control"]["kind"] == "matched-p1-development-control"
        assert report["matched_control"]["p1_run_label"] == "qual-p1"
        run_dir = external / "qualification-runs" / "qual-a-p2c-development"
        manifests = sorted(run_dir.glob("*.manifest.json"))
        assert len(manifests) == 1
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        workload = manifest["workload"]
        assert workload["task_source"] == {"kind": "catalog"}
        assert workload["catalog_digest"] == catalog_digest()
        assert workload["version"] == "2.5.0"
        assert workload["controller"] == "evidence-grounding-v1"
        assert "sealed_set" not in workload
        blob = json.dumps(report) + json.dumps(manifest)
        for forbidden in ("127.0.0.1", "/opt/models/", str(custody_root), "obs-", "syn-"):
            assert forbidden not in blob

    def test_sealed_fields_and_custody_dir_are_refused_for_p2c(self, tmp_path):
        for stage in AUTHORIZED_STAGES:
            config = qualification_config_dict("P2C", stage)
            section = placeholder_binding(stage if stage != "freeze" else "development")
            with pytest.raises(ConfigError, match=r"P2C .* refuses config keys: sealed_set"):
                validate_authorized_qualification_config(
                    dict(config, sealed_set=section), candidate_id="P2C", stage=stage
                )
            # The generic D-0024 rule refuses the same thing independently.
            with pytest.raises(SealedSetError, match="binding-not-applicable"):
                binding_from_config(
                    dict(config, sealed_set=section), candidate_id="P2C", stage=stage
                )
            with pytest.raises(ConfigError, match="--custody-dir is refused"):
                require_p2c_runtime("P2C", custody_dir=str(tmp_path))
            with pytest.raises(SealedSetError, match="custody-dir-not-applicable"):
                resolve_sealed_stage(
                    config,
                    candidate_id="P2C",
                    stage=stage,
                    custody_dir=str(tmp_path),
                    repo=Path("/repo"),
                )
        require_p2c_runtime("P2C", custody_dir=None)
        require_p2c_runtime("P2", custody_dir=str(tmp_path))

    def test_catalog_execution_guard(self):
        development = tuple(stage_spec("development")["template_ids"])
        require_p2c_catalog_execution(
            "P2C", stage="development", template_ids=development, sealed_set=None, sealed_tasks=None
        )
        binding = SealedSetBinding.from_config(
            placeholder_binding("development"), stage="development"
        )
        with pytest.raises(ConfigError, match="must not execute sealed input"):
            require_p2c_catalog_execution(
                "P2C", stage="development", template_ids=None, sealed_set=binding, sealed_tasks=None
            )
        with pytest.raises(ConfigError, match="must not execute sealed input"):
            require_p2c_catalog_execution(
                "P2C",
                stage="development",
                template_ids=development,
                sealed_set=None,
                sealed_tasks=object(),
            )
        with pytest.raises(ConfigError, match="frozen catalog templates"):
            require_p2c_catalog_execution(
                "P2C", stage="development", template_ids=None, sealed_set=None, sealed_tasks=None
            )
        with pytest.raises(ConfigError, match="frozen catalog templates"):
            require_p2c_catalog_execution(
                "P2C",
                stage="development",
                template_ids=tuple(stage_spec("holdout")["template_ids"]),
                sealed_set=None,
                sealed_tasks=None,
            )
        with pytest.raises(ConfigError, match="frozen catalog templates"):
            require_p2c_catalog_execution(
                "P2C",
                stage="development",
                template_ids=tuple(reversed(development)),
                sealed_set=None,
                sealed_tasks=None,
            )
        # Other candidates are untouched by this guard.
        require_p2c_catalog_execution(
            "P2", stage="development", template_ids=None, sealed_set=binding, sealed_tasks=object()
        )


# --- fail before the client ----------------------------------------------------------------


def _mutations():
    yield "sealed_set", lambda c: c.__setitem__("sealed_set", placeholder_binding("development"))
    yield "workload-2.4.1", lambda c: c.__setitem__("workload_version", "2.4.1")
    yield "workload-2.4.0", lambda c: c.__setitem__("workload_version", "2.4.0")
    yield "workload-unknown", lambda c: c.__setitem__("workload_version", "9.9.9")
    yield "controller-none", lambda c: c.__setitem__("controller", None)
    yield "controller-v2", lambda c: c.__setitem__("controller", "evidence-grounding-v2")
    yield "temperature", lambda c: c["generation"].__setitem__("temperature", 1.0)
    yield "seed", lambda c: c["generation"].__setitem__("seed", 20260907)
    yield "max_tokens", lambda c: c["generation"].__setitem__("max_tokens", 512)
    yield "top_p", lambda c: c["generation"].__setitem__("top_p", 0.9)
    yield "prompt-key", lambda c: c.__setitem__("system_prompt", "x")
    yield "prompt-in-generation", lambda c: c["generation"].__setitem__("prompt", "x")
    yield "evaluator-key", lambda c: c.__setitem__("evaluator_version", "3.0.0")
    yield "template_ids", lambda c: c.__setitem__("template_ids", ["dns-failures-001"])
    yield "frozen_template_id", lambda c: c.__setitem__("frozen_template_id", "dns-failures-001")
    yield "frozen_template_ids", lambda c: c.__setitem__("frozen_template_ids", [])
    yield "private_scenarios", lambda c: c.__setitem__("private_scenarios", [{}])
    yield "task_source", lambda c: c.__setitem__("task_source", {"kind": "catalog"})
    yield "custody_dir-key", lambda c: c.__setitem__("custody_dir", "/anywhere")
    yield "tasks-19", lambda c: c.__setitem__("tasks_per_repetition", 19)
    yield "tasks-21", lambda c: c.__setitem__("tasks_per_repetition", 21)
    yield "warmup-1", lambda c: c.__setitem__("warmup_passes", 1)
    yield "repetitions-2", lambda c: c.__setitem__("repetitions", 2)
    yield "candidate-p2", lambda c: c.__setitem__("candidate_id", "P2")
    yield "candidate-p1", lambda c: c.__setitem__("candidate_id", "P1")


class TestP2CFailsBeforeClient:
    @pytest.mark.parametrize(
        ("label", "mutate"), list(_mutations()), ids=lambda v: v if isinstance(v, str) else ""
    )
    def test_invalid_config_blocks_with_zero_client_construction(
        self, label, mutate, ready_cli, tmp_path, monkeypatch, capsys
    ):
        constructed, streamed = _spy_client(monkeypatch)
        runs: list[object] = []
        monkeypatch.setattr(realbench, "run_real_cell", lambda *a, **k: runs.append(1))
        config = qualification_config_dict("P2C", "development")
        mutate(config)
        path = tmp_path / f"{label}.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        assert main(_argv(path, candidate="P2C", stage="development")) == 1
        err = capsys.readouterr().err
        assert err.startswith("BLOCKED")
        assert constructed == [] and streamed == [] and runs == []
        with pytest.raises((ConfigError, QualificationError)):
            validate_authorized_qualification_config(
                config, candidate_id="P2C", stage="development"
            )

    def test_custody_dir_blocks_before_the_config_is_read(
        self, ready_cli, tmp_path, monkeypatch, capsys
    ):
        constructed, streamed = _spy_client(monkeypatch)
        runs: list[object] = []
        monkeypatch.setattr(realbench, "run_real_cell", lambda *a, **k: runs.append(1))
        reads: list[object] = []
        original = qualification.load_qualification_config
        monkeypatch.setattr(
            qualification,
            "load_qualification_config",
            lambda *a, **k: reads.append(1) or original(*a, **k),
        )
        custody_root = tmp_path / "custody"
        write_synthetic_custody(custody_root, commit=COMMIT)
        for stage in AUTHORIZED_STAGES:
            path = _write_config(tmp_path, "P2C", stage)
            assert main(_argv(path, candidate="P2C", stage=stage, custody_dir=custody_root)) == 1
            err = capsys.readouterr().err
            assert "BLOCKED" in err and "--custody-dir is refused" in err
            assert str(custody_root) not in err
        assert reads == [] and constructed == [] and streamed == [] and runs == []

    def test_contract_drift_blocks_before_the_client(
        self, ready_cli, tmp_path, monkeypatch, capsys
    ):
        constructed, streamed = _spy_client(monkeypatch)
        runs: list[object] = []
        monkeypatch.setattr(realbench, "run_real_cell", lambda *a, **k: runs.append(1))
        monkeypatch.setitem(agent_module.SYSTEM_PROMPTS_BY_VERSION, "2.5.0", SYSTEM_PROMPT_V240)
        path = _write_config(tmp_path, "P2C", "development")
        assert main(_argv(path, candidate="P2C", stage="development")) == 1
        assert "P2C contract drift" in capsys.readouterr().err
        assert constructed == [] and streamed == [] and runs == []

    def test_assembled_spec_guard_runs_before_the_client(
        self, ready_cli, tmp_path, monkeypatch, capsys
    ):
        """If the stage split were tampered after validation, the assembled
        spec guard still stops the run before the client exists."""
        constructed, streamed = _spy_client(monkeypatch)
        runs: list[object] = []
        monkeypatch.setattr(realbench, "run_real_cell", lambda *a, **k: runs.append(1))
        real_cls = realbench.RealRunSpec
        holdout = tuple(stage_spec("holdout")["template_ids"])

        def tampered(**kwargs):
            kwargs["template_ids"] = holdout
            return real_cls(**kwargs)

        monkeypatch.setattr(realbench, "RealRunSpec", tampered)
        path = _write_config(tmp_path, "P2C", "development")
        assert main(_argv(path, candidate="P2C", stage="development")) == 1
        assert "frozen catalog templates" in capsys.readouterr().err
        assert constructed == [] and streamed == [] and runs == []
        binding = SealedSetBinding.from_config(
            placeholder_binding("development"), stage="development"
        )

        def sealed_tamper(**kwargs):
            kwargs["template_ids"] = None
            kwargs["sealed_set"] = binding
            return real_cls(**kwargs)

        monkeypatch.setattr(realbench, "RealRunSpec", sealed_tamper)
        assert main(_argv(path, candidate="P2C", stage="development")) == 1
        assert "must not execute sealed input" in capsys.readouterr().err
        assert constructed == [] and streamed == [] and runs == []


# --- P2 and the other candidates are preserved ------------------------------------------


class TestOthersPreserved:
    def test_p2_remains_the_sealed_candidate(self):
        assert SEALED_CANDIDATES == ("P2",)
        assert "P2" in AUTHORIZED_CANDIDATES
        config = qualification_config_dict("P2", "development")
        assert "sealed_set" in config
        validate_authorized_qualification_config(config, candidate_id="P2", stage="development")
        missing = {k: v for k, v in config.items() if k != "sealed_set"}
        with pytest.raises(SealedSetError, match="binding-missing"):
            validate_authorized_qualification_config(
                missing, candidate_id="P2", stage="development"
            )
        with pytest.raises(SealedSetError, match="custody-dir-required"):
            resolve_sealed_stage(
                config, candidate_id="P2", stage="development", custody_dir=None, repo=Path("/repo")
            )
        freeze = qualification_config_dict("P2", "freeze")
        assert "sealed_set" not in freeze
        validate_authorized_qualification_config(freeze, candidate_id="P2", stage="freeze")

    def test_receipts_of_other_candidates_are_unchanged(self):
        def receipt(candidate, stage, sealed=None):
            return sanitized_receipt(
                run_label=f"qual-a-{candidate.lower()}-{stage}",
                candidate_id=candidate,
                stage=stage,
                config_sha256="abc",
                identity_digest=candidate_identity_digest(candidate),
                gates={"stopped": False},
                files=(),
                stopped=False,
                sealed_set=sealed,
            )

        for candidate, stage in (
            ("C1", "development"),
            ("C2", "holdout"),
            ("P1", "freeze"),
            ("P2", "freeze"),
        ):
            assert "controlled_experiment" not in receipt(candidate, stage)
        binding = SealedSetBinding.from_config(placeholder_binding("holdout"), stage="holdout")
        p2 = receipt("P2", "holdout", binding)
        assert "controlled_experiment" not in p2 and p2["sealed_set"] == binding.provenance()
        p2c = receipt("P2C", "holdout")
        assert p2c["controlled_experiment"] == p2c_experiment_record()
        assert p2c["controller"] == "evidence-grounding-v1" and p2c["workload_version"] == "2.5.0"
        assert "sealed_set" not in p2c
        with pytest.raises(QualificationError, match="exactly for P2 dev/holdout"):
            receipt("P2C", "holdout", binding)

    def test_c1_c2_p1_configs_do_not_hit_the_p2c_rules(self):
        for candidate in ("C1", "C2", "P1", "P2"):
            config = qualification_config_dict(candidate, "freeze")
            config["template_ids"] = ["dns-failures-001"]
            # Unknown keys were never validated for other candidates; the P2C
            # key rule is scoped to P2C so their behavior is unchanged.
            require_p2c_config(config, candidate_id=candidate)
        for candidate in ("C1", "C2", "P1"):
            assert candidate_controller(candidate) is None
            assert "controller" not in json.loads(serialize_candidate(candidate))


# --- no private scenario or frozen-template support; no leakage --------------------------


class TestNoPrivateSupportOrLeakage:
    def test_no_private_scenario_or_frozen_template_fields_exist(self):
        fields = set(RealRunSpec.__dataclass_fields__)
        assert not {f for f in fields if "frozen_template" in f or "private" in f}
        assert "template_ids" in fields and "sealed_set" in fields
        assert list(inspect.signature(generate_task_instances).parameters) == [
            "template_ids",
            "tasks_per_repetition",
            "seed",
        ]
        modules = (qualification, realbench, sealed_binding_module, agent_module, evidence_module)
        for module in modules:
            names = {name for name in dir(module) if "frozen_template" in name.lower()}
            names |= {name for name in dir(module) if "private_scenario" in name.lower()}
            assert names == set(), names
        assert {
            "frozen_template_id",
            "frozen_template_ids",
            "private_scenarios",
        } <= P2C_FORBIDDEN_CONFIG_KEYS
        tokens = ("frozen_template_id", "private_scenario")
        for module in modules[1:]:
            source = inspect.getsource(module)
            assert not any(token in source for token in tokens), module.__name__
        source = inspect.getsource(qualification)
        refusal = source[source.index("P2C_FORBIDDEN_CONFIG_KEYS = frozenset(") :]
        refusal = refusal[: refusal.index(")\n")]
        for token in tokens:
            # The only mentions are the refusal list itself.
            assert source.count(token) == refusal.count(token) > 0
        for stage in AUTHORIZED_STAGES:
            for instance in stage_schedule(stage)["measured"][0]:
                assert instance.template_id in catalog()

    def test_controller_text_carries_no_catalog_answer_or_scenario_content(self):
        tokens = {t for t in _catalog_answer_tokens() if len(t) >= 6}
        scenario = catalog()[SCENARIO_ID]
        client = ScriptedClient([health(), search("audit"), cite_all])
        settings = GenerationSettings(
            temperature=0.2,
            top_p=0.95,
            max_tokens=1024,
            seed=FROZEN_SEED,
            reasoning_mode=True,
            workload_version=candidate_workload_version("P2C"),
        )
        execution = run_task(
            scenario,
            client,
            SimulatedToolbox(scenario, clock=FakeClock()),
            settings,
            timeout_s=30.0,
            clock=FakeClock(),
        )
        assert execution.status == "completed"
        assert client.seen[0][0][0].content == SYSTEM_PROMPT_V241
        summary = execution.evidence_grounding
        assert summary["controller"] == "evidence-grounding-v1"
        summary_blob = json.dumps(summary)
        for token in tokens:
            assert token not in summary_blob
        ids: list[str] = []
        for message in client.seen[-1][0]:
            if message.role != "tool":
                continue
            payload = json.loads(message.content)
            if isinstance(payload, dict) and OBSERVATION_ID_FIELD in payload:
                ids.append(payload[OBSERVATION_ID_FIELD])
        assert ids and all(_OBSERVATION_ID_RE.match(i) for i in ids)
        for token in tokens:
            for value in ids:
                assert token not in value
        # Every string the controller module itself defines is content-free.
        for name in dir(evidence_module):
            value = getattr(evidence_module, name)
            if isinstance(value, str):
                for token in tokens:
                    assert token not in value, name
        # The summary is counts only: no ids, no payloads, no answers.
        assert set(summary) == {
            "controller",
            "observations",
            "eligible_observations",
            "terminal_attempts",
            "rejected_terminal_attempts",
            "accepted_evidence_refs",
        }
        assert all(isinstance(v, int) for k, v in summary.items() if k != "controller")


# --- CLI surface -------------------------------------------------------------------------


class TestP2CCli:
    def test_parser_accepts_p2c_and_documents_it(self, capsys):
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "qualify-agent",
                "--run-tag",
                RUN_TAG,
                "--run-label",
                "qual-a",
                "--candidate",
                "P2C",
                "--stage",
                "holdout",
                "--config",
                "/absolute/outside/qualify.json",
            ]
        )
        assert args.candidate == "P2C" and args.stage == "holdout" and args.custody_dir is None
        with pytest.raises(SystemExit) as caught:
            parser.parse_args(["qualify-agent", "--help"])
        assert caught.value.code == 0
        help_text = " ".join(capsys.readouterr().out.split())
        assert "P2C (the controlled public-catalog version of P2" in help_text
        assert "P1 is its control" in help_text
        assert "P2 (temperature 0.2, workload 2.5.0, evidence-grounding-v1 controller)" in help_text

    def test_validate_only_reports_p2c_without_custody(
        self, ready_cli, tmp_path, monkeypatch, capsys
    ):
        assert ready_cli.is_dir()
        constructed, streamed = _spy_client(monkeypatch)
        for stage in AUTHORIZED_STAGES:
            path = _write_config(tmp_path, "P2C", stage)
            argv = [*_argv(path, candidate="P2C", stage=stage)[:-2], "--validate-only"]
            assert main(argv) == 0
            report = json.loads(capsys.readouterr().out)
            assert report["mode"] == "validate-only" and report["executed"] is False
            assert report["model_client_constructed"] is False
            assert report["candidate_id"] == "P2C"
            assert report["sealed_input"] is False and "sealed_set" not in report
            assert report["controlled_experiment"] == p2c_experiment_record()
            assert report["candidate_identity_sha256"] == FROZEN_P2C_IDENTITY_SHA256
            if stage == "development":
                assert report["matched_control"]["region"] == "us-sea"
                assert report["matched_control"]["p1_run_label"] == "qual-p1"
            else:
                assert "matched_control" not in report
            argv_custody = [*argv, "--custody-dir", str(tmp_path)]
            assert main(argv_custody) == 1
            assert "--custody-dir is refused" in capsys.readouterr().err
        assert constructed == [] and streamed == []

    def test_approval_phrase_names_p2c(self):
        phrase = approval_phrase(RUN_TAG, "qual-a", "P2C", "0" * 64)
        assert "candidate P2C" in phrase
        assert approval_phrase(RUN_TAG, "qual-a", "P2", "0" * 64) != phrase
