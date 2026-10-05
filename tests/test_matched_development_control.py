"""Same-session P1/P2C development control (decision D-0027).

Every refusal happens before a model client exists, before a turn is
streamed, and without a provider call or a result write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_evidence_grounding import (
    COMMIT,
    RUN_TAG,
    _observed,
    _ready_ledger,
    qualification_config_dict,
)

from blackwell_lab.cloud import cli, lifecycle, provenance, realbench
from blackwell_lab.cloud.artifacts import write_private_json
from blackwell_lab.cloud.cli import AUTHORIZED_PILOT_REGION, main
from blackwell_lab.cloud.matched_control import (
    audit_matched_controls,
    authenticate_matched_development_control,
    control_record_path,
    install_verified_p1_development_control,
    ledger_path_for,
)
from blackwell_lab.cloud.mvl import FROZEN_REGION
from blackwell_lab.cloud.qualification import validate_authorized_qualification_config
from blackwell_lab.workload.openai_client import OpenAICompatibleClient
from blackwell_lab.workload.validation import ConfigError

P1_LABEL = "qual-p1"


def _spy(monkeypatch):
    from blackwell_lab.workload import openai_client

    constructed: list[object] = []
    streamed: list[object] = []

    class SpyClient(OpenAICompatibleClient):
        def __init__(self, *args, **kwargs):
            constructed.append(self)
            super().__init__(*args, **kwargs)

        def stream_turn(self, *args, **kwargs):
            streamed.append(args)
            raise AssertionError("stream_turn must never run")

    monkeypatch.setattr(openai_client, "OpenAICompatibleClient", SpyClient)
    return constructed, streamed


@pytest.fixture
def bound(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
    monkeypatch.setattr(cli, "_git_head", lambda: COMMIT)
    monkeypatch.setattr(cli, "_tree_clean", lambda: True)
    external = tmp_path / "external"
    monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
    paths = lifecycle.lifecycle_paths(external, RUN_TAG)
    write_private_json(paths.ledger_path, _ready_ledger())
    monkeypatch.setattr(provenance, "verify_live_provenance", lambda **kwargs: _observed())
    config = qualification_config_dict("P2C", "development")
    install_verified_p1_development_control(
        external,
        config,
        run_tag=RUN_TAG,
        p1_run_label=P1_LABEL,
        canonical_commit=COMMIT,
    )
    fetches: list[object] = []

    def refuse_fetch(*args, **kwargs):
        fetches.append(args)
        raise AssertionError("provider fetch")

    monkeypatch.setattr("blackwell_lab.cloud.preflight.get_json", refuse_fetch)
    return external, config, fetches


def _rewrite(external: Path, config: dict, mutate) -> None:
    section = config["development_control"]
    path = control_record_path(external, section["p1_run_label"])
    record = json.loads(path.read_text(encoding="utf-8"))
    mutate(record)
    section["control_record_sha256"] = write_private_json(path, record)


def _run(tmp_path: Path, config: dict) -> int:
    path = tmp_path / "p2c.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    from test_p2c_controlled_catalog import _argv

    return main(_argv(path, candidate="P2C", stage="development"))


def _assert_refused(capsys, external: Path, constructed, streamed, fetches, message: str) -> None:
    err = capsys.readouterr().err
    assert err.startswith("BLOCKED")
    assert message in err
    assert str(external) not in err
    assert constructed == [] and streamed == [] and fetches == []
    family = external / "qualification-runs"
    assert not list(family.glob("qual-a-p2c-development*"))
    assert not list(family.glob("*-failure.json"))


class TestRegionLock:
    def test_one_region_lock(self):
        assert FROZEN_REGION == AUTHORIZED_PILOT_REGION == "us-iad-2"
        variables = Path("infra/akamai/variables.tf").read_text(encoding="utf-8")
        assert variables.count('var.region == "us-iad-2"') == 1
        assert 'default     = "us-iad-2"' in variables


class TestPositiveBinding:
    def test_completed_control_authenticates_without_private_fields(self, bound):
        external, config, _fetches = bound
        summary = authenticate_matched_development_control(
            config,
            results_dir=external,
            run_tag=RUN_TAG,
            p2c_run_label="qual-a",
        )
        assert summary["region"] == "us-iad-2"
        assert summary["precision"] == "bf16"
        assert summary["stopped"] is False
        assert summary["terminal_event"] == "qualification_completed"
        assert summary["p1_run_label"] == P1_LABEL
        assert summary["canonical_commit"] == COMMIT
        ledger = json.loads(ledger_path_for(external, RUN_TAG).read_text(encoding="utf-8"))
        firewall = next(item for item in ledger["resources"] if item["type"] == "linode_firewall")
        assert firewall["region"] == ""
        blob = json.dumps(summary)
        record = control_record_path(external, P1_LABEL).read_text(encoding="utf-8")
        for forbidden in (str(external), '"42"', '"555"', "/opt/", "provider_id"):
            assert forbidden not in blob
            assert forbidden not in record
        assert audit_matched_controls(external) == []

    def test_other_stages_reject_the_section(self):
        config = qualification_config_dict("P2C", "development")
        section = config["development_control"]
        for candidate, stage in (
            ("P2C", "holdout"),
            ("P2C", "freeze"),
            ("P1", "development"),
            ("C1", "development"),
        ):
            other = qualification_config_dict(candidate, stage)
            other["development_control"] = section
            with pytest.raises(ConfigError, match="valid only on P2C development"):
                validate_authorized_qualification_config(other, candidate_id=candidate, stage=stage)


class TestRefusalsBeforeClient:
    def test_old_us_sea_config(self, bound, tmp_path, monkeypatch, capsys):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        runs: list[object] = []
        monkeypatch.setattr(realbench, "run_real_cell", lambda *a, **k: runs.append(1))
        config["cloud"]["region"] = "us-sea"
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "us-iad-2")
        assert runs == []

    def test_absent_control_is_p2c_before_p1(self, bound, tmp_path, monkeypatch, capsys):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        control_record_path(external, P1_LABEL).unlink()
        assert _run(tmp_path, config) == 1
        _assert_refused(
            capsys,
            external,
            constructed,
            streamed,
            fetches,
            "completed same-session P1 control",
        )

    def test_missing_section(self, bound, tmp_path, monkeypatch, capsys):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        del config["development_control"]
        assert _run(tmp_path, config) == 1
        _assert_refused(
            capsys,
            external,
            constructed,
            streamed,
            fetches,
            "completed same-session P1 control",
        )

    def test_historical_us_sea_record(self, bound, tmp_path, monkeypatch, capsys):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        _rewrite(external, config, lambda record: record.__setitem__("region", "us-sea"))
        assert _run(tmp_path, config) == 1
        _assert_refused(
            capsys, external, constructed, streamed, fetches, "historical or cross-region"
        )

    def test_wrong_run_tag(self, bound, tmp_path, monkeypatch, capsys):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        config["development_control"]["run_tag"] = "p3-qual-20260918b"
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "run tag does not match")

    def test_cross_session_record(self, bound, tmp_path, monkeypatch, capsys):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        _rewrite(
            external,
            config,
            lambda record: record.__setitem__("run_tag", "p3-qual-20260918b"),
        )
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "run tag does not match")

    def test_own_label_and_wrong_label(self, bound, tmp_path, monkeypatch, capsys):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        config["development_control"]["p1_run_label"] = "qual-a"
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "own run label")
        config["development_control"]["p1_run_label"] = P1_LABEL
        _rewrite(external, config, lambda record: record.__setitem__("run_label", "qual-other"))
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "label does not match")

    def test_wrong_commit(self, bound, tmp_path, monkeypatch, capsys):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        _rewrite(
            external,
            config,
            lambda record: record.__setitem__("canonical_commit", "b" * 40),
        )
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "canonical commit")

    def test_wrong_resource_identity(self, bound, tmp_path, monkeypatch, capsys):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        path = ledger_path_for(external, RUN_TAG)
        ledger = json.loads(path.read_text(encoding="utf-8"))
        ledger["resources"][0]["provider_id"] = "99"
        config["development_control"]["ledger_sha256"] = write_private_json(path, ledger)
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "resource identity")

    def test_wrong_config_and_result_digests(self, bound, tmp_path, monkeypatch, capsys):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        _rewrite(
            external,
            config,
            lambda record: record.__setitem__("config_sha256", "ab" * 32),
        )
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "digest mismatch")
        original = config["development_control"]["config_sha256"]
        _rewrite(external, config, lambda record: record.__setitem__("config_sha256", original))
        result = next((external / "qualification-runs").rglob("p1.result.json"))
        result.write_bytes(result.read_bytes() + b" ")
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "digest mismatch")

    def test_failed_and_incomplete_control(self, bound, tmp_path, monkeypatch, capsys):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        _rewrite(
            external,
            config,
            lambda record: record.update({"completed": False, "failure_record": True}),
        )
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "failed or incomplete")

        def _restore(record):
            record["completed"] = True
            record["failure_record"] = False
            record["verified"] = True

        _rewrite(external, config, _restore)
        result = next((external / "qualification-runs").rglob("p1.result.json"))
        result.unlink()
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "failed or incomplete")

    def test_changed_pins_schedule_and_evaluator(self, bound, tmp_path, monkeypatch, capsys):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        _rewrite(external, config, lambda record: record.__setitem__("evaluator_version", "9.9.9"))
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "pins do not match")
        _rewrite(
            external,
            config,
            lambda record: record.__setitem__("schedule_digest", "cd" * 32),
        )
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "pins do not match")

    def test_substitution_and_private_path(self, bound, tmp_path, monkeypatch, capsys):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        path = control_record_path(external, P1_LABEL)
        path.write_bytes(path.read_bytes() + b" ")
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "digest mismatch")
        install_verified_p1_development_control(
            external,
            config,
            run_tag=RUN_TAG,
            p1_run_label=P1_LABEL,
            canonical_commit=COMMIT,
        )
        _rewrite(
            external,
            config,
            lambda record: record.__setitem__("container_digest", "/var/lib/private-control"),
        )
        assert _run(tmp_path, config) == 1
        err = capsys.readouterr().err
        assert "private path" in err
        assert "/var/lib/private-control" not in err
        assert str(external) not in err
        assert constructed == [] and streamed == [] and fetches == []

    def test_audit_reports_a_substituted_result_by_filename_only(self, bound):
        external, config, _fetches = bound
        result = next((external / "qualification-runs").rglob("p1.result.json"))
        result.write_bytes(b"{}\n")
        failures = audit_matched_controls(external)
        assert failures
        assert all(str(external) not in json.dumps(item) for item in failures)
        assert any(item["error"] == "P2C development control digest mismatch" for item in failures)
        assert config["development_control"]["region"] == "us-iad-2"


def _install_on(external: Path, ledger: dict) -> dict:
    paths = lifecycle.lifecycle_paths(external, RUN_TAG)
    write_private_json(paths.ledger_path, ledger)
    config = qualification_config_dict("P2C", "development")
    install_verified_p1_development_control(
        external,
        config,
        run_tag=RUN_TAG,
        p1_run_label=P1_LABEL,
        canonical_commit=COMMIT,
    )
    return config


class TestProductionLedgerAndPins:
    def test_firewall_without_a_region_uses_the_instance_region(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path / "external"))
        external = tmp_path / "external"
        ledger = _ready_ledger()
        for resource in ledger["resources"]:
            if resource["type"] == "linode_firewall":
                resource.pop("region", None)
        config = _install_on(external, ledger)
        summary = authenticate_matched_development_control(
            config,
            results_dir=external,
            run_tag=RUN_TAG,
            p2c_run_label="qual-a",
        )
        assert summary["region"] == "us-iad-2"
        assert "region" not in next(
            item for item in ledger["resources"] if item["type"] == "linode_firewall"
        )

    def test_firewall_region_other_than_the_instance_is_cross_region(self, tmp_path, monkeypatch):
        from blackwell_lab.cloud.qualification import QualificationError

        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path / "external"))
        external = tmp_path / "external"
        ledger = _ready_ledger()
        for resource in ledger["resources"]:
            if resource["type"] == "linode_firewall":
                resource["region"] = "us-sea"
        paths = lifecycle.lifecycle_paths(external, RUN_TAG)
        write_private_json(paths.ledger_path, ledger)
        config = qualification_config_dict("P2C", "development")
        with pytest.raises(QualificationError, match="historical or cross-region"):
            install_verified_p1_development_control(
                external,
                config,
                run_tag=RUN_TAG,
                p1_run_label=P1_LABEL,
                canonical_commit=COMMIT,
            )

    def test_changed_precision_claim_is_rejected(self, bound, tmp_path, monkeypatch, capsys):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        _rewrite(external, config, lambda record: record.__setitem__("precision", "fp8"))
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "pins do not match")

    def test_stopped_or_unfinished_p1_cannot_authenticate(
        self, bound, tmp_path, monkeypatch, capsys
    ):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        family = external / "qualification-runs"
        receipt_path = family / f"{P1_LABEL}-p1-development-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["stopped"] = True
        receipt["gates"] = {"stopped": True, "continue": False}
        digest = write_private_json(receipt_path, receipt)
        _rewrite(external, config, lambda record: record.__setitem__("receipt_sha256", digest))
        assert _run(tmp_path, config) == 1
        _assert_refused(
            capsys,
            external,
            constructed,
            streamed,
            fetches,
            "not a completed non-stopped P1",
        )

        def _restore_receipt(record):
            body = json.loads(receipt_path.read_text(encoding="utf-8"))
            body["stopped"] = False
            body["gates"] = {"stopped": False, "continue": True}
            record["receipt_sha256"] = write_private_json(receipt_path, body)

        _rewrite(external, config, _restore_receipt)
        session_path = external / "infra-lifecycle" / RUN_TAG / "session.json"
        write_private_json(
            session_path,
            {
                "run_tag": RUN_TAG,
                "events": [
                    {
                        "event": "qualification_stopped",
                        "detail": {
                            "stage": "development",
                            "candidate_id": "P1",
                            "stopped": True,
                        },
                    }
                ],
            },
        )
        assert _run(tmp_path, config) == 1
        _assert_refused(
            capsys,
            external,
            constructed,
            streamed,
            fetches,
            "not a completed non-stopped P1",
        )
        write_private_json(session_path, {"run_tag": RUN_TAG, "events": []})
        assert _run(tmp_path, config) == 1
        _assert_refused(
            capsys,
            external,
            constructed,
            streamed,
            fetches,
            "terminal event does not match",
        )
        receipt_path.unlink()
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "failed or incomplete")


class TestVerifyResultsRelationship:
    def _plant_p2c(self, external: Path, config: dict) -> Path:
        summary = authenticate_matched_development_control(
            config,
            results_dir=external,
            run_tag=RUN_TAG,
            p2c_run_label="qual-a",
        )
        family = external / "qualification-runs"
        cell = family / "qual-a-p2c-development"
        cell.mkdir()
        (cell / "p2c.result.json").write_text("{}\n", encoding="utf-8")
        receipt_path = family / "qual-a-p2c-development-receipt.json"
        write_private_json(
            receipt_path,
            {
                "candidate_id": "P2C",
                "stage": "development",
                "matched_control": summary,
            },
        )
        return receipt_path

    def test_complete_relationship_authenticates(self, bound):
        external, config, _fetches = bound
        self._plant_p2c(external, config)
        assert audit_matched_controls(external) == []

    def test_removed_tampered_substituted_and_stopped_provenance_fail(self, bound, capsys):
        external, config, _fetches = bound
        receipt_path = self._plant_p2c(external, config)
        summary = json.loads(receipt_path.read_text(encoding="utf-8"))["matched_control"]

        removed = {"candidate_id": "P2C", "stage": "development"}
        write_private_json(receipt_path, removed)
        failures = audit_matched_controls(external)
        assert any("missing matched-control provenance" in item["error"] for item in failures)
        assert all("/" not in item["file"] for item in failures)

        tampered = {
            "candidate_id": "P2C",
            "stage": "development",
            "matched_control": {**summary, "precision": "fp8"},
        }
        write_private_json(receipt_path, tampered)
        failures = audit_matched_controls(external)
        assert any("does not match the completed P1" in item["error"] for item in failures)

        substituted = {
            "candidate_id": "P2C",
            "stage": "development",
            "matched_control": {**summary, "control_record_sha256": "ab" * 32},
        }
        write_private_json(receipt_path, substituted)
        failures = audit_matched_controls(external)
        assert any("does not match the completed P1" in item["error"] for item in failures)

        orphan = external / "qualification-runs" / "qual-b-p2c-development"
        orphan.mkdir()
        (orphan / "orphan.result.json").write_text("{}\n", encoding="utf-8")
        failures = audit_matched_controls(external)
        assert any(item["file"] == "qual-b-p2c-development-receipt.json" for item in failures)

        family = external / "qualification-runs"
        p1_receipt = family / f"{P1_LABEL}-p1-development-receipt.json"
        body = json.loads(p1_receipt.read_text(encoding="utf-8"))
        body["stopped"] = True
        body["gates"] = {"stopped": True, "continue": False}
        digest = write_private_json(p1_receipt, body)
        record_path = control_record_path(external, P1_LABEL)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["receipt_sha256"] = digest
        write_private_json(record_path, record)
        write_private_json(
            receipt_path,
            {
                "candidate_id": "P2C",
                "stage": "development",
                "matched_control": summary,
            },
        )
        failures = audit_matched_controls(external)
        assert any("not a completed non-stopped P1" in item["error"] for item in failures)
        assert all(str(external) not in json.dumps(item) for item in failures)
        assert main(["verify-results", "--subdirectory", "qualification-runs"]) == 1
        report = json.loads(capsys.readouterr().out)
        rendered = json.dumps(report)
        assert "not a completed non-stopped P1" in rendered
        assert str(external) not in rendered

    def test_changed_resource_identity_is_rejected_when_the_ledger_is_unchanged(
        self, bound, capsys
    ):
        external, _config, _fetches = bound
        receipt_path = self._plant_p2c(external, _config)
        record_path = control_record_path(external, P1_LABEL)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["resource_identity_sha256"] = "ab" * 32
        control_digest = write_private_json(record_path, record)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["matched_control"]["resource_identity_sha256"] = "ab" * 32
        receipt["matched_control"]["control_record_sha256"] = control_digest
        write_private_json(receipt_path, receipt)
        failures = audit_matched_controls(external)
        assert any("resource identity does not match" in item["error"] for item in failures)
        assert all(str(external) not in json.dumps(item) for item in failures)
        assert main(["verify-results", "--subdirectory", "qualification-runs"]) == 1
        rendered = json.dumps(json.loads(capsys.readouterr().out))
        assert "resource identity does not match" in rendered
        assert str(external) not in rendered


def _session(external: Path) -> tuple[Path, dict]:
    path = external / "infra-lifecycle" / RUN_TAG / "session.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


def _rebind_receipt(external: Path, config: dict, mutate) -> None:
    path = external / "qualification-runs" / f"{P1_LABEL}-p1-development-receipt.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    mutate(receipt)
    digest = write_private_json(path, receipt)

    def _store(record):
        record["receipt_sha256"] = digest

    _rewrite(external, config, _store)


class TestPreInferenceSession:
    def test_bad_ledger_never_reaches_inference(self, tmp_path, monkeypatch, capsys):
        from test_qualification import qual_argv

        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        monkeypatch.setattr(cli, "_git_head", lambda: COMMIT)
        monkeypatch.setattr(cli, "_tree_clean", lambda: True)
        external = tmp_path / "external"
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        paths = lifecycle.lifecycle_paths(external, RUN_TAG)
        provenance_calls: list[object] = []

        def boom(**kwargs):
            provenance_calls.append(kwargs)
            raise AssertionError("live provenance")

        monkeypatch.setattr("blackwell_lab.cloud.provenance.verify_live_provenance", boom)
        fetches: list[object] = []

        def refuse_fetch(*args, **kwargs):
            fetches.append(args)
            raise AssertionError("provider fetch")

        monkeypatch.setattr("blackwell_lab.cloud.preflight.get_json", refuse_fetch)
        constructed, streamed = _spy(monkeypatch)
        config = qualification_config_dict("P1", "development")
        path = tmp_path / "p1.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        argv = qual_argv(path, candidate="P1")

        def run(mutate) -> None:
            ledger = _ready_ledger()
            mutate(ledger)
            write_private_json(paths.ledger_path, ledger)
            assert main(argv) == 1
            err = capsys.readouterr().err
            assert "P1 development session" in err
            assert str(external) not in err
            assert '"42"' not in err
            family = external / "qualification-runs"
            assert not list(family.glob("*.result.json"))
            assert not list(family.glob("*-receipt.json"))
            assert not list(family.glob("*-control.json"))
            assert not list(family.glob("*/*.result.json"))

        def wrong_firewall_region(ledger):
            for resource in ledger["resources"]:
                if resource["type"] == "linode_firewall":
                    resource["region"] = "us-sea"

        def missing_provider_id(ledger):
            ledger["resources"][0].pop("provider_id")

        def wrong_address_and_type(ledger):
            ledger["resources"][1]["address"] = "linode_firewall.other"
            ledger["resources"][1]["type"] = "linode_volume"

        def malformed_identity(ledger):
            ledger["resources"][0]["label"] = 12

        for mutate in (
            wrong_firewall_region,
            missing_provider_id,
            wrong_address_and_type,
            malformed_identity,
        ):
            run(mutate)
        assert provenance_calls == [] and constructed == [] and streamed == [] and fetches == []


class TestExactBinding:
    def test_duplicate_cross_label_cross_config_and_cross_session_events(
        self, bound, tmp_path, monkeypatch, capsys
    ):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        path, session = _session(external)
        event = session["events"][0]
        original_config = event["detail"]["config_sha256"]
        session["events"].append(json.loads(json.dumps(event)))
        write_private_json(path, session)
        assert _run(tmp_path, config) == 1
        _assert_refused(
            capsys, external, constructed, streamed, fetches, "terminal event does not match"
        )

        path, session = _session(external)
        session["events"] = [event]
        session["events"][0]["detail"]["run_label"] = "qual-b"
        write_private_json(path, session)
        assert _run(tmp_path, config) == 1
        _assert_refused(
            capsys, external, constructed, streamed, fetches, "terminal event does not match"
        )

        path, session = _session(external)
        session["events"][0]["detail"]["run_label"] = P1_LABEL
        session["events"][0]["detail"]["config_sha256"] = "ab" * 32
        write_private_json(path, session)
        assert _run(tmp_path, config) == 1
        _assert_refused(
            capsys, external, constructed, streamed, fetches, "terminal event does not match"
        )

        path, session = _session(external)
        session["events"][0]["detail"]["config_sha256"] = original_config
        session["run_tag"] = "p3-qual-20260918b"
        write_private_json(path, session)
        assert _run(tmp_path, config) == 1
        _assert_refused(
            capsys, external, constructed, streamed, fetches, "terminal event does not match"
        )

        path, session = _session(external)
        session["run_tag"] = RUN_TAG
        session["events"].append(
            {
                "event": "qualification_stopped",
                "detail": {
                    "stage": "development",
                    "candidate_id": "P1",
                    "run_label": P1_LABEL,
                    "config_sha256": original_config,
                    "stopped": True,
                },
            }
        )
        write_private_json(path, session)
        assert _run(tmp_path, config) == 1
        _assert_refused(
            capsys,
            external,
            constructed,
            streamed,
            fetches,
            "not a completed non-stopped P1",
        )

    def test_receipt_claims_must_match_the_selected_control(
        self, bound, tmp_path, monkeypatch, capsys
    ):
        external, config, fetches = bound
        constructed, streamed = _spy(monkeypatch)
        _rebind_receipt(
            external,
            config,
            lambda receipt: receipt.__setitem__("run_label", "qual-b-p1-development"),
        )
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "label does not match")
        _rebind_receipt(
            external,
            config,
            lambda receipt: receipt.__setitem__("run_label", f"{P1_LABEL}-p1-development"),
        )
        _rebind_receipt(
            external,
            config,
            lambda receipt: receipt.__setitem__("config_sha256", "cd" * 32),
        )
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "digest mismatch")
        original = config["development_control"]["config_sha256"]
        _rebind_receipt(
            external,
            config,
            lambda receipt: receipt.__setitem__("config_sha256", original),
        )
        _rebind_receipt(
            external,
            config,
            lambda receipt: receipt.__setitem__("candidate_identity_sha256", "ef" * 32),
        )
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "digest mismatch")
        _rebind_receipt(
            external,
            config,
            lambda receipt: receipt.__setitem__(
                "candidate_identity_sha256",
                json.loads(
                    (
                        external / "qualification-runs" / f"{P1_LABEL}-p1-development-control.json"
                    ).read_text(encoding="utf-8")
                )["identity_sha256"],
            ),
        )
        _rebind_receipt(
            external,
            config,
            lambda receipt: receipt.__setitem__("workload_version", "9.9.9"),
        )
        assert _run(tmp_path, config) == 1
        _assert_refused(capsys, external, constructed, streamed, fetches, "pins do not match")


class TestSafeLabel:
    def test_unsafe_label_does_not_read_outside_qualification_runs(self, tmp_path, monkeypatch):
        family = tmp_path / "qualification-runs"
        family.mkdir()
        receipt = family / "qual-a-p2c-development-receipt.json"
        reads: list[Path] = []
        real_text = Path.read_text
        real_bytes = Path.read_bytes
        real_open = Path.open

        def spy_text(self, *args, **kwargs):
            reads.append(self)
            return real_text(self, *args, **kwargs)

        def spy_bytes(self, *args, **kwargs):
            reads.append(self)
            return real_bytes(self, *args, **kwargs)

        def spy_open(self, *args, **kwargs):
            reads.append(self)
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", spy_text)
        monkeypatch.setattr(Path, "read_bytes", spy_bytes)
        monkeypatch.setattr(Path, "open", spy_open)
        for label in ("/etc/passwd", "../outside", "qual/p1", "..", "Qual-P1", "a" * 80, ""):
            write_private_json(
                receipt,
                {
                    "candidate_id": "P2C",
                    "stage": "development",
                    "matched_control": {"p1_run_label": label},
                },
            )
            reads.clear()
            failures = audit_matched_controls(tmp_path)
            assert any("label is unsafe" in item["error"] for item in failures)
            root = family.resolve()
            for accessed in reads:
                resolved = accessed.resolve()
                assert resolved == root or root in resolved.parents
