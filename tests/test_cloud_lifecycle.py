"""Mocked lifecycle tests: approval gates, exact ledgers, no broad cleanup.

No terraform binary and no provider API is ever invoked: every test injects a
fake command runner or fetcher.
"""

from __future__ import annotations

import json

import pytest

from blackwell_lab.cloud.lifecycle import (
    APPLY_APPROVAL_TEMPLATE,
    DESTROY_APPROVAL_TEMPLATE,
    PROJECT_TAG,
    ApprovalError,
    CommandResult,
    LifecycleError,
    apply,
    build_ledger,
    destroy,
    load_ledger,
    orphan_report,
    plan,
    teardown_plan,
    validate_run_tag,
    write_ledger,
)

RUN_TAG = "p3-pilot-20260907a"
LOCAL_ENV: dict = {}  # no hosted markers

SHOW_JSON = {
    "values": {
        "root_module": {
            "resources": [
                {
                    "address": "linode_instance.gpu_baseline",
                    "type": "linode_instance",
                    "name": "gpu_baseline",
                    "values": {
                        "id": "12345678",
                        "label": f"bwlab-{RUN_TAG}",
                        "region": "us-fake-1",
                        "tags": [PROJECT_TAG, f"run:{RUN_TAG}"],
                    },
                }
            ]
        }
    }
}


class RecordingRunner:
    def __init__(self, results: list[CommandResult]):
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv, cwd):
        self.calls.append(argv)
        return self.results.pop(0)


class TestRunTag:
    @pytest.mark.parametrize("tag", ["p3-pilot-20260907a", "run-0001", "abcd"])
    def test_valid_tags(self, tag):
        assert validate_run_tag(tag) == tag

    @pytest.mark.parametrize("tag", ["", "ab", "UPPER", "has space", "run_tag", "a" * 60, None])
    def test_invalid_tags_are_rejected(self, tag):
        with pytest.raises(LifecycleError):
            validate_run_tag(tag)


class TestPlan:
    def test_plan_is_the_default_safe_verb(self, tmp_path):
        runner = RecordingRunner([CommandResult(0, "Plan: 1 to add")])
        result = plan(RUN_TAG, tf_dir=tmp_path, runner=runner)
        assert result.returncode == 0
        argv = runner.calls[0]
        assert argv[:2] == ["terraform", "plan"]
        assert f"-var=run_tag={RUN_TAG}" in argv
        assert "-auto-approve" not in argv


class TestApply:
    def test_wrong_phrase_executes_nothing(self, tmp_path):
        runner = RecordingRunner([])
        with pytest.raises(ApprovalError, match="nothing was executed"):
            apply(
                RUN_TAG,
                "yes please",
                tf_dir=tmp_path,
                runner=runner,
                ledger_dir=tmp_path / "ledgers",
                environ=LOCAL_ENV,
            )
        assert runner.calls == []

    @pytest.mark.parametrize("marker", ["CI", "GITHUB_ACTIONS", "CURSOR_AGENT"])
    def test_hosted_environments_are_refused(self, tmp_path, marker):
        runner = RecordingRunner([])
        with pytest.raises(LifecycleError, match="hosted"):
            apply(
                RUN_TAG,
                APPLY_APPROVAL_TEMPLATE.format(run_tag=RUN_TAG),
                tf_dir=tmp_path,
                runner=runner,
                ledger_dir=tmp_path / "ledgers",
                environ={marker: "1"},
            )
        assert runner.calls == []

    def test_approved_apply_records_the_exact_ledger(self, tmp_path):
        runner = RecordingRunner(
            [CommandResult(0, "applied"), CommandResult(0, json.dumps(SHOW_JSON))]
        )
        ledger_path = apply(
            RUN_TAG,
            APPLY_APPROVAL_TEMPLATE.format(run_tag=RUN_TAG),
            tf_dir=tmp_path,
            runner=runner,
            ledger_dir=tmp_path / "ledgers",
            environ=LOCAL_ENV,
        )
        assert runner.calls[0][:2] == ["terraform", "apply"]
        assert runner.calls[1][:3] == ["terraform", "show", "-json"]
        ledger = load_ledger(ledger_path)
        assert ledger["run_tag"] == RUN_TAG
        assert ledger["resources"][0]["provider_id"] == "12345678"
        assert ledger["resources"][0]["address"] == "linode_instance.gpu_baseline"

    def test_failed_apply_raises_with_recovery_guidance(self, tmp_path):
        runner = RecordingRunner([CommandResult(1, "", "boom")])
        with pytest.raises(LifecycleError, match="partially created"):
            apply(
                RUN_TAG,
                APPLY_APPROVAL_TEMPLATE.format(run_tag=RUN_TAG),
                tf_dir=tmp_path,
                runner=runner,
                ledger_dir=tmp_path / "ledgers",
                environ=LOCAL_ENV,
            )

    def test_empty_show_output_refuses_an_empty_ledger(self):
        with pytest.raises(LifecycleError, match="empty"):
            build_ledger(RUN_TAG, {"values": {"root_module": {"resources": []}}})


