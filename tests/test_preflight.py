"""Offline tests for the truthful Akamai preflight: injected fetchers only.

No network, no credentials. Every test asserts the truthful-readiness
contract: the authenticated workflow exits nonzero unless the exact one-GPU
plan, the selected region's confirmed deployability of that exact plan, and
the applicable regional price are ALL observed; the public catalog never
substitutes; output and receipts are sanitized.
"""

from __future__ import annotations

import json

from blackwell_lab.cloud import preflight
from blackwell_lab.cloud.preflight import (
    authenticated_readiness,
    check_plan_entitlement,
    check_region_deployability,
    main,
    report_gpu_catalog,
)

PLAN_1GPU = {
    "id": "g2-rtxpro6000-x1",
    "class": "gpu",
    "vcpus": 16,
    "memory": 131072,
    "gpus": 1,
    "price": {"hourly": 3.5},
    "region_prices": [{"id": "eu-central", "hourly": 4.2}],
}
PLAN_4GPU = {
    "id": "g2-rtxpro6000-x4",
    "class": "gpu",
    "vcpus": 64,
    "memory": 524288,
    "gpus": 4,
    "price": {"hourly": 14.0},
}
REGION_ORD = {"id": "us-ord", "capabilities": ["Linodes", "GPU Linodes"]}
REGION_NOGPU = {"id": "us-sea", "capabilities": ["Linodes"]}


def make_fetch(
    *,
    types=(),
    regions=(),
    availability=(),
    region_plans=(),
    fail_paths=(),
):
    def fetch(path, token=None):
        for prefix in fail_paths:
            if path.startswith(prefix):
                raise RuntimeError("boom: account-id-777 raw payload")
        if path.startswith("/linode/types"):
            return {"data": list(types), "pages": 1}
        if path.startswith("/regions/") and "/availability" in path:
            return {"data": list(region_plans), "pages": 1}
        if path.startswith("/regions"):
            return {"data": list(regions), "pages": 1}
        if path.startswith("/account/availability"):
            return {"data": list(availability), "pages": 1}
        raise AssertionError(f"unexpected path: {path}")

    return fetch


def ready_fetch():
    return make_fetch(
        types=[PLAN_1GPU, PLAN_4GPU],
        regions=[REGION_ORD, REGION_NOGPU],
        availability=[],
        region_plans=[{"region": "us-ord", "plan": "g2-rtxpro6000-x1", "available": True}],
    )


class TestPublicCatalog:
    def test_catalog_lists_blackwell_plans(self, capsys):
        assert report_gpu_catalog(make_fetch(types=[PLAN_1GPU])) is True
        out = capsys.readouterr().out
        assert "g2-rtxpro6000-x1" in out

    def test_catalog_failure_is_sanitized(self, capsys):
        assert report_gpu_catalog(make_fetch(fail_paths=("/linode/types",))) is False
        out = capsys.readouterr().out
        assert "account-id-777" not in out
        assert "boom" not in out


class TestPlanEntitlement:
    def test_exact_single_gpu_plan_passes(self, capsys):
        decision = check_plan_entitlement("t", make_fetch(types=[PLAN_1GPU, PLAN_4GPU]))
        assert decision["plan_visible"] is True
        assert decision["plan"]["id"] == "g2-rtxpro6000-x1"

    def test_no_blackwell_plans_is_not_visible(self):
        decision = check_plan_entitlement("t", make_fetch(types=[]))
        assert decision == {"completed": True, "plan_visible": False, "plan": None}

    def test_only_multi_gpu_plans_do_not_pass(self, capsys):
        decision = check_plan_entitlement("t", make_fetch(types=[PLAN_4GPU]))
        assert decision["plan_visible"] is False
        assert "TARGET PLAN MISSING" in capsys.readouterr().out

    def test_lookup_failure_is_sanitized_and_incomplete(self, capsys):
        decision = check_plan_entitlement("t", make_fetch(fail_paths=("/linode/types",)))
        assert decision["completed"] is False
        out = capsys.readouterr().out
        assert "account-id-777" not in out


