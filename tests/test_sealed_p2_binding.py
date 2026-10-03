"""Adversarial offline tests for the sealed-set-to-P2 execution adapter (D-0024).

Every custody tree here is a synthetic external fixture written to a pytest
``tmp_path`` with the production custody writer. Task bodies are
re-encodings of the public synthetic scenario catalog with invented
instance surfaces; no real sealed task, accepted answer, private result,
provider, credential, or inference endpoint is involved. The suite proves:

- P2 development and holdout load exactly twenty tasks from the bound
  stage, in deterministic task-id order, without consulting the catalog;
- the other stage's private payloads are never opened (``io.open``,
  ``builtins.open`` and ``os.open`` are instrumented);
- every binding, path, mode, manifest, receipt, digest, identity, stage,
  count, and payload failure stops before a model client is constructed,
  before live provenance is contacted, and with no content or path leak;
- ``--validate-only`` verifies the config and the selected stage with zero
  client construction;
- run/result verification rejects mismatched sealed provenance;
- C1, C2, P1, freeze, the P2 prompt, evaluator 3.1.0, thresholds, and the
  accepted answers are unchanged.
"""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import shutil
from pathlib import Path

import jsonschema
import pytest
from fakes import FakeClock
from sealed_fixtures import (
    binding_for,
    placeholder_binding,
    stage_blob_paths,
    synthetic_stage,
    tree_snapshot,
    write_synthetic_custody,
)
from test_evidence_grounding import (
    _HOST,
    COMMIT,
    FROZEN_ACCEPTED_ANSWERS_SHA256,
    FROZEN_C1_IDENTITY_SHA256,
    FROZEN_C2_IDENTITY_SHA256,
    FROZEN_P1_IDENTITY_SHA256,
    FROZEN_SYSTEM_PROMPT_V241_SHA256,
    RUN_TAG,
    _accepted_answers_payload,
    _FakeSampler,
    _observed,
    _qual_argv,
    _ready_ledger,
    _real_spec,
    _UsageMockClient,
    qualification_config_dict,
)

from blackwell_lab.cloud import cli, lifecycle, provenance, realbench
from blackwell_lab.cloud.cli import main
from blackwell_lab.cloud.qualification import (
    DEVELOPMENT_QUALITY_FLOOR,
    DEVELOPMENT_TASKS,
    HOLDOUT_QUALITY_FLOOR,
    HOLDOUT_TASKS,
    QualificationError,
    candidate_identity_digest,
    sanitized_receipt,
    validate_authorized_qualification_config,
)
from blackwell_lab.cloud.realbench import RealRunSpec, run_real_cell
from blackwell_lab.cloud.sealed_binding import (
    BINDING_FIELDS,
    SealedSetBinding,
    SealedSetError,
    SealedStageTasks,
    binding_from_config,
    load_sealed_stage,
    require_manifest_provenance,
    require_sealed_stage_evidence,
    requires_sealed_set,
    resolve_sealed_stage,
)
from blackwell_lab.cloud.sealed_payload import (
    PAYLOAD_SCHEMA_VERSION,
    SealedPayloadError,
    decode_sealed_task,
    encode_sealed_task,
    load_payload_schema,
    scenario_document,
)
from blackwell_lab.schemas import validate_run_manifest
from blackwell_lab.sealed_sets.model import OpaqueTask
from blackwell_lab.workload.agent import SYSTEM_PROMPT_V241
from blackwell_lab.workload.clock import SYSTEM_CLOCK
from blackwell_lab.workload.evaluator import EVALUATOR_VERSION, QUALITY_THRESHOLD
from blackwell_lab.workload.model_client import GenerationSettings
from blackwell_lab.workload.openai_client import OpenAICompatibleClient
from blackwell_lab.workload.scenarios import catalog
from blackwell_lab.workload.validation import (
    ConfigError,
    SemanticValidationError,
    validate_result_semantics,
)

REPO = Path(__file__).resolve().parents[1]
STAGES = ("development", "holdout")
OTHER = {"development": "holdout", "holdout": "development"}


# --- fixtures and harness -------------------------------------------------------


@pytest.fixture
def custody(tmp_path):
    root = tmp_path / "custody"
    manifest = write_synthetic_custody(root, commit=COMMIT)
    return root, manifest


def _binding(root, manifest, stage) -> SealedSetBinding:
    return SealedSetBinding.from_config(binding_for(root, manifest, stage), stage=stage)


def _load(root, manifest, stage, **overrides) -> SealedStageTasks:
    kwargs = dict(custody_dir=root, repo=REPO, canonical_commit=COMMIT)
    kwargs.update(overrides)
    return load_sealed_stage(_binding(root, manifest, stage), **kwargs)


def _expect(reason: str):
    return pytest.raises(SealedSetError, match=rf"^sealed-set: {reason}$")