class TestTeardown:
    def _ledger(self, tmp_path):
        return load_ledger(write_ledger(build_ledger(RUN_TAG, SHOW_JSON), tmp_path / "ledgers"))

    def test_teardown_plan_targets_exactly_the_ledger_resources(self, tmp_path):
        plan_doc = teardown_plan(self._ledger(tmp_path))
        assert plan_doc["resource_count"] == 1
        assert plan_doc["targets"] == ["linode_instance.gpu_baseline"]
        assert plan_doc["destroy_arguments"] == ["-target=linode_instance.gpu_baseline"]
        assert "ONLY" in plan_doc["note"]

    def test_destroy_requires_its_own_distinct_phrase(self, tmp_path):
        ledger = self._ledger(tmp_path)
        runner = RecordingRunner([])
        apply_phrase = APPLY_APPROVAL_TEMPLATE.format(run_tag=RUN_TAG)
        with pytest.raises(ApprovalError):
            destroy(
                RUN_TAG, apply_phrase, ledger, tf_dir=tmp_path, runner=runner, environ=LOCAL_ENV
            )
        assert runner.calls == []

    def test_destroy_passes_only_ledger_targets(self, tmp_path):
        ledger = self._ledger(tmp_path)
        runner = RecordingRunner([CommandResult(0, "destroyed")])
        destroy(
            RUN_TAG,
            DESTROY_APPROVAL_TEMPLATE.format(run_tag=RUN_TAG),
            ledger,
            tf_dir=tmp_path,
            runner=runner,
            environ=LOCAL_ENV,
        )
        argv = runner.calls[0]
        assert argv[:2] == ["terraform", "destroy"]
        assert "-target=linode_instance.gpu_baseline" in argv
        # No broad cleanup: every target is explicit; no bare destroy.
        assert sum(1 for a in argv if a.startswith("-target=")) == 1

    def test_destroy_refuses_a_mismatched_ledger(self, tmp_path):
        ledger = self._ledger(tmp_path)
        ledger["run_tag"] = "other-run"
        with pytest.raises(LifecycleError, match="another run"):
            destroy(
                RUN_TAG,
                DESTROY_APPROVAL_TEMPLATE.format(run_tag=RUN_TAG),
                ledger,
                tf_dir=tmp_path,
                runner=RecordingRunner([]),
                environ=LOCAL_ENV,
            )

    def test_failed_destroy_warns_that_billing_continues(self, tmp_path):
        ledger = self._ledger(tmp_path)
        runner = RecordingRunner([CommandResult(1, "", "boom")])
        with pytest.raises(LifecycleError, match="still bill"):
            destroy(
                RUN_TAG,
                DESTROY_APPROVAL_TEMPLATE.format(run_tag=RUN_TAG),
                ledger,
                tf_dir=tmp_path,
                runner=runner,
                environ=LOCAL_ENV,
            )


class TestOrphanReport:
    INSTANCES: dict = {  # noqa: RUF012 - read-only fixture data
        "data": [
            {
                "id": 12345678,
                "label": f"bwlab-{RUN_TAG}",
                "region": "us-fake-1",
                "type": "g9-fake-gpu-plan",
                "tags": [PROJECT_TAG, f"run:{RUN_TAG}"],
            },
            {
                "id": 999,
                "label": "unrelated-web-server",
                "region": "us-fake-1",
                "type": "g6-standard-2",
                "tags": ["someone-elses-project"],
            },
            {
                "id": 555,
                "label": "mystery-gpu",
                "region": "us-fake-1",
                "type": "g9-fake-gpu-plan",
                "tags": [],
            },
        ]
    }
    VOLUMES: dict = {  # noqa: RUF012 - read-only fixture data
        "data": [
            {"id": 777, "label": "bwlab-scratch", "region": "us-fake-1", "tags": [PROJECT_TAG]},
            {"id": 888, "label": "other-volume", "region": "us-fake-1", "tags": []},
        ]
    }

    def fetch(self, path, token=None):
        if path.startswith("/linode/instances"):
            return self.INSTANCES
        if path.startswith("/volumes"):
            return self.VOLUMES
        raise AssertionError(f"unexpected path: {path}")

    def test_report_classifies_findings_against_the_ledger(self, tmp_path):
        ledger = build_ledger(RUN_TAG, SHOW_JSON)
        report = orphan_report("tok", fetch=self.fetch, ledger=ledger)
        by_label = {f["label"]: f for f in report["findings"]}
        # The ledger-recorded instance is found and matched.
        assert by_label[f"bwlab-{RUN_TAG}"]["in_ledger"] is True
        # An untagged GPU instance is flagged as suspicious.
        assert by_label["mystery-gpu"]["suspicious_untagged_gpu"] is True
        # A project-tagged volume outside the ledger is an unrecorded finding.
        assert by_label["bwlab-scratch"]["in_ledger"] is False
        assert not report["clean"]
        # Unrelated resources are never touched or listed.
        assert "unrelated-web-server" not in by_label
        assert "other-volume" not in by_label

    def test_clean_report_after_full_teardown(self):
        empty = {"data": []}
        report = orphan_report("tok", fetch=lambda p, t=None: empty)
        assert report["clean"] is True
        assert report["findings"] == []

    def test_sweep_failure_is_sanitized(self):
        def boom(path, token=None):
            raise OSError("secret-account-id-in-error")

        with pytest.raises(LifecycleError) as excinfo:
            orphan_report("tok", fetch=boom)
        assert "secret-account-id" not in str(excinfo.value)
