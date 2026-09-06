"""Mocked tests for the read-only preflight scripts.

These tests never contact any provider: network and CLI lookups are mocked.
They verify read-only behavior, blocked-path exit codes, and that secrets,
account identifiers, and raw error payloads are never printed.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PREFLIGHT_DIR = Path(__file__).resolve().parents[1] / "scripts" / "preflight"


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, PREFLIGHT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The Akamai implementation lives in the package (unit-testable offline);
# scripts/preflight/check_akamai.py is a thin wrapper around its main().
from blackwell_lab.cloud import preflight as check_akamai  # noqa: E402

check_akamai_script = load_script("check_akamai")
check_gcp = load_script("check_gcp")
check_aws = load_script("check_aws")

# Synthetic sensitive values injected into mocked errors. None of these are
# real; all of them must be absent from every line the scripts print.
SENSITIVE_VALUES = (
    "123456789012",  # synthetic AWS account id
    "synthetic-project-84121",  # synthetic GCP project id
    "arn:aws:iam::123456789012:role/synthetic-role",  # synthetic ARN
    "AKIAFAKEFAKEFAKEFAKE",  # synthetic access-key-like string
    "ya29.synthetic-oauth-token-value",  # synthetic OAuth-token-like string
    "/home/fakeuser/.aws/credentials",  # synthetic credential path
    "https://internal-10-0-0-5.example.internal:8443/api",  # private endpoint
)
SENSITIVE_BLOB = " ".join(SENSITIVE_VALUES)


def assert_no_sensitive_output(text: str) -> None:
    for value in SENSITIVE_VALUES:
        assert value not in text, f"sensitive value {value!r} leaked into output"


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

FAKE_REGIONS = {
    "data": [
        {"id": "us-fake-1", "capabilities": ["Linodes", "GPU Linodes"]},
        {"id": "eu-fake-2", "capabilities": ["Linodes", "GPU Linodes"]},
        {"id": "ap-fake-3", "capabilities": ["Linodes"]},
    ]
}


def fake_fetch(path, token=None):
    if path.startswith("/linode/types"):
        return FAKE_CATALOG
    if path.startswith("/regions"):
        return FAKE_REGIONS
    if path.startswith("/account/availability"):
        return FAKE_AVAILABILITY
    raise AssertionError(f"unexpected path: {path}")


class TestAkamai:
    def test_script_is_a_thin_wrapper_around_the_package_module(self):
        assert check_akamai_script.main is check_akamai.main

    def test_catalog_reports_blackwell_plans(self, capsys):
        assert check_akamai.report_gpu_catalog(fetch=fake_fetch)
        out = capsys.readouterr().out
        assert "g9-gpu-rtxpro6000-blackwell-1" in out
        assert "16 vCPU" in out
        # Non-Blackwell plans are not reported as matches.
        assert "g1-gpu-rtx6000-1" not in out

    def test_catalog_pagination_is_followed(self, capsys):
        pages = {
            1: {"data": FAKE_CATALOG["data"][:1], "pages": 2},
            2: {"data": FAKE_CATALOG["data"][1:], "pages": 2},
        }

        def paged(path, token=None):
            page = int(path.split("page=")[1])
            return pages[page]

        assert check_akamai.report_gpu_catalog(fetch=paged)
        assert "1 RTX PRO 6000 Blackwell plan(s)" in capsys.readouterr().out

    def test_catalog_handles_unreachable_api(self, capsys):
        def boom(path, token=None):
            raise OSError("network unreachable")

        assert not check_akamai.report_gpu_catalog(fetch=boom)
        assert "BLOCKED" in capsys.readouterr().out

    def test_catalog_error_text_is_never_printed(self, capsys):
        def boom(path, token=None):
            raise OSError(f"connect failed via {SENSITIVE_BLOB}")

        assert not check_akamai.report_gpu_catalog(fetch=boom)
        assert_no_sensitive_output(capsys.readouterr().out)

    def test_entitlement_reports_the_exact_single_gpu_plan(self, capsys):
        assert check_akamai.report_plan_entitlement("tok", fetch=fake_fetch)
        out = capsys.readouterr().out
        assert "ENTITLEMENT: 1 RTX PRO 6000 Blackwell plan(s)" in out
        assert "TARGET PLAN OK: g9-gpu-rtxpro6000-blackwell-1" in out
        assert "$1.23/hr" in out  # account-visible hourly price
        assert "tok" not in out

    def test_entitlement_not_visible_requires_onboarding(self, capsys):
        no_gpu = {"data": [{"id": "g6-standard-2", "class": "standard"}]}
        assert check_akamai.report_plan_entitlement("tok", fetch=lambda p, t=None: no_gpu)
        out = capsys.readouterr().out
        assert "NOT VISIBLE" in out
        assert "onboarding" in out

    def test_entitlement_rejects_multi_gpu_only_catalogs(self, capsys):
        multi = {
            "data": [
                {
                    "id": "g9-gpu-rtxpro6000-blackwell-x4",
                    "class": "gpu",
                    "gpus": 4,
                    "vcpus": 64,
                    "memory": 736 * 1024,
                    "price": {"hourly": 9.99},
                }
            ]
        }
        assert check_akamai.report_plan_entitlement("tok", fetch=lambda p, t=None: multi)
        out = capsys.readouterr().out
        assert "TARGET PLAN MISSING" in out
        assert "Do not substitute a larger plan" in out

    def test_entitlement_failure_is_sanitized(self, capsys):
        def boom(path, token=None):
            raise OSError(f"403 for {SENSITIVE_BLOB}")

        assert not check_akamai.report_plan_entitlement("tok", fetch=boom)
        out = capsys.readouterr().out
        assert "BLOCKED" in out
        assert_no_sensitive_output(out)

    def test_eligible_regions_cross_reference_capability_and_account(self, capsys):
        assert check_akamai.report_eligible_regions("tok", fetch=fake_fetch)
        out = capsys.readouterr().out
        # us-fake-1: GPU-capable and available -> eligible.
        assert "Regions eligible for GPU Linodes on this account: 1" in out
        # eu-fake-2: GPU-capable but account-unavailable.
        assert "eu-fake-2 (unavailable)" in out
        # ap-fake-3 has no GPU capability and is never listed as eligible.
        assert "  - ap-fake-3\n" not in out

    def test_region_failure_is_sanitized_and_actionable(self, capsys):
        def boom(path, token=None):
            raise OSError(f"403 forbidden for {SENSITIVE_BLOB}")

        assert not check_akamai.report_eligible_regions("tok", fetch=boom)
        out = capsys.readouterr().out
        assert "BLOCKED" in out
        assert "scope" in out
        assert_no_sensitive_output(out)

    def test_missing_token_is_blocked_and_nonzero(self, monkeypatch, capsys):
        monkeypatch.delenv("LINODE_TOKEN", raising=False)
        monkeypatch.setattr(check_akamai, "report_gpu_catalog", lambda fetch=None: True)
        assert check_akamai.main([]) == 1
        out = capsys.readouterr().out
        assert "READ-ONLY" in out
        assert "BLOCKED" in out
        assert "Missing capability" in out
        assert "--public-only" in out

    def test_public_only_mode_succeeds_without_token(self, monkeypatch, capsys):
        monkeypatch.delenv("LINODE_TOKEN", raising=False)
        monkeypatch.setattr(check_akamai, "report_gpu_catalog", lambda fetch=None: True)
        assert check_akamai.main(["--public-only"]) == 0
        out = capsys.readouterr().out
        assert "intentionally skipped" in out

    def test_public_only_mode_still_fails_on_catalog_error(self, monkeypatch):
        monkeypatch.setattr(check_akamai, "report_gpu_catalog", lambda fetch=None: False)
        assert check_akamai.main(["--public-only"]) == 1

    def test_token_value_is_never_printed(self, monkeypatch, capsys):
        secret = "fake-token-for-test-only"  # noqa: S105 - synthetic test value
        monkeypatch.setenv("LINODE_TOKEN", secret)
        monkeypatch.setattr(check_akamai, "get_json", fake_fetch)
        monkeypatch.setattr(check_akamai, "report_gpu_catalog", lambda fetch=None: True)
        assert check_akamai.main([]) == 0
        out = capsys.readouterr().out
        assert secret not in out
        assert "eu-fake-2" in out

    def test_availability_failure_is_sanitized_actionable_and_nonzero(self, monkeypatch, capsys):
        def boom(path, token=None):
            raise OSError(f"403 forbidden for {SENSITIVE_BLOB}")

        monkeypatch.setenv("LINODE_TOKEN", "fake-token-for-test-only")
        monkeypatch.setattr(check_akamai, "get_json", boom)
        monkeypatch.setattr(check_akamai, "report_gpu_catalog", lambda fetch=None: True)
        assert check_akamai.main([]) == 1
        out = capsys.readouterr().out
        assert "BLOCKED" in out
        assert "scope" in out
        assert_no_sensitive_output(out)


def make_aws_run(offerings_result, quota_result, sts_code=0):
    def fake_run(cmd):
        if "sts" in cmd:
            return (sts_code, "{}")
        if "describe-instance-type-offerings" in cmd:
            return offerings_result
        if "service-quotas" in cmd:
            return quota_result
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run


class TestAws:
    def test_missing_cli_is_blocked_with_actionable_message(self, monkeypatch, capsys):
        monkeypatch.setattr(check_aws.shutil, "which", lambda name: None)
        assert check_aws.main([]) == 1
        out = capsys.readouterr().out
        assert "READ-ONLY" in out
        assert "BLOCKED" in out
        assert "Missing capability" in out

    def test_missing_credentials_is_blocked(self, monkeypatch, capsys):
        monkeypatch.setattr(check_aws.shutil, "which", lambda name: "/usr/bin/aws")
        monkeypatch.setattr(check_aws, "run", lambda cmd: (255, ""))
        assert check_aws.main([]) == 1
        assert "no working credentials" in capsys.readouterr().out

    def test_full_success_path(self, monkeypatch, capsys):
        offerings = json.dumps({"InstanceTypeOfferings": [{"InstanceType": "g7e.4xlarge"}]})
        quota = json.dumps({"Quota": {"Value": 64.0}})
        monkeypatch.setattr(check_aws.shutil, "which", lambda name: "/usr/bin/aws")
        monkeypatch.setattr(check_aws, "run", make_aws_run((0, offerings), (0, quota)))
        assert check_aws.main([]) == 0
        out = capsys.readouterr().out
        assert "g7e.4xlarge" in out
        assert "64.0" in out
        assert "does not prove immediate/live capacity" in out

    def test_offering_failure_is_sanitized_and_nonzero(self, monkeypatch, capsys):
        quota = json.dumps({"Quota": {"Value": 64.0}})
        monkeypatch.setattr(check_aws.shutil, "which", lambda name: "/usr/bin/aws")
        monkeypatch.setattr(check_aws, "run", make_aws_run((254, SENSITIVE_BLOB), (0, quota)))
        assert check_aws.main([]) == 1
        out = capsys.readouterr().out
        assert "BLOCKED" in out
        assert_no_sensitive_output(out)

    def test_quota_failure_is_sanitized_and_nonzero(self, monkeypatch, capsys):
        offerings = json.dumps({"InstanceTypeOfferings": []})
        monkeypatch.setattr(check_aws.shutil, "which", lambda name: "/usr/bin/aws")
        monkeypatch.setattr(check_aws, "run", make_aws_run((0, offerings), (254, SENSITIVE_BLOB)))
        assert check_aws.main([]) == 1
        out = capsys.readouterr().out
        assert "BLOCKED" in out
        assert "servicequotas:GetServiceQuota" in out
        assert_no_sensitive_output(out)

    def test_no_pricing_api_claim(self, capsys):
        with pytest.raises(SystemExit):
            check_aws.main(["--help"])
        out = capsys.readouterr().out
        assert "does not query the AWS Pricing API" in out


def make_gcp_run(machine_result, quota_result, auth_result=(0, "ACTIVE")):
    def fake_run(cmd):
        if "auth" in cmd:
            return auth_result
        if "machine-types" in cmd:
            return machine_result
        if "regions" in cmd:
            return quota_result
        raise AssertionError(f"unexpected command: {cmd}")

    return fake_run


class TestGcp:
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

    def test_full_success_path(self, monkeypatch, capsys):
        monkeypatch.setattr(check_gcp.shutil, "which", lambda name: "/usr/bin/gcloud")
        monkeypatch.setattr(
            check_gcp,
            "run",
            make_gcp_run((0, "us-fake1-a\nus-fake1-b"), (0, "us-fake1  8")),
        )
        assert check_gcp.main([]) == 0
        out = capsys.readouterr().out
        assert "us-fake1-a" in out
        assert "quota overview" in out
        assert "does not prove immediate/live capacity" in out

    def test_machine_type_failure_is_sanitized_and_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(check_gcp.shutil, "which", lambda name: "/usr/bin/gcloud")
        monkeypatch.setattr(check_gcp, "run", make_gcp_run((1, SENSITIVE_BLOB), (0, "table")))
        assert check_gcp.main([]) == 1
        out = capsys.readouterr().out
        assert "BLOCKED" in out
        assert_no_sensitive_output(out)

    def test_quota_failure_is_sanitized_and_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(check_gcp.shutil, "which", lambda name: "/usr/bin/gcloud")
        monkeypatch.setattr(check_gcp, "run", make_gcp_run((0, "us-fake1-a"), (1, SENSITIVE_BLOB)))
        assert check_gcp.main([]) == 1
        out = capsys.readouterr().out
        assert "BLOCKED" in out
        assert "compute.regions.list" in out
        assert_no_sensitive_output(out)


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
        sources = [
            PREFLIGHT_DIR / "check_akamai.py",
            PREFLIGHT_DIR / "check_gcp.py",
            PREFLIGHT_DIR / "check_aws.py",
            # The Akamai implementation module itself.
            Path(check_akamai.__file__),
        ]
        for path in sources:
            source = path.read_text(encoding="utf-8")
            for token in self.MUTATING_TOKENS[:-1]:
                assert token not in source, f"{path.name} contains mutating token {token!r}"
            # urllib requests must never set an explicit non-GET method.
            assert 'method="POST"' not in source
            assert 'method="PUT"' not in source
            assert 'method="DELETE"' not in source

    def test_help_describes_read_only_behavior(self, capsys):
        for module in (check_akamai, check_gcp, check_aws):
            with pytest.raises(SystemExit) as excinfo:
                module.main(["--help"])
            assert excinfo.value.code == 0
            assert "READ-ONLY" in capsys.readouterr().out