class _OpenRecorder:
    """Records every path opened through io.open, builtins.open, or os.open."""

    def __init__(self, monkeypatch):
        self.paths: list[Path] = []
        real_io_open = io.open
        real_os_open = os.open

        def io_open(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                self.paths.append(Path(os.fsdecode(file)))
            return real_io_open(file, *args, **kwargs)

        def os_open(path, flags, mode=0o777, *, dir_fd=None):
            if isinstance(path, (str, bytes, os.PathLike)):
                self.paths.append(Path(os.fsdecode(path)))
            return real_os_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(io, "open", io_open)
        monkeypatch.setattr(builtins, "open", io_open)
        monkeypatch.setattr(os, "open", os_open)

    def under(self, directory: Path) -> set[Path]:
        return {p for p in self.paths if directory == p or directory in p.parents}


class _Harness:
    """CLI harness: ready ledger, frozen git head, spies for every live boundary."""

    def __init__(self, tmp_path, monkeypatch, *, commit=COMMIT):
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.external = tmp_path / "external"
        monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
        monkeypatch.setattr(cli, "_git_head", lambda: commit)
        monkeypatch.setattr(cli, "_tree_clean", lambda: True)
        monkeypatch.setenv("LAB_RESULTS_DIR", str(self.external))
        paths = lifecycle.lifecycle_paths(self.external, RUN_TAG)
        lifecycle.write_private_json(paths.ledger_path, _ready_ledger())
        self.constructed: list[object] = []
        self.provenance_calls: list[dict] = []
        self.runs: list[dict] = []
        harness = self

        class SpyClient(OpenAICompatibleClient):
            def __init__(self, *args, **kwargs):
                harness.constructed.append(self)
                super().__init__(*args, **kwargs)

        from blackwell_lab.workload import openai_client

        monkeypatch.setattr(openai_client, "OpenAICompatibleClient", SpyClient)

        def fake_provenance(**kwargs):
            harness.provenance_calls.append(kwargs)
            return _observed(full_host=True)

        monkeypatch.setattr(provenance, "verify_live_provenance", fake_provenance)

        def fake_run(spec, client, **kwargs):
            harness.runs.append({"spec": spec, "client": client, **kwargs})
            raise AssertionError("run_real_cell must not be reached in this test")

        self.real_run = realbench.run_real_cell
        monkeypatch.setattr(realbench, "run_real_cell", fake_run)

    def go_live(self):
        """Let the real run_real_cell execute against an offline mock endpoint.

        The CLI still constructs ``OpenAICompatibleClient`` (a recording
        subclass); every turn is answered by the deterministic mock with
        usage counts and telemetry comes from the fake sampler, so no
        network, credentials, or inference are involved.
        """
        from blackwell_lab.cloud import telemetry
        from blackwell_lab.workload import openai_client

        harness = self

        class OfflineClient(OpenAICompatibleClient):
            def __init__(self, *args, **kwargs):
                harness.constructed.append(self)
                super().__init__(*args, **kwargs)
                self._mock = _UsageMockClient()

            def stream_turn(self, messages, settings, *, deadline=None, clock=SYSTEM_CLOCK):
                yield from self._mock.stream_turn(
                    messages, settings, deadline=deadline, clock=clock
                )

        self.monkeypatch.setattr(openai_client, "OpenAICompatibleClient", OfflineClient)
        self.monkeypatch.setattr(telemetry, "GpuSamplerThread", _FakeSampler)
        self.monkeypatch.setattr(realbench, "run_real_cell", self.real_run)
        return OfflineClient

    def run(self, config: dict, *, stage="development", candidate="P2", custody_dir=None, extra=()):
        path = self.tmp_path / f"{candidate}-{stage}.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        argv = _qual_argv(path, candidate=candidate, stage=stage, custody_dir=custody_dir)
        return main([*argv, *extra])

    def assert_nothing_live(self):
        assert self.constructed == []
        assert self.provenance_calls == []
        assert self.runs == []


def _clean_stream(text: str, root: Path, tasks) -> None:
    assert str(root) not in text
    assert str(root.parent) not in text
    for task in tasks:
        assert task.task_id not in text
    for scenario in catalog().values():
        assert scenario.title not in text
        assert scenario.description[:40] not in text
        for answer in (*scenario.accepted_diagnoses, *scenario.accepted_remediations):
            assert answer not in text


def _tamper_file(path: Path, mutate) -> None:
    mode = path.stat().st_mode & 0o777
    data = path.read_bytes()
    os.chmod(path, 0o600)
    path.write_bytes(mutate(data))
    os.chmod(path, mode)


def _rewrite_json(path: Path, mutate) -> None:
    def _apply(data: bytes) -> bytes:
        document = json.loads(data)
        mutate(document)
        return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()

    _tamper_file(path, _apply)


# --- payload contract -----------------------------------------------------------------


class TestPayloadContract:
    def test_schema_is_valid_and_every_catalog_scenario_round_trips(self):
        schema = load_payload_schema()
        jsonschema.validators.validator_for(schema).check_schema(schema)
        for scenario_id, scenario in catalog().items():
            payload = encode_sealed_task(
                task_id="syn-roundtrip-task",
                scenario=scenario,
                instance_seed=7,
                tracking_id="SYN-0000abcd",
                reported_minute=12,
            )
            jsonschema.validate(json.loads(payload), schema)
            task = decode_sealed_task(payload, expected_task_id="syn-roundtrip-task")
            assert task.scenario == scenario
            assert task.instance.template_id == scenario_id
            assert task.instance.instance_id == "syn-roundtrip-task"
            assert task.instance.tracking_id == "SYN-0000abcd"
            assert task.instance.reported_minute == 12

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.__setitem__("extra", 1),
            lambda d: d.__setitem__("kind", "qualification-task"),
            lambda d: d.__setitem__("payload_schema_version", "1.1.0"),
            lambda d: d.__setitem__("task_id", "syn-other-task-id"),
            lambda d: d.pop("instance"),
            lambda d: d["instance"].__setitem__("instance_seed", True),
            lambda d: d["instance"].__setitem__("instance_seed", -1),
            lambda d: d["instance"].__setitem__("reported_minute", 99999),
            lambda d: d["instance"].__setitem__("tracking_id", "has space"),
            lambda d: d["instance"].__setitem__("unexpected", "x"),
            lambda d: d["scenario"].__setitem__("accepted_answer", "leak"),
            lambda d: d["scenario"].pop("evidence_predicates"),
            lambda d: d["scenario"].__setitem__("evidence_predicates", []),
            lambda d: d["scenario"].__setitem__("accepted_diagnoses", []),
            lambda d: d["scenario"].__setitem__(
                "distractor_diagnoses", list(d["scenario"]["accepted_diagnoses"])
            ),
            lambda d: d["scenario"].__setitem__("root_cause_id", "not-a-candidate"),
            lambda d: d["scenario"].__setitem__("incident_class", ""),
            lambda d: d["scenario"]["metrics"].__setitem__("bad", {"unit": "ms"}),
            lambda d: d["scenario"]["logs"].append({"message": "orphan"}),
            lambda d: d["scenario"]["evidence_predicates"][0]["alternatives"][0].__setitem__(
                "tool", "exec_shell"
            ),
            lambda d: d["scenario"]["evidence_predicates"][0]["alternatives"][0].__setitem__(
                "result", {"kind": "always_true"}
            ),
            lambda d: d["scenario"]["evidence_predicates"][0]["alternatives"][0].__setitem__(
                "result", {"kind": "metric_available", "metric": "x", "bonus": "y"}
            ),
            lambda d: d["scenario"]["reference_tool_sequence"].append(
                {"tool": "rm", "arguments": {}}
            ),
        ],
    )
    def test_malformed_or_unexpected_fields_are_rejected(self, mutate):
        scenario = next(iter(catalog().values()))
        document = json.loads(
            encode_sealed_task(
                task_id="syn-malformed-task",
                scenario=scenario,
                instance_seed=1,
                tracking_id="SYN-1",
                reported_minute=1,
            )
        )
        mutate(document)
        payload = json.dumps(document).encode()
        with pytest.raises(SealedPayloadError) as caught:
            decode_sealed_task(payload, expected_task_id="syn-malformed-task")
        assert str(caught.value) in {"malformed-task", "payload-version-mismatch"}

    def test_non_json_shapes_are_rejected(self):
        for payload in (
            b"",
            b"[]",
            b"null",
            b"\xff\xfe",
            b'{"a": 1, "a": 2}',
            b'{"payload_schema_version": NaN}',
            b"x" * (1_048_577),
        ):
            with pytest.raises(SealedPayloadError):
                decode_sealed_task(payload, expected_task_id="syn-shape-task")
        with pytest.raises(SealedPayloadError):
            decode_sealed_task(b"{}", expected_task_id="BAD ID")

    def test_scenario_document_has_json_native_pairs(self):
        document = scenario_document(next(iter(catalog().values())))
        for predicate in document["evidence_predicates"]:
            for alternative in predicate["alternatives"]:
                assert all(isinstance(pair, list) for pair in alternative["argument_contains"])


# --- config binding -------------------------------------------------------------------


