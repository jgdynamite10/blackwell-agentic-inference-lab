"""Tests for the blackwell-cloud CLI: gates, guards, and verification.

Everything runs offline. Billable verbs are exercised only through their
refusal paths or injected fakes; the full-baseline command must refuse
unconditionally (Phase 3B is not authorized).
"""

from __future__ import annotations

import json

import pytest
from fakes import FakeClock

from blackwell_lab.cloud import cli, lifecycle
from blackwell_lab.cloud.cli import FULL_BASELINE_AUTHORIZED, main
from blackwell_lab.workload.runner import run_cell

RUN_TAG = "p3-pilot-20260907a"


class TestReadiness:
    def test_readiness_is_offline_and_passes_in_this_tree(self, capsys):
        assert main(["readiness"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["cloud_access"] == "none (offline validation only)"
        assert report["credentials_required"] is False
        assert report["ok"] is True
        # Every check either passed or was explicitly skipped, never failed.
        assert all(c["status"] in ("ok", "skipped") for c in report["checks"].values())

    def test_readiness_covers_every_required_area(self, capsys):
        main(["readiness"])
        report = json.loads(capsys.readouterr().out)
        assert set(report["checks"]) == {
            "terraform_files",
            "terraform_static",
            "bootstrap_scripts",
            "bootstrap_pins",
            "python_modules",
            "schemas_and_examples",
        }


class TestFullBaselineGate:
    def test_the_constant_is_disabled_in_source(self):
        assert FULL_BASELINE_AUTHORIZED is False

    def test_full_baseline_refuses_with_a_dedicated_exit_code(self, capsys):
        assert main(["full-baseline"]) == 3
        err = capsys.readouterr().err
        assert "DISABLED" in err
        assert "not authorized" in err
        assert "decision-log" in err


class TestPilotGate:
    def test_pilot_refuses_in_hosted_environments(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("CI", "1")
        config = tmp_path / "pilot.json"
        config.write_text("{}", encoding="utf-8")
        assert main(["pilot", "--run-label", "x", "--config", str(config), "--approve", "y"]) == 1
        assert "hosted" in capsys.readouterr().err

    def test_pilot_refuses_without_the_exact_approval_phrase(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        config = tmp_path / "pilot.json"
        config.write_text("{}", encoding="utf-8")
        assert (
            main(["pilot", "--run-label", "pilot-a", "--config", str(config), "--approve", "ok"])
            == 1
        )
        err = capsys.readouterr().err
        assert "BLOCKED" in err
        assert "nothing was executed" in err

    def test_pilot_with_approval_still_fails_closed_without_results_dir(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        monkeypatch.delenv("LAB_RESULTS_DIR", raising=False)
        config = tmp_path / "pilot.json"
        config.write_text("{}", encoding="utf-8")
        phrase = cli.PILOT_APPROVAL_TEMPLATE.format(run_label="pilot-a")
        assert (
            main(["pilot", "--run-label", "pilot-a", "--config", str(config), "--approve", phrase])
            == 3
        )
        assert "LAB_RESULTS_DIR" in capsys.readouterr().err


class TestApplyDestroyGates:
    def test_apply_without_phrase_is_an_error_and_runs_nothing(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
        # Hosted markers are present in CI; either refusal path is safe, but
        # the approval gate must trigger even in a local-looking environment.
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        assert main(["apply", "--run-tag", RUN_TAG, "--approve", "sure"]) == 1
        assert "approval phrase" in capsys.readouterr().err

    def test_destroy_without_a_ledger_refuses(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
        assert main(["destroy", "--run-tag", RUN_TAG, "--approve", "x"]) == 1
        assert "ledger" in capsys.readouterr().err


class TestTeardownPlan:
    def test_teardown_plan_prints_exact_ledger_targets(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
        show_json = {
            "values": {
                "root_module": {
                    "resources": [
                        {
                            "address": "linode_instance.gpu_baseline",
                            "type": "linode_instance",
                            "name": "gpu_baseline",
                            "values": {"id": "42", "label": f"bwlab-{RUN_TAG}"},
                        }
                    ]
                }
            }
        }
        ledger = lifecycle.build_ledger(RUN_TAG, show_json)
        lifecycle.write_ledger(ledger, tmp_path / "infra-ledgers")
        assert main(["teardown-plan", "--run-tag", RUN_TAG]) == 0
        plan_doc = json.loads(capsys.readouterr().out)
        assert plan_doc["targets"] == ["linode_instance.gpu_baseline"]
        assert plan_doc["resource_count"] == 1


class TestOrphanReportGate:
    def test_orphan_report_requires_a_local_token(self, capsys, monkeypatch):
        monkeypatch.delenv("LINODE_TOKEN", raising=False)
        assert main(["orphan-report"]) == 1
        assert "BLOCKED" in capsys.readouterr().err


class TestVerifyResults:
    @pytest.fixture()
    def persisted_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
        records = run_cell(
            profile_name="interactive",
            concurrency=1,
            repetitions=1,
            warmup_passes=0,
            tasks_per_repetition=4,
            scenario_ids=["elevated-latency-001", "dns-failures-001"],
            clock=FakeClock(),
            results_dir=tmp_path,
        )
        return tmp_path, records[0]

    def test_verify_results_fails_closed_without_the_guard(self, capsys, monkeypatch):
        monkeypatch.delenv("LAB_RESULTS_DIR", raising=False)
        assert main(["verify-results"]) == 3
        assert "LAB_RESULTS_DIR" in capsys.readouterr().err

    def test_empty_directory_verifies_trivially(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
        assert main(["verify-results"]) == 0
        assert json.loads(capsys.readouterr().out)["verified"] == 0

    def test_valid_run_verifies_and_prints_no_absolute_paths(self, persisted_run, capsys):
        tmp_path, _record = persisted_run
        assert main(["verify-results", "--subdirectory", "synthetic-mock-runs"]) == 0
        out = capsys.readouterr().out
        report = json.loads(out)
        assert report["verified"] == 1
        assert report["ok"] is True
        assert str(tmp_path) not in out  # absolute private paths never printed

    def test_tampered_observations_fail_verification(self, persisted_run, capsys):
        tmp_path, record = persisted_run
        target = tmp_path / "synthetic-mock-runs" / record.result["observations"]["measured_file"]
        document = json.loads(target.read_text(encoding="utf-8"))
        document["observations"][0]["e2e_ms"] = 0.001  # falsified latency
        target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        assert main(["verify-results", "--subdirectory", "synthetic-mock-runs"]) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["ok"] is False
        assert "hash mismatch" in report["failed"][0]["error"]
