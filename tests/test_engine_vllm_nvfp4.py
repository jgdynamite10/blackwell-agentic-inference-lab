"""Offline tests for the discovered vLLM NVFP4 single-GPU profile."""

from __future__ import annotations

import ast
import hashlib
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from blackwell_lab.cloud.provenance import ProvenanceError, verify_live_provenance
from blackwell_lab.cloud.realbench import RealRunSpec, run_real_cell
from blackwell_lab.engines import (
    BUILTIN_VLLM_BF16_SINGLE_GPU,
    READINESS_BLOCKED,
    READINESS_CONDITIONAL,
    READINESS_READY,
    EngineContractError,
    declaration_from_mapping,
    evaluate_engine_contract,
    get_profile,
    list_profiles,
    load_registered_components,
    require_ready_contract,
    require_supported_contract,
    reset_registry,
)
from blackwell_lab.engines.components.vllm_nvfp4 import (
    CONTAINER_DIGEST,
    ENGINE_VERSION,
    MODEL_ARTIFACT,
    MODEL_ARTIFACT_HASH,
    MODEL_REVISION,
    PROFILE_ID,
)
from blackwell_lab.workload.model_client import DeterministicMockClient

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONTRACT = ROOT / "examples" / "example-engine-contract.json"
COMPONENT_PATH = ROOT / "src" / "blackwell_lab" / "engines" / "components" / "vllm_nvfp4.py"
OTHER_REVISION = "a9904d24bcc1d289a1950fa9d2b978c47cf903b9"
OTHER_DIGEST = "sha256:" + "ab" * 32
ALT_DIGEST = "sha256:" + "cd" * 32
INDEX_DIGEST = (
    "docker.io/vllm/vllm-openai@sha256:"
    "0a51ea5b4ae2dc5d81890e5173f54203d2a3ae0cfffe51b8fd2afd4391bfd967"
)
BF16_ARTIFACT = "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16"


def _payload(**overrides) -> dict:
    payload = {
        "engine": "vllm",
        "precision": "nvfp4",
        "topology": {"kind": "single-gpu", "gpu_count": 1, "node_count": 1},
        "identity": {
            "model_artifact": MODEL_ARTIFACT,
            "model_revision": MODEL_REVISION,
            "model_artifact_hash": OTHER_DIGEST,
            "container_digest": CONTAINER_DIGEST,
            "engine_version": ENGINE_VERSION,
        },
    }
    identity = overrides.pop("identity", None)
    payload.update(overrides)
    if identity is not None:
        payload["identity"] = identity
    return payload


def _declare(**overrides):
    return declaration_from_mapping(_payload(**overrides))


def _discovered_profile():
    load_registered_components()
    return get_profile(PROFILE_ID)


@pytest.fixture(autouse=True)
def _restore_registry():
    reset_registry()
    yield
    reset_registry()


