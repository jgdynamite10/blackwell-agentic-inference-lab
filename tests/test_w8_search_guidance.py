"""W8 keeps W7's catalog and adds the public literal search tokens."""

from blackwell_lab.cloud.qualification import (
    CANDIDATE_W7,
    CANDIDATE_W8,
    candidate_controller,
    candidate_identity_digest,
    candidate_workload_version,
    frozen_candidate_fields,
)
from blackwell_lab.workload.agent import SYSTEM_PROMPT_V241, system_prompt
from blackwell_lab.workload.catalog_agent import SEARCH_GUIDANCE_V210, _system_prompt
from blackwell_lab.workload.scenarios import catalog

ACCEPTED_SEARCH_TOKENS = (
    "servfail",
    "search domain",
    "readiness",
    "migration",
    "startup",
    "dep-8102",
)


def test_w8_prompt_contains_the_accepted_literal_tokens():
    scenario = next(iter(catalog().values()))
    prompt = _system_prompt(scenario, "2.10.0")
    folded = prompt.casefold()
    for token in ACCEPTED_SEARCH_TOKENS:
        assert token in folded
        assert token not in SYSTEM_PROMPT_V241.casefold()
    assert prompt == f"{SYSTEM_PROMPT_V241} {SEARCH_GUIDANCE_V210}"
    assert system_prompt(scenario, "2.8.0") == SYSTEM_PROMPT_V241
    assert _system_prompt(scenario, "2.9.0") == SYSTEM_PROMPT_V241


def test_w8_is_a_new_identity_on_the_same_controller():
    assert candidate_workload_version(CANDIDATE_W8) == "2.10.0"
    assert candidate_controller(CANDIDATE_W8) == candidate_controller(CANDIDATE_W7)
    w7 = frozen_candidate_fields(CANDIDATE_W7)
    w8 = frozen_candidate_fields(CANDIDATE_W8)
    assert w7["system_prompt_sha256"] != w8["system_prompt_sha256"]
    assert w7["evidence_contract"] == w8["evidence_contract"]
    assert candidate_identity_digest(CANDIDATE_W7) != candidate_identity_digest(CANDIDATE_W8)
