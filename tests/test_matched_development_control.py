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
        assert summary["p1_run_label"] == P1_LABEL
        assert summary["canonical_commit"] == COMMIT
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
