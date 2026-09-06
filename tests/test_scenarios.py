"""Scenario-catalog validity, versioning, and determinism."""

from __future__ import annotations

import re

from blackwell_lab.workload.scenarios import (
    INCIDENT_CLASSES,
    WORKLOAD_VERSION,
    Scenario,
    canonical_catalog_json,
    catalog,
    catalog_digest,
)
from blackwell_lab.workload.tools import TERMINAL_TOOL, TOOL_SPECS, validate_tool_call


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
    """Every scenario carries fixtures, ground truth, distractors, and
    machine-checkable success criteria (workload definition, incident catalog)."""

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
            assert scenario.root_cause_keywords, scenario.scenario_id
            assert scenario.accepted_remediations, scenario.scenario_id
            assert scenario.distractor_remediations, scenario.scenario_id
            assert scenario.required_evidence, scenario.scenario_id
            assert scenario.reference_tool_sequence, scenario.scenario_id

    def test_accepted_and_distractor_remediations_are_disjoint(self):
        for scenario in catalog().values():
            overlap = set(scenario.accepted_remediations) & set(scenario.distractor_remediations)
            assert not overlap, scenario.scenario_id

    def test_success_criteria_are_machine_checkable(self):
        """The keywords must appear in the ground-truth summary, so a correct
        agent (or the mock replay) can achieve full root-cause credit."""
        for scenario in catalog().values():
            summary = scenario.root_cause_summary.casefold()
            for keyword in scenario.root_cause_keywords:
                assert keyword.casefold() in summary, (scenario.scenario_id, keyword)

    def test_distractor_signals_exist_in_runbook_fixtures(self):
        """Distractor remediations must actually be visible to the agent
        (listed in a runbook), otherwise they are not distractors."""
        for scenario in catalog().values():
            runbook_ids = {
                rid for runbook in scenario.runbooks.values() for rid in runbook["remediation_ids"]
            }
            assert set(scenario.accepted_remediations) & runbook_ids, scenario.scenario_id
            assert set(scenario.distractor_remediations) & runbook_ids, scenario.scenario_id

    def test_required_evidence_names_real_tools(self):
        for scenario in catalog().values():
            for tool in scenario.required_evidence:
                assert tool in TOOL_SPECS
                assert tool != TERMINAL_TOOL

    def test_reference_sequences_are_valid_and_nonterminal(self):
        """The reference sequence must be executable evidence-gathering; the
        mock client appends the terminal recommendation itself."""
        for scenario in catalog().values():
            for step in scenario.reference_tool_sequence:
                validate_tool_call(step["tool"], step["arguments"])
                assert step["tool"] != TERMINAL_TOOL
            covered = {step["tool"] for step in scenario.reference_tool_sequence}
            assert set(scenario.required_evidence) <= covered, scenario.scenario_id


class TestDeterminism:
    def test_canonical_json_is_stable(self):
        assert canonical_catalog_json() == canonical_catalog_json()

    def test_catalog_digest_is_stable_and_well_formed(self):
        digest = catalog_digest()
        assert digest == catalog_digest()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
