"""The actual W7 CLI packet paths validate without external work."""

from __future__ import annotations

import json

import pytest
from test_canary import _canary_config
from test_qualification import qualification_config_dict

from blackwell_lab.cloud.cli import main
from blackwell_lab.workload.public_metadata.catalog_v2 import evidence_contract


@pytest.mark.parametrize("stage", ["development", "holdout", "freeze"])
def test_w7_qualification_validate_only_packet(stage, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("validate-only must not construct or contact a client")

    monkeypatch.setattr("blackwell_lab.workload.openai_client.OpenAICompatibleClient", forbidden)
    monkeypatch.setattr("blackwell_lab.cloud.provenance.verify_live_provenance", forbidden)
    config = qualification_config_dict("W7", stage)
    config["evidence_contract"] = evidence_contract()
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert (
        main(
            [
                "qualify-agent",
                "--candidate",
                "W7",
                "--stage",
                stage,
                "--run-tag",
                "w7-packet-20261012a",
                "--run-label",
                "packet-a",
                "--config",
                str(path),
                "--validate-only",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["candidate_id"] == "W7"
    assert report["workload_version"] == "2.9.0"
    assert report["executed"] is False
    assert report["model_client_constructed"] is False
    assert report["endpoint_contacted"] is False


def test_w7_canary_validate_only_packet(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("validate-only must not construct or contact a client")

    monkeypatch.setattr("blackwell_lab.workload.openai_client.OpenAICompatibleClient", forbidden)
    monkeypatch.setattr("blackwell_lab.cloud.provenance.verify_live_provenance", forbidden)
    config = _canary_config("W7")
    config["evidence_contract"] = evidence_contract()
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert (
        main(
            [
                "canary-agent",
                "--candidate",
                "W7",
                "--run-tag",
                "w7-packet-20261012a",
                "--run-label",
                "packet-a",
                "--config",
                str(path),
                "--validate-only",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["executed"] is False
    assert report["endpoint_contacted"] is False
    assert report["schedule"]["tasks"] == 10
    assert report["schedule"]["holdout_excluded"] is True
