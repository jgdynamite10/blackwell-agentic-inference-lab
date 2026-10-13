"""W9 keeps W8's search tokens and requires exactly one native tool call."""

from blackwell_lab.cloud.qualification import (
    CANDIDATE_W7,
    CANDIDATE_W8,
    CANDIDATE_W9,
    candidate_controller,
    candidate_identity_digest,
    candidate_workload_version,
    frozen_candidate_fields,
)
from blackwell_lab.workload.agent import SYSTEM_PROMPT_V241
from blackwell_lab.workload.catalog_agent import (
    SEARCH_GUIDANCE_V210,
    SINGLE_CALL_GUIDANCE_V211,
    _system_prompt,
)
from blackwell_lab.workload.scenarios import catalog


def test_w9_prompt_requires_exactly_one_native_tool_call():
    scenario = next(iter(catalog().values()))
    prompt = _system_prompt(scenario, "2.11.0")
    assert prompt == (f"{SYSTEM_PROMPT_V241} {SEARCH_GUIDANCE_V210} {SINGLE_CALL_GUIDANCE_V211}")
    assert "exactly one native tool call" in prompt
    assert "exactly one native tool call" not in SYSTEM_PROMPT_V241
    assert "exactly one native tool call" not in _system_prompt(scenario, "2.10.0")
    assert _system_prompt(scenario, "2.9.0") == SYSTEM_PROMPT_V241


def test_w9_is_a_new_identity_on_the_same_controller():
    assert candidate_workload_version(CANDIDATE_W9) == "2.11.0"
    assert candidate_controller(CANDIDATE_W9) == candidate_controller(CANDIDATE_W8)
    w8 = frozen_candidate_fields(CANDIDATE_W8)
    w9 = frozen_candidate_fields(CANDIDATE_W9)
    assert w8["system_prompt_sha256"] != w9["system_prompt_sha256"]
    assert w8["evidence_contract"] == w9["evidence_contract"]
    assert w8["native_tool_contract_sha256"] == w9["native_tool_contract_sha256"]
    assert candidate_identity_digest(CANDIDATE_W7) != candidate_identity_digest(CANDIDATE_W9)
    assert candidate_identity_digest(CANDIDATE_W8) != candidate_identity_digest(CANDIDATE_W9)
