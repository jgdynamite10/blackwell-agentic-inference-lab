"""Adversarial tests for live provenance verification (Phase 3A safety).

Every fact in a genuine manifest must be OBSERVED from the live system, never
copied from configuration. These tests fabricate mismatches one at a time and
assert each one fails visibly, and that observation failures are never
silently replaced by configured values.
"""

from __future__ import annotations

import hashlib

import pytest

from blackwell_lab.cloud import provenance, telemetry
from blackwell_lab.cloud.provenance import (
    METADATA_BASE,
    ProvenanceError,
    observe_engine_version,
    observe_instance_identity,
    verify_live_provenance,
)

RUN_TAG = "bwlab-20260906-pilot"
IMAGE = "vllm/vllm-openai:v0.27.1"
DIGEST = f"vllm/vllm-openai@sha256:{'a' * 64}"
ENGINE_VERSION = "0.27.1"
CONTAINER_CUDA = "12.8"
PROVIDER_ID = "12345678"
REGION = "us-ord"
INSTANCE_TYPE = "g8-gpu-rtx6000b-1"

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


@pytest.fixture()
def model_dir(tmp_path):
    artifact_dir = tmp_path / "model"
    artifact_dir.mkdir()
    payload = b"pinned model bytes"
    (artifact_dir / "model.safetensors").write_bytes(payload)
    file_hex = hashlib.sha256(payload).hexdigest()
    manifest = tmp_path / "digests.sha256"
    manifest.write_text(f"{file_hex}  model.safetensors\n", encoding="utf-8")
    aggregate = hashlib.sha256(f"{file_hex}  model.safetensors".encode()).hexdigest()
    return artifact_dir, manifest, f"sha256:{aggregate}"


def make_approved(artifact_hash: str) -> dict:
    return {
        "serving": {
            "image": IMAGE,
            "container_digest": DIGEST,
            "engine": "vllm",
            "engine_version": ENGINE_VERSION,
            "container_cuda_runtime_version": CONTAINER_CUDA,
        },
        "model": {"artifact_hash": artifact_hash},
        "cloud": {"instance_type": INSTANCE_TYPE, "region": REGION},
        "host": {
            "storage_description": "local NVMe",
            "network_description": "private VLAN",
        },
        "expected_gpu_model": "RTX 6000 Blackwell",
    }


def make_ledger(*, provider_id: str = PROVIDER_ID, region: str = REGION) -> dict:
    return {
        "run_tag": RUN_TAG,
        "reconciled": True,
        "resources": [
            {
                "address": "linode_instance.gpu_baseline",
                "type": "linode_instance",
                "provider_id": provider_id,
                "region": region,
            },
            {
                "address": "linode_firewall.gpu_baseline",
                "type": "linode_firewall",
                "provider_id": "990",
                "region": region,
            },
        ],
    }


def fake_runner(*, digest: str = DIGEST, container_cuda: str = CONTAINER_CUDA):
    def runner(argv: list[str]) -> str:
        if argv[:2] == ["docker", "inspect"]:
            return digest + "\n"
        if argv[:2] == ["docker", "exec"]:
            return container_cuda + "\n"
        raise AssertionError(f"unexpected command observed: {argv[:2]}")

    return runner


def fake_http_get(
    *,
    engine_version: str = ENGINE_VERSION,
    instance: dict | None = None,
):
    payload = instance or {
        "id": PROVIDER_ID,
        "type": INSTANCE_TYPE,
        "region": REGION,
        "tags": ["blackwell-lab", f"run:{RUN_TAG}"],
    }

    def http_get(url: str) -> dict:
        if url.endswith("/v1/version"):
            raise AssertionError(f"vLLM /version is not under the OpenAI /v1 prefix; refused {url}")
        if url == "http://127.0.0.1:8000/version":
            return {"version": engine_version}
        if url.startswith(METADATA_BASE):
            return payload
        raise AssertionError(f"unexpected URL observed: {url}")

    return http_get


