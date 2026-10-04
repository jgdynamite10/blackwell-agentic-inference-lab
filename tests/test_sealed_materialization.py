"""Tests for the offline sealed-task materialization (decision D-0025).

Every source entry here is synthetic: a public catalog scenario re-serialized
with an artificial historical task id. Nothing real is read. Tests that call
the git-bound ``prepare``/``materialize``/``import`` operations run against
this checkout and, like the custody tests, require a clean committed
worktree.
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
from blackwell_lab.cloud.sealed_binding import SealedSetBinding, load_sealed_stage
from blackwell_lab.cloud.sealed_materialize import (
    APPROVAL_TEMPLATE,
    IMPLEMENTATION_SOURCE_PATHS,
    RECORD_NAME,
    SCHEDULE_RULE,
    build_materialization_request,
    fixed_task_id,
    fixed_task_ids,
    frozen_schedule,
    implementation_digest_from_sources,
    import_materialized_bundles,
    load_materialized_bundle,
    materialization_approval_phrase,
    materialize_authorized_bundles,
    materialize_stage,
    prepare_materialization,
    prepare_materialized_import,
    require_frozen_sources,
    running_implementation_digest,
    running_implementation_parts,
    schedule_tuples,
    validate_request,
)
from blackwell_lab.cloud.sealed_payload import (
    PAYLOAD_KIND,
    PAYLOAD_SCHEMA_VERSION,
    decode_sealed_task,
    encode_sealed_task,
    scenario_document,
)
from blackwell_lab.paths import repository_root
from blackwell_lab.sealed_sets.custody import load_bundle_directory
from blackwell_lab.sealed_sets.model import (
    CONTROLLER_SOURCE_PATHS,
    CustodyError,
    OpaqueTask,
    approval_phrase,
    set_identity_for,
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
STAGES = ("development", "holdout")


def _lab() -> tuple[Path, str]:
    repo = repository_root()
    head = subprocess.run(
        [GIT, "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, head


def _source_bytes(task_id: str, scenario: dict) -> bytes:
    document = {"task_id": task_id, "scenario": scenario}
    return (json.dumps(document, sort_keys=True, indent=1) + "\n").encode("utf-8")


def _entry(task_id: str, scenario: dict) -> OpaqueTask:
    return OpaqueTask(task_id, _source_bytes(task_id, scenario))


def source_stage(
    stage: str,
    distribution: Counter[str] | dict[str, int] | None = None,
    *,
    salt: str = "",
    grouped: bool = True,
) -> tuple[OpaqueTask, ...]:
    """Synthetic source entries for ``stage``.

    ``grouped`` (default) lists all copies of one scenario consecutively —
    the opposite of the generator's round-robin — so no test can pass by
    accident of input order.
    """
    distribution = distribution or expected_stage_distribution(stage)
    tasks: list[OpaqueTask] = []
    if grouped:
        index = 0
        for template_id in sorted(distribution):
            for _ in range(distribution[template_id]):
                tasks.append(
                    _entry(
                        f"hist-{salt}{stage[:3]}-{index:02d}",
                        scenario_document(CATALOG[template_id]),
                    )
                )
                index += 1
    else:
        for index, instance in enumerate(frozen_schedule(stage)):
            tasks.append(
                _entry(
                    f"hist-{salt}{stage[:3]}-{index:02d}",
                    scenario_document(CATALOG[instance.template_id]),
                )
            )
    assert len(tasks) == sum(distribution.values())
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


def production_schedule(stage: str):
    spec = stage_spec(stage)
    return generate_task_instances(spec["template_ids"], spec["tasks"], MEASURED_REPETITION_SEED)


def private_strings(*stages: Sequence[OpaqueTask]) -> set[str]:
    """Strings that must never appear in public output: source ids and answers."""
    secrets: set[str] = set()
    for tasks in stages:
        for task in tasks:
            secrets.add(task.task_id)
            scenario = json.loads(task.content)["scenario"]
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


def expect_reason(reason: str, call, *args, **kwargs) -> None:
    with pytest.raises(CustodyError) as caught:
        call(*args, **kwargs)
    assert caught.value.reason == reason
    assert caught.value.__cause__ is None


@pytest.fixture
def sources() -> tuple[tuple[OpaqueTask, ...], tuple[OpaqueTask, ...]]:
    return source_stage("development"), source_stage("holdout")


def request_for(development, holdout):
    return build_materialization_request(
        commit=COMMIT,
        implementation_digest=FAKE_IMPLEMENTATION,
        development=development,
        holdout=holdout,
    )


# --------------------------------------------------------------------------
# One-call generator schedule, seed, fixed ids, ordered equality
# --------------------------------------------------------------------------


class TestFrozenSchedule:
    def test_seed_is_20260907_and_the_measured_repetition_seed(self, sources):
        request, _dev, _hold = request_for(*sources)
        assert MEASURED_REPETITION_SEED == 20260907
        assert MEASURED_REPETITION_SEED == FROZEN_SEED + 1
        assert request["algorithm"]["seed"] == 20260907
        assert request["algorithm"]["schedule_rule"] == SCHEDULE_RULE
        assert request["algorithm"]["generator"] == (
            "blackwell_lab.workload.sampling.generate_task_instances"
        )
        assert request["algorithm"]["development_template_ids"] == list(DEVELOPMENT_TEMPLATE_IDS)
        assert request["algorithm"]["holdout_template_ids"] == list(HOLDOUT_TEMPLATE_IDS)

    @pytest.mark.parametrize("stage", STAGES)
    def test_schedule_is_exactly_one_production_call(self, stage, monkeypatch):
        calls: list[tuple] = []
        original = materialize.generate_task_instances

        def spy(template_ids, tasks_per_repetition, seed):
            calls.append((tuple(template_ids), tasks_per_repetition, seed))
            return original(template_ids, tasks_per_repetition, seed)

        monkeypatch.setattr(materialize, "generate_task_instances", spy)
        materialized = materialize_stage(stage, source_stage(stage))
        spec = stage_spec(stage)
        expected_call = (tuple(spec["template_ids"]), spec["tasks"], 20260907)
        # Exactly one call with exactly the production arguments; never per template.
        assert calls == [expected_call]
        assert materialized.schedule == tuple(production_schedule(stage))

    @pytest.mark.parametrize("stage", STAGES)
    def test_ordered_tuples_equal_the_production_generator_sequence(self, stage):
        materialized = materialize_stage(stage, source_stage(stage))
        items = decoded(materialized.tasks)
        production = production_schedule(stage)
        assert schedule_tuples(item.instance for item in items) == schedule_tuples(production)
        # Order-sensitive: the same multiset in a different order is not equal.
        grouped = sorted(production, key=lambda item: (item.template_id, item.instance_id))
        assert schedule_tuples(grouped) != schedule_tuples(production)
        assert [item.scenario.scenario_id for item in items] == [
            instance.template_id for instance in production
        ]
        for slot, (item, instance) in enumerate(zip(items, production, strict=True)):
            assert instance.instance_id == f"{instance.template_id}#{instance.instance_id[-4:]}"
            assert item.instance.instance_id == fixed_task_id(stage, slot)
            assert item.instance.template_id == instance.template_id

    @pytest.mark.parametrize("stage", STAGES)
    def test_fixed_output_ids_follow_the_global_generator_slot(self, stage):
        ids = fixed_task_ids(stage)
        assert ids == tuple(f"sealed-{stage}-{slot:04d}" for slot in range(20))
        assert ids[0] == f"sealed-{stage}-0000" and ids[-1] == f"sealed-{stage}-0019"
        assert list(ids) == sorted(ids)
        materialized = materialize_stage(stage, source_stage(stage))
        assert materialized.task_ids == ids
        assert [task.task_id for task in materialized.tasks] == sorted(materialized.task_ids)
        expect_reason("generator-mismatch", fixed_task_id, stage, 20)
        expect_reason("generator-mismatch", fixed_task_id, stage, -1)
        expect_reason("generator-mismatch", fixed_task_id, "freeze", 0)

    @pytest.mark.parametrize("stage", STAGES)
    def test_payloads_are_encoder_output_with_catalog_scenarios(self, stage):
        materialized = materialize_stage(stage, source_stage(stage))
        for slot, (task, instance) in enumerate(
            zip(materialized.tasks, production_schedule(stage), strict=True)
        ):
            expected = encode_sealed_task(
                task_id=fixed_task_id(stage, slot),
                scenario=CATALOG[instance.template_id],
                instance_seed=instance.instance_seed,
                tracking_id=instance.tracking_id,
                reported_minute=instance.reported_minute,
            )
            assert task.content == expected
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
            assert payload["scenario"] == json.loads(
                json.dumps(scenario_document(CATALOG[instance.template_id]))
            )

    def test_distribution_is_the_frozen_one(self, sources):
        request, dev, hold = request_for(*sources)
        assert Counter(dev.distribution) == expected_stage_distribution("development")
        assert Counter(hold.distribution) == expected_stage_distribution("holdout")
        assert request["distribution"]["development"]["counts"] == [4, 4, 3, 3, 3, 3]
        assert request["distribution"]["holdout"]["counts"] == [5, 5, 5, 5]


# --------------------------------------------------------------------------
# Source ids and input order never influence the output
# --------------------------------------------------------------------------


class TestSourceIndependence:
    @pytest.mark.parametrize("stage", STAGES)
    def test_scrambled_legal_source_ids_do_not_change_the_output(self, stage):
        base = source_stage(stage)
        scrambled = tuple(
            _entry(f"zz-{(index * 7) % 20:02d}-scrambled", json.loads(task.content)["scenario"])
            for index, task in enumerate(base)
        )
        assert {t.task_id for t in scrambled}.isdisjoint({t.task_id for t in base})
        first = materialize_stage(stage, base)
        second = materialize_stage(stage, scrambled)
        assert first.tasks == second.tasks
        assert first.task_ids == fixed_task_ids(stage)
        source_ids = {t.task_id for t in base} | {t.task_id for t in scrambled}
        assert not (set(first.task_ids) & source_ids)

    def test_grouped_and_round_robin_inputs_produce_identical_output(self):
        grouped = source_stage("development", grouped=True)
        round_robin = source_stage("development", grouped=False)
        assert [json.loads(t.content)["scenario"]["scenario_id"] for t in grouped] != [
            json.loads(t.content)["scenario"]["scenario_id"] for t in round_robin
        ]
        assert materialize_stage("development", grouped).tasks == (
            materialize_stage("development", round_robin).tasks
        )

    def test_input_sequence_order_and_source_ids_change_only_the_source_block(self, sources):
        development, holdout = sources
        base, _d, _h = request_for(development, holdout)
        reversed_inputs, _d, _h = request_for(
            tuple(reversed(development)), tuple(reversed(holdout))
        )
        assert reversed_inputs == base
        renamed, _d, _h = request_for(
            source_stage("development", salt="x"), source_stage("holdout", salt="y")
        )
        assert renamed["source"] != base["source"]
        assert renamed["output"] == base["output"]
        assert renamed["distribution"] == base["distribution"]
        assert renamed["materialization_request_digest"] != base["materialization_request_digest"]

    def test_output_is_a_pure_function_of_catalog_and_generator(self, sources):
        _request, dev, hold = request_for(*sources)
        for stage, materialized in (("development", dev), ("holdout", hold)):
            expected = tuple(
                OpaqueTask(
                    fixed_task_id(stage, slot),
                    encode_sealed_task(
                        task_id=fixed_task_id(stage, slot),
                        scenario=CATALOG[instance.template_id],
                        instance_seed=instance.instance_seed,
                        tracking_id=instance.tracking_id,
                        reported_minute=instance.reported_minute,
                    ),
                )
                for slot, instance in enumerate(production_schedule(stage))
            )
            assert materialized.tasks == expected


# --------------------------------------------------------------------------
# Exact frozen source gate — adversarial rejections
# --------------------------------------------------------------------------


class TestFrozenSourceGate:
    def _dev(self, tasks):
        return materialize_stage("development", tasks)

    def test_frozen_sources_pass(self, sources):
        assert len(require_frozen_sources("development", sources[0])) == 20
        assert len(require_frozen_sources("holdout", sources[1])) == 20

    def test_holdout_templates_supplied_as_development_are_refused(self):
        twenty_holdout_scenarios = source_stage("holdout")
        assert len(twenty_holdout_scenarios) == 20
        expect_reason("scenario-set-mismatch", self._dev, twenty_holdout_scenarios)

    def test_development_and_holdout_swapped_are_refused(self, sources):
        development, holdout = sources
        expect_reason("scenario-set-mismatch", materialize_stage, "development", holdout)
        expect_reason("scenario-set-mismatch", materialize_stage, "holdout", development)
        expect_reason("scenario-set-mismatch", request_for, holdout, development)

    def test_one_modified_catalog_field_is_refused(self, sources):
        development = list(sources[0])
        document = json.loads(development[3].content)
        document["scenario"]["root_cause_summary"] += " (edited)"
        development[3] = OpaqueTask(development[3].task_id, json.dumps(document).encode())
        expect_reason("scenario-mismatch", self._dev, development)
        document = json.loads(sources[0][0].content)
        document["scenario"]["metrics"] = {}
        expect_reason(
            "scenario-mismatch",
            self._dev,
            (OpaqueTask(sources[0][0].task_id, json.dumps(document).encode()), *sources[0][1:]),
        )

    def test_substituted_scenario_with_a_frozen_id_is_refused(self, sources):
        document = json.loads(sources[0][0].content)
        other = scenario_document(CATALOG[DEVELOPMENT_TEMPLATE_IDS[1]])
        other["scenario_id"] = document["scenario"]["scenario_id"]
        document["scenario"] = other
        expect_reason(
            "scenario-mismatch",
            self._dev,
            (OpaqueTask(sources[0][0].task_id, json.dumps(document).encode()), *sources[0][1:]),
        )

    def test_twenty_copies_of_one_scenario_are_refused(self):
        expect_reason(
            "distribution-mismatch",
            self._dev,
            source_stage("development", {DEVELOPMENT_TEMPLATE_IDS[0]: 20}),
        )

    def test_unknown_scenario_id_is_refused(self, sources):
        document = json.loads(sources[0][0].content)
        document["scenario"]["scenario_id"] = "unknown-scenario-001"
        expect_reason(
            "scenario-set-mismatch",
            self._dev,
            (OpaqueTask(sources[0][0].task_id, json.dumps(document).encode()), *sources[0][1:]),
        )

    def test_eleven_nine_distribution_is_refused(self):
        expect_reason(
            "distribution-mismatch",
            materialize_stage,
            "holdout",
            source_stage("holdout", {HOLDOUT_TEMPLATE_IDS[0]: 11, HOLDOUT_TEMPLATE_IDS[1]: 9}),
        )
        expected = expected_stage_distribution("development")
        shifted = dict(expected)
        shifted[DEVELOPMENT_TEMPLATE_IDS[0]] -= 1
        shifted[DEVELOPMENT_TEMPLATE_IDS[2]] += 1
        expect_reason("distribution-mismatch", self._dev, source_stage("development", shifted))

    def test_nineteen_and_twenty_one_tasks_are_refused(self, sources):
        expect_reason("task-count", self._dev, sources[0][:19])
        extra = _entry("hist-extra-00", json.loads(sources[0][0].content)["scenario"])
        expect_reason("task-count", self._dev, (*sources[0], extra))

    def test_duplicate_source_id_is_refused(self, sources):
        duplicate = (sources[0][0], *sources[0][1:19], sources[0][0])
        expect_reason("duplicate-id", self._dev, duplicate)
        shared = (*sources[1][:19], sources[0][0])
        expect_reason("stage-separation", request_for, sources[0], shared)

    def test_source_shape_failures(self, sources):
        first = json.loads(sources[0][0].content)

        def replace(content: bytes):
            return (OpaqueTask(sources[0][0].task_id, content), *sources[0][1:])

        with_instance = dict(first)
        with_instance["instance"] = {"instance_seed": 1, "tracking_id": "X", "reported_minute": 1}
        expect_reason("source-shape", self._dev, replace(json.dumps(with_instance).encode()))
        enveloped = dict(first)
        enveloped["payload_schema_version"] = PAYLOAD_SCHEMA_VERSION
        enveloped["kind"] = PAYLOAD_KIND
        expect_reason("source-shape", self._dev, replace(json.dumps(enveloped).encode()))
        renamed = dict(first)
        renamed["task_id"] = "hist-other-identifier"
        expect_reason("source-shape", self._dev, replace(json.dumps(renamed).encode()))
        expect_reason("source-shape", self._dev, replace(b"[]"))
        expect_reason("source-shape", self._dev, replace(b"\xff\xfe"))
        expect_reason("source-shape", self._dev, replace(b'{"task_id": NaN, "scenario": {}}'))
        expect_reason(
            "source-shape",
            self._dev,
            replace(b'{"task_id": "a", "task_id": "b", "scenario": {"scenario_id": "x"}}'),
        )
        already = materialize_stage("development", sources[0]).tasks
        expect_reason("source-shape", self._dev, already)

    def test_unsafe_source_id_is_refused_by_the_source_aggregate(self, sources):
        bad = (
            OpaqueTask(
                "Bad ID", _source_bytes("Bad ID", json.loads(sources[0][0].content)["scenario"])
            ),
        )
        expect_reason("unsafe-id", request_for, (*sources[0][1:], *bad), sources[1])


# --------------------------------------------------------------------------
# Request binding
# --------------------------------------------------------------------------


class TestRequest:
    def test_request_is_schema_valid_content_free_and_deterministic(self, sources):
        development, holdout = sources
        request, dev, hold = request_for(development, holdout)
        again, _d, _h = request_for(development, holdout)
        assert request == again
        validate_request(request)
        assert_content_free(json.dumps(request), development, holdout)
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
        assert request["output"]["set_identity"] == set_identity_for(dev.tasks, hold.tasks)
        assert request["output"]["ordering"] == "generator-schedule"
        assert request["controller_commit"] == COMMIT
        assert request["implementation_digest"] == FAKE_IMPLEMENTATION

    def test_every_bound_input_changes_the_request_digest(self, sources):
        development, holdout = sources
        base, _d, _h = request_for(development, holdout)
        digests = {base["materialization_request_digest"]}
        for variant in (
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
            request_for(source_stage("development", salt="x"), holdout),
            request_for(development, source_stage("holdout", salt="y")),
        ):
            digests.add(variant[0]["materialization_request_digest"])
        assert len(digests) == 5

    def test_tampered_request_is_rejected(self, sources):
        request, _d, _h = request_for(*sources)
        for mutate in (
            lambda r: r.__setitem__("materialization_request_digest", "sha256:" + "0" * 64),
            lambda r: r["algorithm"].__setitem__("seed", 20260906),
            lambda r: r["algorithm"].__setitem__(
                "schedule_rule", "task-id-sorted-per-template-counter"
            ),
            lambda r: r["algorithm"].__setitem__("task_id_rule", "source-id"),
            lambda r: r["algorithm"]["development_template_ids"].reverse(),
            lambda r: r["distribution"]["holdout"].__setitem__("counts", [11, 9]),
            lambda r: r["output"].__setitem__("ordering", "task-id-sorted"),
            lambda r: r["output"].__setitem__("development_task_count", 19),
            lambda r: r.__setitem__("note", "x"),
            lambda r: r.pop("distribution"),
        ):
            trial = json.loads(json.dumps(request))
            mutate(trial)
            expect_reason("request-invalid", validate_request, trial)

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
        expect_reason("approval-mismatch", materialization_approval_phrase, "e" * 64)


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
        assert set(IMPLEMENTATION_SOURCE_PATHS) == {
            "schemas/sealed-task-payload.schema.json",
            "src/blackwell_lab/cloud/sealed_materialize.py",
            "src/blackwell_lab/cloud/sealed_payload.py",
            "src/blackwell_lab/workload/sampling.py",
        }

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
            expect_reason("implementation-digest-mismatch", implementation_digest_from_sources, bad)

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


def _prepare(external):
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
    return repo, commit, request, kwargs


class TestOperations:
    def test_end_to_end_through_the_unchanged_importer_and_loader(self, external, tmp_path):
        repo, commit, request, kwargs = _prepare(external)
        assert request["controller_commit"] == commit
        assert request["implementation_digest"] == running_implementation_digest()
        assert not external["output"].exists()
        _assert_sources_untouched(external)

        record = materialize_authorized_bundles(**kwargs)
        assert record["status"] == "pass" and record["request"] == request
        _assert_sources_untouched(external)

        out = external["output"]
        assert sorted(p.name for p in out.iterdir()) == ["development", "holdout", RECORD_NAME]
        assert os.stat(out).st_mode & 0o777 == 0o700
        for stage in STAGES:
            assert os.stat(out / stage).st_mode & 0o777 == 0o700
            assert sorted(p.name for p in (out / stage).iterdir()) == list(fixed_task_ids(stage))
            for path in (out / stage).iterdir():
                assert os.stat(path).st_mode & 0o777 == 0o600
        stored = json.loads((out / RECORD_NAME).read_text(encoding="utf-8"))
        assert stored == record
        assert_content_free(
            json.dumps(stored),
            kwargs["development"],
            kwargs["holdout"],
            paths=[out, external["development"]],
        )

        # Unchanged D-0023 importer, fed in fixed order -> identity equality.
        import_request = prepare_materialized_import(
            repo=repo,
            expected_commit=commit,
            development=out / "development",
            holdout=out / "holdout",
        )
        assert import_request["set_identity"] == request["output"]["set_identity"]
        assert (
            import_request["development_aggregate_digest"]
            == (request["output"]["development_aggregate_digest"])
        )
        assert (
            import_request["holdout_aggregate_digest"]
            == request["output"]["holdout_aggregate_digest"]
        )
        custody_root = tmp_path / "custody"
        receipt = import_materialized_bundles(
            repo=repo,
            output_root=custody_root,
            expected_commit=commit,
            expected_controller_digest=import_request["controller_digest"],
            expected_request_digest=import_request["import_request_digest"],
            approval=approval_phrase(import_request["import_request_digest"]),
            development=out / "development",
            holdout=out / "holdout",
        )
        manifest = json.loads((custody_root / "manifest.json").read_text(encoding="utf-8"))
        assert receipt.set_identity == request["output"]["set_identity"]
        assert manifest["set_identity"] == request["output"]["set_identity"]
        for stage in STAGES:
            index = json.loads(
                (custody_root / "private" / stage / "index.json").read_text(encoding="utf-8")
            )
            assert [row["task_id"] for row in index["rows"]] == list(fixed_task_ids(stage))
            # Custody index order == fixed-id order == import (schedule) order.
            assert [row["content_digest"] for row in index["rows"]] == index["order"]

        # Unchanged D-0024 loader: task-id order == scenario order == generator order.
        for stage in STAGES:
            binding = SealedSetBinding.from_config(
                binding_for(custody_root, manifest, stage), stage=stage
            )
            loaded = load_sealed_stage(
                binding, custody_dir=custody_root, repo=repo, canonical_commit=commit
            )
            production = production_schedule(stage)
            assert list(loaded.task_ids) == list(fixed_task_ids(stage))
            assert list(loaded.task_ids) == sorted(loaded.task_ids)
            assert [task.scenario.scenario_id for task in loaded.tasks] == [
                instance.template_id for instance in production
            ]
            assert schedule_tuples(loaded.instances) == schedule_tuples(production)

    def test_fixed_output_is_independent_of_filenames_iteration_and_creation_order(
        self, tmp_path, monkeypatch
    ):
        repo, commit = _lab()
        baseline_request = None
        baseline_tasks = None
        variants = [
            (source_stage("development"), source_stage("holdout")),
            (source_stage("development", salt="q"), source_stage("holdout", salt="r")),
            (source_stage("development", grouped=False), source_stage("holdout", grouped=False)),
        ]
        for index, (development, holdout) in enumerate(variants):
            root = tmp_path / f"case-{index}"
            root.mkdir()
            order = list(development) if index % 2 == 0 else list(reversed(development))
            dev_dir = write_bundle(root / "development-source", order)
            hold_dir = write_bundle(root / "holdout-source", holdout)
            if index == 1:
                original = Path.iterdir

                def reversed_iterdir(self, _original=original):
                    return iter(sorted(_original(self), reverse=True))

                monkeypatch.setattr(Path, "iterdir", reversed_iterdir)
            dev = load_bundle_directory(dev_dir, repo=repo)
            hold = load_bundle_directory(hold_dir, repo=repo)
            monkeypatch.undo()
            request = prepare_materialization(
                repo=repo, expected_commit=commit, development=dev, holdout=hold
            )
            out = root / "materialized"
            materialize_authorized_bundles(
                repo=repo,
                output_root=out,
                expected_commit=commit,
                expected_implementation_digest=request["implementation_digest"],
                expected_request_digest=request["materialization_request_digest"],
                approval=materialization_approval_phrase(request["materialization_request_digest"]),
                development=dev,
                holdout=hold,
            )
            written = tuple(load_materialized_bundle(out / stage, repo=repo) for stage in STAGES)
            if baseline_request is None:
                baseline_request, baseline_tasks = request, written
            else:
                assert request["output"] == baseline_request["output"]
                assert written == baseline_tasks
            for stage, tasks in zip(STAGES, written, strict=True):
                assert schedule_tuples(item.instance for item in decoded(tasks)) == schedule_tuples(
                    production_schedule(stage)
                )

    def test_materialized_loader_orders_by_fixed_id_not_directory_iteration(
        self, external, monkeypatch
    ):
        repo, _commit, _request, kwargs = _prepare(external)
        materialize_authorized_bundles(**kwargs)
        out = external["output"]
        original = Path.iterdir
        monkeypatch.setattr(
            Path, "iterdir", lambda self: iter(sorted(original(self), reverse=True))
        )
        reversed_view = load_bundle_directory(out / "development", repo=repo)
        assert [t.task_id for t in reversed_view] == list(reversed(fixed_task_ids("development")))
        ordered = load_materialized_bundle(out / "development", repo=repo)
        assert [t.task_id for t in ordered] == list(fixed_task_ids("development"))
        monkeypatch.undo()
        expect_reason(
            "materialized-ids", load_materialized_bundle, external["development"], repo=repo
        )
        expect_reason(
            "materialized-ids",
            prepare_materialized_import,
            repo=repo,
            expected_commit=kwargs["expected_commit"],
            development=out / "holdout",
            holdout=out / "development",
        )

    def test_destination_is_never_overwritten(self, external):
        _repo, _commit, _request, kwargs = _prepare(external)
        out = external["output"]
        os.mkdir(out, 0o700)
        before = tree_snapshot(out)
        expect_reason("destination-exists", materialize_authorized_bundles, **kwargs)
        assert tree_snapshot(out) == before
        (out / "keep").write_text("x", encoding="utf-8")
        before = tree_snapshot(out)
        expect_reason("destination-exists", materialize_authorized_bundles, **kwargs)
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
        _repo, _commit, _request, kwargs = _prepare(external)
        kwargs[field] = value
        expect_reason(reason, materialize_authorized_bundles, **kwargs)
        assert not external["output"].exists()
        _assert_sources_untouched(external)

    def test_custody_approval_phrase_does_not_authorize_materialization(self, external):
        _repo, _commit, request, kwargs = _prepare(external)
        kwargs["approval"] = approval_phrase(request["materialization_request_digest"])
        expect_reason("approval-mismatch", materialize_authorized_bundles, **kwargs)
        assert not external["output"].exists()

    def test_output_inside_a_repository_is_refused(self, external):
        repo, _commit, _request, kwargs = _prepare(external)
        kwargs["output_root"] = repo / "materialized-should-not-exist"
        expect_reason("repository-path", materialize_authorized_bundles, **kwargs)
        assert not (repo / "materialized-should-not-exist").exists()

    def test_non_frozen_sources_fail_before_any_write(self, tmp_path):
        repo, commit = _lab()
        dev_dir = write_bundle(
            tmp_path / "dev", source_stage("development", {DEVELOPMENT_TEMPLATE_IDS[0]: 20})
        )
        hold_dir = write_bundle(tmp_path / "hold", source_stage("holdout"))
        expect_reason(
            "distribution-mismatch",
            prepare_materialization,
            repo=repo,
            expected_commit=commit,
            development=load_bundle_directory(dev_dir, repo=repo),
            holdout=load_bundle_directory(hold_dir, repo=repo),
        )
        assert not (tmp_path / "materialized").exists()


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

    def test_prepare_materialize_and_import_print_content_free_json(
        self, external, sources, tmp_path
    ):
        repo, commit = _lab()
        common = ["--repo", str(repo), "--commit", commit]
        source_args = [
            "--development",
            str(external["development"]),
            "--holdout",
            str(external["holdout"]),
        ]
        prepared = run_cli("prepare-materialization", *common, *source_args)
        assert prepared.returncode == 0, prepared.stderr
        request = json.loads(prepared.stdout)
        validate_request(request)
        paths = [external["development"], external["holdout"], external["output"], tmp_path]
        assert_content_free(prepared.stdout + prepared.stderr, *sources, paths=paths)
        assert not external["output"].exists()

        write_args = [
            *common,
            *source_args,
            "--output",
            str(external["output"]),
            "--implementation-digest",
            request["implementation_digest"],
            "--request-digest",
            request["materialization_request_digest"],
        ]
        blocked = run_cli("materialize-bundles", *write_args, "--approve", "I approve nothing")
        assert blocked.returncode == 1 and blocked.stdout == ""
        assert blocked.stderr.strip() == "BLOCKED: approval-mismatch"
        assert not external["output"].exists()

        written = run_cli(
            "materialize-bundles",
            *write_args,
            "--approve",
            materialization_approval_phrase(request["materialization_request_digest"]),
        )
        assert written.returncode == 0, written.stderr
        record = json.loads(written.stdout)
        assert record["status"] == "pass" and record["request"] == request
        assert_content_free(written.stdout + written.stderr, *sources, paths=paths)
        _assert_sources_untouched(external)

        out = external["output"]
        import_args = ["--development", str(out / "development"), "--holdout", str(out / "holdout")]
        prepared_import = run_cli("prepare-import", *common, *import_args)
        assert prepared_import.returncode == 0, prepared_import.stderr
        import_request = json.loads(prepared_import.stdout)
        assert import_request["set_identity"] == request["output"]["set_identity"]
        custody_root = tmp_path / "custody"
        imported = run_cli(
            "import-materialized",
            *common,
            *import_args,
            "--output",
            str(custody_root),
            "--controller-digest",
            import_request["controller_digest"],
            "--request-digest",
            import_request["import_request_digest"],
            "--approve",
            approval_phrase(import_request["import_request_digest"]),
        )
        assert imported.returncode == 0, imported.stderr
        receipt = json.loads(imported.stdout)
        assert receipt["set_identity"] == request["output"]["set_identity"]
        assert_content_free(imported.stdout + imported.stderr, *sources, paths=paths)
        assert (custody_root / "manifest.json").is_file()

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
        assert completed.returncode == 1 and completed.stdout == ""
        assert completed.stderr.startswith("BLOCKED: ")
        assert str(tmp_path) not in completed.stderr
