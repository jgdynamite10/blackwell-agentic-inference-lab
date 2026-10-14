"""W11 keeps W10's prompt and requires check_recent_changes first."""

from blackwell_lab.cloud.qualification import (
    CANDIDATE_W10,
    CANDIDATE_W11,
    candidate_controller,
    candidate_identity_digest,
    candidate_workload_version,
    frozen_candidate_fields,
)
from blackwell_lab.workload.agent import SYSTEM_PROMPT_V241
from blackwell_lab.workload.catalog_agent import (
    CHANGE_GUIDANCE_V213,
    KNOWN_TOOL_GUIDANCE_V212,
    SEARCH_GUIDANCE_V210,
    SINGLE_CALL_GUIDANCE_V211,
    _system_prompt,
)
from blackwell_lab.workload.scenarios import catalog


def test_w11_prompt_requires_the_change_feed_before_the_terminal():
    scenario = next(iter(catalog().values()))
    prompt = _system_prompt(scenario, "2.13.0")
    expected = (
        f"{SYSTEM_PROMPT_V241} {SEARCH_GUIDANCE_V210} "
        f"{SINGLE_CALL_GUIDANCE_V211} {KNOWN_TOOL_GUIDANCE_V212} "
        f"{CHANGE_GUIDANCE_V213}"
    )
    assert prompt == expected
    assert "call check_recent_changes" in prompt
    assert "call check_recent_changes" not in _system_prompt(scenario, "2.12.0")


def test_w11_is_a_new_identity_on_the_same_controller():
    assert candidate_workload_version(CANDIDATE_W11) == "2.13.0"
    assert candidate_controller(CANDIDATE_W11) == candidate_controller(CANDIDATE_W10)
    w10 = frozen_candidate_fields(CANDIDATE_W10)
    w11 = frozen_candidate_fields(CANDIDATE_W11)
    assert w10["system_prompt_sha256"] != w11["system_prompt_sha256"]
    assert w10["evidence_contract"] == w11["evidence_contract"]
    assert w10["native_tool_contract_sha256"] == w11["native_tool_contract_sha256"]
    assert candidate_identity_digest(CANDIDATE_W10) != candidate_identity_digest(CANDIDATE_W11)