class TestConfigBinding:
    def test_requires_sealed_set_only_for_p2_development_and_holdout(self):
        for candidate in ("C1", "C2", "P1", "P2"):
            for stage in ("development", "holdout", "freeze"):
                expected = candidate == "P2" and stage in STAGES
                assert requires_sealed_set(candidate, stage) is expected

    def test_binding_fields_and_portability(self, custody):
        root, manifest = custody
        section = binding_for(root, manifest, "development")
        assert set(section) == set(BINDING_FIELDS)
        assert set(BINDING_FIELDS) >= {
            "schema_version",
            "custody_manifest_sha256",
            "custody_controller_digest",
            "import_request_digest",
            "set_identity",
            "stage",
            "stage_aggregate_digest",
            "task_count",
        }
        blob = json.dumps(section)
        assert str(root) not in blob and "/" not in blob.replace("sha256:", "")
        binding = SealedSetBinding.from_config(section, stage="development")
        assert binding.provenance() == section
        assert binding.task_count == 20

    @pytest.mark.parametrize("stage", STAGES)
    def test_p2_config_validates_with_binding_and_fails_without(self, custody, stage):
        root, manifest = custody
        section = binding_for(root, manifest, stage)
        config = qualification_config_dict("P2", stage, sealed_set=section)
        validate_authorized_qualification_config(config, candidate_id="P2", stage=stage)
        missing = {k: v for k, v in config.items() if k != "sealed_set"}
        with _expect("binding-missing"):
            validate_authorized_qualification_config(missing, candidate_id="P2", stage=stage)
        swapped = dict(config, sealed_set=binding_for(root, manifest, OTHER[stage]))
        with _expect("binding-stage-mismatch"):
            validate_authorized_qualification_config(swapped, candidate_id="P2", stage=stage)

    @pytest.mark.parametrize(
        ("mutate", "reason"),
        [
            (lambda b: b.pop("set_identity"), "binding-malformed"),
            (lambda b: b.__setitem__("custody_dir", "/anywhere"), "binding-malformed"),
            (lambda b: b.__setitem__("task_count", 19), "binding-task-count"),
            (lambda b: b.__setitem__("task_count", True), "binding-malformed"),
            (lambda b: b.__setitem__("task_count", "20"), "binding-malformed"),
            (lambda b: b.__setitem__("schema_version", "2.0.0"), "binding-version-unsupported"),
            (
                lambda b: b.__setitem__("payload_schema_version", "0.9.0"),
                "payload-version-unsupported",
            ),
            (
                lambda b: b.__setitem__("custody_manifest_sha256", "sha256:" + "0" * 64),
                "binding-malformed",
            ),
            (lambda b: b.__setitem__("custody_controller_digest", "0" * 64), "binding-malformed"),
            (lambda b: b.__setitem__("stage_aggregate_digest", ""), "binding-malformed"),
            (lambda b: b.__setitem__("stage", "freeze"), "binding-malformed"),
            (lambda b: b.__setitem__("stage", "holdout"), "binding-stage-mismatch"),
        ],
    )
    def test_malformed_bindings_fail_closed(self, mutate, reason):
        section = placeholder_binding("development")
        mutate(section)
        with _expect(reason):
            SealedSetBinding.from_config(section, stage="development")
        with _expect(reason):
            validate_authorized_qualification_config(
                qualification_config_dict("P2", "development", sealed_set=section),
                candidate_id="P2",
                stage="development",
            )
        for bad in (None, "text", 7, []):
            with pytest.raises(SealedSetError):
                SealedSetBinding.from_config(bad, stage="development")

    def test_catalog_cells_refuse_the_binding_and_are_unchanged(self):
        for candidate, stage in (
            ("C1", "development"),
            ("C2", "holdout"),
            ("P1", "development"),
            ("P2", "freeze"),
        ):
            config = qualification_config_dict(candidate, stage)
            assert "sealed_set" not in config
            validate_authorized_qualification_config(config, candidate_id=candidate, stage=stage)
            assert binding_from_config(config, candidate_id=candidate, stage=stage) is None
            with _expect("binding-not-applicable"):
                validate_authorized_qualification_config(
                    dict(
                        config,
                        sealed_set=placeholder_binding(stage if stage in STAGES else "development"),
                    ),
                    candidate_id=candidate,
                    stage=stage,
                )
            with _expect("custody-dir-not-applicable"):
                resolve_sealed_stage(
                    config, candidate_id=candidate, stage=stage, custody_dir="/elsewhere", repo=REPO
                )
        assert candidate_identity_digest("C1") == FROZEN_C1_IDENTITY_SHA256
        assert candidate_identity_digest("C2") == FROZEN_C2_IDENTITY_SHA256
        assert candidate_identity_digest("P1") == FROZEN_P1_IDENTITY_SHA256

    def test_receipt_requires_provenance_exactly_for_sealed_cells(self, custody):
        root, manifest = custody
        binding = _binding(root, manifest, "holdout")
        receipt = sanitized_receipt(
            run_label="qual-a-p2-holdout",
            candidate_id="P2",
            stage="holdout",
            config_sha256="abc",
            identity_digest="def",
            gates={"continue": True, "stopped": False},
            files=(),
            stopped=False,
            sealed_set=binding,
        )
        assert receipt["sealed_set"] == binding.provenance()
        with pytest.raises(QualificationError):
            sanitized_receipt(
                run_label="qual-a-p2-holdout",
                candidate_id="P2",
                stage="holdout",
                config_sha256="abc",
                identity_digest="def",
                gates={},
                files=(),
                stopped=False,
            )
        with pytest.raises(QualificationError):
            sanitized_receipt(
                run_label="qual-a-c1-development",
                candidate_id="C1",
                stage="development",
                config_sha256="abc",
                identity_digest="def",
                gates={},
                files=(),
                stopped=False,
                sealed_set=binding,
            )
        assert "sealed_set" not in sanitized_receipt(
            run_label="qual-a-c1-development",
            candidate_id="C1",
            stage="development",
            config_sha256="abc",
            identity_digest="def",
            gates={},
            files=(),
            stopped=False,
        )


# --- stage-specific loader --------------------------------------------------------------