def run_verify(model_dir, *, approved=None, ledger=None, runner=None, http_get=None):
    artifact_dir, manifest, artifact_hash = model_dir
    return verify_live_provenance(
        run_tag=RUN_TAG,
        approved=approved or make_approved(artifact_hash),
        ledger=ledger or make_ledger(),
        artifact_dir=artifact_dir,
        digest_manifest=manifest,
        serving_base_url="http://127.0.0.1:8000",
        runner=runner or fake_runner(),
        http_get=http_get or fake_http_get(),
        host_facts=HOST_FACTS,
        gpu_facts=GPU_FACTS,
    )


class TestObservedFactsAreReturned:
    def test_matching_observations_pass_and_are_returned_verbatim(self, model_dir):
        _, _, artifact_hash = model_dir
        observed = run_verify(model_dir)
        assert observed.container_digest == DIGEST
        assert observed.model_artifact_hash == artifact_hash
        assert observed.engine_version == ENGINE_VERSION
        assert observed.container_cuda_runtime_version == CONTAINER_CUDA
        assert observed.instance["provider_id"] == PROVIDER_ID
        assert observed.host_facts == HOST_FACTS
        assert observed.gpu_facts == GPU_FACTS


class TestFabricatedFactsFailVisibly:
    def test_container_digest_mismatch_fails(self, model_dir):
        runner = fake_runner(digest=f"vllm/vllm-openai@sha256:{'b' * 64}")
        with pytest.raises(ProvenanceError, match="container digest"):
            run_verify(model_dir, runner=runner)

    def test_tampered_model_file_fails(self, model_dir):
        artifact_dir, _, _ = model_dir
        (artifact_dir / "model.safetensors").write_bytes(b"tampered")
        with pytest.raises(telemetry.ArtifactVerificationError, match="digest mismatch"):
            run_verify(model_dir)

    def test_configured_artifact_hash_that_nothing_backs_fails(self, model_dir):
        approved = make_approved(f"sha256:{'c' * 64}")
        with pytest.raises(ProvenanceError, match="model artifact hash"):
            run_verify(model_dir, approved=approved)

    def test_engine_version_mismatch_fails(self, model_dir):
        with pytest.raises(ProvenanceError, match="engine version"):
            run_verify(model_dir, http_get=fake_http_get(engine_version="0.28.0"))

    def test_container_cuda_runtime_mismatch_fails(self, model_dir):
        with pytest.raises(ProvenanceError, match="CUDA runtime"):
            run_verify(model_dir, runner=fake_runner(container_cuda="12.4"))

    def test_provider_id_differing_from_ledger_fails(self, model_dir):
        ledger = make_ledger(provider_id="999999")
        with pytest.raises(ProvenanceError, match="provider id"):
            run_verify(model_dir, ledger=ledger)

    def test_instance_type_differing_from_approved_fails(self, model_dir):
        http_get = fake_http_get(
            instance={
                "id": PROVIDER_ID,
                "type": "g6-standard-2",
                "region": REGION,
                "tags": ["blackwell-lab", f"run:{RUN_TAG}"],
            }
        )
        with pytest.raises(ProvenanceError, match="instance type"):
            run_verify(model_dir, http_get=http_get)

    def test_region_differing_from_approved_and_ledger_fails(self, model_dir):
        http_get = fake_http_get(
            instance={
                "id": PROVIDER_ID,
                "type": INSTANCE_TYPE,
                "region": "us-east",
                "tags": ["blackwell-lab", f"run:{RUN_TAG}"],
            }
        )
        with pytest.raises(ProvenanceError, match="region"):
            run_verify(model_dir, http_get=http_get)

    def test_missing_run_tag_on_the_instance_fails(self, model_dir):
        http_get = fake_http_get(
            instance={
                "id": PROVIDER_ID,
                "type": INSTANCE_TYPE,
                "region": REGION,
                "tags": ["blackwell-lab", "run:someone-elses-run"],
            }
        )
        with pytest.raises(ProvenanceError, match="tags"):
            run_verify(model_dir, http_get=http_get)

    def test_gpu_model_mismatch_fails(self, model_dir):
        _, _, artifact_hash = model_dir
        approved = make_approved(artifact_hash)
        approved["expected_gpu_model"] = "H100"
        with pytest.raises(ProvenanceError, match="GPU model"):
            run_verify(model_dir, approved=approved)

    def test_all_mismatches_are_reported_together(self, model_dir):
        _, _, artifact_hash = model_dir
        approved = make_approved(artifact_hash)
        approved["serving"]["engine_version"] = "9.9.9"
        approved["expected_gpu_model"] = "H100"
        with pytest.raises(ProvenanceError) as excinfo:
            run_verify(model_dir, approved=approved)
        message = str(excinfo.value)
        assert "engine version" in message
        assert "GPU model" in message


