"""Offline tests for the NVIDIA NIM 2.0.13 component profiles.

Discovery, classification, and the execution gate only. No network, image
pull, provider call, or inference.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from blackwell_lab.cloud.realbench import RealRunSpec, run_real_cell
from blackwell_lab.engines import (
    BUILTIN_VLLM_BF16_SINGLE_GPU,
    READINESS_BLOCKED,
    READINESS_CONDITIONAL,
    READINESS_READY,
    EngineContractError,
    declaration_from_mapping,
    evaluate_engine_contract,
    list_profiles,
    load_registered_components,
    require_ready_contract,
    require_supported_contract,
    reset_registry,
)
from blackwell_lab.workload.clock import SYSTEM_CLOCK
from blackwell_lab.workload.model_client import DeterministicMockClient

ROOT = Path(__file__).resolve().parents[1]
NIM_SOURCE = ROOT / "src" / "blackwell_lab" / "engines" / "components" / "nim.py"
NIM_DOC = ROOT / "docs" / "components" / "nim.md"
EXAMPLE_CONTRACT = ROOT / "examples" / "example-engine-contract.json"
DOCUMENTED_ENGINE_VERSION = "2.0.13"


def _nim():
    """Import after discovery so reload cannot double-register."""
    load_registered_components()
    from blackwell_lab.engines.components import nim

    return nim


def _profile_specs() -> tuple[tuple[str, str], ...]:
    return _nim().PROFILE_SPECS


def _nim_profile_ids() -> tuple[str, ...]:
    return tuple(profile_id for profile_id, _precision in _profile_specs())


def _precisions() -> tuple[str, ...]:
    return tuple(precision for _profile_id, precision in _profile_specs())


_IDENTITY = {
    "model_artifact": "nvidia/nemotron-3.5-lightning",
    "model_revision": "a" * 40,
    "model_artifact_hash": "sha256:" + "ab" * 32,
    "container_digest": ("nvcr.io/nim/nvidia/nemotron-3.5-lightning-30b-a3b@sha256:" + "cd" * 32),
    "engine_version": DOCUMENTED_ENGINE_VERSION,
}

_CATALOG_AMD64_DIGEST = (
    "nvcr.io/nim/nvidia/nemotron-3.5-lightning-30b-a3b@sha256:"
    "4f93f7e217ca05954bb34d3ccabbb3080f419e0ee5e1c0053f70debc00eb97e8"
)


def _declaration(**overrides) -> dict:
    payload = {
        "engine": "nim",
        "precision": "bf16",
        "topology": {"kind": "single-gpu", "gpu_count": 1, "node_count": 1},
        "identity": dict(_IDENTITY),
    }
    payload.update(overrides)
    if "identity" in overrides:
        identity = dict(_IDENTITY)
        identity.update(overrides["identity"])
        payload["identity"] = identity
    return payload


def _profile_id(precision: str) -> str:
    for profile_id, candidate in _profile_specs():
        if candidate == precision:
            return profile_id
    raise AssertionError(precision)


@pytest.fixture(autouse=True)
def _restore_registry():
    reset_registry()
    yield
    reset_registry()


def _discovered_ids() -> list[str]:
    load_registered_components()
    return [profile.profile_id for profile in list_profiles()]


class TestDiscovery:
    def test_automatic_discovery_finds_every_nim_profile(self):
        ids = _discovered_ids()
        assert set(_nim_profile_ids()) <= set(ids)
        for profile_id in _nim_profile_ids():
            assert ids.count(profile_id) == 1

    def test_discovery_is_idempotent(self):
        reset_registry()
        first = load_registered_components()
        first_ids = [profile.profile_id for profile in list_profiles()]
        second = load_registered_components()
        second_ids = [profile.profile_id for profile in list_profiles()]
        assert first == second
        assert second_ids == first_ids
        for profile_id in _nim_profile_ids():
            assert second_ids.count(profile_id) == 1
        reset_registry()
        load_registered_components()
        reloaded = [profile.profile_id for profile in list_profiles()]
        for profile_id in _nim_profile_ids():
            assert reloaded.count(profile_id) == 1

    def test_profile_ids_are_unique_without_depending_on_total_count(self):
        ids = _discovered_ids()
        assert len(ids) == len(set(ids))
        assert len(set(_nim_profile_ids())) == len(_nim_profile_ids())
        assert BUILTIN_VLLM_BF16_SINGLE_GPU in ids


class TestConditionalCeiling:
    @pytest.mark.parametrize("precision", ["bf16", "w4a16", "nvfp4"])
    def test_evidence_consistent_declaration_is_conditional(self, precision):
        readiness = evaluate_engine_contract(
            declaration_from_mapping(_declaration(precision=precision))
        )
        assert readiness.status == READINESS_CONDITIONAL
        assert readiness.profile_id == _profile_id(precision)
        assert readiness.status != READINESS_READY
        for condition in _nim().UNRESOLVED_CONDITIONS:
            assert condition in readiness.reasons

    @pytest.mark.parametrize("precision", ["bf16", "w4a16", "nvfp4"])
    def test_catalog_digest_and_satisfied_entitlement_stay_conditional(self, precision):
        identity = dict(_IDENTITY)
        identity["container_digest"] = _CATALOG_AMD64_DIGEST
        readiness = evaluate_engine_contract(
            declaration_from_mapping(
                _declaration(
                    precision=precision,
                    identity=identity,
                    entitlements=[
                        {"name": "ngc-nim-entitlement", "required": True, "satisfied": True}
                    ],
                )
            )
        )
        assert readiness.status == READINESS_CONDITIONAL
        assert readiness.profile_id == _profile_id(precision)

    @pytest.mark.parametrize("precision", ["bf16", "w4a16", "nvfp4"])
    def test_no_nim_declaration_is_ready(self, precision):
        variants = [
            _declaration(precision=precision),
            _declaration(precision=precision, profile_id=_profile_id(precision)),
            _declaration(
                precision=precision,
                identity={**_IDENTITY, "container_digest": _CATALOG_AMD64_DIGEST},
                entitlements=[{"name": "ngc-nim-entitlement", "required": True, "satisfied": True}],
            ),
        ]
        for payload in variants:
            readiness = evaluate_engine_contract(declaration_from_mapping(payload))
            assert readiness.status != READINESS_READY
            assert readiness.profile_id == _profile_id(precision)

    def test_profile_source_has_no_ready_status(self):
        source = NIM_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "ready":
                raise AssertionError("NIM profile source contains status ready")
        assert "READINESS_READY" not in source

    @pytest.mark.parametrize("precision", ["bf16", "w4a16", "nvfp4"])
    def test_ready_and_supported_gates_reject_conditional_nim(self, precision):
        readiness = evaluate_engine_contract(
            declaration_from_mapping(_declaration(precision=precision))
        )
        with pytest.raises(EngineContractError, match="not authorized for genuine inference"):
            require_ready_contract(readiness)
        with pytest.raises(EngineContractError, match="not authorized for genuine inference"):
            require_supported_contract(readiness)


class TestBlockedDeclarations:
    @pytest.mark.parametrize("satisfied", [False, None])
    def test_required_entitlement_false_or_null_is_blocked(self, satisfied):
        readiness = evaluate_engine_contract(
            declaration_from_mapping(
                _declaration(
                    entitlements=[
                        {"name": "ngc-nim-entitlement", "required": True, "satisfied": satisfied}
                    ]
                )
            )
        )
        assert readiness.status == READINESS_BLOCKED
        assert readiness.profile_id == _profile_id("bf16")
        if satisfied is None:
            assert "unknown" in readiness.reasons[0]
        else:
            assert "not satisfied" in readiness.reasons[0]

    @pytest.mark.parametrize("precision", ["fp8", "int4"])
    def test_unsupported_precision_is_blocked(self, precision):
        if precision == "int4":
            with pytest.raises(EngineContractError, match="unknown precision"):
                declaration_from_mapping(_declaration(precision=precision))
            return
        readiness = evaluate_engine_contract(
            declaration_from_mapping(_declaration(precision=precision))
        )
        assert readiness.status == READINESS_BLOCKED
        assert readiness.profile_id is None
        with pytest.raises(EngineContractError, match="unsupported"):
            require_supported_contract(readiness)

    @pytest.mark.parametrize("version", ["2.0.9-variant", "2.0.12", "0.28.0", "latest"])
    def test_unsupported_or_floating_version_is_blocked(self, version):
        payload = _declaration(identity={**_IDENTITY, "engine_version": version})
        if version == "latest":
            with pytest.raises(EngineContractError, match="exact pin"):
                declaration_from_mapping(payload)
            return
        readiness = evaluate_engine_contract(declaration_from_mapping(payload))
        assert readiness.status == READINESS_BLOCKED
        assert readiness.profile_id is None
        with pytest.raises(EngineContractError, match="unsupported"):
            require_supported_contract(readiness)

    def test_pinned_profile_rejects_a_different_version(self):
        readiness = evaluate_engine_contract(
            declaration_from_mapping(
                _declaration(
                    profile_id=_profile_id("bf16"),
                    identity={**_IDENTITY, "engine_version": "2.0.9-variant"},
                )
            )
        )
        assert readiness.status == READINESS_BLOCKED
        assert readiness.profile_id == _profile_id("bf16")
        with pytest.raises(EngineContractError, match="does not match"):
            require_supported_contract(readiness)

    @pytest.mark.parametrize(
        "topology",
        [
            {"kind": "multi-gpu", "gpu_count": 2, "node_count": 1},
            {"kind": "multi-gpu", "gpu_count": 4, "node_count": 1},
            {"kind": "multi-node", "gpu_count": 2, "node_count": 2},
            {"kind": "multi-node", "gpu_count": 1, "node_count": 2},
        ],
    )
    def test_multi_gpu_and_multi_node_are_blocked(self, topology):
        readiness = evaluate_engine_contract(
            declaration_from_mapping(_declaration(topology=topology))
        )
        assert readiness.status == READINESS_BLOCKED
        with pytest.raises(EngineContractError):
            require_supported_contract(readiness)

    def test_pinned_single_gpu_profile_rejects_multi_gpu(self):
        readiness = evaluate_engine_contract(
            declaration_from_mapping(
                _declaration(
                    profile_id=_profile_id("nvfp4"),
                    precision="nvfp4",
                    topology={"kind": "multi-gpu", "gpu_count": 2, "node_count": 1},
                )
            )
        )
        assert readiness.status == READINESS_BLOCKED
        assert readiness.profile_id == _profile_id("nvfp4")

    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("model_revision", "not-a-commit", "40-character"),
            ("model_revision", "latest", "exact pin"),
            ("model_artifact_hash", "sha256:abcd", "model_artifact_hash"),
            ("model_artifact_hash", "latest", "exact pin"),
            (
                "container_digest",
                "nvcr.io/nim/nvidia/nemotron-3.5-lightning-30b-a3b:2.0.13",
                "container_digest",
            ),
            (
                "container_digest",
                "nvcr.io/nim/nvidia/nemotron-3.5-lightning-30b-a3b:latest",
                "container_digest",
            ),
            ("engine_version", "", "engine_version"),
            ("engine_version", "stable", "exact pin"),
            ("model_artifact", "", "model_artifact"),
        ],
    )
    def test_malformed_and_floating_identities_fail_closed(self, field, value, match):
        identity = dict(_IDENTITY)
        identity[field] = value
        with pytest.raises(EngineContractError, match=match):
            declaration_from_mapping(_declaration(identity=identity))


class TestExecutionRefused:
    @pytest.mark.parametrize("precision", ["bf16", "w4a16", "nvfp4"])
    def test_real_execution_stops_before_the_spy_client(self, precision):
        class _Spy(DeterministicMockClient):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def stream_turn(self, messages, settings, *, deadline=None, clock=SYSTEM_CLOCK):
                self.calls += 1
                raise AssertionError("inference client was called")

        spec = RealRunSpec(
            profile_name="interactive",
            concurrency=1,
            comparison_mode="provider-native",
            instance_type="g9-fake-gpu-plan",
            region="us-fake-1",
            list_price_usd_per_hour=2.5,
            price_source_date="2026-09-06",
            model={
                "artifact": _IDENTITY["model_artifact"],
                "revision": _IDENTITY["model_revision"],
                "artifact_hash": _IDENTITY["model_artifact_hash"],
                "precision": precision,
            },
            engine="nim",
            engine_version=DOCUMENTED_ENGINE_VERSION,
            container_digest=_IDENTITY["container_digest"],
        )
        client = _Spy()
        with pytest.raises(EngineContractError, match="not authorized for genuine inference"):
            run_real_cell(spec, client, host={"gpu_count": 1}, sampler_factory=lambda: None)
        assert client.calls == 0

    def test_live_provenance_refuses_conditional_nim(self, tmp_path):
        import hashlib

        from test_provenance import fake_http_get, fake_runner, make_approved, run_verify

        from blackwell_lab.cloud.provenance import ProvenanceError

        artifact_dir = tmp_path / "model"
        artifact_dir.mkdir()
        payload = b"pinned model bytes"
        (artifact_dir / "model.safetensors").write_bytes(payload)
        file_hex = hashlib.sha256(payload).hexdigest()
        manifest = tmp_path / "digests.sha256"
        manifest.write_text(f"{file_hex}  model.safetensors\n", encoding="utf-8")
        aggregate = hashlib.sha256(f"{file_hex}  model.safetensors".encode()).hexdigest()
        model_dir = (artifact_dir, manifest, f"sha256:{aggregate}")
        approved = make_approved(model_dir[2])
        approved["serving"]["engine"] = "nim"
        approved["serving"]["engine_version"] = DOCUMENTED_ENGINE_VERSION
        approved["serving"]["container_digest"] = _IDENTITY["container_digest"]
        approved["model"]["artifact"] = _IDENTITY["model_artifact"]
        approved["model"]["precision"] = "bf16"
        with pytest.raises(ProvenanceError, match="not authorized for genuine inference"):
            run_verify(
                model_dir,
                approved=approved,
                runner=fake_runner(digest=_IDENTITY["container_digest"]),
                http_get=fake_http_get(engine_version=DOCUMENTED_ENGINE_VERSION),
            )


class TestModelContainerBoundary:
    def test_unrelated_model_is_not_conditional(self):
        identity = dict(_IDENTITY)
        identity["model_artifact"] = "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16"
        readiness = evaluate_engine_contract(
            declaration_from_mapping(_declaration(identity=identity))
        )
        assert readiness.status == READINESS_BLOCKED
        assert readiness.status != READINESS_CONDITIONAL
        assert readiness.profile_id not in _nim_profile_ids()

    def test_unrelated_container_repository_is_not_conditional(self):
        identity = dict(_IDENTITY)
        identity["container_digest"] = "docker.io/vllm/vllm-openai@sha256:" + "cd" * 32
        readiness = evaluate_engine_contract(
            declaration_from_mapping(_declaration(identity=identity))
        )
        assert readiness.status == READINESS_BLOCKED
        assert readiness.status != READINESS_CONDITIONAL
        assert readiness.profile_id not in _nim_profile_ids()

    @pytest.mark.parametrize(
        "identity_override",
        [
            {"model_artifact": "meta/llama-unrelated"},
            {"container_digest": "nvcr.io/nim/nvidia/other-model@sha256:" + "ab" * 32},
        ],
    )
    def test_pinned_wrong_model_or_container_is_blocked(self, identity_override):
        identity = dict(_IDENTITY)
        identity.update(identity_override)
        readiness = evaluate_engine_contract(
            declaration_from_mapping(
                _declaration(profile_id=_profile_id("bf16"), identity=identity)
            )
        )
        assert readiness.status == READINESS_BLOCKED
        assert readiness.profile_id == _profile_id("bf16")
        with pytest.raises(EngineContractError, match="does not match"):
            require_supported_contract(readiness)

    def test_correct_model_and_repository_shape_stays_conditional(self):
        other_digest = "nvcr.io/nim/nvidia/nemotron-3.5-lightning-30b-a3b@sha256:" + "ef" * 32
        for digest in (_IDENTITY["container_digest"], other_digest, _CATALOG_AMD64_DIGEST):
            identity = dict(_IDENTITY)
            identity["container_digest"] = digest
            readiness = evaluate_engine_contract(
                declaration_from_mapping(_declaration(identity=identity))
            )
            assert readiness.status == READINESS_CONDITIONAL
            assert readiness.profile_id == _profile_id("bf16")
            assert readiness.status != READINESS_READY
            with pytest.raises(EngineContractError, match="not authorized for genuine inference"):
                require_ready_contract(readiness)

    def test_wrong_boundary_never_reaches_the_spy_client(self):
        class _Spy(DeterministicMockClient):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def stream_turn(self, messages, settings, *, deadline=None, clock=SYSTEM_CLOCK):
                self.calls += 1
                raise AssertionError("inference client was called")

        client = _Spy()
        spec = RealRunSpec(
            profile_name="interactive",
            concurrency=1,
            comparison_mode="provider-native",
            instance_type="g9-fake-gpu-plan",
            region="us-fake-1",
            list_price_usd_per_hour=2.5,
            price_source_date="2026-09-06",
            model={
                "artifact": "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16",
                "revision": _IDENTITY["model_revision"],
                "artifact_hash": _IDENTITY["model_artifact_hash"],
                "precision": "bf16",
            },
            engine="nim",
            engine_version=DOCUMENTED_ENGINE_VERSION,
            container_digest="docker.io/vllm/vllm-openai@sha256:" + "cd" * 32,
        )
        with pytest.raises(EngineContractError):
            run_real_cell(spec, client, host={"gpu_count": 1}, sampler_factory=lambda: None)
        assert client.calls == 0


class TestImportBeforeDiscovery:
    def test_import_before_load_registers_each_profile_once(self):
        script = r"""
