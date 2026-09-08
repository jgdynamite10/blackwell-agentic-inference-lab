"""Tests for the blackwell-cloud CLI: gates, guards, and verification.

Everything runs offline. Billable verbs are exercised only through their
refusal paths or injected fakes. The full-baseline command is D-0017
implementation-authorized and still fail-closed without the digest-bearing
phrase, a clean commit, and a clean ledger.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

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
    def test_the_constant_is_enabled_in_source(self):
        assert FULL_BASELINE_AUTHORIZED is True

    def test_full_baseline_refuses_in_hosted_environments(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("CI", "1")
        assert (
            main(
                [
                    "full-baseline",
                    "--run-tag",
                    RUN_TAG,
                    "--run-label",
                    "baseline-a",
                    "--config",
                    str(tmp_path / "missing.json"),
                    "--approve",
                    "no",
                ]
            )
            == 1
        )
        assert "hosted" in capsys.readouterr().err


def pilot_config(tmp_path, comparison_mode="provider-native", **overrides):
    config = {
        "endpoint": {"base_url": "http://127.0.0.1:8000/v1", "model": "m"},
        "cloud": {
            "instance_type": "g3-gpu-rtxpro6000-blackwell-1",
            "region": "us-sea",
            "list_price_usd_per_hour": 3.0,
            "price_source_date": "2026-09-06",
        },
        "model": {
            "artifact": "a",
            "revision": "r",
            "artifact_hash": "sha256:" + "ab" * 32,
            "precision": "bf16",
        },
        "serving": {
            "engine": "vllm",
            "engine_version": "0.27.1",
            "image": "docker.io/vllm/vllm-openai:v0.27.1",
            "container_digest": "docker.io/vllm/vllm-openai@sha256:" + "cd" * 32,
        },
        "host": {
            "storage_description": "plan NVMe",
            "network_description": "plan default networking",
        },
        "comparison_mode": comparison_mode,
        "expected_gpu_model": "RTX PRO 6000 Blackwell",
        "model_verification": {
            "artifact_dir": str(tmp_path / "model"),
            "digest_manifest": str(tmp_path / "model.sha256"),
        },
        "cells": [
            {"profile": "interactive", "concurrency": 1},
            {"profile": "batch-heavy", "concurrency": 4},
            {"profile": "batch-heavy", "concurrency": 8},
        ],
        "warmup_passes": 1,
        "repetitions": 1,
        "tasks_per_repetition": 20,
    }
    config.update(overrides)
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def config_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pilot_argv(config_path, run_label="pilot-a", run_tag=RUN_TAG, approve=None):
    phrase = approve or cli.PILOT_APPROVAL_TEMPLATE.format(
        run_tag=run_tag,
        run_label=run_label,
        config_sha256=config_sha256(config_path),
    )
    return [
        "pilot",
        "--run-tag",
        run_tag,
        "--run-label",
        run_label,
        "--config",
        str(config_path),
        "--approve",
        phrase,
    ]


def pilot_ready_ledger(**overrides):
    ledger = {
        "run_tag": RUN_TAG,
        "reconciled": True,
        "reconciliation": {"provider_checked": True},
        "resources": [
            {
                "address": "linode_instance.gpu_baseline",
                "type": "linode_instance",
                "provider_id": "42",
                "region": "us-ord",
            },
            {
                "address": "linode_firewall.gpu_baseline",
                "type": "linode_firewall",
                "provider_id": "555",
            },
        ],
    }
    ledger.update(overrides)
    return ledger


class TestPilotGate:
    def test_pilot_refuses_in_hosted_environments(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("CI", "1")
        config = pilot_config(tmp_path)
        assert main(pilot_argv(config, approve="y")) == 1
        assert "hosted" in capsys.readouterr().err

    def test_pilot_refuses_without_the_exact_approval_phrase(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        config = pilot_config(tmp_path)
        assert main(pilot_argv(config, approve="ok")) == 1
        err = capsys.readouterr().err
        assert "BLOCKED" in err
        assert "nothing was executed" in err

    def test_pilot_with_approval_still_fails_closed_without_results_dir(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        monkeypatch.delenv("LAB_RESULTS_DIR", raising=False)
        config = pilot_config(tmp_path)
        assert main(pilot_argv(config)) == 3
        assert "LAB_RESULTS_DIR" in capsys.readouterr().err

    def test_pilot_rejects_config_labeled_controlled_resource(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path / "external"))
        config = pilot_config(tmp_path, comparison_mode="controlled-resource")
        assert main(pilot_argv(config)) == 1
        err = capsys.readouterr().err
        assert "provider-native" in err
        assert "genuinely enforced" in err

    def test_pilot_is_blocked_while_a_lifecycle_operation_is_pending(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        external = tmp_path / "external"
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        paths = lifecycle.lifecycle_paths(external, RUN_TAG)
        lifecycle.write_private_json(paths.ledger_path, pilot_ready_ledger())
        lifecycle.write_pending(paths, run_tag=RUN_TAG, operation="apply", plan_sha256="x")
        config = pilot_config(tmp_path)
        assert main(pilot_argv(config)) == 1
        assert "pending lifecycle operation" in capsys.readouterr().err

    def test_pilot_is_blocked_without_provider_checked(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        external = tmp_path / "external"
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        paths = lifecycle.lifecycle_paths(external, RUN_TAG)
        lifecycle.write_private_json(
            paths.ledger_path,
            pilot_ready_ledger(reconciliation={"provider_checked": False}),
        )
        config = pilot_config(tmp_path)
        assert main(pilot_argv(config)) == 1
        assert "provider API" in capsys.readouterr().err

    def test_pilot_is_blocked_without_expected_firewall(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        external = tmp_path / "external"
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        paths = lifecycle.lifecycle_paths(external, RUN_TAG)
        lifecycle.write_private_json(
            paths.ledger_path,
            pilot_ready_ledger(resources=[pilot_ready_ledger()["resources"][0]]),
        )
        config = pilot_config(tmp_path)
        assert main(pilot_argv(config)) == 1
        assert "firewall" in capsys.readouterr().err

    def test_pilot_is_blocked_until_reconciliation_is_clean(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        external = tmp_path / "external"
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        paths = lifecycle.lifecycle_paths(external, RUN_TAG)
        lifecycle.write_private_json(
            paths.ledger_path,
            pilot_ready_ledger(reconciled=False),
        )
        config = pilot_config(tmp_path)
        assert main(pilot_argv(config)) == 1
        assert "not cleanly reconciled" in capsys.readouterr().err

    def test_pilot_fails_visibly_on_fabricated_provenance(self, tmp_path, capsys, monkeypatch):
        # The ledger and config exist, but nothing genuine backs them: the
        # provenance observation layer must fail visibly, never trust config.
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        external = tmp_path / "external"
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        paths = lifecycle.lifecycle_paths(external, RUN_TAG)
        lifecycle.write_private_json(paths.ledger_path, pilot_ready_ledger())
        config = pilot_config(tmp_path)
        assert main(pilot_argv(config)) == 1
        err = capsys.readouterr().err
        assert "error:" in err

    def test_second_cell_provenance_drift_fails_before_measurement(
        self, tmp_path, capsys, monkeypatch
    ):
        from blackwell_lab.cloud import provenance, realbench
        from blackwell_lab.cloud.provenance import ObservedProvenance, ProvenanceError

        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        external = tmp_path / "external"
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        paths = lifecycle.lifecycle_paths(external, RUN_TAG)
        lifecycle.write_private_json(paths.ledger_path, pilot_ready_ledger())

        calls = {"n": 0}
        run_calls: list[int] = []

        def fake_verify(**kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise ProvenanceError(
                    "container digest differs from the approved pilot configuration"
                )
            return ObservedProvenance(
                container_digest="digest",
                model_artifact_hash="sha256:" + "ab" * 32,
                engine_version="0.27.1",
                instance={
                    "provider_id": "42",
                    "instance_type": "g3-gpu-rtxpro6000-blackwell-1",
                    "region": "us-sea",
                    "tags": ["blackwell-lab", f"run:{RUN_TAG}"],
                },
                host_facts={
                    "storage_description": "plan NVMe",
                    "network_description": "plan default networking",
                },
                gpu_facts={"gpu_model": "RTX PRO 6000 Blackwell", "driver_version": "580"},
                container_cuda_runtime_version="12.8",
            )

        monkeypatch.setattr(provenance, "verify_live_provenance", fake_verify)
        monkeypatch.setattr(
            realbench,
            "run_real_cell",
            lambda *args, **kwargs: run_calls.append(1) or [],
        )

        config = pilot_config(tmp_path)

        assert main(pilot_argv(config)) == 1
        assert len(run_calls) == 1
        assert "container digest" in capsys.readouterr().err

    def test_pilot_observes_vllm_version_at_service_root_not_openai_prefix(
        self, tmp_path, monkeypatch
    ):
        from blackwell_lab.cloud import provenance, realbench, telemetry

        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        external = tmp_path / "external"
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        paths = lifecycle.lifecycle_paths(external, RUN_TAG)
        ledger = pilot_ready_ledger()
        ledger["resources"][0]["region"] = "us-sea"
        lifecycle.write_private_json(paths.ledger_path, ledger)

        config = pilot_config(tmp_path)
        approved = json.loads(config.read_text(encoding="utf-8"))
        assert approved["endpoint"]["base_url"] == "http://127.0.0.1:8000/v1"
        requested: list[str] = []

        def http_get(url: str) -> dict:
            if url.endswith("/v1/version"):
                raise AssertionError(
                    f"vLLM /version is not under the OpenAI /v1 prefix; refused {url}"
                )
            requested.append(url)
            if url == "http://127.0.0.1:8000/version":
                return {"version": approved["serving"]["engine_version"]}
            if url.startswith(provenance.METADATA_BASE):
                raise AssertionError(
                    f"engine-version HTTP GET must not call the Metadata API: {url}"
                )
            raise AssertionError(f"unexpected URL observed: {url}")

        def http_request(method: str, url: str, headers: dict[str, str]) -> object:
            if method == "PUT" and url == provenance.METADATA_TOKEN_URL:
                return ["cli-metadata-token-not-a-credential"]
            if method == "GET" and url == provenance.METADATA_INSTANCE_URL:
                return {
                    "id": "42",
                    "type": approved["cloud"]["instance_type"],
                    "region": approved["cloud"]["region"],
                    "tags": ["blackwell-lab", f"run:{RUN_TAG}"],
                }
            raise AssertionError(f"unexpected metadata request: {method} {url}")

        real_verify = provenance.verify_live_provenance

        def verify_with_injected_http(**kwargs):
            kwargs["http_get"] = http_get
            kwargs["http_request"] = http_request
            return real_verify(**kwargs)

        monkeypatch.setattr(provenance, "verify_live_provenance", verify_with_injected_http)
        monkeypatch.setattr(
            telemetry,
            "resolve_container_digest",
            lambda *args, **kwargs: approved["serving"]["container_digest"],
        )
        monkeypatch.setattr(
            telemetry,
            "verify_model_artifact",
            lambda *args, **kwargs: approved["model"]["artifact_hash"],
        )
        monkeypatch.setattr(
            telemetry, "observe_container_cuda_version", lambda *args, **kwargs: "13.0"
        )
        monkeypatch.setattr(
            telemetry, "collect_host_facts", lambda **kwargs: dict(approved["host"])
        )
        monkeypatch.setattr(
            telemetry,
            "collect_gpu_facts",
            lambda *args, **kwargs: {
                "gpu_model": "NVIDIA RTX PRO 6000 Blackwell",
                "driver_version": "580",
            },
        )
        monkeypatch.setattr(realbench, "run_real_cell", lambda *args, **kwargs: [])

        assert main(pilot_argv(config)) == 0
        version_urls = [url for url in requested if url.endswith("/version")]
        assert version_urls == ["http://127.0.0.1:8000/version"] * 3

    def _pilot_with_live_provenance(self, tmp_path, monkeypatch, *, http_request, run_calls):
        from blackwell_lab.cloud import provenance, realbench, telemetry

        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        external = tmp_path / "external"
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        paths = lifecycle.lifecycle_paths(external, RUN_TAG)
        ledger = pilot_ready_ledger()
        ledger["resources"][0]["region"] = "us-sea"
        lifecycle.write_private_json(paths.ledger_path, ledger)
        config = pilot_config(tmp_path)
        approved = json.loads(config.read_text(encoding="utf-8"))

        def http_get(url: str) -> dict:
            if url == "http://127.0.0.1:8000/version":
                return {"version": approved["serving"]["engine_version"]}
            raise AssertionError(f"unexpected URL observed: {url}")

        real_verify = provenance.verify_live_provenance

        def verify_with_injected_http(**kwargs):
            kwargs["http_get"] = http_get
            kwargs["http_request"] = http_request
            return real_verify(**kwargs)

        monkeypatch.setattr(provenance, "verify_live_provenance", verify_with_injected_http)
        monkeypatch.setattr(
            telemetry,
            "resolve_container_digest",
            lambda *args, **kwargs: approved["serving"]["container_digest"],
        )
        monkeypatch.setattr(
            telemetry,
            "verify_model_artifact",
            lambda *args, **kwargs: approved["model"]["artifact_hash"],
        )
        monkeypatch.setattr(
            telemetry, "observe_container_cuda_version", lambda *args, **kwargs: "13.0"
        )
        monkeypatch.setattr(
            telemetry, "collect_host_facts", lambda **kwargs: dict(approved["host"])
        )
        monkeypatch.setattr(
            telemetry,
            "collect_gpu_facts",
            lambda *args, **kwargs: {
                "gpu_model": "NVIDIA RTX PRO 6000 Blackwell",
                "driver_version": "580",
            },
        )
        monkeypatch.setattr(
            realbench, "run_real_cell", lambda *args, **kwargs: run_calls.append(1) or []
        )
        return config, approved

    def test_each_pilot_cell_obtains_fresh_metadata_token(self, tmp_path, monkeypatch):
        from blackwell_lab.cloud import provenance

        tokens = [f"fresh-cell-token-{index}" for index in (1, 2, 3)]
        issued = iter(tokens)
        calls: list[tuple[str, str, dict[str, str]]] = []

        def http_request(method: str, url: str, headers: dict[str, str]) -> object:
            calls.append((method, url, dict(headers)))
            if method == "PUT" and url == provenance.METADATA_TOKEN_URL:
                return [next(issued)]
            if method == "GET" and url == provenance.METADATA_INSTANCE_URL:
                return {
                    "id": "42",
                    "type": "g3-gpu-rtxpro6000-blackwell-1",
                    "region": "us-sea",
                    "tags": ["blackwell-lab", f"run:{RUN_TAG}"],
                }
            raise AssertionError(f"unexpected metadata request: {method} {url}")

        run_calls: list[int] = []
        config, _approved = self._pilot_with_live_provenance(
            tmp_path, monkeypatch, http_request=http_request, run_calls=run_calls
        )
        assert main(pilot_argv(config)) == 0
        assert [method for method, _url, _headers in calls] == ["PUT", "GET"] * 3
        assert [
            headers["Metadata-Token"] for method, _url, headers in calls if method == "GET"
        ] == tokens
        assert len(run_calls) == 3

    def test_metadata_failure_makes_zero_measurement_calls(self, tmp_path, capsys, monkeypatch):
        from blackwell_lab.cloud import provenance

        def http_request(method: str, url: str, headers: dict[str, str]) -> object:
            if method == "PUT" and url == provenance.METADATA_TOKEN_URL:
                raise OSError("metadata token unavailable")
            raise AssertionError(f"instance GET must not run after token PUT failure: {method}")

        run_calls: list[int] = []
        config, _approved = self._pilot_with_live_provenance(
            tmp_path, monkeypatch, http_request=http_request, run_calls=run_calls
        )
        assert main(pilot_argv(config)) == 1
        assert run_calls == []
        err = capsys.readouterr().err
        assert "never taken from configuration" in err
        assert "metadata token unavailable" not in err

    def test_pilot_rejects_relative_config_path(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        config = pilot_config(tmp_path)
        assert main(pilot_argv("pilot.json", approve="x")) == 1
        assert "outside the repository" in capsys.readouterr().err
        assert config.is_file()

    def test_pilot_rejects_config_inside_the_repository(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        repo_file = Path(__file__).resolve().parents[1] / "README.md"
        assert main(pilot_argv(repo_file)) == 1
        assert "outside the repository" in capsys.readouterr().err

    def test_pilot_rejects_approval_without_config_digest(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        config = pilot_config(tmp_path)
        stale = f"I approve the short Akamai pilot for run {RUN_TAG} (pilot-a)"
        assert main(pilot_argv(config, approve=stale)) == 1
        err = capsys.readouterr().err
        assert "BLOCKED" in err
        assert config_sha256(config) in err

    def test_pilot_does_not_construct_client_before_validation(self, tmp_path, capsys, monkeypatch):
        constructed: list[int] = []

        class Boom:
            def __init__(self, *args, **kwargs):
                constructed.append(1)

        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        monkeypatch.setattr(
            "blackwell_lab.workload.openai_client.OpenAICompatibleClient",
            Boom,
        )
        config = pilot_config(tmp_path, cells=[{"profile": "interactive", "concurrency": 1}])
        assert main(pilot_argv(config)) == 1
        assert constructed == []
        assert "exactly interactive/1" in capsys.readouterr().err


class TestAuthorizedPilotConfig:
    def test_valid_d0014_config_is_accepted(self, tmp_path):
        path = pilot_config(tmp_path)
        config = json.loads(path.read_bytes())
        cli.validate_authorized_pilot_config(config)

    def test_missing_cell_is_rejected(self, tmp_path):
        path = pilot_config(
            tmp_path,
            cells=[
                {"profile": "interactive", "concurrency": 1},
                {"profile": "batch-heavy", "concurrency": 4},
            ],
        )
        config = json.loads(path.read_text(encoding="utf-8"))
        with pytest.raises(cli.ConfigError, match="exactly interactive/1"):
            cli.validate_authorized_pilot_config(config)

    def test_duplicate_cell_is_rejected(self, tmp_path):
        path = pilot_config(
            tmp_path,
            cells=[
                {"profile": "interactive", "concurrency": 1},
                {"profile": "batch-heavy", "concurrency": 4},
                {"profile": "batch-heavy", "concurrency": 4},
            ],
        )
        config = json.loads(path.read_text(encoding="utf-8"))
        with pytest.raises(cli.ConfigError, match="duplicate"):
            cli.validate_authorized_pilot_config(config)

    def test_additional_cell_is_rejected(self, tmp_path):
        path = pilot_config(
            tmp_path,
            cells=[
                {"profile": "interactive", "concurrency": 1},
                {"profile": "batch-heavy", "concurrency": 4},
                {"profile": "batch-heavy", "concurrency": 8},
                {"profile": "reasoning-heavy", "concurrency": 1},
            ],
        )
        config = json.loads(path.read_text(encoding="utf-8"))
        with pytest.raises(cli.ConfigError, match="additional"):
            cli.validate_authorized_pilot_config(config)

    def test_wrong_precision_is_rejected(self, tmp_path):
        path = pilot_config(tmp_path)
        config = json.loads(path.read_text(encoding="utf-8"))
        config["model"]["precision"] = "nvfp4"
        with pytest.raises(cli.ConfigError, match="bf16"):
            cli.validate_authorized_pilot_config(config)

    def test_wrong_gpu_is_rejected(self, tmp_path):
        path = pilot_config(tmp_path, expected_gpu_model="A100")
        config = json.loads(path.read_text(encoding="utf-8"))
        with pytest.raises(cli.ConfigError, match="RTX PRO 6000 Blackwell"):
            cli.validate_authorized_pilot_config(config)

    def test_wrong_warmup_or_repetitions_or_tasks_are_rejected(self, tmp_path):
        path = pilot_config(tmp_path, warmup_passes=2)
        config = json.loads(path.read_text(encoding="utf-8"))
        with pytest.raises(cli.ConfigError, match="warmup_passes"):
            cli.validate_authorized_pilot_config(config)
        config["warmup_passes"] = 1
        config["repetitions"] = 3
        with pytest.raises(cli.ConfigError, match="repetitions"):
            cli.validate_authorized_pilot_config(config)
        config["repetitions"] = 1
        config["tasks_per_repetition"] = 100
        with pytest.raises(cli.ConfigError, match="tasks_per_repetition"):
            cli.validate_authorized_pilot_config(config)


class TestApplyDestroyGates:
    def test_apply_without_a_reviewed_saved_plan_refuses(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
        # Hosted markers are present in CI; either refusal path is safe, but
        # the saved-plan gate must trigger even in a local-looking environment.
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        assert main(["apply", "--run-tag", RUN_TAG, "--approve", "sure"]) == 1
        assert "no reviewed apply plan" in capsys.readouterr().err

    def test_destroy_without_a_ledger_refuses(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
        assert main(["destroy", "--run-tag", RUN_TAG, "--approve", "x"]) == 1
        assert "ledger" in capsys.readouterr().err


class TestTeardownPlan:
    def test_teardown_plan_without_a_ledger_refuses(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
        assert main(["teardown-plan", "--run-tag", RUN_TAG]) == 1
        assert "ledger" in capsys.readouterr().err


class TestSessionSummary:
    def test_session_summary_reads_the_external_record(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
        paths = lifecycle.lifecycle_paths(tmp_path, RUN_TAG)
        lifecycle.record_session_event(paths, "provisioned", {})
        lifecycle.record_session_event(paths, "deletion_confirmed", {})
        assert main(["session-summary", "--run-tag", RUN_TAG, "--hourly-price", "3.5"]) == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["observed_billable_s"] is not None
        assert summary["estimated_total_cost_usd"] is not None

    def test_session_summary_without_a_record_refuses(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
        assert main(["session-summary", "--run-tag", RUN_TAG]) == 1
        assert "session record" in capsys.readouterr().err


class TestSanitizedErrors:
    def test_unexpected_exception_text_is_never_printed(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))

        def explode(run_tag, paths):
            raise RuntimeError("raw provider payload with account-id-999 and /private/path")

        monkeypatch.setattr(
            cli,
            "_paths_for",
            lambda run_tag: (_ for _ in ()).throw(
                RuntimeError("raw provider payload with account-id-999 and /private/path")
            ),
        )
        assert main(["reconcile", "--run-tag", RUN_TAG]) == 1
        err = capsys.readouterr().err
        assert "account-id-999" not in err
        assert "/private/path" not in err
        assert "details suppressed" in err


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

    def test_empty_directory_is_a_failure_by_default(self, tmp_path, capsys, monkeypatch):
        # An empty directory never verifies a required pilot result.
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
        assert main(["verify-results"]) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["verified"] == 0
        assert report["ok"] is False

    def test_empty_directory_passes_only_with_allow_empty(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path))
        assert main(["verify-results", "--allow-empty"]) == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True

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