class TestLedgerShape:
    def test_ledger_without_an_instance_fails_closed(self, model_dir):
        ledger = {"run_tag": RUN_TAG, "reconciled": True, "resources": []}
        with pytest.raises(ProvenanceError, match="exactly one instance"):
            run_verify(model_dir, ledger=ledger)

    def test_ledger_with_two_instances_fails_closed(self, model_dir):
        ledger = make_ledger()
        ledger["resources"].append(dict(ledger["resources"][0], provider_id="777"))
        with pytest.raises(ProvenanceError, match="exactly one instance"):
            run_verify(model_dir, ledger=ledger)


class TestObservationFailuresNeverFallBackToConfig:
    def test_unreachable_version_endpoint_is_an_error_not_a_fallback(self):
        def failing(url: str) -> dict:
            raise OSError("connection refused")

        with pytest.raises(ProvenanceError, match="cannot be taken from configuration"):
            observe_engine_version("http://127.0.0.1:8000", http_get=failing)

    def test_empty_reported_version_fails(self):
        with pytest.raises(ProvenanceError, match="empty version"):
            observe_engine_version("http://127.0.0.1:8000", http_get=lambda url: {"version": "  "})

    def test_unreachable_metadata_service_is_an_error(self):
        def failing(url: str) -> dict:
            raise OSError("no route to host")

        with pytest.raises(ProvenanceError, match="never taken from configuration"):
            observe_instance_identity(http_get=failing)

    def test_incomplete_metadata_response_fails(self):
        with pytest.raises(ProvenanceError, match="incomplete"):
            observe_instance_identity(http_get=lambda url: {"id": "1", "type": "", "region": ""})


class TestVersionEndpointUrl:
    @pytest.mark.parametrize(
        ("base_url", "expected"),
        [
            ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000/version"),
            ("http://127.0.0.1:8000/v1/", "http://127.0.0.1:8000/version"),
            ("http://127.0.0.1:8000", "http://127.0.0.1:8000/version"),
            ("http://127.0.0.1:8000/proxy/v1", "http://127.0.0.1:8000/proxy/version"),
        ],
    )
    def test_exact_url_equality(self, base_url, expected):
        assert provenance.version_endpoint_url(base_url) == expected

    @pytest.mark.parametrize(
        ("base_url", "expected"),
        [
            ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000/version"),
            ("http://127.0.0.1:8000/v1/", "http://127.0.0.1:8000/version"),
            ("http://127.0.0.1:8000", "http://127.0.0.1:8000/version"),
            ("http://127.0.0.1:8000/proxy/v1", "http://127.0.0.1:8000/proxy/version"),
        ],
    )
    def test_observe_engine_version_requests_the_exact_live_url(self, base_url, expected):
        seen: list[str] = []

        def http_get(url: str) -> dict:
            if url.endswith("/v1/version"):
                raise AssertionError(
                    f"vLLM /version is not under the OpenAI /v1 prefix; refused {url}"
                )
            seen.append(url)
            return {"version": ENGINE_VERSION}

        assert observe_engine_version(base_url, http_get=http_get) == ENGINE_VERSION
        assert seen == [expected]

    def test_version_endpoint_keeps_loopback_enforcement(self):
        with pytest.raises(ProvenanceError, match="public or named host"):
            provenance.version_endpoint_url("http://example.com/v1")


class TestEndpointSafety:
    def test_public_named_hosts_are_refused(self):
        with pytest.raises(ProvenanceError, match="public or named host"):
            provenance.default_http_get_json("http://example.com/version")

    def test_public_addresses_are_refused(self):
        with pytest.raises(ProvenanceError, match="public address"):
            provenance.default_http_get_json("http://8.8.8.8/version")

    def test_link_local_metadata_address_is_permitted_by_the_guard(self):
        # Only the URL guard is under test; no request is made.
        provenance._require_local_or_private(f"{METADATA_BASE}/instance")
        provenance._require_local_or_private("http://127.0.0.1:8000/version")
