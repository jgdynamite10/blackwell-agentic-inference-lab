"""Adversarial tests for the provider-neutral engine/precision contract."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from blackwell_lab.cloud.cli import main
from blackwell_lab.engines import (
    BUILTIN_VLLM_BF16_SINGLE_GPU,
    ENGINE_CONTRACT_VERSION,
    READINESS_BLOCKED,
    READINESS_CONDITIONAL,
    READINESS_READY,
    EngineContractError,
    EngineReadiness,
    ImmutableIdentity,
    TopologyDeclaration,
    declaration_from_mapping,
    evaluate_engine_contract,
    get_profile,
    list_profiles,
    register_engine_profile,
    require_supported_contract,
    reset_registry,
)
from blackwell_lab.schemas import validate_engine_contract, validate_run_manifest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONTRACT = ROOT / "examples" / "example-engine-contract.json"
EXAMPLE_MANIFEST = ROOT / "examples" / "example-run-manifest.json"

VALID_IDENTITY = {
    "model_artifact": "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16",
    "model_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "model_artifact_hash": "sha256:" + "ab" * 32,
    "container_digest": "docker.io/vllm/vllm-openai@sha256:" + "cd" * 32,
    "engine_version": "0.27.1",
}


def _declaration(**overrides) -> dict:
    payload = {
        "engine": "vllm",
        "precision": "bf16",
        "topology": {"kind": "single-gpu", "gpu_count": 1, "node_count": 1},
        "identity": dict(VALID_IDENTITY),
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _restore_registry():
    reset_registry()
    yield
    reset_registry()


class TestFailClosedMissingFields:
    @pytest.mark.parametrize("field", ["engine", "precision"])
    def test_missing_engine_or_precision_fails_closed(self, field):
        payload = _declaration()
        payload[field] = ""
        with pytest.raises(EngineContractError, match=field):
            declaration_from_mapping(payload)

    def test_missing_topology_fails_closed(self):
        payload = _declaration()
        del payload["topology"]
        with pytest.raises(EngineContractError, match="topology"):
            declaration_from_mapping(payload)

    @pytest.mark.parametrize(
        "field",
        [
            "model_artifact",
            "model_revision",
            "model_artifact_hash",
            "container_digest",
            "engine_version",
        ],
    )
    def test_missing_immutable_identity_fails_closed(self, field):
        payload = _declaration()
        payload["identity"][field] = ""
        with pytest.raises(EngineContractError, match="identity"):
            declaration_from_mapping(payload)

    def test_floating_engine_version_fails_closed(self):
        payload = _declaration()
        payload["identity"]["engine_version"] = "latest"
        with pytest.raises(EngineContractError, match="exact pin"):
            declaration_from_mapping(payload)

    def test_mutable_container_tag_fails_closed(self):
        payload = _declaration()
        payload["identity"]["container_digest"] = "docker.io/vllm/vllm-openai:v0.27.1"
        with pytest.raises(EngineContractError, match="container_digest"):
            declaration_from_mapping(payload)


class TestUnsupportedCombinations:
    def test_nvfp4_is_blocked_before_inference(self):
        declaration = declaration_from_mapping(_declaration(precision="nvfp4"))
        readiness = evaluate_engine_contract(declaration)
        assert readiness.status == READINESS_BLOCKED
        with pytest.raises(EngineContractError, match="unsupported"):
            require_supported_contract(readiness)

    def test_tensorrt_llm_is_blocked_before_inference(self):
        declaration = declaration_from_mapping(_declaration(engine="tensorrt-llm"))
        readiness = evaluate_engine_contract(declaration)
        assert readiness.status == READINESS_BLOCKED
        with pytest.raises(EngineContractError, match="unsupported"):
            require_supported_contract(readiness)

    def test_multi_gpu_and_multi_node_are_blocked_without_a_component(self):
        multi_gpu = declaration_from_mapping(
            _declaration(topology={"kind": "multi-gpu", "gpu_count": 2, "node_count": 1})
        )
        multi_node = declaration_from_mapping(
            _declaration(topology={"kind": "multi-node", "gpu_count": 1, "node_count": 2})
        )
        assert evaluate_engine_contract(multi_gpu).status == READINESS_BLOCKED
        assert evaluate_engine_contract(multi_node).status == READINESS_BLOCKED


class TestExistingVllmBf16RemainsValid:
    def test_example_gpu_manifest_without_topology_still_validates(self):
        document = json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
        assert "topology" not in document["serving"]
        assert "engine_profile_id" not in document["serving"]
        validate_run_manifest(document)

    def test_example_engine_contract_is_ready(self):
        payload = json.loads(EXAMPLE_CONTRACT.read_text(encoding="utf-8"))
        validate_engine_contract(payload)
        readiness = evaluate_engine_contract(declaration_from_mapping(payload))
        assert readiness.status == READINESS_READY
        assert readiness.profile_id == BUILTIN_VLLM_BF16_SINGLE_GPU
        assert ENGINE_CONTRACT_VERSION == "1.0.0"
        require_supported_contract(readiness)

    def test_builtin_profile_is_the_only_core_registration(self):
        profiles = list_profiles()
        assert [profile.profile_id for profile in profiles] == [BUILTIN_VLLM_BF16_SINGLE_GPU]
        assert get_profile(BUILTIN_VLLM_BF16_SINGLE_GPU).engine == "vllm"

    def test_example_manifest_remains_valid_when_optional_topology_is_added(self):
        document = json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
        document["serving"]["topology"] = {
            "kind": "single-gpu",
            "gpu_count": 1,
            "node_count": 1,
        }
        document["serving"]["engine_profile_id"] = BUILTIN_VLLM_BF16_SINGLE_GPU
        validate_run_manifest(document)


class TestComponentRegistration:
    def test_component_registers_without_modifying_the_core_contract(self):
        class _ExternalNimProfile:
            profile_id = "test-only-nim-bf16-single-gpu"
            engine = "nim"
            precision = "bf16"
            topologies = frozenset({"single-gpu"})

            def matches(self, declaration):
                return (
                    declaration.engine == self.engine
                    and declaration.precision == self.precision
                    and declaration.topology.kind in self.topologies
                )

            def evaluate(self, declaration):
                return EngineReadiness(
                    status=READINESS_CONDITIONAL,
                    profile_id=self.profile_id,
                    reasons=("component-registered NIM profile; launch is not implemented",),
                    declaration=declaration,
                )

        register_engine_profile(_ExternalNimProfile())
        declaration = declaration_from_mapping(_declaration(engine="nim"))
        readiness = evaluate_engine_contract(declaration)
        assert readiness.status == READINESS_CONDITIONAL
        assert readiness.profile_id == "test-only-nim-bf16-single-gpu"
        require_supported_contract(readiness)
        core = Path(__file__).resolve().parents[1] / "src" / "blackwell_lab" / "engines"
        for path in core.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "test-only-nim-bf16-single-gpu" not in text
        component_modules = [
            path.name for path in (core / "components").glob("*.py") if path.name != "__init__.py"
        ]
        assert component_modules == []

    def test_unknown_named_profile_fails_closed(self):
        declaration = declaration_from_mapping(_declaration(profile_id="does-not-exist"))
        with pytest.raises(EngineContractError, match="unknown engine profile"):
            evaluate_engine_contract(declaration)

    def test_required_unknown_entitlement_fails_closed(self):
        payload = _declaration(
            entitlements=[{"name": "ngc-entitlement", "required": True, "satisfied": None}]
        )
        readiness = evaluate_engine_contract(declaration_from_mapping(payload))
        assert readiness.status == READINESS_BLOCKED
        assert "ngc-entitlement" in readiness.reasons[0]


class TestCliAndSchema:
    def test_cli_lists_profiles_and_validates_the_example(self, capsys):
        assert main(["engine-contract", "--list"]) == 0
        listed = json.loads(capsys.readouterr().out)
        assert listed["credentials_required"] is False
        assert listed["profiles"][0]["profile_id"] == BUILTIN_VLLM_BF16_SINGLE_GPU
        assert main(["engine-contract", "--config", str(EXAMPLE_CONTRACT)]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["readiness"]["status"] == READINESS_READY

    def test_cli_blocks_an_unsupported_combination_before_inference(self, tmp_path, capsys):
        payload = _declaration(precision="nvfp4")
        path = tmp_path / "blocked.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert main(["engine-contract", "--config", str(path)]) == 2
        captured = capsys.readouterr()
        report = json.loads(captured.out)
        assert report["readiness"]["status"] == READINESS_BLOCKED
        assert "unsupported" in captured.err

    def test_schema_rejects_a_mutable_identity(self):
        payload = json.loads(EXAMPLE_CONTRACT.read_text(encoding="utf-8"))
        payload["identity"]["container_digest"] = "docker.io/example/vllm:latest"
        with pytest.raises(jsonschema.ValidationError):
            validate_engine_contract(payload)

    def test_topology_helpers_reject_inconsistent_counts(self):
        with pytest.raises(EngineContractError, match="single-gpu"):
            TopologyDeclaration(kind="single-gpu", gpu_count=2, node_count=1)
        with pytest.raises(EngineContractError, match="multi-gpu"):
            TopologyDeclaration(kind="multi-gpu", gpu_count=1, node_count=1)
        with pytest.raises(EngineContractError, match="40-character"):
            ImmutableIdentity(
                model_artifact="model",
                model_revision="not-a-commit",
                model_artifact_hash="sha256:" + "ab" * 32,
                container_digest="docker.io/vllm/vllm-openai@sha256:" + "cd" * 32,
                engine_version="0.27.1",
            )