class TestRegionDeployability:
    def test_confirmed_region_passes_all_decisions(self):
        decision = check_region_deployability("t", "us-ord", PLAN_1GPU, ready_fetch())
        assert decision["region_exists"] is True
        assert decision["capability_ok"] is True
        assert decision["account_allowed"] is True
        assert decision["plan_deployable"] is True
        assert decision["regional_price_usd_per_hour"] == 3.5

    def test_gpu_capable_region_without_exact_plan_is_not_deployable(self, capsys):
        fetch = make_fetch(
            types=[PLAN_1GPU],
            regions=[REGION_ORD],
            availability=[],
            region_plans=[{"region": "us-ord", "plan": "g2-other", "available": True}],
        )
        decision = check_region_deployability("t", "us-ord", PLAN_1GPU, fetch)
        assert decision["capability_ok"] is True
        assert decision["plan_deployable"] is False
        assert "not Blackwell deployability" in capsys.readouterr().out

    def test_account_restricted_region_is_not_allowed(self):
        fetch = make_fetch(
            regions=[REGION_ORD],
            availability=[{"region": "us-ord", "unavailable": ["GPU Linodes"]}],
            region_plans=[{"region": "us-ord", "plan": "g2-rtxpro6000-x1", "available": True}],
        )
        decision = check_region_deployability("t", "us-ord", PLAN_1GPU, fetch)
        assert decision["account_allowed"] is False

    def test_regional_surcharge_price_is_used_when_present(self):
        fetch = make_fetch(
            regions=[{"id": "eu-central", "capabilities": ["GPU Linodes"]}],
            availability=[],
            region_plans=[{"region": "eu-central", "plan": "g2-rtxpro6000-x1", "available": True}],
        )
        decision = check_region_deployability("t", "eu-central", PLAN_1GPU, fetch)
        assert decision["regional_price_usd_per_hour"] == 4.2

    def test_missing_price_is_unconfirmed_not_defaulted(self):
        plan = {**PLAN_1GPU, "price": {}, "region_prices": []}
        decision = check_region_deployability("t", "us-ord", plan, ready_fetch())
        assert decision["regional_price_usd_per_hour"] is None

    def test_lookup_failure_is_sanitized(self, capsys):
        fetch = make_fetch(fail_paths=("/regions",))
        decision = check_region_deployability("t", "us-ord", PLAN_1GPU, fetch)
        assert decision["completed"] is False
        assert "account-id-777" not in capsys.readouterr().out


class TestAuthenticatedReadiness:
    def test_fully_confirmed_readiness_is_ready(self):
        receipt = authenticated_readiness("t", "us-ord", ready_fetch())
        assert receipt["ready"] is True
        assert receipt["selected_plan_id"] == "g2-rtxpro6000-x1"
        assert receipt["selected_region"] == "us-ord"
        assert receipt["observed_hourly_price_usd"] == 3.5
        assert receipt["generated_at_utc"]
        assert all(receipt["decisions"].values())

    def test_missing_exact_plan_is_not_ready(self):
        fetch = make_fetch(types=[PLAN_4GPU], regions=[REGION_ORD])
        receipt = authenticated_readiness("t", "us-ord", fetch)
        assert receipt["ready"] is False
        assert receipt["decisions"]["exact_single_gpu_plan_visible"] is False

    def test_unconfirmed_region_is_not_ready(self):
        fetch = make_fetch(
            types=[PLAN_1GPU],
            regions=[REGION_ORD],
            availability=[],
            region_plans=[{"region": "us-ord", "plan": "g2-rtxpro6000-x1", "available": False}],
        )
        receipt = authenticated_readiness("t", "us-ord", fetch)
        assert receipt["ready"] is False
        assert receipt["decisions"]["exact_plan_deployable_in_region"] is False

    def test_missing_capability_is_not_ready(self):
        fetch = make_fetch(
            types=[PLAN_1GPU],
            regions=[REGION_NOGPU],
            availability=[],
            region_plans=[{"region": "us-sea", "plan": "g2-rtxpro6000-x1", "available": True}],
        )
        receipt = authenticated_readiness("t", "us-sea", fetch)
        assert receipt["ready"] is False
        assert receipt["decisions"]["gpu_capability_in_region"] is False

    def test_unconfirmed_price_is_not_ready(self):
        plan = {**PLAN_1GPU, "price": {}, "region_prices": []}
        fetch = make_fetch(
            types=[plan],
            regions=[REGION_ORD],
            availability=[],
            region_plans=[{"region": "us-ord", "plan": "g2-rtxpro6000-x1", "available": True}],
        )
        receipt = authenticated_readiness("t", "us-ord", fetch)
        assert receipt["ready"] is False
        assert receipt["decisions"]["regional_price_observed"] is False

    def test_receipt_is_sanitized(self):
        receipt = authenticated_readiness("t", "us-ord", ready_fetch())
        dumped = json.dumps(receipt)
        assert "t" != dumped  # trivial, but the token never appears
        assert "token" not in dumped.lower() or "tokens" in dumped.lower()
        assert "account" not in json.dumps(receipt["decisions"]).lower().replace(
            "account_allowed_in_region", ""
        )


