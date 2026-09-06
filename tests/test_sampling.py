"""Seeded, deterministic, balanced task-instance generation (D-0010)."""

from __future__ import annotations

import pytest

from blackwell_lab.workload.sampling import (
    generate_task_instances,
    sample_design_summary,
)
from blackwell_lab.workload.scenarios import catalog

TEMPLATES = list(catalog())


class TestBalance:
    def test_default_measurement_plan_is_exactly_balanced(self):
        """200 tasks over 10 templates: exactly 20 instances per template."""
        instances = generate_task_instances(TEMPLATES, 200, seed=1)
        counts: dict[str, int] = {}
        for instance in instances:
            counts[instance.template_id] = counts.get(instance.template_id, 0) + 1
        assert set(counts) == set(TEMPLATES)
        assert all(count == 20 for count in counts.values())

    def test_every_prefix_is_maximally_balanced(self):
        """Round-robin interleaving: any two templates differ by at most one
        occurrence in every prefix of the schedule."""
        instances = generate_task_instances(TEMPLATES, 47, seed=1)
        counts = dict.fromkeys(TEMPLATES, 0)
        for instance in instances:
            counts[instance.template_id] += 1
            assert max(counts.values()) - min(counts.values()) <= 1

    def test_five_repetitions_yield_1000_observations(self):
        total = sum(len(generate_task_instances(TEMPLATES, 200, seed=s)) for s in range(5))
        assert total == 1000


class TestDeterminismAndIdentity:
    def test_same_seed_reproduces_identical_instances(self):
        assert generate_task_instances(TEMPLATES, 50, seed=9) == generate_task_instances(
            TEMPLATES, 50, seed=9
        )

    def test_different_seeds_produce_different_surface_variants(self):
        first = generate_task_instances(TEMPLATES, 50, seed=1)
        second = generate_task_instances(TEMPLATES, 50, seed=2)
        assert [i.instance_id for i in first] == [i.instance_id for i in second]
        assert [i.tracking_id for i in first] != [i.tracking_id for i in second]

    def test_instances_record_template_instance_and_seed_identifiers(self):
        instances = generate_task_instances(TEMPLATES, 20, seed=3)
        for instance in instances:
            assert instance.instance_id.startswith(f"{instance.template_id}#")
            assert instance.instance_seed >= 0
            assert instance.tracking_id.startswith("ALRT-")
            assert 1 <= instance.reported_minute <= 60
            assert instance.tracking_id in instance.surface_variant_text()

    def test_invalid_inputs_are_rejected(self):
        with pytest.raises(ValueError):
            generate_task_instances([], 10, seed=1)
        with pytest.raises(ValueError):
            generate_task_instances(TEMPLATES, 0, seed=1)


class TestSampleDesignSummary:
    def test_summary_records_the_honest_quality_bound(self):
        """unique_template_count — not tasks_per_repetition — bounds the
        independent quality cases; the note says so explicitly."""
        instances = generate_task_instances(TEMPLATES, 200, seed=4)
        summary = sample_design_summary(instances, seed=4)
        assert summary["tasks_per_repetition"] == 200
        assert summary["total_attempts"] == 200
        assert summary["seed"] == 4
        assert summary["unique_template_count"] == 10
        assert summary["unique_instance_count"] == 200
        assert "unique_template_count" in summary["quality_independence_note"]

    def test_byte_identical_duplicates_are_not_counted_as_unique(self):
        instances = generate_task_instances(TEMPLATES, 10, seed=5)
        duplicated = instances + instances  # byte-identical repeats
        summary = sample_design_summary(duplicated, seed=5)
        assert summary["total_attempts"] == 20
        assert summary["unique_instance_count"] == 10
