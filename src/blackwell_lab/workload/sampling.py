"""Seeded, deterministic, balanced task-instance generation (decision D-0010).

Each measured repetition contains ``tasks_per_repetition`` task instances
(measurement default 200) balanced across all incident templates: with the
ten-template catalog and the default, every template contributes exactly 20
instances, and five repetitions yield 1,000 task observations per cell for
the separately labeled Phase 7 cell-level p99.

Instances are deterministic functions of ``(seed, template_id, occurrence)``:
a SHA-256 derivation (no ``random`` module) produces per-instance surface
variants (a fictional tracking id and reported-time offset) that are injected
into the task prompt. Instances of the same template therefore differ at the
prompt surface, but they share the same fixtures and ground truth — the
sample design records ``unique_template_count`` so byte-identical or
template-identical duplicates are never characterized as independent quality
cases (quality diversity is bounded by the template count).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskInstance:
    """One deterministic task instance (a seeded variant of a template)."""

    template_id: str
    instance_id: str
    instance_seed: int
    tracking_id: str
    reported_minute: int

    def surface_variant_text(self) -> str:
        """Prompt lines that vary per instance (synthetic surface only)."""
        return (
            f"tracking_id: {self.tracking_id}\n"
            f"reported_at: t+{self.reported_minute}m (operator report; synthetic)"
        )


def _derive(seed: int, template_id: str, occurrence: int) -> tuple[int, str, int]:
    """Deterministic (instance_seed, tracking_id, reported_minute) triple."""
    digest = hashlib.sha256(f"{seed}:{template_id}:{occurrence}".encode()).hexdigest()
    # 8 hex chars -> at most 10 decimal digits, so a serialized seed can never
    # resemble a 12-digit account identifier to safety scanners.
    instance_seed = int(digest[:8], 16)
    tracking_id = f"ALRT-{digest[12:20]}"
    reported_minute = 1 + (int(digest[20:24], 16) % 59)
    return instance_seed, tracking_id, reported_minute


def generate_task_instances(
    template_ids: list[str] | tuple[str, ...],
    tasks_per_repetition: int,
    seed: int,
) -> list[TaskInstance]:
    """Balanced, seeded, deterministic instances for one repetition.

    Templates are interleaved round-robin in catalog order, so every prefix of
    the schedule is as balanced as possible and, when ``tasks_per_repetition``
    is a multiple of the template count, each template appears exactly
    ``tasks_per_repetition / len(template_ids)`` times.
    """
    if not template_ids:
        raise ValueError("template_ids must not be empty")
    if tasks_per_repetition < 1:
        raise ValueError("tasks_per_repetition must be >= 1")
    instances: list[TaskInstance] = []
    occurrences = dict.fromkeys(template_ids, 0)
    for i in range(tasks_per_repetition):
        template_id = template_ids[i % len(template_ids)]
        occurrence = occurrences[template_id]
        occurrences[template_id] = occurrence + 1
        instance_seed, tracking_id, reported_minute = _derive(seed, template_id, occurrence)
        instances.append(
            TaskInstance(
                template_id=template_id,
                instance_id=f"{template_id}#{occurrence:04d}",
                instance_seed=instance_seed,
                tracking_id=tracking_id,
                reported_minute=reported_minute,
            )
        )
    return instances


def sample_design_summary(instances: list[TaskInstance], seed: int) -> dict:
    """The sample-design block recorded in every benchmark result.

    ``unique_template_count`` is the honest bound on independent quality
    cases: instances of one template share fixtures and ground truth, so
    prompt-surface variants must not be presented as independent quality
    samples (decision D-0010).
    """
    templates = [inst.template_id for inst in instances]
    distinct_payloads = {
        (inst.template_id, inst.tracking_id, inst.reported_minute) for inst in instances
    }
    return {
        "tasks_per_repetition": len(instances),
        "seed": seed,
        "unique_template_count": len(set(templates)),
        "unique_instance_count": len(distinct_payloads),
        "total_attempts": len(instances),
        "quality_independence_note": (
            "Task instances are seeded prompt-surface variants of their incident "
            "template; instances sharing a template share fixtures and ground "
            "truth, so independent quality cases are bounded by "
            "unique_template_count, not tasks_per_repetition."
        ),
    }