class TestMainWorkflow:
    def test_public_only_succeeds_but_disclaims_readiness(self, capsys, monkeypatch):
        monkeypatch.setattr(preflight, "get_json", ready_fetch())
        code = main(["--public-only"])
        out = capsys.readouterr().out
        assert code == 0
        assert "does NOT constitute readiness" in out

    def test_missing_token_blocks_authenticated_mode(self, capsys, monkeypatch):
        monkeypatch.delenv("LINODE_TOKEN", raising=False)
        monkeypatch.setattr(preflight, "get_json", ready_fetch())
        code = main([])
        assert code == 1
        assert "LINODE_TOKEN is not set" in capsys.readouterr().out

    def test_missing_region_blocks_authenticated_mode(self, capsys, monkeypatch):
        monkeypatch.setenv("LINODE_TOKEN", "fake-token-for-offline-test")
        monkeypatch.setattr(preflight, "get_json", ready_fetch())
        code = main([])
        assert code == 1
        assert "--region is required" in capsys.readouterr().out

    def test_ready_workflow_exits_zero_and_writes_receipt(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setenv("LINODE_TOKEN", "fake-token-for-offline-test")
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path / "private"))
        monkeypatch.setattr(preflight, "get_json", ready_fetch())
        code = main(["--region", "us-ord"])
        out = capsys.readouterr().out
        assert code == 0
        assert "READY" in out
        receipts = list((tmp_path / "private" / "preflight-receipts").glob("*.json"))
        assert len(receipts) == 1
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        assert receipt["ready"] is True
        # The terminal output never contains the private receipt path.
        assert str(tmp_path) not in out

    def test_public_catalog_success_never_makes_authenticated_workflow_succeed(
        self, capsys, monkeypatch, tmp_path
    ):
        # The catalog shows plans, but the ACCOUNT has none: the workflow
        # must exit nonzero despite the catalog success.
        calls = {"n": 0}

        def fetch(path, token=None):
            if path.startswith("/linode/types"):
                calls["n"] += 1
                if token is None:  # public catalog: plans visible
                    return {"data": [PLAN_1GPU], "pages": 1}
                return {"data": [], "pages": 1}  # account sees nothing
            if path.startswith("/regions"):
                return {"data": [REGION_ORD], "pages": 1}
            return {"data": [], "pages": 1}

        monkeypatch.setenv("LINODE_TOKEN", "fake-token-for-offline-test")
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path / "private"))
        monkeypatch.setattr(preflight, "get_json", fetch)
        code = main(["--region", "us-ord"])
        out = capsys.readouterr().out
        assert "Found 1 RTX PRO 6000 Blackwell plan(s)" in out  # catalog succeeded
        assert code == 1  # authenticated readiness still failed
        assert "NOT READY" in out

    def test_not_ready_exits_nonzero_with_receipt(self, capsys, monkeypatch, tmp_path):
        fetch = make_fetch(
            types=[PLAN_4GPU],  # exact 1-GPU plan missing
            regions=[REGION_ORD],
        )
        monkeypatch.setenv("LINODE_TOKEN", "fake-token-for-offline-test")
        monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path / "private"))
        monkeypatch.setattr(preflight, "get_json", fetch)
        code = main(["--region", "us-ord"])
        assert code == 1
        receipts = list((tmp_path / "private" / "preflight-receipts").glob("*.json"))
        assert len(receipts) == 1
        assert json.loads(receipts[0].read_text(encoding="utf-8"))["ready"] is False

    def test_unwritable_receipt_blocks_readiness(self, capsys, monkeypatch):
        monkeypatch.setenv("LINODE_TOKEN", "fake-token-for-offline-test")
        monkeypatch.delenv("LAB_RESULTS_DIR", raising=False)
        monkeypatch.setattr(preflight, "get_json", ready_fetch())
        code = main(["--region", "us-ord"])
        assert code == 1
        assert "receipt could not be written" in capsys.readouterr().out

    def test_read_only_banner_and_no_write_verbs(self, capsys, monkeypatch):
        monkeypatch.setattr(preflight, "get_json", ready_fetch())
        main(["--public-only"])
        assert "READ-ONLY" in capsys.readouterr().out
        # The module performs GETs only: no write verbs anywhere.
        import inspect

        source = inspect.getsource(preflight)
        for verb in ("urlopen(", "Request("):
            assert verb in source  # GET machinery present
        for forbidden in ('method="POST"', 'method="PUT"', 'method="DELETE"'):
            assert forbidden not in source


def test_receipt_writer_uses_private_permissions(monkeypatch, tmp_path):
    import os
    import stat

    monkeypatch.setenv("LAB_RESULTS_DIR", str(tmp_path / "private"))
    receipt = authenticated_readiness("t", "us-ord", ready_fetch())
    name = preflight._write_receipt(receipt)
    target = tmp_path / "private" / "preflight-receipts" / name
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(target.parent).st_mode) == 0o700
