"""Native OpenAI tool schemas, stream assembly, and vLLM parser-flag gates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blackwell_lab.cloud.bootstrap_pins import (
    APPROVED_VLLM_EXTRA_ARGS,
    parse_env_assignments,
    validate_candidate_pins,
    validate_vllm_parser_flags,
)
from blackwell_lab.workload.model_client import NativeToolCallError
from blackwell_lab.workload.native_tools import (
    REASONING_PARSER,
    TOOL_CALL_PARSER,
    TOOL_CALL_TRANSPORT,
    TOOL_CHOICE,
    ToolCallAssembler,
    openai_tool_definitions,
)
from blackwell_lab.workload.tools import TOOL_SPECS

EXAMPLE = (
    Path(__file__).resolve().parents[1] / "infra" / "akamai" / "bootstrap" / "bootstrap.env.example"
)


class TestOpenAIToolDefinitions:
    def test_every_tool_spec_is_projected_deterministically(self):
        first = openai_tool_definitions()
        second = openai_tool_definitions()
        assert first == second
        assert [entry["function"]["name"] for entry in first] == sorted(TOOL_SPECS)
        for entry in first:
            function = entry["function"]
            spec = TOOL_SPECS[function["name"]]
            assert entry["type"] == "function"
            assert function["description"]
            parameters = function["parameters"]
            assert parameters["type"] == "object"
            assert parameters["additionalProperties"] is False
            assert parameters["required"] == sorted(spec["required"])
            assert set(parameters["properties"]) == set(spec["required"]) | set(spec["optional"])
            for name, python_type in {**spec["required"], **spec["optional"]}.items():
                expected = {str: "string", int: "integer"}[python_type]
                assert parameters["properties"][name] == {"type": expected}

    def test_transport_identity_constants_are_frozen(self):
        assert TOOL_CALL_TRANSPORT == "openai-native-tools"
        assert TOOL_CALL_PARSER == "qwen3_coder"
        assert REASONING_PARSER == "nemotron_v3"
        assert TOOL_CHOICE == "auto"


class TestAssemblerIndexes:
    def test_valid_index_emits_privacy_safe_first_delta(self):
        assembler = ToolCallAssembler()
        events = assembler.consume_delta(
            {"tool_calls": [{"index": 0, "id": "secret-id", "function": {"name": "get"}}]}
        )
        assert [event.kind for event in events] == ["native_tool_call_delta"]
        assert events[0].text == ""
        assert events[0].tool_call is None
        later = assembler.consume_delta(
            {
                "tool_calls": [
                    {"index": 0, "function": {"name": "service_health", "arguments": "{}"}}
                ]
            }
        )
        assert later == []

    @pytest.mark.parametrize("bad_index", [True, "0", -1, 1.5, None])
    def test_invalid_indexes_are_not_coerced_to_zero(self, bad_index):
        assembler = ToolCallAssembler()
        fragment: dict = {"function": {"name": "get_service_health", "arguments": "{}"}}
        if bad_index is not None:
            fragment["index"] = bad_index
        events = assembler.consume_delta({"tool_calls": [fragment]})
        assert events == []
        error = assembler.finalize()
        assert isinstance(error, NativeToolCallError)
        assert error.category == "malformed_tool_call_index"
        blob = json.dumps(error.diagnostics)
        assert "get_service_health" not in blob
        assert "secret" not in blob
        assert error.diagnostics["tool_call_count"] == 0


class TestAssemblerPrivacy:
    def test_diagnostics_never_include_raw_model_output(self):
        assembler = ToolCallAssembler()
        assembler.consume_delta(
            {"content": 'TOOL_CALL: {"tool": "get_service_health", "secret": "nope"}'}
        )
        error = assembler.finalize()
        blob = json.dumps(error.diagnostics)
        assert "TOOL_CALL" not in blob
        assert "secret" not in blob
        assert "nope" not in blob
        assert str(error) == "legacy_text_tool_call"
        assert "nope" not in str(error)


class TestVllmParserFlags:
    def test_approved_extra_args_include_the_nvidia_trio(self):
        assert "--reasoning-parser nemotron_v3" in APPROVED_VLLM_EXTRA_ARGS
        assert "--tool-call-parser qwen3_coder" in APPROVED_VLLM_EXTRA_ARGS
        assert "--enable-auto-tool-choice" in APPROVED_VLLM_EXTRA_ARGS
        assert validate_vllm_parser_flags(APPROVED_VLLM_EXTRA_ARGS) == []
        example = EXAMPLE.read_text(encoding="utf-8")
        assert parse_env_assignments(example)["VLLM_EXTRA_ARGS"] == APPROVED_VLLM_EXTRA_ARGS
        assert validate_candidate_pins(example) == []

    @pytest.mark.parametrize(
        "missing",
        [
            "--reasoning-parser nemotron_v3",
            "--tool-call-parser qwen3_coder",
            "--enable-auto-tool-choice",
        ],
    )
    def test_omitting_any_nvidia_flag_is_rejected(self, missing):
        stripped = APPROVED_VLLM_EXTRA_ARGS.replace(missing, "").replace("  ", " ").strip()
        problems = validate_vllm_parser_flags(stripped)
        assert problems
        assert any(missing.split()[0] in problem for problem in problems)

    def test_incompatible_duplicate_parsers_are_rejected(self):
        extra = APPROVED_VLLM_EXTRA_ARGS + " --reasoning-parser hermes --tool-call-parser hermes"
        problems = validate_vllm_parser_flags(extra)
        assert any("duplicate --reasoning-parser" in problem for problem in problems)
        assert any("duplicate --tool-call-parser" in problem for problem in problems)

    def test_wrong_parser_values_are_rejected(self):
        swapped = APPROVED_VLLM_EXTRA_ARGS.replace("nemotron_v3", "hermes").replace(
            "qwen3_coder", "hermes"
        )
        problems = validate_vllm_parser_flags(swapped)
        assert any("nemotron_v3" in problem for problem in problems)
        assert any("qwen3_coder" in problem for problem in problems)

    def test_duplicate_enable_auto_tool_choice_is_rejected(self):
        extra = APPROVED_VLLM_EXTRA_ARGS + " --enable-auto-tool-choice"
        problems = validate_vllm_parser_flags(extra)
        assert any("duplicate --enable-auto-tool-choice" in problem for problem in problems)