class TestStageLoader:
    @pytest.mark.parametrize("stage", STAGES)
    def test_loads_exactly_twenty_tasks_in_task_id_order(self, custody, stage):
        root, manifest = custody
        loaded = _load(root, manifest, stage)
        assert len(loaded.tasks) == 20
        assert list(loaded.task_ids) == sorted(loaded.task_ids)
        assert all(task_id.startswith(f"syn-{stage[:3]}-") for task_id in loaded.task_ids)
        assert len(set(loaded.task_ids)) == 20
        assert len(loaded.instances) == 20
        assert set(loaded.scenario_ids) <= set(catalog())
        assert len(loaded.scenario_ids) == 10
        again = _load(root, manifest, stage)
        assert again.task_ids == loaded.task_ids
        assert [t.instance for t in again.tasks] == [t.instance for t in loaded.tasks]

    @pytest.mark.parametrize("stage", STAGES)
    def test_selected_stage_never_opens_the_other_stage(
        self, tmp_path, monkeypatch, custody, stage
    ):
        root, manifest = custody
        selected = stage_blob_paths(root, stage)
        other = stage_blob_paths(root, OTHER[stage])
        assert len(selected) == len(other) == 20 and not selected & other
        recorder = _OpenRecorder(monkeypatch)
        observed: list[Path] = []
        loaded = _load(root, manifest, stage, opened=observed.append)
        assert len(loaded.tasks) == 20
        opened_blobs = recorder.under(root / "private" / "blobs")
        assert opened_blobs == selected
        assert not opened_blobs & other
        assert set(observed) == selected

    def test_order_is_independent_of_import_order(self, tmp_path):
        development = synthetic_stage("development")
        holdout = synthetic_stage("holdout")
        a_root, b_root = tmp_path / "a", tmp_path / "b"
        a_manifest = write_synthetic_custody(
            a_root, commit=COMMIT, development=development, holdout=holdout
        )
        b_manifest = write_synthetic_custody(
            b_root, commit=COMMIT, development=development[::-1], holdout=holdout[::-1]
        )
        assert a_manifest["set_identity"] != b_manifest["set_identity"]
        assert (
            a_manifest["development_aggregate_digest"] == b_manifest["development_aggregate_digest"]
        )
        a = _load(a_root, a_manifest, "development")
        b = _load(b_root, b_manifest, "development")
        assert a.task_ids == b.task_ids == tuple(sorted(a.task_ids))

    def test_missing_relative_repo_and_symlink_paths_fail(self, tmp_path, custody):
        root, manifest = custody
        binding = _binding(root, manifest, "development")
        with _expect("custody-dir-required"):
            resolve_sealed_stage(
                qualification_config_dict("P2", "development", sealed_set=binding.provenance()),
                candidate_id="P2",
                stage="development",
                custody_dir=None,
                repo=REPO,
            )
        with _expect("relative-path"):
            load_sealed_stage(binding, custody_dir="custody", repo=REPO, canonical_commit=COMMIT)
        with _expect("relative-path"):
            load_sealed_stage(
                binding, custody_dir=" " + str(root), repo=REPO, canonical_commit=COMMIT
            )
        with _expect("repository-path"):
            load_sealed_stage(
                binding, custody_dir=str(REPO / "custody"), repo=REPO, canonical_commit=COMMIT
            )
        nested_repo = tmp_path / "other-repo"
        (nested_repo / ".git").mkdir(parents=True)
        with _expect("repository-path"):
            load_sealed_stage(
                binding,
                custody_dir=str(nested_repo / "custody"),
                repo=REPO,
                canonical_commit=COMMIT,
            )
        link = tmp_path / "link"
        link.symlink_to(root)
        with _expect("symlink-escape"):
            load_sealed_stage(binding, custody_dir=str(link), repo=REPO, canonical_commit=COMMIT)
        with _expect("custody-missing"):
            load_sealed_stage(
                binding, custody_dir=str(tmp_path / "absent"), repo=REPO, canonical_commit=COMMIT
            )

    def test_modes_and_symlinked_entries_fail(self, tmp_path, custody):
        root, manifest = custody
        blob = sorted(stage_blob_paths(root, "development"))[0]
        os.chmod(blob, 0o644)
        with _expect("permissive-mode"):
            _load(root, manifest, "development")
        os.chmod(blob, 0o600)
        os.chmod(root / "private", 0o750)  # noqa: S103 - deliberately permissive
        with _expect("permissive-mode"):
            _load(root, manifest, "development")
        os.chmod(root / "private", 0o700)
        os.chmod(root, 0o755)  # noqa: S103 - deliberately permissive
        with _expect("permissive-mode"):
            _load(root, manifest, "development")
        os.chmod(root, 0o700)
        _load(root, manifest, "development")
        # A symlinked blob (even to the identical bytes) is a symlink escape.
        outside = tmp_path / "outside-blob"
        shutil.copy(blob, outside)
        os.chmod(outside, 0o600)
        blob.unlink()
        blob.symlink_to(outside)
        with _expect("symlink-escape"):
            _load(root, manifest, "development")

    def test_manifest_receipt_and_index_tampering_fail(self, custody):
        root, manifest = custody
        before = tree_snapshot(root)
        bound = _binding(root, manifest, "development")

        def load_bound():
            return load_sealed_stage(bound, custody_dir=root, repo=REPO, canonical_commit=COMMIT)

        _tamper_file(
            root / "manifest.json",
            lambda data: data.replace(b'"finalized":true', b'"finalized":true '),
        )
        with _expect("manifest-hash-mismatch"):
            load_bound()
        # Even when the binding is (wrongly) derived from the altered file, the
        # manifest's own digest and canonical-bytes check still reject it.
        with _expect("manifest-invalid"):
            _load(root, manifest, "development")
        shutil.rmtree(root)
        write_synthetic_custody(root, commit=COMMIT)
        assert tree_snapshot(root) == before
        load_bound()
        _rewrite_json(root / "receipt.json", lambda d: d.__setitem__("status", "fail"))
        with _expect("receipt-invalid"):
            _load(root, manifest, "development")
        _rewrite_json(root / "receipt.json", lambda d: d.__setitem__("status", "pass"))
        _load(root, manifest, "development")

        def swap_rows(d):
            d["development"][0], d["development"][1] = d["development"][1], d["development"][0]

        _rewrite_json(root / "private" / "index.json", swap_rows)
        with _expect("tamper"):
            _load(root, manifest, "development")

        def duplicate_id(d):
            d["development"][1]["task_id"] = d["development"][0]["task_id"]

        shutil.rmtree(root)
        write_synthetic_custody(root, commit=COMMIT)
        _rewrite_json(root / "private" / "index.json", duplicate_id)
        with _expect("tamper"):
            _load(root, manifest, "development")

        def duplicate_digest(d):
            d["holdout"][1]["content_digest"] = d["holdout"][0]["content_digest"]

        shutil.rmtree(root)
        write_synthetic_custody(root, commit=COMMIT)
        _rewrite_json(root / "private" / "index.json", duplicate_digest)
        with _expect("tamper"):
            _load(root, manifest, "development")

        def extra_key(d):
            d["notes"] = []

        shutil.rmtree(root)
        write_synthetic_custody(root, commit=COMMIT)
        _rewrite_json(root / "private" / "index.json", extra_key)
        with _expect("tamper"):
            _load(root, manifest, "development")

    def test_payload_tampering_fails_and_leaves_custody_unchanged(self, custody):
        root, manifest = custody
        blob = sorted(stage_blob_paths(root, "development"))[3]
        _tamper_file(blob, lambda data: data.replace(b'"reported_minute":', b'"reported_minute": '))
        before = tree_snapshot(root)
        with _expect("tamper"):
            _load(root, manifest, "development")
        assert tree_snapshot(root) == before
        # Holdout tampering is invisible to a development load (never opened) and
        # fatal to a holdout load.
        hold_blob = sorted(stage_blob_paths(root, "holdout"))[0]
        shutil.rmtree(root)
        write_synthetic_custody(root, commit=COMMIT)
        _tamper_file(hold_blob, lambda data: data + b" ")
        _load(root, manifest, "development")
        with _expect("tamper"):
            _load(root, manifest, "holdout")

    def test_malformed_and_duplicate_payloads_fail_before_any_task_is_returned(self, tmp_path):
        development = list(synthetic_stage("development"))
        document = json.loads(development[4].content)
        document["scenario"]["accepted_answer_hint"] = "leak"
        development[4] = OpaqueTask(development[4].task_id, json.dumps(document).encode())
        root = tmp_path / "malformed"
        manifest = write_synthetic_custody(
            root, commit=COMMIT, development=tuple(development), holdout=synthetic_stage("holdout")
        )
        with _expect("malformed-task"):
            _load(root, manifest, "development")
        _load(root, manifest, "holdout")

        # Same bytes decode fine as a holdout member; a payload whose embedded
        # task_id disagrees with the index is rejected.
        development = list(synthetic_stage("development"))
        document = json.loads(development[0].content)
        document["task_id"] = "syn-dev-task-99"
        development[0] = OpaqueTask(development[0].task_id, json.dumps(document).encode())
        root = tmp_path / "mismatch"
        manifest = write_synthetic_custody(
            root, commit=COMMIT, development=tuple(development), holdout=synthetic_stage("holdout")
        )
        with _expect("malformed-task"):
            _load(root, manifest, "development")

        # Two distinct task ids carrying the same instance surface are duplicates.
        development = list(synthetic_stage("development"))
        first = json.loads(development[0].content)
        second = json.loads(development[10].content)
        second["instance"] = first["instance"]
        development[10] = OpaqueTask(
            development[10].task_id,
            (json.dumps(second, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        root = tmp_path / "duplicate"
        manifest = write_synthetic_custody(
            root, commit=COMMIT, development=tuple(development), holdout=synthetic_stage("holdout")
        )
        with _expect("duplicate-task"):
            _load(root, manifest, "development")

        # The same scenario id must describe the same scenario everywhere.
        development = list(synthetic_stage("development"))
        altered = json.loads(development[10].content)
        altered["scenario"]["title"] = altered["scenario"]["title"] + " (variant)"
        development[10] = OpaqueTask(development[10].task_id, json.dumps(altered).encode())
        root = tmp_path / "inconsistent"
        manifest = write_synthetic_custody(
            root, commit=COMMIT, development=tuple(development), holdout=synthetic_stage("holdout")
        )
        with _expect("malformed-task"):
            _load(root, manifest, "development")

    @pytest.mark.parametrize(
        ("field", "reason"),
        [
            ("custody_manifest_sha256", "manifest-hash-mismatch"),
            ("custody_controller_digest", "controller-digest-mismatch"),
            ("import_request_digest", "import-request-mismatch"),
            ("set_identity", "set-identity-mismatch"),
            ("stage_aggregate_digest", "aggregate-mismatch"),
        ],
    )
    def test_each_binding_digest_mismatch_fails(self, custody, field, reason):
        root, manifest = custody
        section = binding_for(root, manifest, "development")
        value = section[field]
        section[field] = value[:-1] + ("0" if value[-1] != "0" else "1")
        binding = SealedSetBinding.from_config(section, stage="development")
        with _expect(reason):
            load_sealed_stage(binding, custody_dir=root, repo=REPO, canonical_commit=COMMIT)

    def test_wrong_commit_and_other_stage_binding_fail(self, custody, monkeypatch):
        root, manifest = custody
        with _expect("commit-mismatch"):
            _load(root, manifest, "development", canonical_commit="b" * 40)
        # A holdout binding cannot be used to load development bytes.
        holdout_binding = _binding(root, manifest, "holdout")
        loaded = load_sealed_stage(
            holdout_binding, custody_dir=root, repo=REPO, canonical_commit=COMMIT
        )
        assert all(task_id.startswith("syn-hol-") for task_id in loaded.task_ids)
        # And an executing controller that differs from the bound digest fails.
        from blackwell_lab.cloud import sealed_binding as module

        monkeypatch.setattr(module, "running_controller_digest", lambda: "sha256:" + "f" * 64)
        with _expect("controller-digest-mismatch"):
            _load(root, manifest, "development")

    def test_errors_never_carry_paths_or_content(self, custody, tmp_path):
        root, manifest = custody
        tasks = _load(root, manifest, "development").tasks
        messages = []
        for attempt in (
            lambda: load_sealed_stage(
                _binding(root, manifest, "development"),
                custody_dir="relative",
                repo=REPO,
                canonical_commit=COMMIT,
            ),
            lambda: _load(root, manifest, "development", canonical_commit="c" * 40),
        ):
            with pytest.raises(SealedSetError) as caught:
                attempt()
            messages.append(str(caught.value))
        blob = sorted(stage_blob_paths(root, "development"))[0]
        _tamper_file(blob, lambda data: data + b"\n")
        with pytest.raises(SealedSetError) as caught:
            _load(root, manifest, "development")
        messages.append(str(caught.value))
        _clean_stream("\n".join(messages), root, tasks)


# --- run assembly, manifests, and verification ---------------------------------------


class TestSealedRun:
    def test_run_real_cell_uses_only_the_sealed_stage(self, tmp_path, monkeypatch, custody):
        root, manifest = custody
        external = tmp_path / "external"
        external.mkdir()
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        loaded = _load(root, manifest, "development")
        binding = loaded.binding

        def no_catalog():
            raise AssertionError("the public catalog must not be consulted for a sealed run")

        monkeypatch.setattr(realbench, "catalog", no_catalog)
        client = _UsageMockClient()
        spec = _real_spec(
            tasks_per_repetition=20,
            sealed_set=binding,
            artifact_family="qualification-runs",
            run_label="sealed-dev",
        )
        records = run_real_cell(
            spec,
            client,
            host=_HOST,
            sampler_factory=_FakeSampler,
            clock=FakeClock(),
            sealed_tasks=loaded,
        )
        assert len(records) == 1
        record = records[0]
        workload = record.manifest["workload"]
        validate_run_manifest(record.manifest)
        assert workload["sealed_set"] == binding.provenance()
        assert workload["catalog_digest"] == binding.stage_aggregate_digest
        assert workload["tasks_per_repetition"] == 20
        assert workload["scenario_count"] == 10
        assert workload["version"] == "2.5.0" and workload["controller"] == "evidence-grounding-v1"
        observed_ids = [o["instance_id"] for o in record.measured_observations["observations"]]
        assert sorted(observed_ids) == sorted(loaded.task_ids)
        assert record.result["tasks"]["attempted"] == 20
        assert record.result["observations"]["warmup_count"] == 0
        assert client.seen[0][0][0].content == SYSTEM_PROMPT_V241
        require_manifest_provenance(record.manifest, binding)
        require_sealed_stage_evidence(loaded, list(record.measured_observations["observations"]))
        validate_result_semantics(
            record.manifest, record.result, measured_observations=record.measured_observations
        )
        public_blob = json.dumps(record.manifest) + json.dumps(record.result)
        assert str(root) not in public_blob
        for task_id in loaded.task_ids:
            assert task_id not in public_blob

    def test_sealed_spec_contract_is_enforced_before_any_call(self, tmp_path, monkeypatch, custody):
        root, manifest = custody
        external = tmp_path / "external"
        external.mkdir()
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        loaded = _load(root, manifest, "development")
        client = _UsageMockClient()
        base = dict(
            tasks_per_repetition=20, sealed_set=loaded.binding, artifact_family="qualification-runs"
        )
        for overrides, tasks in (
            ({}, None),
            (dict(sealed_set=None), loaded),
            (dict(template_ids=("elevated-latency-001",)), loaded),
            (dict(warmup_passes=1), loaded),
            (dict(repetitions=2), loaded),
            (dict(tasks_per_repetition=10), loaded),
            (dict(artifact_family="real-runs"), loaded),
            (dict(sealed_set=_binding(root, manifest, "holdout")), loaded),
        ):
            with pytest.raises(ConfigError):
                run_real_cell(
                    _real_spec(**{**base, **overrides}),
                    client,
                    host=_HOST,
                    sampler_factory=_FakeSampler,
                    clock=FakeClock(),
                    sealed_tasks=tasks,
                )
        assert client.calls == 0
        assert not list(external.rglob("*.json"))

    def test_provenance_mismatch_is_rejected_by_verification(self, tmp_path, monkeypatch, custody):
        root, manifest = custody
        external = tmp_path / "external"
        external.mkdir()
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        loaded = _load(root, manifest, "development")
        record = run_real_cell(
            _real_spec(
                tasks_per_repetition=20,
                sealed_set=loaded.binding,
                artifact_family="qualification-runs",
            ),
            _UsageMockClient(),
            host=_HOST,
            sampler_factory=_FakeSampler,
            clock=FakeClock(),
            sealed_tasks=loaded,
        )[0]
        for field in BINDING_FIELDS:
            tampered = json.loads(json.dumps(record.manifest))
            value = tampered["workload"]["sealed_set"][field]
            tampered["workload"]["sealed_set"][field] = (
                value + 1
                if isinstance(value, int)
                else ("holdout" if field == "stage" else value[:-1] + "x")
            )
            with _expect("provenance-mismatch"):
                require_manifest_provenance(tampered, loaded.binding)
        no_block = json.loads(json.dumps(record.manifest))
        del no_block["workload"]["sealed_set"]
        with _expect("provenance-mismatch"):
            require_manifest_provenance(no_block, loaded.binding)
        wrong_digest = json.loads(json.dumps(record.manifest))
        wrong_digest["workload"]["catalog_digest"] = "sha256:" + "0" * 64
        with _expect("provenance-mismatch"):
            require_manifest_provenance(wrong_digest, loaded.binding)
        with pytest.raises(SemanticValidationError):
            validate_result_semantics(wrong_digest, record.result)
        wrong_count = json.loads(json.dumps(record.manifest))
        wrong_count["workload"]["sealed_set"]["task_count"] = 19
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(wrong_count)
        with_path = json.loads(json.dumps(record.manifest))
        with_path["workload"]["sealed_set"]["custody_dir"] = "/private"
        with pytest.raises(jsonschema.ValidationError):
            validate_run_manifest(with_path)
        other_stage = [
            dict(o, instance_id="syn-hol-task-00")
            for o in record.measured_observations["observations"]
        ]
        with _expect("incomplete-stage-evidence"):
            require_sealed_stage_evidence(loaded, other_stage)
        with _expect("incomplete-stage-evidence"):
            require_sealed_stage_evidence(
                loaded, list(record.measured_observations["observations"])[:19]
            )

        # verify-results on disk rejects the persisted manifest once its sealed
        # provenance is edited.
        run_dir = external / "qualification-runs" / "p2-manifest"
        manifest_path = next(run_dir.glob("*.manifest.json"))
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        document["workload"]["catalog_digest"] = "sha256:" + "1" * 64
        manifest_path.write_text(json.dumps(document), encoding="utf-8")
        assert main(["verify-results", "--subdirectory", "qualification-runs"]) == 1


# --- CLI: fail before client, isolation, validate-only, leakage ---------------------------


class TestQualifyAgentCli:
    @pytest.mark.parametrize("stage", STAGES)
    def test_sealed_stage_executes_end_to_end_without_the_other_stage(
        self, tmp_path, monkeypatch, capsys, stage
    ):
        harness = _Harness(tmp_path, monkeypatch)
        root = tmp_path / "custody"
        manifest = write_synthetic_custody(root, commit=COMMIT)
        section = binding_for(root, manifest, stage)
        offline = harness.go_live()

        def no_catalog():
            raise AssertionError("the public catalog must not be consulted for a sealed run")

        monkeypatch.setattr(realbench, "catalog", no_catalog)
        recorder = _OpenRecorder(monkeypatch)
        config = qualification_config_dict("P2", stage, sealed_set=section)
        code = harness.run(config, stage=stage, custody_dir=root)
        out, err = capsys.readouterr()
        assert code == 0, err
        assert err == ""
        assert recorder.under(root / "private" / "blobs") == stage_blob_paths(root, stage)
        assert not recorder.under(root / "private" / "blobs") & stage_blob_paths(root, OTHER[stage])
        report = json.loads(out)
        assert report["sealed_set"] == section
        assert report["stage"] == stage and report["candidate_id"] == "P2"
        assert report["gates"]["quality_floor"] == (
            DEVELOPMENT_QUALITY_FLOOR if stage == "development" else HOLDOUT_QUALITY_FLOOR
        )
        assert isinstance(harness.constructed[0], offline)
        assert harness.constructed[0]._mock.seen[0][0][0].content == SYSTEM_PROMPT_V241
        tasks = _load(root, manifest, stage).tasks
        _clean_stream(out + err, root, tasks)
        receipt_path = harness.external / "qualification-runs" / f"qual-a-p2-{stage}-receipt.json"
        receipt_text = receipt_path.read_text(encoding="utf-8")
        _clean_stream(receipt_text, root, tasks)
        assert json.loads(receipt_text)["sealed_set"] == section
        run_dir = harness.external / "qualification-runs" / f"qual-a-p2-{stage}"
        persisted_manifest = json.loads(
            next(run_dir.glob("*.manifest.json")).read_text(encoding="utf-8")
        )
        assert persisted_manifest["workload"]["sealed_set"] == section
        _clean_stream(json.dumps(persisted_manifest), root, tasks)
        assert main(["verify-results", "--subdirectory", "qualification-runs"]) == 0

    def test_missing_binding_or_custody_dir_fails_before_client(
        self, tmp_path, monkeypatch, capsys
    ):
        harness = _Harness(tmp_path, monkeypatch)
        root = tmp_path / "custody"
        manifest = write_synthetic_custody(root, commit=COMMIT)
        section = binding_for(root, manifest, "development")
        # No binding at all: P2 development never falls back to the catalog.
        bare = {
            k: v
            for k, v in qualification_config_dict("P2", "development", sealed_set=section).items()
            if k != "sealed_set"
        }
        assert harness.run(bare, custody_dir=root) == 1
        assert "BLOCKED: sealed-set: binding-missing" in capsys.readouterr().err
        # Binding present but no custody directory supplied.
        assert harness.run(qualification_config_dict("P2", "development", sealed_set=section)) == 1
        assert "BLOCKED: sealed-set: custody-dir-required" in capsys.readouterr().err
        # Holdout binding supplied for a development run.
        assert (
            harness.run(
                qualification_config_dict(
                    "P2", "development", sealed_set=binding_for(root, manifest, "holdout")
                ),
                custody_dir=root,
            )
            == 1
        )
        assert "binding-stage-mismatch" in capsys.readouterr().err
        # Catalog cells refuse the argument and the section.
        assert (
            harness.run(
                qualification_config_dict("C1", "development"), candidate="C1", custody_dir=root
            )
            == 1
        )
        assert "custody-dir-not-applicable" in capsys.readouterr().err
        assert (
            harness.run(
                dict(qualification_config_dict("P1", "holdout"), sealed_set=section),
                candidate="P1",
                stage="holdout",
            )
            == 1
        )
        assert "binding-not-applicable" in capsys.readouterr().err
        assert (
            harness.run(qualification_config_dict("P2", "freeze"), stage="freeze", custody_dir=root)
            == 1
        )
        assert "custody-dir-not-applicable" in capsys.readouterr().err
        harness.assert_nothing_live()
        assert not list(harness.external.rglob("*receipt*"))

    @pytest.mark.parametrize(
        ("field", "reason"),
        [
            ("custody_manifest_sha256", "manifest-hash-mismatch"),
            ("custody_controller_digest", "controller-digest-mismatch"),
            ("import_request_digest", "import-request-mismatch"),
            ("set_identity", "set-identity-mismatch"),
            ("stage_aggregate_digest", "aggregate-mismatch"),
            ("task_count", "binding-task-count"),
            ("stage", "binding-stage-mismatch"),
        ],
    )
    def test_every_binding_mismatch_fails_before_client(
        self, tmp_path, monkeypatch, capsys, field, reason
    ):
        harness = _Harness(tmp_path, monkeypatch)
        root = tmp_path / "custody"
        manifest = write_synthetic_custody(root, commit=COMMIT)
        section = binding_for(root, manifest, "development")
        if field == "task_count":
            section[field] = 21
        elif field == "stage":
            section[field] = "holdout"
        else:
            section[field] = section[field][:-1] + ("0" if section[field][-1] != "0" else "1")
        before = tree_snapshot(root)
        assert (
            harness.run(
                qualification_config_dict("P2", "development", sealed_set=section), custody_dir=root
            )
            == 1
        )
        err = capsys.readouterr().err
        assert f"BLOCKED: sealed-set: {reason}" in err
        harness.assert_nothing_live()
        assert tree_snapshot(root) == before
        _clean_stream(err, root, _load(root, manifest, "development").tasks)

    def test_custody_failures_fail_before_client(self, tmp_path, monkeypatch, capsys):
        harness = _Harness(tmp_path, monkeypatch)
        root = tmp_path / "custody"
        manifest = write_synthetic_custody(root, commit=COMMIT)
        section = binding_for(root, manifest, "development")
        config = qualification_config_dict("P2", "development", sealed_set=section)
        tasks = _load(root, manifest, "development").tasks

        def expect(reason, *, custody_dir=root, cfg=config):
            before = tree_snapshot(root) if root.exists() else None
            assert harness.run(cfg, custody_dir=custody_dir) == 1
            err = capsys.readouterr().err
            assert f"BLOCKED: sealed-set: {reason}" in err
            _clean_stream(err, root, tasks)
            if before is not None:
                assert tree_snapshot(root) == before
            harness.assert_nothing_live()

        expect("relative-path", custody_dir="custody")
        expect("repository-path", custody_dir=REPO / "custody")
        link = tmp_path / "link"
        link.symlink_to(root)
        expect("symlink-escape", custody_dir=link)
        expect("custody-missing", custody_dir=tmp_path / "absent")
        os.chmod(root / "receipt.json", 0o640)
        expect("permissive-mode")
        os.chmod(root / "receipt.json", 0o600)
        # Wrong canonical commit: the custody manifest was frozen elsewhere.
        other = tmp_path / "other-commit"
        other_manifest = write_synthetic_custody(other, commit="d" * 40)
        expect(
            "commit-mismatch",
            custody_dir=other,
            cfg=qualification_config_dict(
                "P2", "development", sealed_set=binding_for(other, other_manifest, "development")
            ),
        )
        _tamper_file(root / "manifest.json", lambda d: d.replace(b"1.1.0", b"1.1.1"))
        expect("manifest-hash-mismatch")
        shutil.rmtree(root)
        write_synthetic_custody(root, commit=COMMIT)
        _rewrite_json(root / "receipt.json", lambda d: d.__setitem__("operation", "verify"))
        expect("receipt-invalid")
        shutil.rmtree(root)
        write_synthetic_custody(root, commit=COMMIT)
        _rewrite_json(root / "private" / "index.json", lambda d: d["development"].pop())
        expect("tamper")
        shutil.rmtree(root)
        write_synthetic_custody(root, commit=COMMIT)
        blob = sorted(stage_blob_paths(root, "development"))[7]
        _tamper_file(blob, lambda d: d + b" ")
        expect("tamper")
        shutil.rmtree(root)
        bad_dev = list(synthetic_stage("development"))
        broken = json.loads(bad_dev[2].content)
        broken["instance"]["instance_seed"] = "seed"
        bad_dev[2] = OpaqueTask(bad_dev[2].task_id, json.dumps(broken).encode())
        bad_manifest = write_synthetic_custody(
            root, commit=COMMIT, development=tuple(bad_dev), holdout=synthetic_stage("holdout")
        )
        expect(
            "malformed-task",
            cfg=qualification_config_dict(
                "P2", "development", sealed_set=binding_for(root, bad_manifest, "development")
            ),
        )

    def test_development_run_never_opens_holdout_even_when_holdout_is_corrupt(
        self, tmp_path, monkeypatch, capsys
    ):
        harness = _Harness(tmp_path, monkeypatch)
        root = tmp_path / "custody"
        manifest = write_synthetic_custody(root, commit=COMMIT)
        hold_blob = sorted(stage_blob_paths(root, "holdout"))[5]
        # Corrupt a holdout body in place (digest now wrong). Development must
        # still run because it never reads holdout bytes; holdout must refuse.
        _tamper_file(hold_blob, lambda d: d.replace(b"syn-hol", b"syn-xol"))
        harness.go_live()
        recorder = _OpenRecorder(monkeypatch)
        config = qualification_config_dict(
            "P2", "development", sealed_set=binding_for(root, manifest, "development")
        )
        assert harness.run(config, custody_dir=root) == 0, capsys.readouterr().err
        capsys.readouterr()
        assert recorder.under(root / "private" / "blobs") == stage_blob_paths(root, "development")
        assert hold_blob not in recorder.paths
        hold_config = qualification_config_dict(
            "P2", "holdout", sealed_set=binding_for(root, manifest, "holdout")
        )
        assert harness.run(hold_config, stage="holdout", custody_dir=root) == 1
        assert "BLOCKED: sealed-set: tamper" in capsys.readouterr().err

    @pytest.mark.parametrize("stage", STAGES)
    def test_validate_only_verifies_offline_with_zero_client_calls(
        self, tmp_path, monkeypatch, capsys, stage
    ):
        harness = _Harness(tmp_path, monkeypatch)
        # validate-only must not need the ledger, approval, or a results directory.
        monkeypatch.delenv("LAB_RESULTS_DIR")
        shutil.rmtree(harness.external)
        root = tmp_path / "custody"
        manifest = write_synthetic_custody(root, commit=COMMIT)
        section = binding_for(root, manifest, stage)
        recorder = _OpenRecorder(monkeypatch)
        config = qualification_config_dict("P2", stage, sealed_set=section)
        path = tmp_path / "validate.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        argv = [
            "qualify-agent",
            "--run-tag",
            RUN_TAG,
            "--run-label",
            "qual-a",
            "--candidate",
            "P2",
            "--stage",
            stage,
            "--config",
            str(path),
            "--custody-dir",
            str(root),
            "--validate-only",
        ]
        assert main(argv) == 0
        out, err = capsys.readouterr()
        assert err == ""
        report = json.loads(out)
        assert report["mode"] == "validate-only"
        assert report["executed"] is False
        assert report["model_client_constructed"] is False
        assert report["endpoint_contacted"] is False
        assert report["approval_checked"] is False
        assert report["sealed_input"] is True
        assert report["sealed_set"] == section
        assert report["sealed_tasks_loaded"] == 20
        assert report["sealed_scenario_count"] == 10
        assert report["candidate_identity_sha256"] == candidate_identity_digest("P2")
        harness.assert_nothing_live()
        assert not harness.external.exists()
        assert recorder.under(root / "private" / "blobs") == stage_blob_paths(root, stage)
        _clean_stream(out, root, _load(root, manifest, stage).tasks)
        # A broken custody set is reported, still without any client.
        _tamper_file(root / "manifest.json", lambda d: d + b"\n")
        assert main(argv) == 1
        assert "BLOCKED: sealed-set: manifest-hash-mismatch" in capsys.readouterr().err
        harness.assert_nothing_live()
        # validate-only for a catalog candidate validates the config only.
        c1 = tmp_path / "c1.json"
        c1.write_text(json.dumps(qualification_config_dict("C1", "development")), encoding="utf-8")
        assert (
            main(
                [
                    "qualify-agent",
                    "--run-tag",
                    RUN_TAG,
                    "--run-label",
                    "qual-a",
                    "--candidate",
                    "C1",
                    "--stage",
                    "development",
                    "--config",
                    str(c1),
                    "--validate-only",
                ]
            )
            == 0
        )
        c1_report = json.loads(capsys.readouterr().out)
        assert c1_report["sealed_input"] is False and "sealed_set" not in c1_report
        harness.assert_nothing_live()

    def test_parser_exposes_custody_dir_and_validate_only(self, capsys):
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "qualify-agent",
                "--run-tag",
                RUN_TAG,
                "--run-label",
                "qual-a",
                "--candidate",
                "P2",
                "--stage",
                "holdout",
                "--config",
                "/abs/outside.json",
                "--custody-dir",
                "/abs/custody",
                "--validate-only",
            ]
        )
        assert args.custody_dir == "/abs/custody" and args.validate_only is True
        args = parser.parse_args(
            [
                "qualify-agent",
                "--run-tag",
                RUN_TAG,
                "--run-label",
                "qual-a",
                "--candidate",
                "C1",
                "--stage",
                "development",
                "--config",
                "/abs/outside.json",
            ]
        )
        assert args.custody_dir is None and args.validate_only is False
        with pytest.raises(SystemExit) as caught:
            parser.parse_args(["qualify-agent", "--help"])
        assert caught.value.code == 0
        help_text = " ".join(capsys.readouterr().out.split())
        assert "--custody-dir" in help_text and "--validate-only" in help_text
        assert "never printed" in help_text


