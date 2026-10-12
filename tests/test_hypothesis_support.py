"""Exact support matching for the D-0034 evidence patterns."""

from __future__ import annotations

from blackwell_lab.workload.relevance_v3 import (
    SUPPORT_CONTRADICTION,
    SUPPORT_PARTIAL_CITATION,
    SUPPORT_SUPPORTED,
    SUPPORT_UNCOVERED,
    pattern_support,
)

TASK = "$task"


def _assertion(kind, identifier, scope, name, state, witness, role="condition"):
    return {
        "subject": {"kind": kind, "id": identifier, "scope": scope},
        "condition": {"name": name, "state": state},
        "polarity": "affirmed",
        "witness_type": witness,
        "role": role,
    }


def _finding(
    kind,
    identifier,
    scope,
    name,
    state,
    role="condition",
    polarity="affirmed",
    certainty="asserted",
):
    return {
        "subject": {"kind": kind, "id": identifier, "scope": scope},
        "condition": {"name": name, "state": state},
        "polarity": polarity,
        "certainty": certainty,
        "role": role,
        "sources": [{"kind": "log_span", "start": 0, "end": 1, "quote": "x"}],
    }


def _hypothesis(diagnosis_id, assertions):
    about = []
    for assertion in assertions:
        subject = assertion["subject"]
        if subject not in about:
            about.append(subject)
    return {
        "id": diagnosis_id,
        "about": about,
        "claim": {"text": "Hypothesis.", "support_all_of": assertions},
    }


def _support(diagnosis, assertions, witnesses, cited=None):
    return pattern_support(
        catalog=[_hypothesis(diagnosis, assertions)],
        diagnosis_id=diagnosis,
        witnesses=witnesses,
        cited_ids=cited,
    )


def test_explicit_condition_supports_and_mention_does_not():
    assertion = _assertion("certificate", "cert-blue", TASK, "validity", "expired", "log")
    finding = _finding("certificate", "cert-blue", TASK, "validity", "expired")
    assert _support("expired-cert", [assertion], [("obs-1", "log", [finding])]) == SUPPORT_SUPPORTED
    mention_only = _finding("service", "queue-blue", TASK, "name", "present", role="context")
    outage = _assertion("service", "queue-blue", TASK, "availability", "outage", "log")
    assert _support("outage", [outage], [("obs-1", "log", [mention_only])]) == SUPPORT_UNCOVERED


def test_symptom_negation_ambiguity_and_scope():
    leak = _assertion("process", "worker-blue", TASK, "memory_leak", "present", "log")
    symptom = _finding("process", "worker-blue", TASK, "memory_pressure", "oom", role="symptom")
    assert _support("leak", [leak], [("obs-1", "log", [symptom])]) == SUPPORT_UNCOVERED
    expired = _assertion("certificate", "cert-blue", TASK, "validity", "expired", "log")
    denied = _finding("certificate", "cert-blue", TASK, "validity", "expired", polarity="denied")
    assert _support("expired", [expired], [("obs-1", "log", [denied])]) == SUPPORT_CONTRADICTION
    ambiguous = _finding(
        "certificate", "cert-blue", TASK, "validity", "expired", certainty="ambiguous"
    )
    assert _support("expired", [expired], [("obs-1", "log", [ambiguous])]) == SUPPORT_UNCOVERED
    absent = _assertion("deployment", "dep-blue", TASK, "required_migration", "absent", "log")
    affirmed_absence = _finding("deployment", "dep-blue", TASK, "required_migration", "absent")
    assert (
        _support("migration", [absent], [("obs-1", "log", [affirmed_absence])]) == SUPPORT_SUPPORTED
    )
    wrong_scope = _finding("deployment", "dep-old", TASK, "required_migration", "absent")
    assert _support("migration", [absent], [("obs-1", "log", [wrong_scope])]) == SUPPORT_UNCOVERED


