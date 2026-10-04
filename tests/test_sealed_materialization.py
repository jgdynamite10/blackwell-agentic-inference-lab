"""Tests for the offline sealed-task materialization (decision D-0025).

Every source entry here is synthetic: a public catalog scenario re-serialized
with a ``syn-…`` task id. Nothing real is read. Tests that call the
git-bound ``prepare``/``materialize`` operations run against this checkout
and, like the custody tests, require a clean committed worktree.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import jsonschema
import pytest
from sealed_fixtures import binding_for, tree_snapshot

from blackwell_lab.cloud import sealed_materialize as materialize
from blackwell_lab.cloud.qualification import (
    DEVELOPMENT_TEMPLATE_IDS,
    FROZEN_SEED,
    HOLDOUT_TEMPLATE_IDS,
    MEASURED_REPETITION_SEED,
    expected_stage_distribution,
    stage_spec,
)
from blackwell_lab.cloud.sealed_binding import load_sealed_stage
from blackwell_lab.cloud.sealed_materialize import (
    APPROVAL_TEMPLATE,
    IMPLEMENTATION_SOURCE_PATHS,
    OCCURRENCE_RULE,
    RECORD_NAME,
    build_materialization_request,
    frozen_stage_order,
    implementation_digest_from_sources,
    materialization_approval_phrase,
    materialize_authorized_bundles,
    materialize_stage,
    occurrences,
    parse_source_entry,
    prepare_materialization,
    running_implementation_digest,
    running_implementation_parts,
    validate_request,
)
from blackwell_lab.cloud.sealed_payload import (
    PAYLOAD_KIND,
    PAYLOAD_SCHEMA_VERSION,
    decode_sealed_task,
    scenario_document,
)
from blackwell_lab.paths import repository_root
from blackwell_lab.sealed_sets.custody import (
    import_authorized_set,
    load_bundle_directory,
    prepare_import_request,
)
from blackwell_lab.sealed_sets.model import (
    CONTROLLER_SOURCE_PATHS,
    CustodyError,
    OpaqueTask,
    approval_phrase,
    validate_bundles,
)
from blackwell_lab.workload.sampling import generate_task_instances
from blackwell_lab.workload.scenarios import catalog

GIT = shutil.which("git")
if GIT is None:
    pytest.skip("git is required", allow_module_level=True)

CATALOG = catalog()
COMMIT = "a" * 40
FAKE_IMPLEMENTATION = "sha256:" + "b" * 64


def _lab() -> tuple[Path, str]:
    repo = repository_root()
    head = subprocess.run(
        [GIT, "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, head


def _source_bytes(task_id: str, scenario: dict) -> bytes:
    document = {"task_id": task_id, "scenario": scenario}
    return (json.dumps(document, sort_keys=True, indent=1) + "\n").encode("utf-8")


def source_stage(
    stage: str,
    distribution: Counter[str] | dict[str, int] | None = None,
    *,
    salt: str = "",
) -> tuple[OpaqueTask, ...]:
    """Twenty synthetic source entries following ``distribution`` (default: frozen catalog)."""
    distribution = distribution or expected_stage_distribution(stage)
    tasks: list[OpaqueTask] = []
    index = 0
    for template_id in sorted(distribution):
        for _ in range(distribution[template_id]):
            task_id = f"syn-{salt}{stage[:3]}-{index:02d}-{template_id[:12]}"
            tasks.append(
                OpaqueTask(task_id, _source_bytes(task_id, scenario_document(CATALOG[template_id])))
            )
            index += 1
    assert len(tasks) == 20
    return tuple(tasks)


def write_bundle(directory: Path, tasks: Sequence[OpaqueTask]) -> Path:
    os.mkdir(directory, 0o700)
    os.chmod(directory, 0o700)
    for task in tasks:
        fd = os.open(directory / task.task_id, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, task.content)
        finally:
            os.close(fd)
        os.chmod(directory / task.task_id, 0o600)
    return directory


def decoded(tasks: Sequence[OpaqueTask]):
    return [decode_sealed_task(task.content, expected_task_id=task.task_id) for task in tasks]


def surfaces(instances) -> list[tuple[str, int, str, int]]:
    return sorted(
        (item.template_id, item.instance_seed, item.tracking_id, item.reported_minute)
        for item in instances
    )


def private_strings(*stages: Sequence[OpaqueTask]) -> set[str]:
    """Strings that must never appear in public output: ids, scenario ids, answers."""
    secrets: set[str] = set()
    for tasks in stages:
        for task in tasks:
            secrets.add(task.task_id)
            scenario = json.loads(task.content)["scenario"]
            secrets.add(scenario["scenario_id"])
            secrets.add(scenario["root_cause_id"])
            secrets.add(scenario["title"])
            secrets.update(scenario["accepted_diagnoses"])
            secrets.update(scenario["accepted_remediations"])
    return secrets


def assert_content_free(
    text: str, *stages: Sequence[OpaqueTask], paths: Sequence[Path] = ()
) -> None:
    for secret in private_strings(*stages):
        assert secret not in text
    for path in paths:
        assert str(path) not in text


@pytest.fixture
def sources() -> tuple[tuple[OpaqueTask, ...], tuple[OpaqueTask, ...]]:
    return source_stage("development"), source_stage("holdout")


# --------------------------------------------------------------------------
# Seed and occurrence semantics
# --------------------------------------------------------------------------


class TestSeedAndOccurrence:
    def test_seed_is_the_measured_repetition_seed_of_the_production_runner(self, sources):
        request, _dev, _hold = build_materialization_request(
            commit=COMMIT,
            implementation_digest=FAKE_IMPLEMENTATION,
            development=sources[0],
            holdout=sources[1],
        )
        assert MEASURED_REPETITION_SEED == FROZEN_SEED + 1
        assert request["algorithm"]["seed"] == MEASURED_REPETITION_SEED
        assert request["algorithm"]["occurrence_rule"] == OCCURRENCE_RULE
        assert request["algorithm"]["generator"] == (
            "blackwell_lab.workload.sampling.generate_task_instances"
        )

    def test_occurrence_is_the_per_template_counter_over_task_id_order(self, sources):
        development, _holdout = sources
        entries = frozen_stage_order([parse_source_entry(task) for task in development])
        assert [entry.task_id for entry in entries] == sorted(task.task_id for task in development)
        counters: Counter[str] = Counter()
        expected = []
        for entry in entries:
            expected.append(counters[entry.scenario_id])
            counters[entry.scenario_id] += 1
        assert list(occurrences(entries)) == expected

    @pytest.mark.parametrize("stage", ["development", "holdout"])
    def test_instance_triples_come_from_generate_task_instances(self, stage):
        tasks = source_stage(stage)
        materialized = materialize_stage(stage, tasks)
        by_template: dict[str, list] = {}
        for task in decoded(materialized.tasks):
            by_template.setdefault(task.scenario.scenario_id, []).append(task)
        for template_id, group in by_template.items():
            group.sort(key=lambda item: item.task_id)
            production = generate_task_instances(
                (template_id,), len(group), MEASURED_REPETITION_SEED
            )
            for occurrence, (task, reference) in enumerate(zip(group, production, strict=True)):
                assert reference.instance_id == f"{template_id}#{occurrence:04d}"
                assert task.instance.instance_seed == reference.instance_seed
                assert task.instance.tracking_id == reference.tracking_id
                assert task.instance.reported_minute == reference.reported_minute
                assert task.instance.instance_id == task.task_id
                assert task.instance.template_id == template_id

    @pytest.mark.parametrize("stage", ["development", "holdout"])
    def test_surfaces_equal_the_production_catalog_schedule_for_the_frozen_distribution(
        self, stage
    ):
        """With the frozen per-template distribution the materialized instance
        surfaces are exactly those the catalog runner would generate."""
        spec = stage_spec(stage)
        production = generate_task_instances(
            spec["template_ids"], spec["tasks"], MEASURED_REPETITION_SEED
        )
        materialized = materialize_stage(stage, source_stage(stage))
        assert surfaces(task.instance for task in decoded(materialized.tasks)) == surfaces(
            production
        )

    def test_a_different_task_id_order_reassigns_occurrences_deterministically(self):
        template_id = DEVELOPMENT_TEMPLATE_IDS[0]
        distribution = {template_id: 20}
        scenario = scenario_document(CATALOG[template_id])
        forward = tuple(
            OpaqueTask(f"syn-order-{index:02d}", _source_bytes(f"syn-order-{index:02d}", scenario))
            for index in range(20)
        )
        first = decoded(materialize_stage("development", forward).tasks)
        shuffled_input = tuple(reversed(forward))
        second = decoded(materialize_stage("development", shuffled_input).tasks)
        assert [t.task_id for t in first] == [t.task_id for t in second]
        assert [t.instance for t in first] == [t.instance for t in second]
        reference = generate_task_instances((template_id,), 20, MEASURED_REPETITION_SEED)
        for occurrence, task in enumerate(first):
            assert task.instance.instance_seed == reference[occurrence].instance_seed
        assert Counter(materialize_stage("development", forward).distribution) == Counter(
            distribution
        )

    def test_seed_argument_changes_every_surface(self, sources):
        base = decoded(materialize_stage("development", sources[0]).tasks)
        other = decoded(materialize_stage("development", sources[0], seed=FROZEN_SEED).tasks)
        assert surfaces(t.instance for t in base) != surfaces(t.instance for t in other)


# --------------------------------------------------------------------------
# Preservation and envelope
# --------------------------------------------------------------------------


class TestPreservation:
    @pytest.mark.parametrize("stage", ["development", "holdout"])
    def test_ids_scenarios_distribution_and_order_are_preserved(self, stage):
        tasks = source_stage(stage)
        materialized = materialize_stage(stage, tasks)
        assert [task.task_id for task in materialized.tasks] == sorted(t.task_id for t in tasks)
        source_by_id = {task.task_id: json.loads(task.content)["scenario"] for task in tasks}
        for task, item in zip(materialized.tasks, decoded(materialized.tasks), strict=True):
            payload = json.loads(task.content)
            assert set(payload) == {
                "payload_schema_version",
                "kind",
                "task_id",
                "instance",
                "scenario",
            }
            assert payload["payload_schema_version"] == PAYLOAD_SCHEMA_VERSION
            assert payload["kind"] == PAYLOAD_KIND
            assert payload["task_id"] == task.task_id
            assert set(payload["instance"]) == {"instance_seed", "tracking_id", "reported_minute"}
            assert payload["scenario"] == source_by_id[task.task_id]
            round_trip = json.loads(json.dumps(scenario_document(item.scenario)))
            assert round_trip == source_by_id[task.task_id]
        assert Counter(materialized.distribution) == Counter(
            source["scenario_id"] for source in source_by_id.values()
        )
        assert Counter(materialized.distribution) == expected_stage_distribution(stage)

    def test_non_frozen_distribution_is_preserved_not_rebalanced(self):
        distribution = {HOLDOUT_TEMPLATE_IDS[0]: 11, HOLDOUT_TEMPLATE_IDS[1]: 9}
        tasks = source_stage("holdout", distribution)
        materialized = materialize_stage("holdout", tasks)
        assert materialized.distribution == distribution
        request, _dev, _hold = build_materialization_request(
            commit=COMMIT,
            implementation_digest=FAKE_IMPLEMENTATION,
            development=source_stage("development"),
            holdout=tasks,
        )
        assert request["distribution"]["holdout"]["counts"] == [11, 9]
        assert request["distribution"]["holdout"]["template_count"] == 2

    def test_output_is_valid_custody_input(self, sources):
        _request, dev, hold = build_materialization_request(
            commit=COMMIT,
            implementation_digest=FAKE_IMPLEMENTATION,
            development=sources[0],
            holdout=sources[1],
        )
        validation = validate_bundles(dev.tasks, hold.tasks)
        assert validation.development.task_count == 20
        assert validation.holdout.task_count == 20
        assert set(t.task_id for t in dev.tasks) == set(t.task_id for t in sources[0])
        assert set(t.task_id for t in hold.tasks) == set(t.task_id for t in sources[1])


# --------------------------------------------------------------------------
# Request binding
# --------------------------------------------------------------------------


class TestRequest:
    def test_request_is_schema_valid_content_free_and_deterministic(self, sources):
        development, holdout = sources
        request, dev, hold = build_materialization_request(
            commit=COMMIT,
            implementation_digest=FAKE_IMPLEMENTATION,
            development=development,
            holdout=holdout,
        )
        again, _dev, _hold = build_materialization_request(
            commit=COMMIT,
            implementation_digest=FAKE_IMPLEMENTATION,
            development=development,
            holdout=holdout,
        )
        assert request == again
        validate_request(request)
        text = json.dumps(request)
        assert_content_free(text, development, holdout, dev.tasks, hold.tasks)
        source = validate_bundles(development, holdout)
        output = validate_bundles(dev.tasks, hold.tasks)
        assert (
            request["source"]["development_aggregate_digest"] == source.development.aggregate_digest
        )
        assert request["source"]["holdout_aggregate_digest"] == source.holdout.aggregate_digest
        assert (
            request["output"]["development_aggregate_digest"] == output.development.aggregate_digest
        )
        assert request["output"]["holdout_aggregate_digest"] == output.holdout.aggregate_digest
        assert request["source"] != request["output"]
        assert request["controller_commit"] == COMMIT
        assert request["implementation_digest"] == FAKE_IMPLEMENTATION

    def test_input_sequence_order_does_not_change_the_request(self, sources):
        development, holdout = sources
        request, _d, _h = build_materialization_request(
            commit=COMMIT,
            implementation_digest=FAKE_IMPLEMENTATION,
            development=development,
            holdout=holdout,
        )
        reordered, _d, _h = build_materialization_request(
            commit=COMMIT,
            implementation_digest=FAKE_IMPLEMENTATION,
            development=tuple(reversed(development)),
            holdout=tuple(reversed(holdout)),
        )
        assert reordered == request

    def test_every_bound_input_changes_the_request_digest(self, sources):
        development, holdout = sources
        base, _d, _h = build_materialization_request(
            commit=COMMIT,
            implementation_digest=FAKE_IMPLEMENTATION,
            development=development,
            holdout=holdout,
        )
        changed_body = (
            OpaqueTask(development[0].task_id, development[0].content.replace(b"\n", b" \n", 1)),
            *development[1:],
        )
        variants = [
            build_materialization_request(
                commit="c" * 40,
                implementation_digest=FAKE_IMPLEMENTATION,
                development=development,
                holdout=holdout,
            ),
            build_materialization_request(
                commit=COMMIT,
                implementation_digest="sha256:" + "d" * 64,
                development=development,
                holdout=holdout,
            ),
            build_materialization_request(
                commit=COMMIT,
                implementation_digest=FAKE_IMPLEMENTATION,
                development=changed_body,
                holdout=holdout,
            ),
            build_materialization_request(
                commit=COMMIT,
                implementation_digest=FAKE_IMPLEMENTATION,
                development=development,
                holdout=source_stage("holdout", salt="x"),
            ),
        ]
        digests = {base["materialization_request_digest"]}
        for variant, _d, _h in variants:
            digests.add(variant["materialization_request_digest"])
        assert len(digests) == 5
        # Whitespace-only change to a source body: source aggregate moves,
        # the output (which re-serializes the scenario canonically) does not.
        changed, _d, _h = variants[2]
        assert changed["source"] != base["source"]
        assert changed["output"] == base["output"]

    def test_tampered_request_is_rejected(self, sources):
        request, _d, _h = build_materialization_request(
            commit=COMMIT,
            implementation_digest=FAKE_IMPLEMENTATION,
            development=sources[0],
            holdout=sources[1],
        )
        for mutate in (
            lambda r: r.__setitem__("materialization_request_digest", "sha256:" + "0" * 64),
            lambda r: r["algorithm"].__setitem__("seed", MEASURED_REPETITION_SEED + 1),
            lambda r: r["algorithm"].__setitem__("occurrence_rule", "import-order"),
            lambda r: r["output"].__setitem__("development_task_count", 19),
            lambda r: r.__setitem__("note", "x"),
            lambda r: r.pop("distribution"),
        ):
            trial = json.loads(json.dumps(request))
            mutate(trial)
            with pytest.raises(CustodyError) as caught:
                validate_request(trial)
            assert caught.value.reason == "request-invalid"

    def test_schema_is_valid_json_schema(self):
        schema = materialize.load_request_schema()
        jsonschema.validators.validator_for(schema).check_schema(schema)

    def test_approval_phrase_matches_the_required_format(self):
        digest = "sha256:" + "e" * 64
        assert APPROVAL_TEMPLATE == (
            "I approve sealed qualification-task materialization using request "
            "sha256:{request_digest}"
        )
        assert materialization_approval_phrase(digest) == (
            "I approve sealed qualification-task materialization using request sha256:" + "e" * 64
        )
        assert materialization_approval_phrase(digest) != approval_phrase(digest)
        with pytest.raises(CustodyError) as caught:
            materialization_approval_phrase("e" * 64)
        assert caught.value.reason == "approval-mismatch"


# --------------------------------------------------------------------------
# Source-shape failures
# --------------------------------------------------------------------------


class TestSourceShape:
    def _expect(self, reason: str, stage_tasks: Sequence[OpaqueTask], stage: str = "development"):
        with pytest.raises(CustodyError) as caught:
            materialize_stage(stage, stage_tasks)
        assert caught.value.reason == reason
        assert caught.value.__cause__ is None

    def _replace_first(self, tasks, content: bytes, task_id: str | None = None):
        first = tasks[0]
        return (OpaqueTask(task_id or first.task_id, content), *tasks[1:])

    def test_entry_with_an_instance_object_is_not_a_source(self, sources):
        first = json.loads(sources[0][0].content)
        first["instance"] = {"instance_seed": 1, "tracking_id": "X", "reported_minute": 1}
        self._expect("source-shape", self._replace_first(sources[0], json.dumps(first).encode()))

    def test_entry_with_envelope_fields_is_not_a_source(self, sources):
        first = json.loads(sources[0][0].content)
        first["payload_schema_version"] = PAYLOAD_SCHEMA_VERSION
        first["kind"] = PAYLOAD_KIND
        self._expect("source-shape", self._replace_first(sources[0], json.dumps(first).encode()))

    def test_task_id_must_equal_the_filename(self, sources):
        first = json.loads(sources[0][0].content)
        first["task_id"] = "syn-other-identifier"
        self._expect("source-shape", self._replace_first(sources[0], json.dumps(first).encode()))

    def test_missing_or_malformed_members_fail_closed(self, sources):
        first = json.loads(sources[0][0].content)
        del first["scenario"]
        self._expect("source-shape", self._replace_first(sources[0], json.dumps(first).encode()))
        self._expect("source-shape", self._replace_first(sources[0], b"[]"))
        self._expect("source-shape", self._replace_first(sources[0], b"\xff\xfe"))
        self._expect(
            "source-shape", self._replace_first(sources[0], b'{"task_id": NaN, "scenario": {}}')
        )
        duplicate = b'{"task_id": "a", "task_id": "b", "scenario": {"scenario_id": "x"}}'
        self._expect("source-shape", self._replace_first(sources[0], duplicate))
        first = json.loads(sources[0][0].content)
        first["scenario"]["scenario_id"] = 7
        self._expect("source-shape", self._replace_first(sources[0], json.dumps(first).encode()))

    def test_scenario_must_satisfy_the_production_payload_contract(self, sources):
        first = json.loads(sources[0][0].content)
        del first["scenario"]["root_cause_id"]
        self._expect("source-invalid", self._replace_first(sources[0], json.dumps(first).encode()))
        first = json.loads(sources[0][0].content)
        first["scenario"]["evidence_predicates"][0]["alternatives"][0]["tool"] = "not-a-tool"
        self._expect("source-invalid", self._replace_first(sources[0], json.dumps(first).encode()))

    def test_counts_and_separation(self, sources):
        self._expect("task-count", sources[0][:19])
        with pytest.raises(CustodyError) as caught:
            build_materialization_request(
                commit=COMMIT,
                implementation_digest=FAKE_IMPLEMENTATION,
                development=sources[0],
                holdout=(*sources[1][:19], sources[0][0]),
            )
        assert caught.value.reason == "stage-separation"
        with pytest.raises(CustodyError) as caught:
            materialize_stage("freeze", sources[0])
        assert caught.value.reason == "stage-separation"


# --------------------------------------------------------------------------
# Implementation digest
# --------------------------------------------------------------------------


class TestImplementationDigest:
    def test_running_digest_covers_exactly_the_declared_sources(self):
        parts = running_implementation_parts()
        assert tuple(path for path, _mode, _blob in parts) == IMPLEMENTATION_SOURCE_PATHS
        assert running_implementation_digest() == implementation_digest_from_sources(parts)
        for rel, _mode, blob in parts:
            assert (repository_root() / rel).read_bytes() == blob
        assert "src/blackwell_lab/cloud/sealed_materialize.py" in IMPLEMENTATION_SOURCE_PATHS
        assert "src/blackwell_lab/workload/sampling.py" in IMPLEMENTATION_SOURCE_PATHS
        assert "src/blackwell_lab/cloud/sealed_payload.py" in IMPLEMENTATION_SOURCE_PATHS
        assert "schemas/sealed-task-payload.schema.json" in IMPLEMENTATION_SOURCE_PATHS

    def test_digest_rejects_mutation_reorder_and_wrong_shape(self):
        parts = list(running_implementation_parts())
        base = implementation_digest_from_sources(parts)
        mutated = list(parts)
        mutated[-1] = (parts[-1][0], parts[-1][1], parts[-1][2] + b"\n# x\n")
        assert implementation_digest_from_sources(mutated) != base
        for bad in (
            parts[1:],
            [parts[1], parts[0], *parts[2:]],
            [(p, "100000", b) for p, _m, b in parts],
        ):
            with pytest.raises(CustodyError) as caught:
                implementation_digest_from_sources(bad)
            assert caught.value.reason == "implementation-digest-mismatch"

    def test_materializer_is_not_part_of_the_custody_controller_digest(self):
        assert "src/blackwell_lab/cloud/sealed_materialize.py" not in CONTROLLER_SOURCE_PATHS
        assert not set(CONTROLLER_SOURCE_PATHS) & set(IMPLEMENTATION_SOURCE_PATHS)


# --------------------------------------------------------------------------
# Git-bound operations (clean checkout required, as for custody tests)
# --------------------------------------------------------------------------


@pytest.fixture
def external(tmp_path, sources):
    development = write_bundle(tmp_path / "development-source", sources[0])
    holdout = write_bundle(tmp_path / "holdout-source", sources[1])
    return {
        "development": development,
        "holdout": holdout,
        "output": tmp_path / "materialized",
        "snapshot": (tree_snapshot(development), tree_snapshot(holdout)),
    }


def _assert_sources_untouched(external) -> None:
    assert tree_snapshot(external["development"]) == external["snapshot"][0]
    assert tree_snapshot(external["holdout"]) == external["snapshot"][1]


class TestOperations:
    def test_prepare_then_materialize_then_custody_import_then_p2_load(self, external, tmp_path):
        repo, commit = _lab()
        development = load_bundle_directory(external["development"], repo=repo)
        holdout = load_bundle_directory(external["holdout"], repo=repo)
        request = prepare_materialization(
            repo=repo, expected_commit=commit, development=development, holdout=holdout
        )
        assert request["controller_commit"] == commit
        assert request["implementation_digest"] == running_implementation_digest()
        assert not external["output"].exists()
        _assert_sources_untouched(external)

        record = materialize_authorized_bundles(
            repo=repo,
            output_root=external["output"],
            expected_commit=commit,
            expected_implementation_digest=request["implementation_digest"],
            expected_request_digest=request["materialization_request_digest"],
            approval=materialization_approval_phrase(request["materialization_request_digest"]),
            development=development,
            holdout=holdout,
        )
        assert record["status"] == "pass"
        assert record["request"] == request
        _assert_sources_untouched(external)

        out = external["output"]
        assert sorted(p.name for p in out.iterdir()) == ["development", "holdout", RECORD_NAME]
        assert os.stat(out).st_mode & 0o777 == 0o700
        for stage in ("development", "holdout"):
            assert os.stat(out / stage).st_mode & 0o777 == 0o700
            names = sorted(p.name for p in (out / stage).iterdir())
            assert names == sorted(
                t.task_id for t in (development if stage == "development" else holdout)
            )
            for path in (out / stage).iterdir():
                assert os.stat(path).st_mode & 0o777 == 0o600
        assert os.stat(out / RECORD_NAME).st_mode & 0o777 == 0o600
        stored = json.loads((out / RECORD_NAME).read_text(encoding="utf-8"))
        assert stored == record
        assert_content_free(
            json.dumps(stored), development, holdout, paths=[out, external["development"]]
        )

        # The materialized bundles are ordinary custody input: the unchanged
        # D-0023 tool prepares and imports them, and the unchanged D-0024
        # adapter decodes the stage with the generator-produced surfaces.
        written_dev = load_bundle_directory(out / "development", repo=repo)
        written_hold = load_bundle_directory(out / "holdout", repo=repo)
        output = validate_bundles(written_dev, written_hold)
        assert (
            output.development.aggregate_digest == request["output"]["development_aggregate_digest"]
        )
        assert output.holdout.aggregate_digest == request["output"]["holdout_aggregate_digest"]
        import_request = prepare_import_request(
            repo=repo, expected_commit=commit, development=written_dev, holdout=written_hold
        )
        custody_root = tmp_path / "custody"
        receipt = import_authorized_set(
            repo=repo,
            output_root=custody_root,
            expected_commit=commit,
            expected_controller_digest=import_request["controller_digest"],
            expected_request_digest=import_request["import_request_digest"],
            approval=approval_phrase(import_request["import_request_digest"]),
            development=written_dev,
            holdout=written_hold,
        )
        assert (
            receipt.development_aggregate_digest
            == request["output"]["development_aggregate_digest"]
        )
        manifest = json.loads((custody_root / "manifest.json").read_text(encoding="utf-8"))
        from blackwell_lab.cloud.sealed_binding import SealedSetBinding

        for stage in ("development", "holdout"):
            binding = SealedSetBinding.from_config(
                binding_for(custody_root, manifest, stage), stage=stage
            )
            loaded = load_sealed_stage(
                binding, custody_dir=custody_root, repo=repo, canonical_commit=commit
            )
            spec = stage_spec(stage)
            production = generate_task_instances(
                spec["template_ids"], spec["tasks"], MEASURED_REPETITION_SEED
            )
            assert surfaces(loaded.instances) == surfaces(production)
            assert list(loaded.task_ids) == sorted(loaded.task_ids)

    def test_destination_is_never_overwritten(self, external):
        repo, commit = _lab()
        development = load_bundle_directory(external["development"], repo=repo)
        holdout = load_bundle_directory(external["holdout"], repo=repo)
        request = prepare_materialization(
            repo=repo, expected_commit=commit, development=development, holdout=holdout
        )
        out = external["output"]
        os.mkdir(out, 0o700)
        before = tree_snapshot(out)
        kwargs = dict(
            repo=repo,
            output_root=out,
            expected_commit=commit,
            expected_implementation_digest=request["implementation_digest"],
            expected_request_digest=request["materialization_request_digest"],
            approval=materialization_approval_phrase(request["materialization_request_digest"]),
            development=development,
            holdout=holdout,
        )
        with pytest.raises(CustodyError) as caught:
            materialize_authorized_bundles(**kwargs)
        assert caught.value.reason == "destination-exists"
        assert tree_snapshot(out) == before
        (out / "keep").write_text("x", encoding="utf-8")
        before = tree_snapshot(out)
        with pytest.raises(CustodyError) as caught:
            materialize_authorized_bundles(**kwargs)
        assert caught.value.reason == "destination-exists"
        assert tree_snapshot(out) == before
        _assert_sources_untouched(external)

    @pytest.mark.parametrize(
        ("field", "value", "reason"),
        [
            ("approval", "I approve something else", "approval-mismatch"),
            ("expected_request_digest", "sha256:" + "0" * 64, "request-mismatch"),
            (
                "expected_implementation_digest",
                "sha256:" + "0" * 64,
                "implementation-digest-mismatch",
            ),
            ("expected_commit", "0" * 40, "commit-mismatch"),
        ],
    )
    def test_binding_mismatches_write_nothing(self, external, field, value, reason):
        repo, commit = _lab()
        development = load_bundle_directory(external["development"], repo=repo)
        holdout = load_bundle_directory(external["holdout"], repo=repo)
        request = prepare_materialization(
            repo=repo, expected_commit=commit, development=development, holdout=holdout
        )
        kwargs = dict(
            repo=repo,
            output_root=external["output"],
            expected_commit=commit,
            expected_implementation_digest=request["implementation_digest"],
            expected_request_digest=request["materialization_request_digest"],
            approval=materialization_approval_phrase(request["materialization_request_digest"]),
            development=development,
            holdout=holdout,
        )
        kwargs[field] = value
        with pytest.raises(CustodyError) as caught:
            materialize_authorized_bundles(**kwargs)
        assert caught.value.reason == reason
        assert not external["output"].exists()
        _assert_sources_untouched(external)

    def test_custody_approval_phrase_does_not_authorize_materialization(self, external):
        repo, commit = _lab()
        development = load_bundle_directory(external["development"], repo=repo)
        holdout = load_bundle_directory(external["holdout"], repo=repo)
        request = prepare_materialization(
            repo=repo, expected_commit=commit, development=development, holdout=holdout
        )
        with pytest.raises(CustodyError) as caught:
            materialize_authorized_bundles(
                repo=repo,
                output_root=external["output"],
                expected_commit=commit,
                expected_implementation_digest=request["implementation_digest"],
                expected_request_digest=request["materialization_request_digest"],
                approval=approval_phrase(request["materialization_request_digest"]),
                development=development,
                holdout=holdout,
            )
        assert caught.value.reason == "approval-mismatch"
        assert not external["output"].exists()

    def test_output_inside_a_repository_is_refused(self, external):
        repo, commit = _lab()
        development = load_bundle_directory(external["development"], repo=repo)
        holdout = load_bundle_directory(external["holdout"], repo=repo)
        request = prepare_materialization(
            repo=repo, expected_commit=commit, development=development, holdout=holdout
        )
        with pytest.raises(CustodyError) as caught:
            materialize_authorized_bundles(
                repo=repo,
                output_root=repo / "materialized-should-not-exist",
                expected_commit=commit,
                expected_implementation_digest=request["implementation_digest"],
                expected_request_digest=request["materialization_request_digest"],
                approval=materialization_approval_phrase(request["materialization_request_digest"]),
                development=development,
                holdout=holdout,
            )
        assert caught.value.reason == "repository-path"
        assert not (repo / "materialized-should-not-exist").exists()

    def test_source_that_already_carries_instances_is_refused_before_any_write(
        self, external, tmp_path
    ):
        repo, commit = _lab()
        development = load_bundle_directory(external["development"], repo=repo)
        holdout = load_bundle_directory(external["holdout"], repo=repo)
        _request, dev, _hold = build_materialization_request(
            commit=commit,
            implementation_digest=running_implementation_digest(),
            development=development,
            holdout=holdout,
        )
        already = write_bundle(tmp_path / "already-materialized", dev.tasks)
        with pytest.raises(CustodyError) as caught:
            prepare_materialization(
                repo=repo,
                expected_commit=commit,
                development=load_bundle_directory(already, repo=repo),
                holdout=holdout,
            )
        assert caught.value.reason == "source-shape"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "blackwell_lab.cloud.sealed_materialize", *args],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(repository_root() / "src")},
    )


class TestCli:
    def test_approval_phrase_is_printed_without_approving(self):
        completed = run_cli("approval-phrase")
        assert completed.returncode == 0
        assert completed.stdout.strip() == APPROVAL_TEMPLATE

    def test_prepare_and_materialize_print_content_free_json(self, external, sources):
        repo, commit = _lab()
        prepared = run_cli(
            "prepare-materialization",
            "--repo",
            str(repo),
            "--commit",
            commit,
            "--development",
            str(external["development"]),
            "--holdout",
            str(external["holdout"]),
        )
        assert prepared.returncode == 0, prepared.stderr
        request = json.loads(prepared.stdout)
        validate_request(request)
        assert_content_free(
            prepared.stdout + prepared.stderr,
            *sources,
            paths=[external["development"], external["holdout"], external["output"]],
        )
        assert not external["output"].exists()

        blocked = run_cli(
            "materialize-bundles",
            "--repo",
            str(repo),
            "--commit",
            commit,
            "--development",
            str(external["development"]),
            "--holdout",
            str(external["holdout"]),
            "--output",
            str(external["output"]),
            "--implementation-digest",
            request["implementation_digest"],
            "--request-digest",
            request["materialization_request_digest"],
            "--approve",
            "I approve nothing",
        )
        assert blocked.returncode == 1
        assert blocked.stdout == ""
        assert blocked.stderr.strip() == "BLOCKED: approval-mismatch"
        assert not external["output"].exists()

        written = run_cli(
            "materialize-bundles",
            "--repo",
            str(repo),
            "--commit",
            commit,
            "--development",
            str(external["development"]),
            "--holdout",
            str(external["holdout"]),
            "--output",
            str(external["output"]),
            "--implementation-digest",
            request["implementation_digest"],
            "--request-digest",
            request["materialization_request_digest"],
            "--approve",
            materialization_approval_phrase(request["materialization_request_digest"]),
        )
        assert written.returncode == 0, written.stderr
        record = json.loads(written.stdout)
        assert record["status"] == "pass"
        assert record["request"] == request
        assert_content_free(
            written.stdout + written.stderr,
            *sources,
            paths=[external["development"], external["holdout"], external["output"]],
        )
        assert (external["output"] / RECORD_NAME).is_file()
        _assert_sources_untouched(external)

    def test_blocked_reasons_never_echo_paths(self, tmp_path):
        repo, commit = _lab()
        missing = tmp_path / "absent-bundle"
        completed = run_cli(
            "prepare-materialization",
            "--repo",
            str(repo),
            "--commit",
            commit,
            "--development",
            str(missing),
            "--holdout",
            str(missing),
        )
        assert completed.returncode == 1
        assert completed.stdout == ""
        assert completed.stderr.startswith("BLOCKED: ")
        assert str(tmp_path) not in completed.stderr
