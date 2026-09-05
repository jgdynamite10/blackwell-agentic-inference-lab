"""Mocked tests for the read-only preflight scripts.

These tests never contact any provider: network and CLI lookups are mocked.
They verify read-only behavior, blocked-path reporting, and that secrets are
never printed.
"""

import importlib.util
import sys
from pathlib import Path

PREFLIGHT_DIR = Path(__file__).resolve().parents[1] / "scripts" / "preflight"


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, PREFLIGHT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check_akamai = load_script("check_akamai")
check_gcp = load_script("check_gcp")
check_aws = load_script("check_aws")

FAKE_CATALOG = {
    "data": [
        {
            "id": "g9-gpu-rtxpro6000-blackwell-1",
            "class": "gpu",
            "vcpus": 16,
            "memory": 176 * 1024,
            "gpus": 1,
            "price": {"hourly": 1.23},
        },
        {"id": "g1-gpu-rtx6000-1", "class": "gpu", "vcpus": 8, "memory": 32768, "gpus": 1},
        {"id": "g6-standard-2", "class": "standard", "vcpus": 2, "memory": 4096},
    ]
}

FAKE_AVAILABILITY = {
    "data": [
        {"region": "us-fake-1", "unavailable": []},
        {"region": "eu-fake-2", "unavailable": ["GPU Linodes"]},
    ]
}


class TestAkamai:
    def test_catalog_reports_blackwell_plans(self, capsys):
        assert check_akamai.report_gpu_catalog(fetch=lambda path, token=None: FAKE_CATALOG)
        out = capsys.readouterr().out
        assert "g9-gpu-rtxpro6000-blackwell-1" in out
        assert "16 vCPU" in out
        # Non-Blackwell plans are not reported as matches.
        assert "g1-gpu-rtx6000-1" not in out

    def test_catalog_handles_unreachable_api(self, capsys):
        def boom(path, token=None):
            raise OSError("network unreachable")

        assert not check_akamai.report_gpu_catalog(fetch=boom)
        assert "BLOCKED" in capsys.readouterr().out

    def test_missing_token_reports_missing_capability(self, monkeypatch, capsys):
        monkeypatch.delenv("LINODE_TOKEN", raising=False)
        monkeypatch.setattr(check_akamai, "report_gpu_catalog", lambda fetch=None: True)
        assert check_akamai.main([]) == 0
        out = capsys.readouterr().out
        assert "READ-ONLY" in out
        assert "Missing capability" in out

    def test_token_value_is_never_printed(self, monkeypatch, capsys):
        secret = "fake-token-for-test-only"  # noqa: S105 - synthetic test value
        monkeypatch.setenv("LINODE_TOKEN", secret)
        monkeypatch.setattr(check_akamai, "get_json", lambda path, token=None: FAKE_AVAILABILITY)
        monkeypatch.setattr(check_akamai, "report_gpu_catalog", lambda fetch=None: True)
        assert check_akamai.main([]) == 0
        out = capsys.readouterr().out
        assert secret not in out
        assert "eu-fake-2" in out

    def test_availability_failure_is_actionable(self, capsys):
        def boom(path, token=None):
            raise OSError("403")

        assert not check_akamai.report_account_availability("fake", fetch=boom)
        assert "token may lack scope" in capsys.readouterr().out


class TestGcpBlockedPaths:
    def test_missing_cli_is_blocked_with_actionable_message(self, monkeypatch, capsys):
        monkeypatch.setattr(check_gcp.shutil, "which", lambda name: None)
        assert check_gcp.main([]) == 1
        out = capsys.readouterr().out
        assert "READ-ONLY" in out
        assert "BLOCKED" in out
        assert "Missing capability" in out

    def test_missing_credentials_is_blocked(self, monkeypatch, capsys):
        monkeypatch.setattr(check_gcp.shutil, "which", lambda name: "/usr/bin/gcloud")
        monkeypatch.setattr(check_gcp, "run", lambda cmd: (1, ""))
        assert check_gcp.main([]) == 1
        out = capsys.readouterr().out
        assert "no active credentials" in out
        assert "never commit them" in out


class TestAwsBlockedPaths:
    def test_missing_cli_is_blocked_with_actionable_message(self, monkeypatch, capsys):
        monkeypatch.setattr(check_aws.shutil, "which", lambda name: None)
        assert check_aws.main([]) == 1
        out = capsys.readouterr().out
        assert "READ-ONLY" in out
        assert "BLOCKED" in out
        assert "Missing capability" in out

    def test_missing_credentials_is_blocked(self, monkeypatch, capsys):
        monkeypatch.setattr(check_aws.shutil, "which", lambda name: "/usr/bin/aws")
        monkeypatch.setattr(check_aws, "run", lambda cmd: (255, "unable to locate credentials"))
        assert check_aws.main([]) == 1
        assert "no working credentials" in capsys.readouterr().out


class TestReadOnlyByConstruction:
    """The scripts must contain no mutating CLI verbs or HTTP methods."""

    MUTATING_TOKENS = (
        "create-",
        "delete-",
        "terminate-",
        "start-instances",
        "stop-instances",
        "modify-",
        "run-instances",
        "instances create",
        "instances delete",
        "instances stop",
        "instances start",
        "urllib.request.Request(",  # checked separately below for method usage
    )

    def test_no_mutating_operations_in_sources(self):
        for script in ("check_akamai", "check_gcp", "check_aws"):
            source = (PREFLIGHT_DIR / f"{script}.py").read_text(encoding="utf-8")
            for token in self.MUTATING_TOKENS[:-1]:
                assert token not in source, f"{script}.py contains mutating token {token!r}"
            # urllib requests must never set an explicit non-GET method.
            assert 'method="POST"' not in source
            assert 'method="PUT"' not in source
            assert 'method="DELETE"' not in source

    def test_help_describes_read_only_behavior(self, capsys):
        import pytest

        for module in (check_akamai, check_gcp, check_aws):
            with pytest.raises(SystemExit) as excinfo:
                module.main(["--help"])
            assert excinfo.value.code == 0
            assert "READ-ONLY" in capsys.readouterr().out