def test_cache_requires_both_states_and_role_cannot_be_swapped():
    zero = _assertion("configuration", "cfg-blue", TASK, "cache_max_entries", "0", "log")
    bound = _assertion("configuration", "cfg-blue", TASK, "cache_bound", "unbounded", "log")
    only_zero = _finding("configuration", "cfg-blue", TASK, "cache_max_entries", "0")
    disabled = _finding("configuration", "cfg-blue", TASK, "cache_bound", "disabled")
    assert (
        _support("cache", [zero, bound], [("obs-1", "log", [only_zero, disabled])])
        == SUPPORT_CONTRADICTION
    )
    swapped = _finding(
        "configuration", "cfg-blue", TASK, "cache_bound", "unbounded", role="symptom"
    )
    unbounded = _finding("configuration", "cfg-blue", TASK, "cache_bound", "unbounded")
    assert (
        _support("cache", [zero, bound], [("obs-1", "log", [only_zero, swapped])])
        == SUPPORT_UNCOVERED
    )
    assert (
        _support("cache", [zero, bound], [("obs-1", "log", [only_zero, unbounded])])
        == SUPPORT_SUPPORTED
    )


def test_gpu_pattern_rejects_partial_and_cross_host_witnesses():
    host = "lynx-inference-1.node.lab.example"
    other = "lynx-inference-2.node.lab.example"
    admission = _assertion(
        "batch-job",
        "embed-refresh-44",
        host,
        "online_gpu_pool_admission",
        "without_priority_cap",
        "log",
    )
    saturated = _assertion("allocator", "kv-cache", host, "capacity", "saturated", "log")
    admitted = _finding(
        "batch-job", "embed-refresh-44", host, "online_gpu_pool_admission", "without_priority_cap"
    )
    full = _finding("allocator", "kv-cache", host, "capacity", "saturated")
    assert (
        _support("gpu", [admission, saturated], [("obs-a", "log", [admitted])]) == SUPPORT_UNCOVERED
    )
    assert _support("gpu", [admission, saturated], [("obs-b", "log", [full])]) == SUPPORT_UNCOVERED
    other_host = _finding("allocator", "kv-cache", other, "capacity", "saturated")
    assert (
        _support(
            "gpu",
            [admission, saturated],
            [("obs-a", "log", [admitted]), ("obs-b", "log", [other_host])],
        )
        == SUPPORT_UNCOVERED
    )
    maybe = _finding("allocator", "kv-cache", host, "capacity", "saturated", certainty="ambiguous")
    assert (
        _support(
            "gpu", [admission, saturated], [("obs-a", "log", [admitted]), ("obs-b", "log", [maybe])]
        )
        == SUPPORT_UNCOVERED
    )
    assert (
        _support(
            "gpu", [admission, saturated], [("obs-a", "log", [admitted]), ("obs-b", "log", [full])]
        )
        == SUPPORT_SUPPORTED
    )


def test_health_pattern_and_citation_coverage():
    health = _assertion("service", "heron-auth", TASK, "api.status", "unhealthy", "health")
    upstream = _assertion(
        "service", "heron-auth", TASK, "service_role", "upstream", "log", role="context"
    )
    health_finding = _finding("service", "heron-auth", TASK, "api.status", "unhealthy")
    log_finding = _finding(
        "service", "heron-auth", TASK, "service_role", "upstream", role="context"
    )
    witnesses = [("obs-h", "health", [health_finding]), ("obs-l", "log", [log_finding])]
    assert _support("upstream", [health, upstream], witnesses) == SUPPORT_SUPPORTED
    assert (
        _support("upstream", [health, upstream], witnesses, cited={"obs-l"})
        == SUPPORT_PARTIAL_CITATION
    )
    unrelated = _finding("service", "other", TASK, "api.status", "unhealthy")
    assert (
        _support(
            "upstream",
            [health, upstream],
            [("obs-h", "health", [unrelated]), ("obs-l", "log", [log_finding])],
        )
        == SUPPORT_UNCOVERED
    )
    healthy = _finding("service", "heron-auth", TASK, "api.status", "healthy")
    assert (
        _support(
            "upstream",
            [health, upstream],
            [("obs-h", "health", [healthy]), ("obs-l", "log", [log_finding])],
        )
        == SUPPORT_CONTRADICTION
    )


def test_selective_citation_does_not_hide_a_conflict():
    expired = _assertion("certificate", "cert-blue", TASK, "validity", "expired", "log")
    positive = _finding("certificate", "cert-blue", TASK, "validity", "expired")
    negative = _finding("certificate", "cert-blue", TASK, "validity", "expired", polarity="denied")
    witnesses = [("obs-yes", "log", [positive]), ("obs-no", "log", [negative])]
    assert _support("expired", [expired], witnesses, cited={"obs-yes"}) == SUPPORT_CONTRADICTION
