"""Offline tests for the conditional TensorRT-LLM single-GPU profiles."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
from pathlib import Path

import pytest

from blackwell_lab.cloud.provenance import (
    METADATA_ACCEPT,
    METADATA_INSTANCE_URL,
    METADATA_TOKEN_URL,
    ProvenanceError,
    verify_live_provenance,
)
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
    register_engine_profile,
    require_ready_contract,
    require_supported_contract,
    reset_registry,
)
from blackwell_lab.engines.adapters import require_real_spec_contract
from blackwell_lab.engines.components import tensorrt_llm
from blackwell_lab.engines.components.tensorrt_llm import (
    BF16_VIABILITY,
    DOCUMENTED_ENGINE_VERSION,
    NVFP4_VIABILITY,
    PROFILE_BF16,
    PROFILE_NVFP4,
    RELEASE_REPOSITORY,
    TensorRtLlmSingleGpuProfile,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONTRACT = ROOT / "examples" / "example-engine-contract.json"
MODULE_PATH = Path(tensorrt_llm.__file__)

REVISION = "a" * 40
ARTIFACT_HASH = "sha256:" + "ab" * 32
RELEASE_DIGEST = RELEASE_REPOSITORY + "@sha256:" + "cd" * 32
DEVEL_DIGEST = "nvcr.io/nvidia/tensorrt-llm/devel@sha256:" + "cd" * 32
FOREIGN_DIGEST = "docker.io/vllm/vllm-openai@sha256:" + "cd" * 32

HOST = {
    "operating_system": "Ubuntu 24.04 LTS",
    "cpu_model": "Synthetic Test CPU",
    "vcpu_count": 16,
    "system_memory_gib": 176.0,
    "storage_description": "plan NVMe (not measured)",
    "network_description": "plan default networking",
    "gpu_model": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
    "gpu_count": 1,
    "gpu_memory_gb": 96.0,
    "driver_version": "580.65.06",
    "driver_max_cuda_version": "13.0",
}
HOST_FACTS = {
    "hostname": "bwlab-pilot",
    "os": "Ubuntu 24.04",
    "kernel": "6.8.0",
    "cpu_model": "AMD EPYC",
    "cpu_cores": 14,
    "memory_gb": 100,
    "storage_description": "local NVMe",
    "network_description": "private VLAN",
}
GPU_FACTS = {
    "gpu_model": "NVIDIA RTX 6000 Blackwell",
    "gpu_count": 1,
    "gpu_memory_gb": 96,
    "driver_version": "580.65.06",
    "driver_max_cuda_version": "13.0",
}


@pytest.fixture(autouse=True)
def _restore_registry():
    reset_registry()
    yield
    reset_registry()


def _artifact(precision: str) -> str:
    return {
        "bf16": tensorrt_llm.NEMOTRON_BF16_ARTIFACT,
        "nvfp4": tensorrt_llm.NEMOTRON_NVFP4_ARTIFACT,
    }.get(precision, tensorrt_llm.NEMOTRON_BF16_ARTIFACT)


def _payload(precision: str = "bf16", **overrides) -> dict:
    payload = {
        "engine": "tensorrt-llm",
        "precision": precision,
        "topology": {"kind": "single-gpu", "gpu_count": 1, "node_count": 1},
        "identity": {
            "model_artifact": _artifact(precision),
            "model_revision": REVISION,
            "model_artifact_hash": ARTIFACT_HASH,
            "container_digest": RELEASE_DIGEST,
            "engine_version": DOCUMENTED_ENGINE_VERSION,
        },
    }
    identity_overrides = overrides.pop("identity", None)
    payload.update(overrides)
    if identity_overrides:
        payload["identity"].update(identity_overrides)
    return payload


def _profile_ids() -> list[str]:
    return [profile.profile_id for profile in list_profiles()]


def _component_ids() -> set[str]:
    return {profile_id for profile_id in _profile_ids() if profile_id.startswith("tensorrt-llm-")}


class _SpyClient:
    def __init__(self) -> None:
        self.calls = 0

    def stream_turn(self, messages, settings, *, deadline=None, clock=None):
        self.calls += 1
        raise AssertionError("TensorRT-LLM classification must not call a client")


def _spec(precision: str, **overrides) -> RealRunSpec:
    model = {
        "artifact": _artifact(precision),
        "revision": REVISION,
        "artifact_hash": ARTIFACT_HASH,
        "precision": precision,
    }
    defaults = dict(
        profile_name="interactive",
        concurrency=1,
        comparison_mode="provider-native",
        instance_type="g9-fake-gpu-plan",
        region="us-fake-1",
        list_price_usd_per_hour=2.5,
        price_source_date="2026-09-06",
        model=model,
        engine="tensorrt-llm",
        engine_version=DOCUMENTED_ENGINE_VERSION,
        container_digest=RELEASE_DIGEST,
        repetitions=1,
        warmup_passes=0,
        tasks_per_repetition=1,
        run_label="trtllm-offline",
        topology_kind="single-gpu",
        gpu_count=1,
        node_count=1,
    )
    defaults.update(overrides)
    return RealRunSpec(**defaults)


def _forbid_sampler():
    raise AssertionError("a measured cell must not start")


class TestDiscovery:
    def test_profiles_are_discovered_without_the_offline_cli(self):
        reset_registry()
        assert PROFILE_BF16 not in _profile_ids()
        assert PROFILE_NVFP4 not in _profile_ids()
        loaded = load_registered_components()
        assert "blackwell_lab.engines.components.tensorrt_llm" in loaded
        assert _component_ids() == {PROFILE_BF16, PROFILE_NVFP4}
        assert get_profile(PROFILE_BF16).precision == "bf16"
        assert get_profile(PROFILE_NVFP4).precision == "nvfp4"
        assert get_profile(PROFILE_BF16).engine == "tensorrt-llm"
        assert get_profile(PROFILE_NVFP4).topologies == frozenset({"single-gpu"})

    def test_discovery_is_idempotent_and_order_independent(self):
        reset_registry()
        first = load_registered_components()
        ids_first = _component_ids()
        second = load_registered_components()
        assert second == first
        assert _component_ids() == ids_first
        assert _profile_ids().count(PROFILE_BF16) == 1
        assert _profile_ids().count(PROFILE_NVFP4) == 1

        bf16_then_nvfp4 = (
            evaluate_engine_contract(declaration_from_mapping(_payload("bf16"))),
            evaluate_engine_contract(declaration_from_mapping(_payload("nvfp4"))),
        )
        reset_registry()
        nvfp4_then_bf16 = (
            evaluate_engine_contract(declaration_from_mapping(_payload("nvfp4"))),
            evaluate_engine_contract(declaration_from_mapping(_payload("bf16"))),
        )
        assert bf16_then_nvfp4[0].status == nvfp4_then_bf16[1].status == READINESS_CONDITIONAL
        assert bf16_then_nvfp4[1].status == nvfp4_then_bf16[0].status == READINESS_CONDITIONAL
        assert bf16_then_nvfp4[0].reasons == nvfp4_then_bf16[1].reasons
        assert bf16_then_nvfp4[1].reasons == nvfp4_then_bf16[0].reasons

        reset_registry()
        register_engine_profile(
            TensorRtLlmSingleGpuProfile(
                profile_id=PROFILE_NVFP4,
                precision="nvfp4",
                model_artifact=tensorrt_llm.NEMOTRON_NVFP4_ARTIFACT,
                viability=NVFP4_VIABILITY,
            )
        )
        register_engine_profile(
            TensorRtLlmSingleGpuProfile(
                profile_id=PROFILE_BF16,
                precision="bf16",
                model_artifact=tensorrt_llm.NEMOTRON_BF16_ARTIFACT,
                viability=BF16_VIABILITY,
            )
        )
        # Classification follows the declaration, not registration order.
        # Avoid evaluate_engine_contract here: it would rediscover the module.
        reversed_ids = _component_ids()
        assert reversed_ids == ids_first
        nvfp4 = get_profile(PROFILE_NVFP4)
        bf16 = get_profile(PROFILE_BF16)
        nvfp4_decl = declaration_from_mapping(_payload("nvfp4"))
        bf16_decl = declaration_from_mapping(_payload("bf16"))
        assert nvfp4.evaluate(nvfp4_decl).status == READINESS_CONDITIONAL
        assert bf16.evaluate(bf16_decl).status == READINESS_CONDITIONAL
        assert not nvfp4.matches(bf16_decl)
        assert not bf16.matches(nvfp4_decl)

    def test_repeated_discovery_creates_no_duplicate_registration(self):
        reset_registry()
        load_registered_components()
        load_registered_components()
        assert _profile_ids().count(PROFILE_BF16) == 1
        assert _profile_ids().count(PROFILE_NVFP4) == 1
        reset_registry()
        load_registered_components()
        load_registered_components()
        assert _profile_ids().count(PROFILE_BF16) == 1
        assert _profile_ids().count(PROFILE_NVFP4) == 1

    def test_import_performs_no_external_operation(self, monkeypatch):
        calls: list[str] = []

        def _forbidden(*_args, **_kwargs):
            calls.append("called")
            raise AssertionError("import performed an external operation")

        import socket
        import subprocess
        import urllib.request

        monkeypatch.setattr(socket, "create_connection", _forbidden)
        monkeypatch.setattr(subprocess, "run", _forbidden)
        monkeypatch.setattr(subprocess, "Popen", _forbidden)
        monkeypatch.setattr(urllib.request, "urlopen", _forbidden)
        reset_registry()
        importlib.reload(tensorrt_llm)
        assert calls == []
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert imported == [
            "__future__",
            "re",
            "blackwell_lab.engines.contract",
            "blackwell_lab.engines.registry",
        ]


class TestClassification:
    @pytest.mark.parametrize("precision", ["bf16", "nvfp4"])
    def test_evidence_consistent_declaration_is_conditional_never_ready(self, precision):
        readiness = evaluate_engine_contract(declaration_from_mapping(_payload(precision)))
        assert readiness.status == READINESS_CONDITIONAL
        assert readiness.status != READINESS_READY
        expected = PROFILE_BF16 if precision == "bf16" else PROFILE_NVFP4
        viability = BF16_VIABILITY if precision == "bf16" else NVFP4_VIABILITY
        assert readiness.profile_id == expected
        text = " ".join(readiness.reasons)
        assert "exact supported Nemotron 3.5 Lightning TensorRT-LLM profile" in text
        assert "exact conversion/build artifact and immutable digest" in text
        assert "exact container/runtime identity" in text
        assert "RTX PRO 6000 live behavior" in text
        assert viability in readiness.reasons
        assert "native OpenAI tool-calling compatibility" in text
        assert "reasoning/parser compatibility" in text
        assert "entitlement or registry-access requirements" in text
        assert "SM120 support alone does not establish a functioning Nemotron profile" in text
        with pytest.raises(EngineContractError, match="not authorized for genuine inference"):
            require_ready_contract(readiness)
        with pytest.raises(EngineContractError, match="not authorized for genuine inference"):
            require_supported_contract(readiness)

    @pytest.mark.parametrize("satisfied", [False, None])
    def test_required_entitlement_false_or_null_is_blocked(self, satisfied):
        for precision in ("bf16", "nvfp4"):
            readiness = evaluate_engine_contract(
                declaration_from_mapping(
                    _payload(
                        precision,
                        entitlements=[
                            {
                                "name": "ngc-registry-access",
                                "required": True,
                                "satisfied": satisfied,
                            }
                        ],
                    )
                )
            )
            assert readiness.status == READINESS_BLOCKED
            assert readiness.profile_id == (PROFILE_BF16 if precision == "bf16" else PROFILE_NVFP4)
            assert "ngc-registry-access" in readiness.reasons[0]
            with pytest.raises(EngineContractError, match="ngc-registry-access"):
                require_ready_contract(readiness)
            with pytest.raises(EngineContractError, match="ngc-registry-access"):
                require_supported_contract(readiness)

    def test_satisfied_entitlement_stays_conditional(self):
        readiness = evaluate_engine_contract(
            declaration_from_mapping(
                _payload(
                    "bf16",
                    entitlements=[
                        {"name": "ngc-registry-access", "required": True, "satisfied": True}
                    ],
                )
            )
        )
        assert readiness.status == READINESS_CONDITIONAL
        with pytest.raises(EngineContractError, match="not authorized"):
            require_supported_contract(readiness)

    @pytest.mark.parametrize(
        ("precision", "pinned"),
        [("nvfp4", PROFILE_BF16), ("bf16", PROFILE_NVFP4), ("fp8", PROFILE_BF16)],
    )
    def test_wrong_precision_is_blocked(self, precision, pinned):
        readiness = evaluate_engine_contract(
            declaration_from_mapping(_payload(precision, profile_id=pinned))
        )
        assert readiness.status == READINESS_BLOCKED
        assert readiness.profile_id == pinned
        with pytest.raises(EngineContractError, match="does not match"):
            require_supported_contract(readiness)

    def test_unsupported_precision_does_not_match_either_profile(self):
        readiness = evaluate_engine_contract(declaration_from_mapping(_payload("fp8")))
        assert readiness.status == READINESS_BLOCKED
        assert readiness.profile_id is None
        assert "unsupported" in readiness.reasons[0]

    @pytest.mark.parametrize("version", ["1.2.1", "1.3.0rc28", "v1.1.0", "0.17.0"])
    def test_wrong_engine_version_is_blocked(self, version):
        readiness = evaluate_engine_contract(
            declaration_from_mapping(_payload("nvfp4", identity={"engine_version": version}))
        )
        assert readiness.status == READINESS_BLOCKED
        assert readiness.profile_id == PROFILE_NVFP4
        assert "1.1.0" in readiness.reasons[0]
        with pytest.raises(EngineContractError, match=r"1\.1\.0"):
            require_ready_contract(readiness)

    @pytest.mark.parametrize(
        "digest",
        [FOREIGN_DIGEST, DEVEL_DIGEST, RELEASE_REPOSITORY + ":1.1.0@sha256:" + "cd" * 32],
    )
    def test_wrong_container_identity_is_blocked(self, digest):
        readiness = evaluate_engine_contract(
            declaration_from_mapping(_payload("bf16", identity={"container_digest": digest}))
        )
        assert readiness.status == READINESS_BLOCKED
        assert "container identity" in readiness.reasons[0]

    def test_wrong_model_artifact_is_blocked(self):
        readiness = evaluate_engine_contract(
            declaration_from_mapping(
                _payload(
                    "bf16",
                    identity={"model_artifact": "nvidia/Qwen3-30B-A3B-FP4"},
                )
            )
        )
        assert readiness.status == READINESS_BLOCKED
        assert readiness.profile_id == PROFILE_BF16
        assert "checkpoint" in readiness.reasons[0]

    def test_cross_precision_artifact_is_blocked(self):
        readiness = evaluate_engine_contract(
            declaration_from_mapping(
                _payload(
                    "bf16",
                    identity={"model_artifact": tensorrt_llm.NEMOTRON_NVFP4_ARTIFACT},
                )
            )
        )
        assert readiness.status == READINESS_BLOCKED
        assert readiness.profile_id == PROFILE_BF16

    @pytest.mark.parametrize(
        "topology",
        [
            {"kind": "multi-gpu", "gpu_count": 2, "node_count": 1},
            {"kind": "multi-node", "gpu_count": 2, "node_count": 2},
        ],
    )
    def test_multi_gpu_and_multi_node_do_not_match(self, topology):
        unnamed = evaluate_engine_contract(
            declaration_from_mapping(_payload("bf16", topology=topology))
        )
        assert unnamed.status == READINESS_BLOCKED
        assert unnamed.profile_id is None
        assert "unsupported" in unnamed.reasons[0]
        pinned = evaluate_engine_contract(
            declaration_from_mapping(_payload("nvfp4", topology=topology, profile_id=PROFILE_NVFP4))
        )
        assert pinned.status == READINESS_BLOCKED
        assert pinned.profile_id == PROFILE_NVFP4
        assert "does not match" in pinned.reasons[0]

    def test_floating_and_malformed_identities_fail_closed(self):
        floating = _payload("bf16")
        floating["identity"]["engine_version"] = "latest"
        with pytest.raises(EngineContractError, match="exact pin"):
            declaration_from_mapping(floating)
        tagged = _payload("nvfp4")
        tagged["identity"]["container_digest"] = RELEASE_REPOSITORY + ":latest"
        with pytest.raises(EngineContractError, match="container_digest"):
            declaration_from_mapping(tagged)
        artifact = _payload("bf16")
        artifact["identity"]["model_artifact"] = "latest"
        with pytest.raises(EngineContractError, match="exact pin"):
            declaration_from_mapping(artifact)
        with pytest.raises(EngineContractError, match="single-gpu"):
            declaration_from_mapping(
                _payload("bf16", topology={"kind": "single-gpu", "gpu_count": 2, "node_count": 1})
            )
        with pytest.raises(EngineContractError, match="entitlement must be an object"):
            declaration_from_mapping(_payload("bf16", entitlements=["ngc"]))

    def test_contradictory_version_and_container_are_both_reported(self):
        readiness = evaluate_engine_contract(
            declaration_from_mapping(
                _payload(
                    "nvfp4",
                    identity={
                        "engine_version": "1.2.1",
                        "container_digest": FOREIGN_DIGEST,
                        "model_artifact": tensorrt_llm.NEMOTRON_BF16_ARTIFACT,
                    },
                )
            )
        )
        assert readiness.status == READINESS_BLOCKED
        text = " ".join(readiness.reasons)
        assert "checkpoint" in text
        assert "1.1.0" in text
        assert "container identity" in text


class TestExecutionGates:
    @pytest.mark.parametrize("precision", ["bf16", "nvfp4"])
    def test_real_run_rejects_before_a_client_or_cell(self, precision):
        constructed = {"count": 0}

        class _CountingClient(_SpyClient):
            def __init__(self) -> None:
                constructed["count"] += 1
                super().__init__()

        spec = _spec(precision)
        with pytest.raises(EngineContractError, match="not authorized for genuine inference"):
            require_real_spec_contract(spec)
            _CountingClient()
        assert constructed["count"] == 0

        client = _SpyClient()
        with pytest.raises(EngineContractError, match="not authorized for genuine inference"):
            run_real_cell(spec, client, host=HOST, sampler_factory=_forbid_sampler)
        assert client.calls == 0
        assert constructed["count"] == 0

    @pytest.mark.parametrize("precision", ["bf16", "nvfp4"])
    def test_live_provenance_refuses_before_a_cell(self, precision, tmp_path):
        artifact_dir = tmp_path / "model"
        artifact_dir.mkdir()
        payload = b"synthetic-bytes"
        (artifact_dir / "model.safetensors").write_bytes(payload)
        file_hex = hashlib.sha256(payload).hexdigest()
        manifest = tmp_path / "digests.sha256"
        line = f"{file_hex}  model.safetensors"
        manifest.write_text(line + "\n", encoding="utf-8")
        aggregate = "sha256:" + hashlib.sha256(line.encode()).hexdigest()
        approved = {
            "serving": {
                "image": RELEASE_DIGEST,
                "container_digest": RELEASE_DIGEST,
                "engine": "tensorrt-llm",
                "engine_version": DOCUMENTED_ENGINE_VERSION,
                "container_cuda_runtime_version": "13.0",
            },
            "model": {
                "artifact": _artifact(precision),
                "revision": REVISION,
                "artifact_hash": aggregate,
                "precision": precision,
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
                return RELEASE_DIGEST + "\n"
            if argv[:2] == ["docker", "exec"]:
                return "13.0\n"
            raise AssertionError(argv[:2])

        def http_get(url: str) -> dict:
            if url == "http://127.0.0.1:8000/version":
                return {"version": DOCUMENTED_ENGINE_VERSION}
            raise AssertionError(url)

        def http_request(method: str, url: str, headers: dict[str, str]) -> object:
            if method == "PUT" and url == METADATA_TOKEN_URL:
                return ["test-metadata-session-not-a-credential"]
            if method == "GET" and url == METADATA_INSTANCE_URL:
                assert headers["Accept"] == METADATA_ACCEPT
                return {
                    "id": 12345678,
                    "region": "us-ord",
                    "type": "g8-gpu-rtx6000b-1",
                    "tags": ["blackwell-lab", "run:bwlab-20260906-pilot"],
                }
            raise AssertionError(f"{method} {url}")

        with pytest.raises(ProvenanceError, match="not authorized for genuine inference"):
            verify_live_provenance(
                run_tag="bwlab-20260906-pilot",
                approved=approved,
                ledger={
                    "run_tag": "bwlab-20260906-pilot",
                    "reconciled": True,
                    "resources": [
                        {
                            "address": "linode_instance.gpu_baseline",
                            "type": "linode_instance",
                            "provider_id": "12345678",
                            "region": "us-ord",
                        }
                    ],
                },
                artifact_dir=artifact_dir,
                digest_manifest=manifest,
                serving_base_url="http://127.0.0.1:8000",
                runner=runner,
                http_get=http_get,
                http_request=http_request,
                host_facts=HOST_FACTS,
                gpu_facts=GPU_FACTS,
            )


class TestExistingVllmUnchanged:
    def test_vllm_bf16_single_gpu_remains_ready(self):
        load_registered_components()
        payload = json.loads(EXAMPLE_CONTRACT.read_text(encoding="utf-8"))
        readiness = evaluate_engine_contract(declaration_from_mapping(payload))
        assert readiness.status == READINESS_READY
        assert readiness.profile_id == BUILTIN_VLLM_BF16_SINGLE_GPU
        require_ready_contract(readiness)
        require_supported_contract(readiness)
        builtin = get_profile(BUILTIN_VLLM_BF16_SINGLE_GPU)
        assert builtin.engine == "vllm"
        assert builtin.precision == "bf16"
        assert _profile_ids().count(BUILTIN_VLLM_BF16_SINGLE_GPU) == 1
        assert _component_ids() == {PROFILE_BF16, PROFILE_NVFP4}