# --- unchanged frozen surfaces ----------------------------------------------------------------


class TestUnchangedSurfaces:
    def test_prompt_evaluator_thresholds_and_answers_are_unchanged(self):
        assert (
            hashlib.sha256(SYSTEM_PROMPT_V241.encode()).hexdigest()
            == FROZEN_SYSTEM_PROMPT_V241_SHA256
        )
        assert EVALUATOR_VERSION == "3.1.0" and QUALITY_THRESHOLD == 1.0
        assert DEVELOPMENT_TASKS == HOLDOUT_TASKS == 20
        assert DEVELOPMENT_QUALITY_FLOOR == 0.40 and HOLDOUT_QUALITY_FLOOR == 0.50
        assert (
            hashlib.sha256(_accepted_answers_payload()).hexdigest()
            == FROZEN_ACCEPTED_ANSWERS_SHA256
        )
        assert PAYLOAD_SCHEMA_VERSION == "1.0.0"

    def test_catalog_runs_are_byte_for_byte_unaffected(self, tmp_path, monkeypatch):
        external = tmp_path / "external"
        external.mkdir()
        monkeypatch.setenv("LAB_RESULTS_DIR", str(external))
        spec = _real_spec(
            workload_version="2.4.1",
            controller=None,
            generation=GenerationSettings(
                temperature=0.2, top_p=0.95, reasoning_mode=True, workload_version="2.4.1"
            ),
            run_label="p1-catalog",
        )
        assert spec.sealed_set is None
        records = run_real_cell(
            spec, _UsageMockClient(), host=_HOST, sampler_factory=_FakeSampler, clock=FakeClock()
        )
        workload = records[0].manifest["workload"]
        assert "sealed_set" not in workload
        assert workload["catalog_digest"].startswith("sha256:")
        validate_result_semantics(
            records[0].manifest,
            records[0].result,
            measured_observations=records[0].measured_observations,
        )
        with pytest.raises(ConfigError):
            run_real_cell(
                RealRunSpec(**{**spec.__dict__, "run_label": "p1-catalog-2"}),
                _UsageMockClient(),
                host=_HOST,
                sampler_factory=_FakeSampler,
                clock=FakeClock(),
                sealed_tasks=SealedStageTasks(
                    binding=SealedSetBinding.from_config(
                        placeholder_binding("development"), stage="development"
                    ),
                    tasks=(),
                ),
            )


def test_fixture_manifests_are_content_free(custody):
    root, manifest = custody
    blob = json.dumps(manifest) + (root / "receipt.json").read_text(encoding="utf-8")
    for task in (*synthetic_stage("development"), *synthetic_stage("holdout")):
        assert task.task_id not in blob
    assert str(root) not in blob