class _SpyClient(DeterministicMockClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def stream_turn(self, messages, settings, *, deadline=None, clock=None):
        self.calls += 1
        if clock is None:
            yield from super().stream_turn(messages, settings, deadline=deadline)
        else:
            yield from super().stream_turn(messages, settings, deadline=deadline, clock=clock)


class TestDiscovery:
    def test_unique_profile_is_discovered_without_a_manual_register(self):
        assert all(profile.profile_id != PROFILE_ID for profile in list_profiles())
        readiness = evaluate_engine_contract(_declare())
        assert readiness.profile_id == PROFILE_ID
        found = [profile for profile in list_profiles() if profile.profile_id == PROFILE_ID]
        assert len(found) == 1
        assert type(found[0]).__name__ == "VllmNvfp4SingleGpuProfile"
        assert found[0].engine == "vllm"
        assert found[0].precision == "nvfp4"
        assert found[0].topologies == frozenset({"single-gpu"})

    def test_discovery_is_idempotent(self):
        first = load_registered_components()
        second = load_registered_components()
        assert first == second
        assert first.count("blackwell_lab.engines.components.vllm_nvfp4") == 1
        assert [profile.profile_id for profile in list_profiles()].count(PROFILE_ID) == 1
        evaluate_engine_contract(_declare())
        assert [profile.profile_id for profile in list_profiles()].count(PROFILE_ID) == 1


class TestEvidenceConsistentInput:
    def test_matching_pins_are_conditional_at_most_and_never_ready(self):
        assert MODEL_ARTIFACT_HASH is None
        readiness = evaluate_engine_contract(_declare())
        assert readiness.status in {READINESS_CONDITIONAL, READINESS_BLOCKED}
        assert readiness.status != READINESS_READY
        assert readiness.status == READINESS_BLOCKED
        assert readiness.profile_id == PROFILE_ID
        text = " ".join(readiness.reasons)
        assert "live execution remains blocked until the exact RTX PRO 6000 NVFP4 path" in text
        assert "artifact digest" in text
        assert "SM120" in text
        assert "tool-call" in text

    def test_evaluate_cannot_construct_ready(self):
        tree = ast.parse(COMPONENT_PATH.read_text(encoding="utf-8"))
        statuses: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            if name != "EngineReadiness":
                continue
            for keyword in node.keywords:
                if keyword.arg == "status" and isinstance(keyword.value, ast.Name):
                    statuses.append(keyword.value.id)
        assert statuses
        assert set(statuses) == {"READINESS_BLOCKED"}

    def test_both_genuine_execution_gates_reject_it(self):
        readiness = evaluate_engine_contract(_declare())
        with pytest.raises(EngineContractError):
            require_ready_contract(readiness)
        with pytest.raises(EngineContractError):
            require_supported_contract(readiness)

    def test_satisfied_entitlement_still_cannot_be_ready(self):
        payload = _payload(
            entitlements=[{"name": "optional-note", "required": False, "satisfied": True}]
        )
        readiness = evaluate_engine_contract(declaration_from_mapping(payload))
        assert readiness.status == READINESS_BLOCKED
        with pytest.raises(EngineContractError):
            require_ready_contract(readiness)


class TestIdentityAndTopologyRejections:
    def test_different_model_revision_is_blocked(self):
        identity = _payload()["identity"]
        identity["model_revision"] = OTHER_REVISION
        readiness = evaluate_engine_contract(_declare(identity=identity))
        assert readiness.status == READINESS_BLOCKED
        assert readiness.profile_id == PROFILE_ID
        assert any("model revision" in reason for reason in readiness.reasons)

    def test_different_artifact_digest_is_blocked(self):
        for digest in (OTHER_DIGEST, ALT_DIGEST):
            identity = _payload()["identity"]
            identity["model_artifact_hash"] = digest
            readiness = evaluate_engine_contract(_declare(identity=identity))
            assert readiness.status == READINESS_BLOCKED
            assert any("artifact digest" in reason for reason in readiness.reasons)

    def test_different_container_digest_is_blocked(self):
        identity = _payload()["identity"]
        identity["container_digest"] = INDEX_DIGEST
        readiness = evaluate_engine_contract(_declare(identity=identity))
        assert readiness.status == READINESS_BLOCKED
        assert any("container digest" in reason for reason in readiness.reasons)

    def test_floating_or_different_engine_version_fails(self):
        floating = _payload()
        floating["identity"]["engine_version"] = "latest"
        with pytest.raises(EngineContractError, match="exact pin"):
            declaration_from_mapping(floating)
        identity = _payload()["identity"]
        identity["engine_version"] = "0.28.0"
        readiness = evaluate_engine_contract(_declare(identity=identity))
        assert readiness.status == READINESS_BLOCKED
        assert any("engine version" in reason for reason in readiness.reasons)

    @pytest.mark.parametrize("precision", ["bf16", "fp8", "w4a16"])
    def test_other_precisions_do_not_match(self, precision):
        declaration = _declare(precision=precision)
        profile = _discovered_profile()
        assert profile.matches(declaration) is False
        readiness = evaluate_engine_contract(declaration)
        assert readiness.profile_id != PROFILE_ID

    @pytest.mark.parametrize(
        "topology",
        [
            {"kind": "multi-gpu", "gpu_count": 2, "node_count": 1},
            {"kind": "multi-node", "gpu_count": 1, "node_count": 2},
        ],
    )
    def test_multi_gpu_and_multi_node_do_not_match(self, topology):
        declaration = _declare(topology=topology)
        assert _discovered_profile().matches(declaration) is False
        pinned = _declare(topology=topology, profile_id=PROFILE_ID)
        readiness = evaluate_engine_contract(pinned)
        assert readiness.status == READINESS_BLOCKED
        assert readiness.profile_id == PROFILE_ID

    def test_bf16_model_artifact_is_not_accepted_as_nvfp4(self):
        identity = _payload()["identity"]
        identity["model_artifact"] = BF16_ARTIFACT
        readiness = evaluate_engine_contract(_declare(identity=identity))
        assert readiness.status == READINESS_BLOCKED
        assert any("model artifact" in reason for reason in readiness.reasons)
        assert MODEL_ARTIFACT != BF16_ARTIFACT
        assert MODEL_REVISION != OTHER_REVISION

    def test_required_entitlement_false_or_unknown_is_blocked(self):
        for satisfied in (False, None):
            payload = _payload(
                entitlements=[
                    {
                        "name": "rtx-pro-6000-nvfp4-path",
                        "required": True,
                        "satisfied": satisfied,
                    }
                ]
            )
            readiness = evaluate_engine_contract(declaration_from_mapping(payload))
            assert readiness.status == READINESS_BLOCKED
            assert any("rtx-pro-6000-nvfp4-path" in reason for reason in readiness.reasons)


class TestInferenceIsRefusedBeforeClientUse:
    def test_spy_client_receives_zero_calls(self):
        spec = RealRunSpec(
            profile_name="interactive",
            concurrency=1,
            comparison_mode="provider-native",
            instance_type="g9-fake-gpu-plan",
            region="us-fake-1",
            list_price_usd_per_hour=2.5,
            price_source_date="2026-09-06",
            model={
                "artifact": MODEL_ARTIFACT,
                "revision": MODEL_REVISION,
                "artifact_hash": OTHER_DIGEST,
                "precision": "nvfp4",
            },
            engine="vllm",
            engine_version=ENGINE_VERSION,
            container_digest=CONTAINER_DIGEST,
        )
        client = _SpyClient()
        with pytest.raises(EngineContractError):
            run_real_cell(
                spec,
                client,
                host={"gpu_model": "NVIDIA RTX PRO 6000 Blackwell Server Edition"},
                sampler_factory=lambda: None,
            )
        assert client.calls == 0

    def test_live_provenance_refuses_the_cell(self, tmp_path):
        artifact_dir = tmp_path / "model"
        artifact_dir.mkdir()
        payload = b"synthetic nvfp4 bytes"
        (artifact_dir / "model.safetensors").write_bytes(payload)
        file_hex = hashlib.sha256(payload).hexdigest()
        manifest = tmp_path / "digests.sha256"
        manifest.write_text(f"{file_hex}  model.safetensors\n", encoding="utf-8")
        aggregate = hashlib.sha256(f"{file_hex}  model.safetensors".encode()).hexdigest()
        artifact_hash = f"sha256:{aggregate}"
        approved = {
            "serving": {
                "image": "vllm/vllm-openai:v0.27.1",
                "container_digest": CONTAINER_DIGEST,
                "engine": "vllm",
                "engine_version": ENGINE_VERSION,
                "container_cuda_runtime_version": "13.0",
            },
            "model": {
                "artifact": MODEL_ARTIFACT,
                "revision": MODEL_REVISION,
                "artifact_hash": artifact_hash,
                "precision": "nvfp4",
            },
            "cloud": {"instance_type": "g8-gpu-rtx6000b-1", "region": "us-ord"},
            "host": {
                "storage_description": "local NVMe",
                "network_description": "private VLAN",
                "gpu_count": 1,
            },
            "expected_gpu_model": "RTX 6000 Blackwell",
        }

        def runner(argv: list[str]) -> str:
            if argv[:2] == ["docker", "inspect"]:
                return CONTAINER_DIGEST + "\n"
            if argv[:2] == ["docker", "exec"]:
                return "13.0\n"
            raise AssertionError(f"unexpected command: {argv[:2]}")

        def http_get(url: str) -> dict:
            if url == "http://127.0.0.1:8000/version":
                return {"version": ENGINE_VERSION}
            raise AssertionError(f"unexpected URL: {url}")

        def http_request(method: str, url: str, headers: dict[str, str]) -> object:
            if method == "PUT" and url.endswith("/v1/token"):
                return ["test-metadata-session-not-a-credential"]
            if method == "GET" and url.endswith("/v1/instance"):
                return {
                    "id": 12345678,
                    "region": "us-ord",
                    "type": "g8-gpu-rtx6000b-1",
                    "tags": ["blackwell-lab", "run:bwlab-20260906-pilot"],
                }
            raise AssertionError(f"unexpected metadata request: {method} {url}")

        with pytest.raises(ProvenanceError, match="cell will not run"):
            verify_live_provenance(
                run_tag="bwlab-20260906-pilot",
                approved=approved,
                ledger={
                    "resources": [
                        {
                            "type": "linode_instance",
                            "provider_id": "12345678",
                            "region": "us-ord",
                        }
                    ]
                },
                artifact_dir=artifact_dir,
                digest_manifest=manifest,
                serving_base_url="http://127.0.0.1:8000",
                runner=runner,
                http_get=http_get,
                http_request=http_request,
                host_facts={
                    "hostname": "bwlab-pilot",
                    "os": "Ubuntu 24.04",
                    "kernel": "6.8.0",
                    "cpu_model": "AMD EPYC",
                    "cpu_cores": 14,
                    "memory_gb": 100,
                    "storage_description": "local NVMe",
                    "network_description": "private VLAN",
                },
                gpu_facts={
                    "gpu_model": "NVIDIA RTX 6000 Blackwell",
                    "gpu_count": 1,
                    "gpu_memory_gb": 96,
                    "driver_version": "580.65.06",
                    "driver_max_cuda_version": "13.0",
                },
            )


class TestImportIsOffline:
    def test_component_source_has_no_launch_or_network_operations(self):
        source = COMPONENT_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported.isdisjoint(
            {
                "urllib",
                "requests",
                "httpx",
                "socket",
                "subprocess",
                "huggingface_hub",
                "docker",
                "openai",
            }
        )
        forbidden = ("vllm serve", "docker run", "subprocess", "os.system", "urlopen")
        assert all(token not in source for token in forbidden)

    def test_reload_performs_no_network_subprocess_or_download(self, monkeypatch):
        def fail(*args, **kwargs):
            raise AssertionError("offline profile import performed a live operation")

        monkeypatch.setattr(socket, "create_connection", fail)
        monkeypatch.setattr(urllib.request, "urlopen", fail)
        monkeypatch.setattr(subprocess, "run", fail)
        monkeypatch.setattr(subprocess, "Popen", fail)
        import importlib

        import blackwell_lab.engines.components.vllm_nvfp4 as module

        importlib.reload(module)
        assert module.PROFILE_ID == PROFILE_ID
        assert module.LIVE_EXECUTION_AUTHORIZED is False


class TestBuiltinBf16Unchanged:
    def test_existing_vllm_bf16_single_gpu_remains_ready(self):
        import json

        payload = json.loads(EXAMPLE_CONTRACT.read_text(encoding="utf-8"))
        readiness = evaluate_engine_contract(declaration_from_mapping(payload))
        assert readiness.status == READINESS_READY
        assert readiness.profile_id == BUILTIN_VLLM_BF16_SINGLE_GPU
        builtin = get_profile(BUILTIN_VLLM_BF16_SINGLE_GPU)
        assert builtin.engine == "vllm"
        assert builtin.precision == "bf16"
        assert builtin.topologies == frozenset({"single-gpu"})
        require_ready_contract(readiness)
        require_supported_contract(readiness)
