"""Adversarial offline tests for the Terraform lifecycle safety model.

Every terraform/git invocation is a fake runner; every provider call is a
fake fetcher/probe. Nothing here touches the network, credentials, or a real
terraform binary.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from blackwell_lab.cloud import lifecycle
from blackwell_lab.cloud.lifecycle import (
    APPLY_APPROVAL_TEMPLATE,
    DESTROY_APPROVAL_TEMPLATE,
    ApprovalError,
    CommandResult,
    LifecycleError,
)

RUN_TAG = "p3-pilot-20260907a"
COMMIT = "0123456789abcdef0123456789abcdef01234567"

INSTANCE_STATE = {
    "address": "linode_instance.gpu_baseline",
    "type": "linode_instance",
    "name": "gpu_baseline",
    "values": {
        "id": "12345678",
        "label": f"bwlab-{RUN_TAG}",
        "region": "us-ord",
        "tags": ["blackwell-lab", f"run:{RUN_TAG}", "ttl-hours:6", "phase:3"],
    },
}
FIREWALL_STATE = {
    "address": "linode_firewall.gpu_baseline",
    "type": "linode_firewall",
    "name": "gpu_baseline",
    "values": {
        "id": "555",
        "label": f"bwlab-fw-{RUN_TAG}",
        "tags": ["blackwell-lab", f"run:{RUN_TAG}", "ttl-hours:6", "phase:3"],
    },
}


def state_json(resources=None):
    if resources is None:
        resources = [INSTANCE_STATE, FIREWALL_STATE]
    return {"values": {"root_module": {"resources": resources}}}


def plan_json(changes=None):
    if changes is None:
        changes = [
            ("linode_instance.gpu_baseline", ["create"]),
            ("linode_firewall.gpu_baseline", ["create"]),
        ]
    return {
        "resource_changes": [
            {"address": address, "change": {"actions": actions}} for address, actions in changes
        ]
    }


class FakeRunner:
    """Scripted terraform/git executor recording every invocation."""

    def __init__(
        self,
        *,
        plan_rc=0,
        apply_rc=0,
        init_rc=0,
        show_state=None,
        show_plan=None,
        show_state_rc=0,
        plan_text="Plan: 2 to add. ip 203.0.113.9 ssh-ed25519 AAAAC3Nza key",
        commit=COMMIT,
    ):
        self.plan_rc = plan_rc
        self.apply_rc = apply_rc
        self.init_rc = init_rc
        self.show_state = show_state if show_state is not None else state_json()
        self.show_plan = show_plan if show_plan is not None else plan_json()
        self.show_state_rc = show_state_rc
        self.plan_text = plan_text
        self.commit = commit
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []

    def __call__(self, argv, cwd, env):
        self.calls.append(list(argv))
        self.envs.append(dict(env))
        if argv[:2] == ["git", "rev-parse"]:
            return CommandResult(0, self.commit + "\n")
        if argv[:2] == ["terraform", "version"]:
            return CommandResult(0, json.dumps({"terraform_version": "1.9.8"}))
        if argv[1] == "init":
            return CommandResult(self.init_rc, "")
        if argv[1] == "plan":
            out = next(a for a in argv if a.startswith("-out="))[len("-out=") :]
            Path(out).write_bytes(b"BINARY-PLAN-" + " ".join(argv).encode())
            return CommandResult(self.plan_rc, "")
        if argv[1] == "show" and "-json" in argv:
            if len(argv) > 3:  # terraform show -json <planfile>
                return CommandResult(0, json.dumps(self.show_plan))
            return CommandResult(self.show_state_rc, json.dumps(self.show_state))
        if argv[1] == "show" and "-no-color" in argv:
            return CommandResult(0, self.plan_text)
        if argv[1] == "apply":
            return CommandResult(self.apply_rc, "")
        raise AssertionError(f"unexpected command: {argv}")


@pytest.fixture()
def tf_dir(tmp_path):
    directory = tmp_path / "repo" / "infra" / "akamai"
    directory.mkdir(parents=True)
    (directory / "main.tf").write_text("# config v1\n", encoding="utf-8")
    (directory / "versions.tf").write_text("# versions\n", encoding="utf-8")
    (directory / ".terraform.lock.hcl").write_text("# lock v1\n", encoding="utf-8")
    return directory


@pytest.fixture()
def external(tmp_path):
    directory = tmp_path / "external-results"
    directory.mkdir()
    return directory


@pytest.fixture()
def paths(external):
    lp = lifecycle.lifecycle_paths(external, RUN_TAG)
    lp.var_file.write_text('region = "us-ord"\n', encoding="utf-8")
    return lp


def make_plan(paths, tf_dir, runner=None, stage="apply"):
    runner = runner or FakeRunner()
    meta = lifecycle.save_plan(RUN_TAG, stage=stage, paths=paths, tf_dir=tf_dir, runner=runner)
    return meta, runner


def apply_phrase(meta):
    return APPLY_APPROVAL_TEMPLATE.format(run_tag=RUN_TAG, plan_sha256=meta["plan_sha256"])


def destroy_phrase(meta):
    return DESTROY_APPROVAL_TEMPLATE.format(run_tag=RUN_TAG, plan_sha256=meta["plan_sha256"])


class TestExternalArtifacts:
    def test_lifecycle_paths_require_absolute_external_dir(self):
        with pytest.raises(LifecycleError, match="absolute"):
            lifecycle.lifecycle_paths(Path("relative/dir"), RUN_TAG)

    def test_every_artifact_lives_under_the_external_dir_not_the_repo(
        self, paths, tf_dir, external
    ):
        meta, _ = make_plan(paths, tf_dir)
        assert meta["plan_sha256"]
        repo_root = tf_dir.parents[1]
        for artifact in repo_root.rglob("*"):
            assert artifact.suffix not in (".tfplan", ".tfstate"), artifact
            assert "plan-meta" not in artifact.name, artifact
        assert paths.plan_path("apply").is_file()
        assert paths.plan_text_path("apply").is_file()
        assert paths.plan_meta_path("apply").is_file()
        assert external in paths.plan_path("apply").parents

    def test_private_modes_0700_dirs_0600_files(self, paths, tf_dir):
        make_plan(paths, tf_dir)
        dir_mode = stat.S_IMODE(os.stat(paths.run_dir).st_mode)
        assert dir_mode == 0o700
        for name in ("apply.tfplan", "apply.plan-meta.json", "apply.tfplan.redacted.txt"):
            file_mode = stat.S_IMODE(os.stat(paths.run_dir / name).st_mode)
            assert file_mode == 0o600, name

    def test_init_configures_external_backend_and_tf_data_dir(self, paths, tf_dir):
        runner = FakeRunner()
        report = lifecycle.init_backend(RUN_TAG, paths=paths, tf_dir=tf_dir, runner=runner)
        assert report["external_state"] is True
        init_call = runner.calls[0]
        assert f"-backend-config=path={paths.state_path}" in init_call
        assert runner.envs[0]["TF_DATA_DIR"] == str(paths.tf_data_dir)
        assert paths.tf_data_dir.is_dir()

    def test_atomic_writer_reports_private_permissions(self, tmp_path):
        target = tmp_path / "private" / "artifact.json"
        lifecycle.write_private_json(target, {"x": 1})
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(target.parent).st_mode) == 0o700
        assert [p.name for p in target.parent.iterdir()] == ["artifact.json"]


class TestPilotBlockers:
    def _ready_ledger(self):
        return {
            "run_tag": RUN_TAG,
            "reconciled": True,
            "reconciliation": {"provider_checked": True},
            "resources": [
                {
                    "address": "linode_instance.gpu_baseline",
                    "type": "linode_instance",
                    "provider_id": "12345678",
                },
                {
                    "address": "linode_firewall.gpu_baseline",
                    "type": "linode_firewall",
                    "provider_id": "555",
                },
            ],
        }

    def test_all_gates_pass_with_expected_shape(self):
        assert lifecycle.pilot_blockers(self._ready_ledger(), pending=False) == []

    def test_pending_operation_blocks(self):
        blockers = lifecycle.pilot_blockers(self._ready_ledger(), pending=True)
        assert "a pending lifecycle operation exists" in blockers

    def test_unreconciled_ledger_blocks(self):
        ledger = self._ready_ledger()
        ledger["reconciled"] = False
        assert "not cleanly reconciled" in lifecycle.pilot_blockers(ledger, pending=False)[0]

    def test_missing_provider_check_blocks(self):
        ledger = self._ready_ledger()
        ledger["reconciliation"] = {"provider_checked": False}
        assert any("provider API" in b for b in lifecycle.pilot_blockers(ledger, pending=False))

    def test_wrong_resource_counts_block(self):
        ledger = self._ready_ledger()
        ledger["resources"] = [ledger["resources"][0]]
        blockers = lifecycle.pilot_blockers(ledger, pending=False)
        assert any("firewall" in b for b in blockers)


class TestSavedPlan:
    def test_plan_metadata_records_all_provenance(self, paths, tf_dir):
        meta, _runner = make_plan(paths, tf_dir)
        assert meta["run_tag"] == RUN_TAG
        assert meta["stage"] == "apply"
        assert len(meta["plan_sha256"]) == 64
        assert meta["commit_sha"] == COMMIT
        assert meta["terraform_version"] == "1.9.8"
        assert len(meta["provider_lock_sha256"]) == 64
        assert len(meta["config_sha256"]) == 64
        assert meta["state_sha256"] is None  # no state before first apply
        assert meta["created_at_utc"]
        assert {a["classification"] for a in meta["actions"]} == {"create"}
        assert sorted(meta["resource_addresses"]) == [
            "linode_firewall.gpu_baseline",
            "linode_instance.gpu_baseline",
        ]

    def test_plan_requires_external_var_file(self, external, tf_dir):
        lp = lifecycle.lifecycle_paths(external, RUN_TAG)  # no var file written
        with pytest.raises(LifecycleError, match=r"terraform\.tfvars.*missing"):
            lifecycle.save_plan(
                RUN_TAG, stage="apply", paths=lp, tf_dir=tf_dir, runner=FakeRunner()
            )

    def test_redacted_text_strips_addresses_and_key_material(self, paths, tf_dir):
        make_plan(paths, tf_dir)
        text = paths.plan_text_path("apply").read_text(encoding="utf-8")
        assert "203.0.113.9" not in text
        assert "AAAAC3Nza" not in text
        assert "[REDACTED-IPV4]" in text
        assert "ssh-ed25519 [REDACTED]" in text

    def test_apply_stage_plan_with_delete_fails_closed(self, paths, tf_dir):
        runner = FakeRunner(show_plan=plan_json([("linode_instance.gpu_baseline", ["delete"])]))
        with pytest.raises(LifecycleError, match="delete, replace, update, or unrelated"):
            make_plan(paths, tf_dir, runner)

    def test_apply_stage_plan_with_replace_fails_closed(self, paths, tf_dir):
        runner = FakeRunner(
            show_plan=plan_json([("linode_instance.gpu_baseline", ["delete", "create"])])
        )
        with pytest.raises(LifecycleError, match="delete, replace, update, or unrelated"):
            make_plan(paths, tf_dir, runner)

    def test_apply_stage_plan_with_unrelated_resource_fails_closed(self, paths, tf_dir):
        runner = FakeRunner(
            show_plan=plan_json(
                [
                    ("linode_instance.gpu_baseline", ["create"]),
                    ("linode_volume.sneaky", ["create"]),
                ]
            )
        )
        with pytest.raises(LifecycleError, match="unrelated"):
            make_plan(paths, tf_dir, runner)

    def test_apply_stage_plan_with_update_fails_closed(self, paths, tf_dir):
        runner = FakeRunner(show_plan=plan_json([("linode_instance.gpu_baseline", ["update"])]))
        with pytest.raises(LifecycleError, match="delete, replace, update, or unrelated"):
            make_plan(paths, tf_dir, runner)

    def test_apply_stage_plan_missing_a_required_create_fails_closed(self, paths, tf_dir):
        runner = FakeRunner(show_plan=plan_json([("linode_instance.gpu_baseline", ["create"])]))
        with pytest.raises(LifecycleError, match="must create exactly"):
            make_plan(paths, tf_dir, runner)

    def test_apply_stage_plan_with_no_creates_fails_closed(self, paths, tf_dir):
        runner = FakeRunner(show_plan=plan_json([]))
        with pytest.raises(LifecycleError, match="must create exactly"):
            make_plan(paths, tf_dir, runner)

    def test_wrong_terraform_cli_version_is_rejected(self, paths, tf_dir):
        class WrongVersionRunner(FakeRunner):
            def __call__(self, argv, cwd, env):
                if argv[:2] == ["terraform", "version"]:
                    return CommandResult(0, json.dumps({"terraform_version": "1.9.7"}))
                return super().__call__(argv, cwd, env)

        with pytest.raises(LifecycleError, match=r"must be exactly 1\.9\.8"):
            make_plan(paths, tf_dir, WrongVersionRunner())


class TestVerifySavedPlan:
    def test_verification_passes_for_the_untouched_plan(self, paths, tf_dir):
        meta, runner = make_plan(paths, tf_dir)
        verified = lifecycle.verify_saved_plan(
            RUN_TAG, stage="apply", paths=paths, tf_dir=tf_dir, runner=runner
        )
        assert verified["plan_sha256"] == meta["plan_sha256"]

    def test_modified_binary_plan_is_rejected(self, paths, tf_dir):
        _, runner = make_plan(paths, tf_dir)
        paths.plan_path("apply").write_bytes(b"tampered")
        with pytest.raises(LifecycleError, match="SHA-256"):
            lifecycle.verify_saved_plan(
                RUN_TAG, stage="apply", paths=paths, tf_dir=tf_dir, runner=runner
            )

    def test_changed_configuration_is_rejected(self, paths, tf_dir):
        _, runner = make_plan(paths, tf_dir)
        (tf_dir / "main.tf").write_text("# config v2 CHANGED\n", encoding="utf-8")
        with pytest.raises(LifecycleError, match="configuration changed"):
            lifecycle.verify_saved_plan(
                RUN_TAG, stage="apply", paths=paths, tf_dir=tf_dir, runner=runner
            )

    def test_changed_lock_file_is_rejected(self, paths, tf_dir):
        _, runner = make_plan(paths, tf_dir)
        (tf_dir / ".terraform.lock.hcl").write_text("# lock v2\n", encoding="utf-8")
        with pytest.raises(LifecycleError, match="lock file changed"):
            lifecycle.verify_saved_plan(
                RUN_TAG, stage="apply", paths=paths, tf_dir=tf_dir, runner=runner
            )

    def test_changed_state_is_rejected(self, paths, tf_dir):
        _, runner = make_plan(paths, tf_dir)
        paths.state_path.write_text("{}", encoding="utf-8")  # state appeared after plan
        with pytest.raises(LifecycleError, match="state changed"):
            lifecycle.verify_saved_plan(
                RUN_TAG, stage="apply", paths=paths, tf_dir=tf_dir, runner=runner
            )

    def test_changed_commit_is_rejected(self, paths, tf_dir):
        _, _ = make_plan(paths, tf_dir)
        moved = FakeRunner(commit="f" * 40)
        with pytest.raises(LifecycleError, match="commit changed"):
            lifecycle.verify_saved_plan(
                RUN_TAG, stage="apply", paths=paths, tf_dir=tf_dir, runner=moved
            )

    def test_stale_plan_is_rejected(self, paths, tf_dir):
        _, runner = make_plan(paths, tf_dir)
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        with pytest.raises(LifecycleError, match="stale"):
            lifecycle.verify_saved_plan(
                RUN_TAG, stage="apply", paths=paths, tf_dir=tf_dir, runner=runner, now=future
            )

    def test_missing_plan_metadata_is_rejected(self, paths, tf_dir):
        with pytest.raises(LifecycleError, match="no reviewed apply plan"):
            lifecycle.verify_saved_plan(
                RUN_TAG, stage="apply", paths=paths, tf_dir=tf_dir, runner=FakeRunner()
            )

    def test_other_runs_plan_is_rejected(self, paths, tf_dir):
        meta, runner = make_plan(paths, tf_dir)
        meta["run_tag"] = "another-run"
        lifecycle.write_private_json(paths.plan_meta_path("apply"), meta)
        with pytest.raises(LifecycleError, match="does not belong"):
            lifecycle.verify_saved_plan(
                RUN_TAG, stage="apply", paths=paths, tf_dir=tf_dir, runner=runner
            )


class TestApply:
    def test_apply_executes_exactly_the_saved_plan_no_auto_approve(self, paths, tf_dir):
        meta, runner = make_plan(paths, tf_dir)
        report = lifecycle.apply(
            RUN_TAG,
            apply_phrase(meta),
            paths=paths,
            tf_dir=tf_dir,
            runner=runner,
            environ={},
            fetch=_reconcile_fetch(),
            token="t",
        )
        assert report["reconciled"] is True
        apply_calls = [c for c in runner.calls if c[1] == "apply"]
        assert apply_calls == [
            ["terraform", "apply", "-input=false", str(paths.plan_path("apply"))]
        ]
        assert not any("-auto-approve" in c for c in runner.calls)
        # No fresh plan was generated at apply time.
        plan_calls = [c for c in runner.calls if c[1] == "plan"]
        assert len(plan_calls) == 1  # only the original save_plan

    def test_apply_requires_the_digest_bearing_approval_phrase(self, paths, tf_dir):
        meta, runner = make_plan(paths, tf_dir)
        with pytest.raises(ApprovalError):
            lifecycle.apply(
                RUN_TAG,
                f"I approve creating billable Akamai resources for run {RUN_TAG}",
                paths=paths,
                tf_dir=tf_dir,
                runner=runner,
                environ={},
            )
        assert not any(c[1] == "apply" for c in runner.calls)
        expected = apply_phrase(meta)
        assert meta["plan_sha256"] in expected and RUN_TAG in expected

    def test_apply_refuses_hosted_environments(self, paths, tf_dir):
        meta, runner = make_plan(paths, tf_dir)
        with pytest.raises(LifecycleError, match="hosted"):
            lifecycle.apply(
                RUN_TAG,
                apply_phrase(meta),
                paths=paths,
                tf_dir=tf_dir,
                runner=runner,
                environ={"CI": "true"},
            )

    def test_pending_record_is_written_before_the_attempt(self, paths, tf_dir):
        meta, _ = make_plan(paths, tf_dir)
        seen = {}

        class Recorder(FakeRunner):
            def __call__(self, argv, cwd, env):
                if argv[1] == "apply":
                    seen["pending_at_apply"] = paths.pending_path.is_file()
                return super().__call__(argv, cwd, env)

        recorder = Recorder()
        lifecycle.apply(
            RUN_TAG,
            apply_phrase(meta),
            paths=paths,
            tf_dir=tf_dir,
            runner=recorder,
            environ={},
            fetch=_reconcile_fetch(),
            token="t",
        )
        assert seen["pending_at_apply"] is True
        # Clean reconciliation clears the pending record.
        assert not paths.pending_path.is_file()

    def test_apply_with_provider_lookup_failure_writes_dirty_ledger_and_blocks_pilot(
        self, paths, tf_dir
    ):
        meta, runner = make_plan(paths, tf_dir)

        def failing_fetch(path, token=None):
            raise RuntimeError("provider api unavailable account-id-999")

        with pytest.raises(LifecycleError, match="NOT clean"):
            lifecycle.apply(
                RUN_TAG,
                apply_phrase(meta),
                paths=paths,
                tf_dir=tf_dir,
                runner=runner,
                environ={},
                fetch=failing_fetch,
                token="t",
            )
        assert paths.pending_path.is_file()
        ledger = json.loads(paths.ledger_path.read_text(encoding="utf-8"))
        assert ledger["reconciled"] is False
        assert ledger["reconciliation"]["provider_checked"] is False
        assert ledger["reconciliation"]["provider_lookup_failed"] is True
        assert "recovery" in ledger
        assert "account-id-999" not in json.dumps(ledger)
        assert lifecycle.pilot_blockers(ledger, pending=True)

    def test_failed_apply_still_reconciles_and_keeps_recovery_records(self, paths, tf_dir):
        meta, _ = make_plan(paths, tf_dir)
        runner = FakeRunner(apply_rc=1, show_state=state_json([INSTANCE_STATE]))
        with pytest.raises(LifecycleError, match="MAY be billing"):
            lifecycle.apply(
                RUN_TAG, apply_phrase(meta), paths=paths, tf_dir=tf_dir, runner=runner, environ={}
            )
        # The ledger recorded whatever partial state exists.
        ledger = json.loads(paths.ledger_path.read_text(encoding="utf-8"))
        assert len(ledger["resources"]) == 1

    def test_failed_show_after_apply_writes_a_recovery_record(self, paths, tf_dir):
        meta, _ = make_plan(paths, tf_dir)
        runner = FakeRunner(show_state_rc=1)
        with pytest.raises(LifecycleError, match="NOT clean"):
            lifecycle.apply(
                RUN_TAG, apply_phrase(meta), paths=paths, tf_dir=tf_dir, runner=runner, environ={}
            )
        ledger = json.loads(paths.ledger_path.read_text(encoding="utf-8"))
        assert ledger["state_readable"] is False
        assert ledger["reconciled"] is False
        assert "recovery" in ledger
        assert ledger["recovery"]["pending_operation"]["operation"] == "apply"
        # The pending record survives for the operator.
        assert paths.pending_path.is_file()


class TestReconcile:
    def test_untracked_provider_resource_blocks_reconciliation(self, paths, tf_dir):
        def fetch(path, token=None):
            if path.startswith("/linode/instances"):
                return {
                    "data": [
                        {
                            "id": 12345678,
                            "label": f"bwlab-{RUN_TAG}",
                            "tags": ["blackwell-lab", f"run:{RUN_TAG}"],
                        },
                        {
                            "id": 999,
                            "label": "mystery",
                            "tags": [f"run:{RUN_TAG}"],
                        },
                    ],
                    "pages": 1,
                }
            return {"data": [], "pages": 1}

        runner = FakeRunner(show_state=state_json([INSTANCE_STATE]))
        report = lifecycle.reconcile(
            RUN_TAG, paths=paths, tf_dir=tf_dir, runner=runner, fetch=fetch, token="t"
        )
        assert report["reconciled"] is False
        assert report["provider_checked"] is True
        assert report["untracked_billable_count"] == 1
        assert "blocked" in report["note"].lower() or "NOT CLEAN" in report["note"]

    def test_clean_reconciliation_requires_provider_check(self, paths, tf_dir):
        lifecycle.write_pending(paths, run_tag=RUN_TAG, operation="apply", plan_sha256="x")
        report = lifecycle.reconcile(
            RUN_TAG,
            paths=paths,
            tf_dir=tf_dir,
            runner=FakeRunner(),
            fetch=_reconcile_fetch(),
            token="t",
        )
        assert report["reconciled"] is True
        assert report["provider_checked"] is True
        assert not paths.pending_path.is_file()

    def test_reconciliation_without_provider_access_stays_dirty_and_retains_pending(
        self, paths, tf_dir
    ):
        lifecycle.write_pending(paths, run_tag=RUN_TAG, operation="apply", plan_sha256="x")
        report = lifecycle.reconcile(RUN_TAG, paths=paths, tf_dir=tf_dir, runner=FakeRunner())
        assert report["reconciled"] is False
        assert report["provider_checked"] is False
        assert paths.pending_path.is_file()
        ledger = json.loads(paths.ledger_path.read_text(encoding="utf-8"))
        assert ledger["reconciled"] is False
        assert "recovery" in ledger

    def test_provider_lookup_failure_writes_dirty_ledger_and_retains_pending(self, paths, tf_dir):
        lifecycle.write_pending(paths, run_tag=RUN_TAG, operation="apply", plan_sha256="x")

        def failing_fetch(path, token=None):
            raise RuntimeError("provider api unavailable account-id-999")

        report = lifecycle.reconcile(
            RUN_TAG,
            paths=paths,
            tf_dir=tf_dir,
            runner=FakeRunner(show_state=state_json()),
            fetch=failing_fetch,
            token="t",
        )
        assert report["reconciled"] is False
        assert report["provider_checked"] is False
        assert paths.pending_path.is_file()
        ledger = json.loads(paths.ledger_path.read_text(encoding="utf-8"))
        assert ledger["reconciled"] is False
        assert ledger["reconciliation"]["provider_lookup_failed"] is True
        assert "recovery" in ledger
        dumped = json.dumps(ledger)
        assert "account-id-999" not in dumped
        assert lifecycle.pilot_blockers(ledger, pending=True)

    def test_report_is_sanitized_no_provider_ids(self, paths, tf_dir):
        report = lifecycle.reconcile(
            RUN_TAG,
            paths=paths,
            tf_dir=tf_dir,
            runner=FakeRunner(),
            fetch=_reconcile_fetch(),
            token="t",
        )
        assert "12345678" not in json.dumps(report)

    def test_shared_numeric_ids_are_compared_as_typed_identity(self, paths, tf_dir):
        colliding = json.loads(json.dumps(INSTANCE_STATE))
        colliding["values"]["id"] = "555"

        def fetch(path, token=None):
            instance = {
                "id": 555,
                "label": f"bwlab-{RUN_TAG}",
                "region": "us-ord",
                "tags": ["blackwell-lab", f"run:{RUN_TAG}", "ttl-hours:6", "phase:3"],
            }
            if path.startswith("/linode/instances/"):
                return instance
            if path.startswith("/networking/firewalls/"):
                raise RuntimeError("firewall absent")
            if path.startswith("/linode/instances"):
                return {"data": [instance], "pages": 1}
            if path.startswith("/networking/firewalls"):
                return {"data": [], "pages": 1}
            return {"data": [], "pages": 1}

        report = lifecycle.reconcile(
            RUN_TAG,
            paths=paths,
            tf_dir=tf_dir,
            runner=FakeRunner(show_state=state_json([colliding, FIREWALL_STATE])),
            fetch=fetch,
            token="t",
        )
        assert report["reconciled"] is False
        assert report["missing_from_provider_count"] == 1
        ledger = json.loads(paths.ledger_path.read_text(encoding="utf-8"))
        assert ledger["reconciled"] is False

    def test_extra_instance_sharing_firewall_numeric_id_is_untracked(self, paths, tf_dir):
        extra = {"id": 555, "label": "mystery", "tags": [f"run:{RUN_TAG}"]}
        report = lifecycle.reconcile(
            RUN_TAG,
            paths=paths,
            tf_dir=tf_dir,
            runner=FakeRunner(),
            fetch=_reconcile_fetch(extra_instances=(extra,)),
            token="t",
        )
        assert report["reconciled"] is False
        assert report["untracked_billable_count"] == 1

    def test_region_mismatch_blocks_reconciliation(self, paths, tf_dir):
        report = lifecycle.reconcile(
            RUN_TAG,
            paths=paths,
            tf_dir=tf_dir,
            runner=FakeRunner(),
            fetch=_reconcile_fetch(instance_overrides={"region": "us-sea"}),
            token="t",
        )
        assert report["reconciled"] is False
        ledger = json.loads(paths.ledger_path.read_text(encoding="utf-8"))
        assert any("region" in item for item in ledger["reconciliation"]["identity_mismatches"])

    def test_label_mismatch_blocks_reconciliation(self, paths, tf_dir):
        report = lifecycle.reconcile(
            RUN_TAG,
            paths=paths,
            tf_dir=tf_dir,
            runner=FakeRunner(),
            fetch=_reconcile_fetch(instance_overrides={"label": "someone-elses-box"}),
            token="t",
        )
        assert report["reconciled"] is False

    def test_missing_authorized_firewall_blocks_reconciliation(self, paths, tf_dir):
        report = lifecycle.reconcile(
            RUN_TAG,
            paths=paths,
            tf_dir=tf_dir,
            runner=FakeRunner(show_state=state_json([INSTANCE_STATE])),
            fetch=_reconcile_fetch(),
            token="t",
        )
        assert report["reconciled"] is False
        assert report["untracked_billable_count"] == 1

    def test_duplicate_state_addresses_block_reconciliation(self, paths, tf_dir):
        report = lifecycle.reconcile(
            RUN_TAG,
            paths=paths,
            tf_dir=tf_dir,
            runner=FakeRunner(
                show_state=state_json([INSTANCE_STATE, INSTANCE_STATE, FIREWALL_STATE])
            ),
            fetch=_reconcile_fetch(),
            token="t",
        )
        assert report["reconciled"] is False
        ledger = json.loads(paths.ledger_path.read_text(encoding="utf-8"))
        assert ledger["reconciliation"]["duplicate_resources"]


def _reconcile_fetch(
    extra_instances=(), extra_firewalls=(), instance_overrides=None, firewall_overrides=None
):
    instance = {
        "id": 12345678,
        "label": f"bwlab-{RUN_TAG}",
        "region": "us-ord",
        "tags": ["blackwell-lab", f"run:{RUN_TAG}", "ttl-hours:6", "phase:3"],
    }
    firewall = {
        "id": 555,
        "label": f"bwlab-fw-{RUN_TAG}",
        "tags": ["blackwell-lab", f"run:{RUN_TAG}", "ttl-hours:6", "phase:3"],
    }
    instance.update(instance_overrides or {})
    firewall.update(firewall_overrides or {})

    def fetch(path, token=None):
        if path.startswith("/linode/instances/"):
            wanted = path.rsplit("/", 1)[-1]
            if wanted == str(instance["id"]):
                return instance
            for extra in extra_instances:
                if str(extra.get("id")) == wanted:
                    return extra
            raise RuntimeError("instance not found")
        if path.startswith("/networking/firewalls/"):
            wanted = path.rsplit("/", 1)[-1]
            if wanted == str(firewall["id"]):
                return firewall
            for extra in extra_firewalls:
                if str(extra.get("id")) == wanted:
                    return extra
            raise RuntimeError("firewall not found")
        if path.startswith("/linode/instances"):
            return {"data": [instance, *extra_instances], "pages": 1}
        if path.startswith("/networking/firewalls"):
            return {"data": [firewall, *extra_firewalls], "pages": 1}
        return {"data": [], "pages": 1}

    return fetch


def _provider_fetch(instance_overrides=None, firewall_overrides=None):
    instance = {
        "id": 12345678,
        "label": f"bwlab-{RUN_TAG}",
        "type": "rtxpro6000-x1",
        "region": "us-ord",
        "tags": ["blackwell-lab", f"run:{RUN_TAG}", "ttl-hours:6", "phase:3"],
    }
    firewall = {
        "id": 555,
        "label": f"bwlab-fw-{RUN_TAG}",
        "tags": ["blackwell-lab", f"run:{RUN_TAG}", "ttl-hours:6", "phase:3"],
    }
    instance.update(instance_overrides or {})
    firewall.update(firewall_overrides or {})

    def fetch(path, token=None):
        if path.startswith("/linode/instances/"):
            return instance
        if path.startswith("/networking/firewalls/"):
            return firewall
        return {"data": [], "pages": 1}

    return fetch


def make_ledger(paths, tf_dir, runner=None, show_state=None):
    runner = runner or FakeRunner(show_state=show_state or state_json())
    lifecycle.reconcile(
        RUN_TAG,
        paths=paths,
        tf_dir=tf_dir,
        runner=runner,
        fetch=_reconcile_fetch(),
        token="t",
    )
    return lifecycle.load_ledger(paths.ledger_path)


class TestTeardownIdentity:
    def test_identity_verification_passes_when_everything_matches(self, paths, tf_dir):
        ledger = make_ledger(paths, tf_dir)
        report = lifecycle.verify_teardown_identity(
            RUN_TAG,
            ledger,
            paths=paths,
            tf_dir=tf_dir,
            runner=FakeRunner(),
            fetch=_provider_fetch(),
            token="t",
        )
        assert report["verified"] is True
        assert report["provider_checked"] is True

    def test_wrong_run_tag_ledger_is_rejected(self, paths, tf_dir):
        ledger = make_ledger(paths, tf_dir)
        ledger["run_tag"] = "some-other-run"
        with pytest.raises(LifecycleError, match="another run"):
            lifecycle.verify_teardown_identity(
                RUN_TAG, ledger, paths=paths, tf_dir=tf_dir, runner=FakeRunner()
            )

    def test_stale_ledger_provider_id_fails_closed(self, paths, tf_dir):
        ledger = make_ledger(paths, tf_dir)
        ledger["resources"][0]["provider_id"] = "87654321"  # stale/wrong id
        with pytest.raises(LifecycleError, match="identity verification FAILED"):
            lifecycle.verify_teardown_identity(
                RUN_TAG,
                ledger,
                paths=paths,
                tf_dir=tf_dir,
                runner=FakeRunner(),
                fetch=_provider_fetch(),
                token="t",
            )

    def test_missing_run_tag_in_state_fails_closed(self, paths, tf_dir):
        ledger = make_ledger(paths, tf_dir)
        untagged = json.loads(json.dumps(INSTANCE_STATE))
        untagged["values"]["tags"] = ["blackwell-lab"]  # run tag gone
        runner = FakeRunner(show_state=state_json([untagged, FIREWALL_STATE]))
        with pytest.raises(LifecycleError, match="run tag missing"):
            lifecycle.verify_teardown_identity(
                RUN_TAG,
                ledger,
                paths=paths,
                tf_dir=tf_dir,
                runner=runner,
                fetch=_provider_fetch(),
                token="t",
            )

    def test_provider_api_mismatch_fails_closed(self, paths, tf_dir):
        ledger = make_ledger(paths, tf_dir)
        with pytest.raises(LifecycleError, match="API observation"):
            lifecycle.verify_teardown_identity(
                RUN_TAG,
                ledger,
                paths=paths,
                tf_dir=tf_dir,
                runner=FakeRunner(),
                fetch=_provider_fetch(instance_overrides={"label": "someone-elses-box"}),
                token="t",
            )

    def test_resource_absent_from_state_fails_closed(self, paths, tf_dir):
        ledger = make_ledger(paths, tf_dir)
        runner = FakeRunner(show_state=state_json([FIREWALL_STATE]))  # instance vanished
        with pytest.raises(LifecycleError, match="absent from the current state"):
            lifecycle.verify_teardown_identity(
                RUN_TAG,
                ledger,
                paths=paths,
                tf_dir=tf_dir,
                runner=runner,
                fetch=_provider_fetch(),
                token="t",
            )

    def test_unexpected_resource_type_fails_closed(self, paths, tf_dir):
        volume_state = {
            "address": "linode_volume.other",
            "type": "linode_volume",
            "name": "other",
            "values": {"id": "1", "label": "bwlab-x", "tags": ["blackwell-lab"]},
        }
        runner = FakeRunner(show_state=state_json([INSTANCE_STATE, FIREWALL_STATE, volume_state]))
        lifecycle.reconcile(
            RUN_TAG,
            paths=paths,
            tf_dir=tf_dir,
            runner=runner,
            fetch=_reconcile_fetch(),
            token="t",
        )
        ledger = lifecycle.load_ledger(paths.ledger_path)
        with pytest.raises(LifecycleError, match="not a resource this configuration"):
            lifecycle.verify_teardown_identity(
                RUN_TAG,
                ledger,
                paths=paths,
                tf_dir=tf_dir,
                runner=runner,
                fetch=_provider_fetch(),
                token="t",
            )


class TestDestroy:
    def _destroy_runner(self):
        return FakeRunner(
            show_plan=plan_json(
                [
                    ("linode_instance.gpu_baseline", ["delete"]),
                    ("linode_firewall.gpu_baseline", ["delete"]),
                ]
            )
        )

    def _prepared(self, paths, tf_dir):
        ledger = make_ledger(paths, tf_dir)
        runner = self._destroy_runner()
        meta = lifecycle.plan_destroy(
            RUN_TAG,
            ledger,
            paths=paths,
            tf_dir=tf_dir,
            runner=runner,
            fetch=_provider_fetch(),
            token="t",
        )
        return ledger, meta, runner

    def test_destroy_plan_must_match_ledger_addresses_exactly(self, paths, tf_dir):
        ledger = make_ledger(paths, tf_dir)
        runner = FakeRunner(show_plan=plan_json([("linode_instance.gpu_baseline", ["delete"])]))
        with pytest.raises(LifecycleError, match="exactly match the ledger"):
            lifecycle.plan_destroy(
                RUN_TAG,
                ledger,
                paths=paths,
                tf_dir=tf_dir,
                runner=runner,
                fetch=_provider_fetch(),
                token="t",
            )

    def test_destroy_stage_plan_with_create_fails_closed(self, paths, tf_dir):
        ledger = make_ledger(paths, tf_dir)
        runner = FakeRunner(
            show_plan=plan_json(
                [
                    ("linode_instance.gpu_baseline", ["delete"]),
                    ("linode_firewall.gpu_baseline", ["create"]),
                ]
            )
        )
        with pytest.raises(LifecycleError, match="non-delete"):
            lifecycle.plan_destroy(
                RUN_TAG,
                ledger,
                paths=paths,
                tf_dir=tf_dir,
                runner=runner,
                fetch=_provider_fetch(),
                token="t",
            )

    def test_destroy_requires_digest_bearing_approval(self, paths, tf_dir):
        ledger, _meta, runner = self._prepared(paths, tf_dir)
        with pytest.raises(ApprovalError):
            lifecycle.destroy(
                RUN_TAG,
                f"I approve deleting the exact recorded resources for run {RUN_TAG}",
                ledger,
                paths=paths,
                tf_dir=tf_dir,
                runner=runner,
                environ={},
                fetch=_provider_fetch(),
                token="t",
                probe=lambda path, token: "absent",
            )
        assert not any(c[1] == "apply" for c in runner.calls)

    def test_destroy_confirms_deletion_before_reporting_success(self, paths, tf_dir):
        ledger, meta, runner = self._prepared(paths, tf_dir)
        # Simulate Akamai's multi-minute deletion: present for 3 polls each.
        counts: dict[str, int] = {}

        def slow_probe(path, token):
            counts[path] = counts.get(path, 0) + 1
            return "absent" if counts[path] > 3 else "present"

        sleeps: list[float] = []
        timeline = iter(range(0, 100000, 10))
        report = lifecycle.destroy(
            RUN_TAG,
            destroy_phrase(meta),
            ledger,
            paths=paths,
            tf_dir=tf_dir,
            runner=runner,
            environ={},
            fetch=_provider_fetch(),
            token="t",
            probe=slow_probe,
            monotonic=lambda: float(next(timeline)),
            sleeper=sleeps.append,
        )
        assert report["destroyed"] is True
        assert report["deletion_confirmed"] is True
        assert len(sleeps) >= 3  # actually polled through the delay
        updated = json.loads(paths.ledger_path.read_text(encoding="utf-8"))
        assert updated["deletion_confirmed"] is True

    def test_confirmation_timeout_reports_billing_may_continue(self, paths, tf_dir):
        ledger, meta, runner = self._prepared(paths, tf_dir)
        timeline = iter(range(0, 10_000_000, 700))
        with pytest.raises(LifecycleError, match="BILLING MAY CONTINUE"):
            lifecycle.destroy(
                RUN_TAG,
                destroy_phrase(meta),
                ledger,
                paths=paths,
                tf_dir=tf_dir,
                runner=runner,
                environ={},
                fetch=_provider_fetch(),
                token="t",
                probe=lambda path, token: "present",
                monotonic=lambda: float(next(timeline)),
                sleeper=lambda s: None,
            )

    def test_destroy_without_token_makes_zero_terraform_calls(self, paths, tf_dir):
        ledger = make_ledger(paths, tf_dir)
        runner = FakeRunner()
        with pytest.raises(LifecycleError, match="provider verification is required"):
            lifecycle.destroy(
                RUN_TAG,
                "x",
                ledger,
                paths=paths,
                tf_dir=tf_dir,
                runner=runner,
                environ={},
                token=None,
            )
        assert not any(argv[1] in ("plan", "apply") for argv in runner.calls)

    def test_teardown_plan_without_token_makes_zero_terraform_calls(self, paths, tf_dir):
        ledger = make_ledger(paths, tf_dir)
        runner = FakeRunner()
        with pytest.raises(LifecycleError, match="provider verification is required"):
            lifecycle.plan_destroy(
                RUN_TAG,
                ledger,
                paths=paths,
                tf_dir=tf_dir,
                runner=runner,
                token=None,
            )
        assert not any(argv[1] == "plan" for argv in runner.calls)

    def test_destroy_refuses_hosted_environments(self, paths, tf_dir):
        ledger, meta, runner = self._prepared(paths, tf_dir)
        with pytest.raises(LifecycleError, match="hosted"):
            lifecycle.destroy(
                RUN_TAG,
                destroy_phrase(meta),
                ledger,
                paths=paths,
                tf_dir=tf_dir,
                runner=runner,
                environ={"GITHUB_ACTIONS": "true"},
                fetch=_provider_fetch(),
                token="t",
            )


class TestSessionRecord:
    def test_session_summary_computes_billable_window_and_cost(self, paths):
        lifecycle.record_session_event(paths, "apply_attempted", {})
        lifecycle.record_session_event(paths, "provisioned", {})
        lifecycle.record_session_event(paths, "pilot_started", {})
        lifecycle.record_session_event(paths, "pilot_completed", {})
        lifecycle.record_session_event(paths, "teardown_started", {})
        lifecycle.record_session_event(paths, "deletion_confirmed", {})
        summary = lifecycle.session_summary(paths, hourly_price_usd=3.5)
        assert summary["event_count"] == 6
        assert summary["observed_billable_s"] is not None
        assert summary["estimated_total_cost_usd"] is not None
        assert summary["deletion_confirmed"] is True
        assert set(summary["phases"]) == {
            "provisioning_s",
            "setup_s",
            "pilot_s",
            "teardown_s",
        }

    def test_summary_without_confirmed_deletion_reports_open_session(self, paths):
        lifecycle.record_session_event(paths, "provisioned", {})
        summary = lifecycle.session_summary(paths, hourly_price_usd=3.5)
        assert summary["observed_billable_s"] is None
        assert summary["estimated_total_cost_usd"] is None
        assert summary["deletion_confirmed"] is False

    def test_session_record_is_private(self, paths):
        lifecycle.record_session_event(paths, "provisioned", {})
        assert stat.S_IMODE(os.stat(paths.session_path).st_mode) == 0o600


class TestOrphanReport:
    def _fetch(self, instances=(), volumes=(), firewalls=()):
        def fetch(path, token=None):
            if path.startswith("/linode/instances"):
                return {"data": list(instances), "pages": 1}
            if path.startswith("/volumes"):
                return {"data": list(volumes), "pages": 1}
            if path.startswith("/networking/firewalls"):
                return {"data": list(firewalls), "pages": 1}
            raise AssertionError(path)

        return fetch

    def test_clean_when_nothing_project_tagged_exists(self):
        report = lifecycle.orphan_report("t", fetch=self._fetch())
        assert report["clean"] is True

    def test_project_tagged_firewall_is_reported(self):
        fetch = self._fetch(
            firewalls=[{"id": 555, "label": "bwlab-fw-x", "tags": ["blackwell-lab"]}]
        )
        report = lifecycle.orphan_report("t", fetch=fetch)
        assert report["clean"] is False
        assert report["findings"][0]["kind"] == "firewall"

    def test_untagged_gpu_instance_is_suspicious(self):
        fetch = self._fetch(
            instances=[{"id": 9, "label": "mystery", "type": "g2-rtxpro6000-x1", "tags": []}]
        )
        report = lifecycle.orphan_report("t", fetch=fetch)
        assert report["findings"][0]["suspicious_untagged_gpu"] is True

    def test_sweep_failure_never_echoes_payloads(self):
        def broken(path, token=None):
            raise RuntimeError("secret-account-id-123 in raw payload")

        with pytest.raises(LifecycleError) as excinfo:
            lifecycle.orphan_report("t", fetch=broken)
        assert "secret-account-id-123" not in str(excinfo.value)


class TestRunTagAndHostedGuards:
    def test_run_tag_validation(self):
        for bad in ("", "UPPER", "a b", "x", "a" * 60, None, 5):
            with pytest.raises(LifecycleError):
                lifecycle.validate_run_tag(bad)  # type: ignore[arg-type]
        assert lifecycle.validate_run_tag(RUN_TAG) == RUN_TAG

    def test_hosted_markers_refuse_billable_verbs(self):
        for marker in ("CI", "GITHUB_ACTIONS", "CURSOR_AGENT", "CLOUD_AGENT"):
            with pytest.raises(LifecycleError, match="hosted"):
                lifecycle.refuse_hosted_execution({marker: "1"})
        lifecycle.refuse_hosted_execution({})  # local: no exception
