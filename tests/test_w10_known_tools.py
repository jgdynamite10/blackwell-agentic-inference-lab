"""W10 keeps W9's prompt and names the permitted tool identifiers."""

from blackwell_lab.cloud.qualification import (
    CANDIDATE_W8,
    CANDIDATE_W9,
    CANDIDATE_W10,
    candidate_controller,
    candidate_identity_digest,
    candidate_workload_version,
    frozen_candidate_fields,
)
from blackwell_lab.workload.agent import SYSTEM_PROMPT_V241
from blackwell_lab.workload.catalog_agent import (
    KNOWN_TOOL_GUIDANCE_V212,
    SEARCH_GUIDANCE_V210,
    SINGLE_CALL_GUIDANCE_V211,
    _system_prompt,
)
from blackwell_lab.workload.native_tools import openai_tool_definitions
from blackwell_lab.workload.scenarios import catalog


def test_w10_prompt_names_only_the_provided_tools():
    scenario = next(iter(catalog().values()))
    prompt = _system_prompt(scenario, "2.12.0")
    expected = (
        f"{SYSTEM_PROMPT_V241} {SEARCH_GUIDANCE_V210} "
        f"{SINGLE_CALL_GUIDANCE_V211} {KNOWN_TOOL_GUIDANCE_V212}"
    )
    assert prompt == expected
    assert "Do not invent a tool name." in prompt
    assert "Do not invent a tool name." not in _system_prompt(scenario, "2.11.0")
    names = {item["function"]["name"] for item in openai_tool_definitions("2.12.0")}
    for name in names:
        assert name in KNOWN_TOOL_GUIDANCE_V212
    assert _system_prompt(scenario, "2.10.0") == f"{SYSTEM_PROMPT_V241} {SEARCH_GUIDANCE_V210}"


def test_w10_is_a_new_identity_on_the_same_controller():
    assert candidate_workload_version(CANDIDATE_W10) == "2.12.0"
    assert candidate_controller(CANDIDATE_W10) == candidate_controller(CANDIDATE_W9)
    w9 = frozen_candidate_fields(CANDIDATE_W9)
    w10 = frozen_candidate_fields(CANDIDATE_W10)
    assert w9["system_prompt_sha256"] != w10["system_prompt_sha256"]
    assert w9["evidence_contract"] == w10["evidence_contract"]
    assert w9["native_tool_contract_sha256"] == w10["native_tool_contract_sha256"]
    assert candidate_identity_digest(CANDIDATE_W8) != candidate_identity_digest(CANDIDATE_W10)
    assert candidate_identity_digest(CANDIDATE_W9) != candidate_identity_digest(CANDIDATE_W10)