from blackwell_lab.engines import list_profiles, load_registered_components, reset_registry

reset_registry()
import blackwell_lab.engines.components.nim  # noqa: F401

load_registered_components()
ids = [profile.profile_id for profile in list_profiles()]
expected = (
    "nim-nemotron35-bf16-single-gpu",
    "nim-nemotron35-w4a16-single-gpu",
    "nim-nemotron35-nvfp4-single-gpu",
)
for profile_id in expected:
    if ids.count(profile_id) != 1:
        raise SystemExit(f"{profile_id} count={ids.count(profile_id)} ids={ids}")
print("import-before-discovery-ok")
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        assert completed.returncode == 0, completed.stderr
        assert "import-before-discovery-ok" in completed.stdout

    def test_conflicting_profile_id_fails_closed(self):
        script = r"""
from blackwell_lab.engines import EngineContractError, register_engine_profile, reset_registry

reset_registry()

class _Foreign:
    profile_id = "nim-nemotron35-bf16-single-gpu"
    engine = "vllm"
    precision = "bf16"
    topologies = frozenset({"single-gpu"})
    implementation_id = "test-foreign"

    def matches(self, declaration):
        return False

    def evaluate(self, declaration):
        raise AssertionError("foreign profile evaluate was called")

register_engine_profile(_Foreign())
try:
    import blackwell_lab.engines.components.nim  # noqa: F401
except EngineContractError as exc:
    if "conflicting engine profile already registered" not in str(exc):
        raise SystemExit(f"unexpected error: {exc}")
else:
    raise SystemExit("conflicting registration was accepted")
print("conflict-fail-closed-ok")
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        assert completed.returncode == 0, completed.stderr
        assert "conflict-fail-closed-ok" in completed.stdout


class TestExistingVllmUnchanged:
    def test_builtin_vllm_bf16_single_gpu_stays_ready(self):
        payload = json.loads(EXAMPLE_CONTRACT.read_text(encoding="utf-8"))
        readiness = evaluate_engine_contract(declaration_from_mapping(payload))
        assert readiness.status == READINESS_READY
        assert readiness.profile_id == BUILTIN_VLLM_BF16_SINGLE_GPU
        require_ready_contract(readiness)
        require_supported_contract(readiness)


class TestImportHasNoOperations:
    def test_component_source_names_no_runtime_integration(self):
        source = NIM_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        forbidden = ("subprocess", "socket", "urllib", "http", "requests", "docker", "huggingface")
        for name in imported:
            assert not any(name == item or name.startswith(item + ".") for item in forbidden)

    def test_import_performs_no_network_subprocess_or_inference(self):
        script = r"""
