"""W1 / W2 candidate pair and its same-session control (decision D-0031).

Covers candidate identity separation, the historical us-ord digests of
C1/C2/P1/P2/P2C/W1/W2, the current ca-central identity pins, the explicit
single treatment difference,
the frozen pair contract, config refusals, the W1 -> W2 matched control
(including that a P1 control can never authenticate W2 and vice versa), the
unchanged twenty-task / 0.40 development gate, and the offline CLI paths.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_evidence_grounding import (
    RUN_TAG,
    _config_sha256,
    _observed,
    _qual_argv,
    _ready_ledger,
    qualification_config_dict,
)
from test_qualification import _differing_paths

from blackwell_lab.cloud import (
    cli,
    lifecycle,
    provenance,
    qualification,
    realbench,
)
from blackwell_lab.cloud.cli import main
from blackwell_lab.cloud.matched_control import (
    CONTROL_KIND,
    P1_P2C_PAIR,
    W1_W2_PAIR,
    W3_CONTROL_KIND,
    W3_W4_PAIR,
    W5_CONTROL_KIND,
    W5_W6_PAIR,
    W_CONTROL_KIND,
    audit_matched_controls,
    authenticate_matched_development_control,
    control_record_path,
    install_verified_p1_development_control,
    pair_for_control,
    pair_for_treatment,
    require_development_control_section,
)
from blackwell_lab.cloud.qualification import (
    ALL_AUTHORIZED_CANDIDATES,
    AUTHORIZED_CANDIDATES,
    CANDIDATE_CONTROLLERS,
    CANDIDATE_WORKLOAD_VERSIONS,
    CATALOG_CANDIDATES,
    DEVELOPMENT_CONTROL_PAIRS,
    DEVELOPMENT_QUALITY_FLOOR,
    DEVELOPMENT_TASKS,
    UNKNOWN_CANDIDATE_MESSAGE,
    W_PAIR_CONTRACT,
    WORKFLOW_CANDIDATES,
    QualificationError,
    approval_phrase,
    candidate_controller,
    candidate_identity_digest,
    candidate_treatments,
    candidate_workload_version,
    experimental_behavior_fields,
    frozen_candidate_fields,
    is_catalog_candidate,
    p2c_experiment_record,
    require_w_catalog_execution,
    require_w_config,
    require_w_pair_contract,
    require_w_runtime,
    sanitized_receipt,
    stage_spec,
    validate_authorized_qualification_config,
    w_pair_experiment_record,
)
from blackwell_lab.workload.validation import ConfigError

COMMIT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

#: D-0030/D-0031 us-ord identity contract. Historical. D-0032 recomputed
#: these after the region lock moved to ca-central.
HISTORICAL_IDENTITY_DIGESTS = {
    "C1": "fe804d2a14be46f89b32f760d2f7540e05769db83ce041086ec6d8a5477f33bd",
    "C2": "e44bb30ede6e1002eb8751b940b62e58c93901e2031e36ea777eead372f3030d",
    "P1": "d244d01308b525d970d7e706a8acecc3f4e6e10009ef962197e2cdfa926453de",
    "P2": "11cbed1d8a4672fb19b4df35f3a6c25ce9ca536a25d2208063495a2d8b13d138",
    "P2C": "3bd46c049832a66f8de2f68d38195691361462961308ac76501f3f523ea1fd4c",
    "W1": "1dfedcabaf0759e8e03ae3ea270ad482f7b7704be6940c78ff95b4870751fe3b",
    "W2": "da9060df7a03a2f3a7a9d8d9d1bcab9a91b18254b365849abb5e25737de208c7",
}
#: Current D-0032 ca-central identities, recomputed by candidate_identity_digest.
CA_CENTRAL_IDENTITY_DIGESTS = {
    "C1": "22d92d3351ed92b7b46ba0e1c0756c6ecf8c47abe0ae2b6070b9b821e482f27e",
    "C2": "062bc2063a967638a5943fd760126703a66c34f1d9ec07a4f99e245c538f421b",
    "P1": "be54086f77c59f334b5b77bbfbd04d917c295d875a1542c79aa9cb69d0da3bc3",
    "P2": "8811a4be01c8befab0984c806879d0e1940faa94855b23aab8fe46a2109ad215",
    "P2C": "c43cfe22e52a0756b70d11df97e3ac37e64c989ffa259fa703c6b3b3a80fbf46",
    "W1": "2b7c5745084f4459af66e58fa5701d5c513108a9536385acbed00722c02e68cf",
    "W2": "06d673972a696efcddc9ce00d6c9f6a15a032e0c516d4dc8b54f01dbba0917a2",
}
#: The workflow pair's current identities, pinned so a silent change fails CI.
WORKFLOW_IDENTITY_DIGESTS = {
    "W1": CA_CENTRAL_IDENTITY_DIGESTS["W1"],
    "W2": CA_CENTRAL_IDENTITY_DIGESTS["W2"],
}


class TestCandidateTables:
    def test_historical_tables_are_byte_identical(self):
        assert AUTHORIZED_CANDIDATES == ("C1", "C2", "P1", "P2", "P2C")
        assert CATALOG_CANDIDATES == ("C1", "C2", "P1", "P2C")
        assert set(CANDIDATE_WORKLOAD_VERSIONS) == set(AUTHORIZED_CANDIDATES)
        assert set(CANDIDATE_CONTROLLERS) == set(AUTHORIZED_CANDIDATES)
        text = Path("docs/decision-log.md").read_text(encoding="utf-8")
        for candidate, digest in HISTORICAL_IDENTITY_DIGESTS.items():
            assert digest in text, candidate
            assert candidate_identity_digest(candidate) != digest, candidate
        for candidate, digest in CA_CENTRAL_IDENTITY_DIGESTS.items():
            assert candidate_identity_digest(candidate) == digest, candidate

    def test_workflow_pair_lives_in_its_own_tables(self):
        assert WORKFLOW_CANDIDATES == ("W1", "W2")
        assert ALL_AUTHORIZED_CANDIDATES == (
            "C1",
            "C2",
            "P1",
            "P2",
            "P2C",
            "W1",
            "W2",
            "W3",
            "W4",
            "W5",
            "W6",
        )
        assert not set(WORKFLOW_CANDIDATES) & set(AUTHORIZED_CANDIDATES)
        assert DEVELOPMENT_CONTROL_PAIRS == {"P2C": "P1", "W2": "W1", "W4": "W3", "W6": "W5"}
        for candidate in WORKFLOW_CANDIDATES:
            assert is_catalog_candidate(candidate)
            assert candidate_controller(candidate) == "workflow-controller-v1"
        assert candidate_workload_version("W1") == "2.6.0"
        assert candidate_workload_version("W2") == "2.6.1"
        assert candidate_treatments("W1") == ()
        assert candidate_treatments("W2") == ("evidence-refs",)
        for candidate in AUTHORIZED_CANDIDATES:
            assert candidate_treatments(candidate) == ()
        assert "W1" in UNKNOWN_CANDIDATE_MESSAGE and "W2" in UNKNOWN_CANDIDATE_MESSAGE

    def test_workflow_identities_are_distinct_and_pinned(self):
        digests = {c: candidate_identity_digest(c) for c in ALL_AUTHORIZED_CANDIDATES}
        assert len(set(digests.values())) == 11
        for candidate, digest in CA_CENTRAL_IDENTITY_DIGESTS.items():
            assert digests[candidate] == digest, candidate
        for candidate, digest in WORKFLOW_IDENTITY_DIGESTS.items():
            assert digests[candidate] == digest, candidate
        w1 = frozen_candidate_fields("W1")
        w2 = frozen_candidate_fields("W2")
        assert "treatments" not in w1
        assert w2["treatments"] == ["evidence-refs"]
        assert w1["controller"] == w2["controller"] == "workflow-controller-v1"
        for candidate in ("C1", "C2", "P1"):
            assert "treatments" not in frozen_candidate_fields(candidate)
            assert "controller" not in frozen_candidate_fields(candidate)

    def test_w2_differs_from_w1_only_in_identity_version_and_treatment(self):
        w1 = experimental_behavior_fields("W1")
        w2 = experimental_behavior_fields("W2")
        assert _differing_paths(w1, w2) == ["candidate_id", "treatments", "workload_version"]
        assert w1["system_prompt"] == w2["system_prompt"]
        assert w1["system_prompt"] == experimental_behavior_fields("P1")["system_prompt"]

    def test_pair_contract_is_satisfied_and_frozen(self):
        assert require_w_pair_contract() == W_PAIR_CONTRACT
        assert W_PAIR_CONTRACT["control_candidate"] == "W1"
        assert W_PAIR_CONTRACT["treatment_candidate"] == "W2"
        assert W_PAIR_CONTRACT["treatment_treatments"] == ["evidence-refs"]
        assert W_PAIR_CONTRACT["temperature"] == 0.2
        assert W_PAIR_CONTRACT["max_turns"] == 12

    def test_pair_contract_drift_fails_closed(self, monkeypatch):
        monkeypatch.setattr(qualification, "DEVELOPMENT_QUALITY_FLOOR", 0.30)
        with pytest.raises(QualificationError, match="development floor"):
            require_w_pair_contract()

    def test_official_development_gate_is_unchanged(self):
        spec = stage_spec("development")
        assert DEVELOPMENT_TASKS == spec["tasks"] == 20
        assert DEVELOPMENT_QUALITY_FLOOR == spec["quality_floor"] == 0.40
        assert len(spec["template_ids"]) == 6

    def test_unknown_candidate_messages_name_the_pair(self):
        with pytest.raises(ConfigError, match="W5, or W6"):
            frozen_candidate_fields("W7")
        with pytest.raises(ConfigError, match="W5, or W6"):
            candidate_controller("W7")


class TestConfigs:
    @pytest.mark.parametrize("candidate", WORKFLOW_CANDIDATES)
    @pytest.mark.parametrize("stage", ("development", "holdout", "freeze"))
    def test_catalog_configs_validate_for_every_stage(self, candidate, stage):
        config = qualification_config_dict(candidate, stage)
        if candidate == "W2" and stage == "development":
            config["development_control"] = {
                "schema_version": "1.0.0",
                "run_tag": RUN_TAG,
                "w1_run_label": "qual-w1",
                "canonical_commit": COMMIT,
                "region": "ca-central",
                "config_sha256": "0" * 64,
                "result_sha256": "0" * 64,
                "control_record_sha256": "0" * 64,
                "resource_identity_sha256": "0" * 64,
                "ledger_sha256": "0" * 64,
            }
        validate_authorized_qualification_config(config, candidate_id=candidate, stage=stage)

    def test_sealed_and_schedule_keys_are_refused_with_a_pair_reason(self):
        for key in ("sealed_set", "custody_dir", "template_ids", "private_scenarios"):
            config = qualification_config_dict("W1", "development")
            config[key] = {"x": 1}
            with pytest.raises(ConfigError, match="W1 executes the public catalog"):
                require_w_config(config, candidate_id="W1")

    def test_pins_cannot_be_overridden(self):
        config = qualification_config_dict("W2", "development")
        config["controller"] = "evidence-grounding-v1"
        with pytest.raises(ConfigError, match="controller"):
            validate_authorized_qualification_config(config, candidate_id="W2", stage="development")
        config = qualification_config_dict("W2", "development")
        config["treatments"] = []
        with pytest.raises(ConfigError, match="treatments"):
            require_w_config(config, candidate_id="W2")
        config = qualification_config_dict("W1", "development")
        config["max_turns"] = 20
        with pytest.raises(ConfigError, match="max_turns"):
            require_w_config(config, candidate_id="W1")
        config = qualification_config_dict("W1", "development")
        config["generation"]["temperature"] = 0.7
        with pytest.raises(ConfigError):
            validate_authorized_qualification_config(config, candidate_id="W1", stage="development")

    def test_runtime_and_catalog_execution_guards(self):
        with pytest.raises(ConfigError, match="custody-dir is refused"):
            require_w_runtime("W1", custody_dir=str(Path("/custody")))
        require_w_runtime("P2", custody_dir=str(Path("/custody")))  # not the pair's concern
        spec = stage_spec("development")
        require_w_catalog_execution(
            "W2",
            stage="development",
            template_ids=spec["template_ids"],
            sealed_set=None,
            sealed_tasks=None,
        )
        with pytest.raises(ConfigError, match="frozen catalog templates"):
            require_w_catalog_execution(
                "W2",
                stage="development",
                template_ids=spec["template_ids"][:3],
                sealed_set=None,
                sealed_tasks=None,
            )
        with pytest.raises(ConfigError, match="sealed input"):
            require_w_catalog_execution(
                "W1", stage="holdout", template_ids=None, sealed_set=object(), sealed_tasks=None
            )
        for candidate in ("W5", "W6"):
            require_w_catalog_execution(
                candidate,
                stage="development",
                template_ids=spec["template_ids"],
                sealed_set=None,
                sealed_tasks=None,
            )
            for blocked in ("holdout", "freeze"):
                with pytest.raises(ConfigError, match="development catalog only"):
                    require_w_catalog_execution(
                        candidate,
                        stage=blocked,
                        template_ids=stage_spec(blocked)["template_ids"],
                        sealed_set=None,
                        sealed_tasks=None,
                    )

    def test_development_control_section_is_pair_specific(self):
        config = qualification_config_dict("W2", "development")
        with pytest.raises(ConfigError, match="W2 development requires"):
            require_development_control_section(config, candidate_id="W2", stage="development")
        p1_shaped = qualification_config_dict("P2C", "development")["development_control"]
        config["development_control"] = p1_shaped
        with pytest.raises(ConfigError, match="unexpected or missing fields"):
            require_development_control_section(config, candidate_id="W2", stage="development")
        w1_config = qualification_config_dict("W1", "development")
        w1_config["development_control"] = p1_shaped
        with pytest.raises(ConfigError, match="valid only on W2 development"):
            require_development_control_section(w1_config, candidate_id="W1", stage="development")
        p1_config = qualification_config_dict("P1", "development")
        p1_config["development_control"] = p1_shaped
        with pytest.raises(ConfigError, match="valid only on P2C development"):
            require_development_control_section(p1_config, candidate_id="P1", stage="development")

    def test_receipt_requires_and_restricts_matched_control_for_w2_development(self):
        base = dict(
            run_label="x-w2-development",
            candidate_id="W2",
            config_sha256="0" * 64,
            identity_digest=candidate_identity_digest("W2"),
            gates={"stopped": False, "continue": True},
            files=[],
            stopped=False,
        )
        with pytest.raises(QualificationError, match="W2 development receipt requires"):
            sanitized_receipt(stage="development", **base)
        with pytest.raises(QualificationError, match="valid only for P2C or W2 development"):
            sanitized_receipt(stage="holdout", matched_control={"kind": W_CONTROL_KIND}, **base)


class TestPairs:
    def test_pair_lookup_tables(self):
        assert pair_for_treatment("P2C") is P1_P2C_PAIR
        assert pair_for_treatment("W2") is W1_W2_PAIR
        assert pair_for_control("P1") is P1_P2C_PAIR
        assert pair_for_control("W1") is W1_W2_PAIR
        for other in ("C1", "C2", "P2"):
            assert pair_for_treatment(other) is None and pair_for_control(other) is None
        assert P1_P2C_PAIR.kind == CONTROL_KIND == "matched-p1-development-control"
        assert W1_W2_PAIR.kind == W_CONTROL_KIND == "matched-w1-development-control"
        assert W3_W4_PAIR.kind == W3_CONTROL_KIND == "matched-w3-development-control"
        assert W5_W6_PAIR.kind == W5_CONTROL_KIND == "matched-w5-development-control"
        assert P1_P2C_PAIR.label_key == "p1_run_label"
        assert W1_W2_PAIR.label_key == "w1_run_label"
        assert W3_W4_PAIR.label_key == "w3_run_label"
        assert pair_for_treatment("W4") is W3_W4_PAIR
        assert pair_for_control("W3") is W3_W4_PAIR
        assert pair_for_treatment("W6") is W5_W6_PAIR
        assert pair_for_control("W5") is W5_W6_PAIR
        assert W5_W6_PAIR.label_key == "w5_run_label"

    def test_historical_messages_are_unchanged(self):
        assert (
            P1_P2C_PAIR.message("missing")
            == "P2C development requires a completed same-session P1 control"
        )
        assert P1_P2C_PAIR.message("stopped") == (
            "P2C development control is not a completed non-stopped P1"
        )
        assert W1_W2_PAIR.message("missing") == (
            "W2 development requires a completed same-session W1 control"
        )


def _external(tmp_path, monkeypatch) -> Path:
    external = tmp_path / "external"
    monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
    paths = lifecycle.lifecycle_paths(external, RUN_TAG)
    lifecycle.write_private_json(paths.ledger_path, _ready_ledger())
    return external


class TestMatchedControlForTheWorkflowPair:
    def test_w1_control_authenticates_w2_and_is_pair_isolated(self, tmp_path, monkeypatch):
        external = _external(tmp_path, monkeypatch)
        config = qualification_config_dict("W2", "development")
        section = install_verified_p1_development_control(
            external,
            config,
            run_tag=RUN_TAG,
            p1_run_label="qual-w1",
            canonical_commit=COMMIT,
            pair=W1_W2_PAIR,
        )
        assert set(section) == set(W1_W2_PAIR.section_keys)
        record = json.loads(
            control_record_path(external, "qual-w1", pair=W1_W2_PAIR).read_text(encoding="utf-8")
        )
        assert record["kind"] == W_CONTROL_KIND
        assert record["candidate_id"] == "W1"
        assert record["workload_version"] == "2.6.0"
        assert record["identity_sha256"] == candidate_identity_digest("W1")
        block = authenticate_matched_development_control(
            config,
            results_dir=external,
            run_tag=RUN_TAG,
            p2c_run_label="qual-w2",
            pair=W1_W2_PAIR,
        )
        assert block["kind"] == W_CONTROL_KIND
        assert block["w1_run_label"] == "qual-w1"
        assert block["w1_identity_sha256"] == candidate_identity_digest("W1")
        assert "p1_run_label" not in block
        # The same section can never authenticate as the historical pair.
        with pytest.raises((ConfigError, QualificationError)):
            authenticate_matched_development_control(
                config, results_dir=external, run_tag=RUN_TAG, p2c_run_label="qual-w2"
            )
        assert audit_matched_controls(external) == []

    def test_p1_control_cannot_authenticate_w2(self, tmp_path, monkeypatch):
        external = _external(tmp_path, monkeypatch)
        p2c_config = qualification_config_dict("P2C", "development")
        install_verified_p1_development_control(
            external, p2c_config, run_tag=RUN_TAG, p1_run_label="qual-p1", canonical_commit=COMMIT
        )
        w2_config = qualification_config_dict("W2", "development")
        w2_config["development_control"] = {
            **{k: v for k, v in p2c_config["development_control"].items() if k != "p1_run_label"},
            "w1_run_label": "qual-p1",
        }
        with pytest.raises(QualificationError, match="W2 development requires"):
            authenticate_matched_development_control(
                w2_config,
                results_dir=external,
                run_tag=RUN_TAG,
                p2c_run_label="qual-w2",
                pair=W1_W2_PAIR,
            )

    def test_tampered_w1_record_kind_is_malformed(self, tmp_path, monkeypatch):
        external = _external(tmp_path, monkeypatch)
        config = qualification_config_dict("W2", "development")
        install_verified_p1_development_control(
            external,
            config,
            run_tag=RUN_TAG,
            p1_run_label="qual-w1",
            canonical_commit=COMMIT,
            pair=W1_W2_PAIR,
        )
        path = control_record_path(external, "qual-w1", pair=W1_W2_PAIR)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["kind"] = CONTROL_KIND
        payload = json.dumps(record, sort_keys=True, indent=2)
        path.write_text(payload, encoding="utf-8")
        config["development_control"]["control_record_sha256"] = hashlib.sha256(
            payload.encode("utf-8")
        ).hexdigest()
        with pytest.raises(QualificationError, match="malformed"):
            authenticate_matched_development_control(
                config,
                results_dir=external,
                run_tag=RUN_TAG,
                p2c_run_label="qual-w2",
                pair=W1_W2_PAIR,
            )
        failures = audit_matched_controls(external)
        assert failures and all("malformed" in f["error"] for f in failures)

    def test_schema_binds_kind_to_candidate(self):
        from blackwell_lab.cloud.matched_control import _load_schema

        schema = _load_schema()
        assert set(schema["properties"]["kind"]["enum"]) == {
            CONTROL_KIND,
            W_CONTROL_KIND,
            W3_CONTROL_KIND,
        }
        assert set(schema["properties"]["candidate_id"]["enum"]) == {"P1", "W1", "W3"}
        assert len(schema["oneOf"]) == 3
        bindings = {
            branch["properties"]["kind"]["const"]: branch["properties"]["candidate_id"]["const"]
            for branch in schema["oneOf"]
        }
        assert bindings == {
            CONTROL_KIND: "P1",
            W_CONTROL_KIND: "W1",
            W3_CONTROL_KIND: "W3",
        }

    def test_w3_control_authenticates_w4_and_refuses_cross_pair(self, tmp_path, monkeypatch):
        import jsonschema

        from blackwell_lab.cloud.matched_control import _load_schema

        external = _external(tmp_path, monkeypatch)
        config = qualification_config_dict("W4", "development")
        section = install_verified_p1_development_control(
            external,
            config,
            run_tag=RUN_TAG,
            p1_run_label="qual-w3",
            canonical_commit=COMMIT,
            pair=W3_W4_PAIR,
        )
        assert set(section) == set(W3_W4_PAIR.section_keys)
        record_path = control_record_path(external, "qual-w3", pair=W3_W4_PAIR)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        jsonschema.validate(record, _load_schema())
        assert record["kind"] == W3_CONTROL_KIND
        assert record["candidate_id"] == "W3"
        assert record["workload_version"] == "2.7.0"
        assert record["identity_sha256"] == candidate_identity_digest("W3")
        block = authenticate_matched_development_control(
            config,
            results_dir=external,
            run_tag=RUN_TAG,
            p2c_run_label="qual-w4",
            pair=W3_W4_PAIR,
        )
        assert block["kind"] == W3_CONTROL_KIND
        assert block["w3_run_label"] == "qual-w3"
        assert block["w3_identity_sha256"] == candidate_identity_digest("W3")
        assert "w1_run_label" not in block
        assert "p1_run_label" not in block
        assert audit_matched_controls(external) == []

        payload = record_path.read_bytes()
        w1_copy = control_record_path(external, "qual-w3", pair=W1_W2_PAIR)
        w1_copy.write_bytes(payload)
        w2 = qualification_config_dict("W2", "development")
        w2["development_control"] = {
            **{key: value for key, value in section.items() if key != "w3_run_label"},
            "w1_run_label": "qual-w3",
            "control_record_sha256": hashlib.sha256(payload).hexdigest(),
        }
        with pytest.raises(QualificationError, match="malformed"):
            authenticate_matched_development_control(
                w2,
                results_dir=external,
                run_tag=RUN_TAG,
                p2c_run_label="qual-w2",
                pair=W1_W2_PAIR,
            )

    def test_w4_does_not_authenticate_a_w1_control(self, tmp_path, monkeypatch):
        external = _external(tmp_path, monkeypatch)
        w2 = qualification_config_dict("W2", "development")
        section = install_verified_p1_development_control(
            external,
            w2,
            run_tag=RUN_TAG,
            p1_run_label="qual-w1",
            canonical_commit=COMMIT,
            pair=W1_W2_PAIR,
        )
        payload = control_record_path(external, "qual-w1", pair=W1_W2_PAIR).read_bytes()
        w3_copy = control_record_path(external, "qual-w1", pair=W3_W4_PAIR)
        w3_copy.write_bytes(payload)
        w4 = qualification_config_dict("W4", "development")
        w4["development_control"] = {
            **{key: value for key, value in section.items() if key != "w1_run_label"},
            "w3_run_label": "qual-w1",
            "control_record_sha256": hashlib.sha256(payload).hexdigest(),
        }
        with pytest.raises(QualificationError, match="malformed"):
            authenticate_matched_development_control(
                w4,
                results_dir=external,
                run_tag=RUN_TAG,
                p2c_run_label="qual-w4",
                pair=W3_W4_PAIR,
            )


class TestCli:
    @pytest.fixture
    def ready(self, tmp_path, monkeypatch):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        monkeypatch.setattr(cli, "_git_head", lambda: COMMIT)
        monkeypatch.setattr(cli, "_tree_clean", lambda: True)
        external = _external(tmp_path, monkeypatch)
        monkeypatch.setattr(
            provenance, "verify_live_provenance", lambda **kwargs: _observed(full_host=True)
        )
        return external

    def _write(self, tmp_path, config, name):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_validate_only_reports_the_pair_without_executing(self, tmp_path, monkeypatch, capsys):
        config = qualification_config_dict("W1", "holdout")
        path = self._write(tmp_path, config, "w1")
        argv = [*_qual_argv(path, candidate="W1", stage="holdout")[:-2], "--validate-only"]
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path / "external"))
        assert main(argv) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["executed"] is False
        assert report["candidate_id"] == "W1"
        assert report["controller"] == "workflow-controller-v1"
        assert report["controlled_experiment"]["kind"] == "workflow-controlled-pair"
        assert report["controlled_experiment"]["role"] == "control"
        assert report["controlled_experiment"]["historical_candidates_unchanged"] is True
        assert report["qualification_environment"]["ok"] is True

    def test_w2_development_refuses_without_a_same_session_w1_control(
        self, ready, tmp_path, capsys
    ):
        config = qualification_config_dict("W2", "development")
        path = self._write(tmp_path, config, "w2")
        assert main(_qual_argv(path, candidate="W2", stage="development")) == 1
        assert (
            "W2 development requires a completed same-session W1 control" in capsys.readouterr().err
        )

    def test_w1_development_completion_mints_a_w1_control_only(
        self, ready, tmp_path, monkeypatch, capsys
    ):
        from test_evidence_grounding import _FakeRecord, _stage_outcomes

        def fake_run(spec, *_args, **_kwargs):
            cell = ready / "qualification-runs" / spec.run_label
            cell.mkdir(parents=True, exist_ok=True)
            (cell / "w1.result.json").write_text("{}\n", encoding="utf-8")
            return [_FakeRecord(_stage_outcomes("development"))]

        monkeypatch.setattr(realbench, "run_real_cell", fake_run)
        config = qualification_config_dict("W1", "development")
        path = self._write(tmp_path, config, "w1")
        assert main(_qual_argv(path, candidate="W1", stage="development")) == 0
        receipt = json.loads(capsys.readouterr().out)
        assert receipt["candidate_id"] == "W1" and receipt["stopped"] is False
        family = ready / "qualification-runs"
        assert (family / "qual-a-w1-development-control.json").is_file()
        record = json.loads((family / "qual-a-w1-development-control.json").read_text())
        assert record["kind"] == W_CONTROL_KIND
        assert not list(family.glob("*-p1-development-control.json"))
        assert audit_matched_controls(ready) == []

    def test_w2_development_runs_against_an_authenticated_w1_control(
        self, ready, tmp_path, monkeypatch, capsys
    ):
        from test_evidence_grounding import _FakeRecord, _stage_outcomes

        monkeypatch.setattr(
            realbench,
            "run_real_cell",
            lambda *a, **k: [_FakeRecord(_stage_outcomes("development"))],
        )
        config = qualification_config_dict("W2", "development")
        install_verified_p1_development_control(
            ready,
            config,
            run_tag=RUN_TAG,
            p1_run_label="qual-w1",
            canonical_commit=COMMIT,
            pair=W1_W2_PAIR,
        )
        path = self._write(tmp_path, config, "w2")
        phrase = approval_phrase(RUN_TAG, "qual-a", "W2", _config_sha256(path))
        assert main(_qual_argv(path, candidate="W2", stage="development", approve=phrase)) == 0
        receipt = json.loads(capsys.readouterr().out)
        assert receipt["candidate_id"] == "W2"
        assert receipt["controlled_experiment"] == w_pair_experiment_record("W2")
        assert receipt["controlled_experiment"]["control_candidate"] == "W1"
        assert receipt["controlled_experiment"]["role"] == "treatment"
        assert receipt["controlled_experiment"]["treatments"] == ["evidence-refs"]
        assert receipt["matched_control"]["kind"] == W_CONTROL_KIND
        assert receipt["matched_control"]["w1_run_label"] == "qual-w1"
        assert "p1_run_label" not in json.dumps(receipt)
        # W2's completion never mints a control of its own.
        assert not list((ready / "qualification-runs").glob("*-w2-development-control.json"))


class TestReceiptProvenance:
    def _receipt(self, candidate: str, stage: str, **extra):
        return sanitized_receipt(
            run_label=f"qual-a-{candidate.lower()}-{stage}",
            candidate_id=candidate,
            stage=stage,
            config_sha256="ab",
            identity_digest=candidate_identity_digest(candidate),
            gates={"stopped": False, "continue": True},
            files=["measured.json"],
            stopped=False,
            **extra,
        )

    def test_w2_receipts_carry_the_pair_record_and_historical_receipts_do_not(self):
        p2c_block = {
            "kind": "controlled-public-catalog",
            "control_candidate": "P1",
            "treatment": "evidence-grounding-v1",
            "task_source": "catalog",
            "schedule": "d-0019-catalog",
            "blind_generalization_evidence": False,
            "comparable_with_private_sealed_scores": False,
        }
        w2_block = {
            "kind": "workflow-controlled-pair",
            "controller": "workflow-controller-v1",
            "control_candidate": "W1",
            "treatment_candidate": "W2",
            "role": "treatment",
            "treatments": ["evidence-refs"],
            "task_source": "catalog",
            "schedule": "d-0019-catalog",
            "historical_candidates_unchanged": True,
            "blind_generalization_evidence": False,
            "comparable_with_private_sealed_scores": False,
            "comparable_with_p1_p2c_scores": False,
        }
        assert p2c_experiment_record() == p2c_block
        assert w_pair_experiment_record("W2") == w2_block
        assert w_pair_experiment_record("W1")["role"] == "control"
        assert w_pair_experiment_record("W1")["treatments"] == []

        for stage in ("development", "holdout", "freeze"):
            p1 = self._receipt("P1", stage)
            assert "controlled_experiment" not in p1
            assert "workflow-controlled-pair" not in json.dumps(p1)
            p2c_extra = (
                {"matched_control": {"kind": "matched-p1-development-control"}}
                if stage == "development"
                else {}
            )
            p2c = self._receipt("P2C", stage, **p2c_extra)
            assert p2c["controlled_experiment"] == p2c_block
            assert "workflow-controlled-pair" not in json.dumps(p2c)
            expected_extra = {"controlled_experiment"}
            if stage == "development":
                expected_extra.add("matched_control")
            assert set(p2c) - set(p1) == expected_extra
            w1 = self._receipt("W1", stage)
            assert "controlled_experiment" not in w1
            w2_extra = (
                {"matched_control": {"kind": W_CONTROL_KIND}} if stage == "development" else {}
            )
            w2 = self._receipt("W2", stage, **w2_extra)
            assert w2["controlled_experiment"] == w2_block
            if stage == "development":
                assert w2["matched_control"] == {"kind": W_CONTROL_KIND}
            else:
                assert "matched_control" not in w2
