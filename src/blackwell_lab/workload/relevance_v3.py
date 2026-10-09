"""Exact hypothesis-support matcher for ``workflow-controller-v3``.

Support is coverage of the selected hypothesis's declared evidence pattern.
It is not a judgment that the hypothesis is the correct or unique cause.
This module does not import the evaluator, the scenario catalog, accepted
answers, or holdout material.
"""

from __future__ import annotations

from typing import Any

SUPPORT_SUPPORTED = "supported"
SUPPORT_UNCOVERED = "uncovered"
SUPPORT_CONTRADICTION = "contradiction"
SUPPORT_PARTIAL_CITATION = "partial_citation"
SUPPORT_NO_HYPOTHESIS = "no_hypothesis"


def _subject_key(subject: object) -> tuple[str, str, str] | None:
    if not isinstance(subject, dict):
        return None
    kind = subject.get("kind")
    identifier = subject.get("id")
    scope = subject.get("scope")
    if not all(isinstance(item, str) and item for item in (kind, identifier, scope)):
        return None
    return (kind, identifier, scope)


def _condition_key(condition: object) -> tuple[str, str] | None:
    if not isinstance(condition, dict):
        return None
    name = condition.get("name")
    state = condition.get("state")
    if not isinstance(name, str) or not name or not isinstance(state, str) or not state:
        return None
    return (name, state)


def _finding_ok(finding: object) -> bool:
    return isinstance(finding, dict) and _subject_key(finding.get("subject")) is not None


def matches_assertion(
    finding: dict[str, Any], assertion: dict[str, Any], witness_type: str
) -> bool:
    """True when one asserted finding covers one declared assertion exactly."""
    if witness_type != assertion.get("witness_type"):
        return False
    if finding.get("certainty") != "asserted" or finding.get("polarity") != "affirmed":
        return False
    if assertion.get("polarity") != "affirmed":
        return False
    if finding.get("role") != assertion.get("role"):
        return False
    if _subject_key(finding.get("subject")) != _subject_key(assertion.get("subject")):
        return False
    return _condition_key(finding.get("condition")) == _condition_key(assertion.get("condition"))


def contradicts(finding: dict[str, Any], assertion: dict[str, Any]) -> bool:
    """True for a denial or an incompatible affirmed state in the same scope.

    An ambiguous report of the required state is not a witness and is not a
    contradiction. An ambiguous report of a different state stays unresolved.
    """
    if _subject_key(finding.get("subject")) != _subject_key(assertion.get("subject")):
        return False
    finding_condition = _condition_key(finding.get("condition"))
    required = _condition_key(assertion.get("condition"))
    if finding_condition is None or required is None or finding_condition[0] != required[0]:
        return False
    certainty = finding.get("certainty")
    if certainty == "ambiguous":
        return finding_condition[1] != required[1]
    if certainty != "asserted":
        return False
    polarity = finding.get("polarity")
    if polarity == "denied" and finding_condition[1] == required[1]:
        return True
    if polarity == "affirmed" and finding_condition[1] != required[1]:
        return True
    return False


def _hypothesis(catalog: object, diagnosis_id: str) -> dict[str, Any] | None:
    if not isinstance(catalog, list):
        return None
    matches = [
        item for item in catalog if isinstance(item, dict) and item.get("id") == diagnosis_id
    ]
    if len(matches) != 1:
        return None
    claim = matches[0].get("claim")
    if not isinstance(claim, dict) or not isinstance(claim.get("support_all_of"), list):
        return None
    return matches[0]


def _assertions(hypothesis: dict[str, Any]) -> list[dict[str, Any]] | None:
    support = hypothesis["claim"]["support_all_of"]
    if not support or not all(isinstance(item, dict) for item in support):
        return None
    if not any(item.get("role") == "condition" for item in support):
        return None
    return list(support)


def pattern_support(
    *,
    catalog: object,
    diagnosis_id: str,
    witnesses: list[tuple[str, str, list[dict[str, Any]]]],
    cited_ids: set[str] | None,
) -> str:
    """Coverage of the selected hypothesis.

    ``witnesses`` entries are ``(observation_id, witness_type, findings)``
    from eligible earlier health and log results. ``cited_ids`` is ``None``
    for the control (every eligible witness counts) and the cited set for
    the evidence-refs treatment. Contradiction checks always use every
    witness, cited or not.
    """
    hypothesis = _hypothesis(catalog, diagnosis_id)
    if hypothesis is None:
        return SUPPORT_NO_HYPOTHESIS
    assertions = _assertions(hypothesis)
    if assertions is None:
        return SUPPORT_NO_HYPOTHESIS
    for assertion in assertions:
        if any(
            _finding_ok(finding) and contradicts(finding, assertion)
            for _obs, _kind, findings in witnesses
            for finding in findings
        ):
            return SUPPORT_CONTRADICTION
    covered = []
    for assertion in assertions:
        matching_ids = {
            observation_id
            for observation_id, witness_type, findings in witnesses
            if any(
                _finding_ok(finding) and matches_assertion(finding, assertion, witness_type)
                for finding in findings
            )
        }
        covered.append(matching_ids)
    if any(not ids for ids in covered):
        return SUPPORT_UNCOVERED
    if cited_ids is not None and any(ids.isdisjoint(cited_ids) for ids in covered):
        return SUPPORT_PARTIAL_CITATION
    return SUPPORT_SUPPORTED
