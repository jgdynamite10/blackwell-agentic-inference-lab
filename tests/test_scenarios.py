"""Scenario-catalog validity, versioning, structured success criteria, and
determinism."""

from __future__ import annotations

import re

from fakes import FakeClock

from blackwell_lab.workload.agent import ToolTrace
from blackwell_lab.workload.evaluator import predicate_satisfied
from blackwell_lab.workload.scenarios import (
    INCIDENT_CLASSES,
    WORKLOAD_VERSION,
    Scenario,
    canonical_catalog_json,
    catalog,
    catalog_digest,
)
from blackwell_lab.workload.tools import TERMINAL_TOOL, TOOL_SPECS, SimulatedToolbox


class TestCatalogCoverage:
    def test_workload_is_versioned(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+", WORKLOAD_VERSION)

    def test_all_ten_incident_classes_are_covered(self):
        covered = {s.incident_class for s in catalog().values()}
        assert covered == set(INCIDENT_CLASSES)
        assert len(INCIDENT_CLASSES) == 10

    def test_scenario_ids_are_unique_and_stable_order(self):
        ids = list(catalog())
        assert len(ids) == len(set(ids))
        assert ids == list(catalog())  # deterministic ordering


class TestScenarioCompleteness:
    """Every scenario carries fixtures, structured ground truth, distractors,
    and machine-checkable evidence predicates (workload definition)."""

    def test_every_scenario_is_complete(self):
        for scenario in catalog().values():
            assert isinstance(scenario, Scenario)
            assert scenario.health, scenario.scenario_id
            assert scenario.metrics, scenario.scenario_id
            assert scenario.logs, scenario.scenario_id
            assert scenario.runbooks, scenario.scenario_id
            assert scenario.recent_changes, scenario.scenario_id
            assert scenario.root_cause_id, scenario.scenario_id
            assert scenario.root_cause_summary, scenario.scenario_id
            assert scenario.accepted_diagnoses, scenario.scenario_id
            assert scenario.distractor_diagnoses, scenario.scenario_id
            assert scenario.accepted_remediations, scenario.scenario_id
            assert scenario.distractor_remediations, scenario.scenario_id
            assert scenario.evidence_predicates, scenario.scenario_id
            assert scenario.reference_tool_sequence, scenario.scenario_id
            assert scenario.alternative_tool_sequence, scenario.scenario_id

    def test_accepted_and_distractor_remediations_are_disjoint(self):
        for scenario in catalog().values():
            overlap = set(scenario.accepted_remediations) & set(scenario.distractor_remediations)
            assert not overlap, scenario.scenario_id

    def test_accepted_and_distractor_diagnoses_are_disjoint(self):
        for scenario in catalog().values():
            overlap = set(scenario.accepted_diagnoses) & set(scenario.distractor_diagnoses)
            assert not overlap, scenario.scenario_id

    def test_candidate_diagnoses_publish_accepted_and_distractors_sorted(self):
        """The agent selects among published candidates — no hidden strings —
        and the sorted order leaks nothing about which candidate is correct."""
        for scenario in catalog().values():
            candidates = scenario.candidate_diagnoses
            assert set(candidates) == set(scenario.accepted_diagnoses) | set(
                scenario.distractor_diagnoses
            )
            assert list(candidates) == sorted(candidates), scenario.scenario_id
            assert len(candidates) >= 3, scenario.scenario_id

    def test_distractor_signals_exist_in_runbook_fixtures(self):
        """Distractor remediations must actually be visible to the agent
        (listed in a runbook), otherwise they are not distractors."""
        for scenario in catalog().values():
            runbook_ids = {
                rid for runbook in scenario.runbooks.values() for rid in runbook["remediation_ids"]
            }
            assert set(scenario.accepted_remediations) & runbook_ids, scenario.scenario_id
            assert set(scenario.distractor_remediations) & runbook_ids, scenario.scenario_id


def replay_trace(scenario: Scenario, sequence: tuple[dict, ...]) -> list[ToolTrace]:
    """Executes an evidence sequence against the toolbox and records traces."""
    toolbox = SimulatedToolbox(scenario, log_limit=10, metric_window_s=900, clock=FakeClock())
    trace = []
    for step in sequence:
        result = toolbox.execute(step["tool"], step["arguments"])
        trace.append(
            ToolTrace(
                tool=result.tool,
                arguments=dict(step["arguments"]),
                result=result.payload,
                simulated_latency_ms=result.simulated_latency_ms,
            )
        )
    return trace


class TestEvidencePredicates:
    """Predicates are the mandatory evidence gates: well-formed,
    machine-checkable, and satisfiable through BOTH declared paths."""

    def test_predicates_are_well_formed(self):
        for scenario in catalog().values():
            seen_ids = set()
            for predicate in scenario.evidence_predicates:
                assert predicate.predicate_id, scenario.scenario_id
                assert predicate.predicate_id not in seen_ids, scenario.scenario_id
                seen_ids.add(predicate.predicate_id)
                assert predicate.description, scenario.scenario_id
                assert predicate.alternatives, scenario.scenario_id
                for alternative in predicate.alternatives:
                    assert alternative.tool in TOOL_SPECS
                    assert alternative.tool != TERMINAL_TOOL
                    # Every alternative constrains arguments and/or results, so
                    # merely naming the right tool can never satisfy evidence.
                    assert alternative.argument_contains or alternative.result_contains

    def test_every_scenario_declares_an_alternative_evidence_path(self):
        for scenario in catalog().values():
            assert any(
                len(predicate.alternatives) >= 2 for predicate in scenario.evidence_predicates
            ), scenario.scenario_id

    def test_reference_sequence_satisfies_every_predicate(self):
        for scenario in catalog().values():
            trace = replay_trace(scenario, scenario.reference_tool_sequence)
            for predicate in scenario.evidence_predicates:
                assert predicate_satisfied(predicate, trace), (
                    scenario.scenario_id,
                    predicate.predicate_id,
                )

    def test_alternative_sequence_also_satisfies_every_predicate(self):
        for scenario in catalog().values():
            trace = replay_trace(scenario, scenario.alternative_tool_sequence)
            for predicate in scenario.evidence_predicates:
                assert predicate_satisfied(predicate, trace), (
                    scenario.scenario_id,
                    predicate.predicate_id,
                )

    def test_sequences_are_valid_and_nonterminal(self):
        """Evidence sequences must be executable evidence-gathering; the mock
        client appends the terminal recommendation itself."""
        for scenario in catalog().values():
            for sequence in (scenario.reference_tool_sequence, scenario.alternative_tool_sequence):
                for step in sequence:
                    assert step["tool"] in TOOL_SPECS
                    assert step["tool"] != TERMINAL_TOOL


class TestDeterminism:
    def test_canonical_json_is_stable(self):
        assert canonical_catalog_json() == canonical_catalog_json()

    def test_catalog_digest_is_stable_and_well_formed(self):
        digest = catalog_digest()
        assert digest == catalog_digest()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