import os
import socket
import subprocess
import sys

FORBIDDEN_ENV = {
    "NGC_API_KEY",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "LINODE_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
}

class _Env(dict):
    def __getitem__(self, key):
        if key in FORBIDDEN_ENV:
            raise AssertionError(f"credential read: {key}")
        return super().__getitem__(key)

env = _Env(os.environ)
env.__class__ = type(os.environ) if False else _Env
os.environ = env  # type: ignore[assignment]

def _blocked(*_args, **_kwargs):
    raise AssertionError("forbidden operation during NIM import")

socket.socket = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
subprocess.Popen = _blocked
subprocess.run = _blocked
subprocess.call = _blocked
subprocess.check_call = _blocked
subprocess.check_output = _blocked

import blackwell_lab.engines.components.nim as nim

assert len(nim.nim_profiles()) == 3
assert all(profile.engine == "nim" for profile in nim.nim_profiles())
print("import-ok")
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        assert completed.returncode == 0, completed.stderr
        assert "import-ok" in completed.stdout


class TestDocumentation:
    def test_doc_records_sources_profiles_and_boundaries(self):
        text = NIM_DOC.read_text(encoding="utf-8")
        for url in _nim().OFFICIAL_SOURCE_URLS:
            assert url in text
        for profile_id in _nim_profile_ids():
            assert profile_id in text
        for phrase in (
            "Offline classification only.",
            "Live execution blocked.",
            "Conditional does not authorize inference.",
            "Conditional never authorizes genuine inference.",
            "nvidia/nemotron-3.5-lightning",
            "nvcr.io/nim/nvidia/nemotron-3.5-lightning-30b-a3b@sha256:",
            "No launch path exists.",
            "No benchmark finding is recorded.",
            "Future live execution requires separately reviewed immutable identities",
        ):
            assert phrase in text
        for condition in (
            "immutable linux/amd64",
            "runtime profile ID",
            "artifact digest",
            "entitlement",
            "RTX PRO 6000",
            "tool-calling",
            "workload 2.4.0",
            "memory viability",
        ):
            assert condition in text
