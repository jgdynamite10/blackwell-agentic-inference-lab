"""Live provenance verification, run immediately before each genuine cell.

Model, image, engine, cloud, and host facts are never trusted from a JSON
configuration: every fact that ends up in a genuine manifest is **observed**
from the running system immediately before the cell executes and compared
against the approved pilot configuration and the run's lifecycle ledger. Any
mismatch fails visibly (:class:`ProvenanceError`) — a fabricated or stale
configuration can never masquerade as an observation.

Observed facts:

- the running serving container's **immutable image digest**
  (``docker inspect`` on the pinned image reference);
- the **model files** on disk, verified file-by-file against the pinned
  digest manifest;
- the **engine version** reported by the running service itself
  (vLLM's ``/version`` endpoint) — not a configured string;
- the **instance identity** (provider id, type, region, run tags) from the
  provider's link-local Metadata API: a fresh ephemeral metadata token is
  obtained with ``PUT /v1/token`` immediately before each cell, then
  ``GET /v1/instance`` is authenticated with that token. The metadata token
  is not a Linode API credential and is never printed, persisted, or
  returned. Live metadata never falls back to configuration, the ledger, or
  the provider API;
- **host and GPU facts** via :mod:`blackwell_lab.cloud.telemetry`, and the
  container's actual CUDA runtime (separate from the driver's max CUDA).
"""

from __future__ import annotations

import ipaddress
import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from blackwell_lab.cloud import telemetry

#: Linode/Akamai link-local Metadata API (instance-local; not the Linode API).
METADATA_BASE = "http://169.254.169.254/v1"
METADATA_TOKEN_URL = f"{METADATA_BASE}/token"
METADATA_INSTANCE_URL = f"{METADATA_BASE}/instance"
METADATA_EXPIRY_SECONDS = 60
METADATA_ACCEPT = "application/json"

#: Injectable HTTP GET returning decoded JSON: (url) -> dict.
#: Used only for engine-version and other unauthenticated local GETs.
HttpGetJson = Callable[[str], dict]

#: Injectable metadata HTTP request: (method, url, headers) -> JSON value.
#: Used only for the authenticated Metadata API. Kept separate from
#: :data:`HttpGetJson` so engine-version requests never carry metadata tokens.
HttpRequestJson = Callable[[str, str, dict[str, str]], object]


class ProvenanceError(RuntimeError):
    """An observed fact does not match the approved configuration/ledger."""


def _require_local_or_private(url: str) -> None:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in ("localhost",):
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ProvenanceError(
            "provenance observations only query loopback/link-local/private "
            "endpoints; refusing a public or named host"
        ) from exc
    if not (address.is_loopback or address.is_private or address.is_link_local):
        raise ProvenanceError(
            "provenance observations only query loopback/link-local/private "
            "endpoints; refusing a public address"
        )


def default_http_get_json(url: str) -> dict:
    """Unauthenticated JSON GET for engine-version and similar local probes."""
    _require_local_or_private(url)
    request = urllib.request.Request(url)  # noqa: S310 - validated local/private URL
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ProvenanceError("the local HTTP response was not a JSON object")
    return payload


def default_http_request_json(method: str, url: str, headers: dict[str, str]) -> object:
    """Method-aware JSON request for the instance-local Metadata API only."""
    _require_local_or_private(url)
    request = urllib.request.Request(  # noqa: S310 - validated local/private URL
        url,
        data=None,
        headers=dict(headers),
        method=method.upper(),
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        return json.load(response)


def parse_metadata_token_response(payload: object) -> str:
    """Parse the documented singleton JSON array ``["<token>"]``.

    The token string is returned to the caller for immediate use and must
    never be logged, persisted, or interpolated into an error message.
    """
    if not isinstance(payload, list) or len(payload) != 1:
        raise ProvenanceError("the metadata token response was malformed")
    token = payload[0]
    if not isinstance(token, str) or not token.strip():
        raise ProvenanceError("the metadata token response was malformed")
    return token.strip()


def _require_instance_id(value: object) -> str:
    if isinstance(value, bool) or value is None:
        raise ProvenanceError("the instance metadata response was incomplete")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ProvenanceError("the instance metadata response was incomplete")
    if not text:
        raise ProvenanceError("the instance metadata response was incomplete")
    return text


def _require_instance_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProvenanceError("the instance metadata response was incomplete")
    return value.strip()


def _require_instance_tags(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ProvenanceError("the instance metadata tags were invalid")
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ProvenanceError("the instance metadata tags were invalid")
        tags.append(item)
    return tags


def version_endpoint_url(base_url: str) -> str:
    """Map an OpenAI-compatible API base URL to vLLM's live ``/version``.

    The approved chat API lives under ``/v1`` (for example
    ``http://127.0.0.1:8000/v1``). vLLM reports its version at the service
    root (``http://127.0.0.1:8000/version``), not under the OpenAI prefix.
    Exactly one terminal ``/v1`` path segment is removed; any reverse-proxy
    prefix is preserved; trailing slashes are ignored. The result is still
    subject to loopback/private-address enforcement.
    """
    _require_local_or_private(base_url)
    parts = urllib.parse.urlsplit(base_url)
    stripped = (parts.path or "").rstrip("/")
    if stripped.endswith("/v1"):
        stripped = stripped[: -len("/v1")]
    service_path = stripped.rstrip("/")
    version_path = f"{service_path}/version" if service_path else "/version"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, version_path, "", ""))


def observe_engine_version(base_url: str, *, http_get: HttpGetJson = default_http_get_json) -> str:
    """The engine version reported BY the running service (vLLM ``/version``)."""
    try:
        payload = http_get(version_endpoint_url(base_url))
    except ProvenanceError:
        raise
    except Exception:
        raise ProvenanceError(
            "the running service did not report its version (/version failed); "
            "the engine version cannot be taken from configuration instead"
        ) from None
    version = str(payload.get("version", "")).strip()
    if not version:
        raise ProvenanceError("the running service reported an empty version")
    return version


def observe_instance_identity(*, http_request: HttpRequestJson = default_http_request_json) -> dict:
    """Instance identity from the official Akamai Metadata API.

    Obtains a fresh ephemeral metadata token with ``PUT /v1/token`` and
    immediately GETs ``/v1/instance`` with that token. The token is not a
    Linode API credential and is never printed, persisted, returned, or
    included in an exception. Failures never fall back to configuration,
    the lifecycle ledger, or the provider API.
    """
    try:
        token_payload = http_request(
            "PUT",
            METADATA_TOKEN_URL,
            {
                "Metadata-Token-Expiry-Seconds": str(METADATA_EXPIRY_SECONDS),
                "Accept": METADATA_ACCEPT,
            },
        )
        token = parse_metadata_token_response(token_payload)
    except ProvenanceError:
        raise
    except Exception:
        raise ProvenanceError(
            "the instance metadata token could not be obtained; instance "
            "identity cannot be observed (and is never taken from configuration)"
        ) from None

    try:
        payload = http_request(
            "GET",
            METADATA_INSTANCE_URL,
            {
                "Metadata-Token": token,
                "Accept": METADATA_ACCEPT,
            },
        )
    except ProvenanceError as exc:
        if token in str(exc):
            raise ProvenanceError(
                "the instance metadata service is unreachable; instance "
                "identity cannot be observed (and is never taken from configuration)"
            ) from None
        raise
    except Exception:
        raise ProvenanceError(
            "the instance metadata service is unreachable; instance identity "
            "cannot be observed (and is never taken from configuration)"
        ) from None

    if not isinstance(payload, dict):
        raise ProvenanceError("the instance metadata response was incomplete")
    provider_id = _require_instance_id(payload.get("id"))
    instance_type = _require_instance_string(payload.get("type"))
    region = _require_instance_string(payload.get("region"))
    tags = _require_instance_tags(payload.get("tags"))
    return {
        "provider_id": provider_id,
        "instance_type": instance_type,
        "region": region,
        "tags": tags,
    }


@dataclass(frozen=True)
class ObservedProvenance:
    """Every observed fact a genuine manifest is allowed to use."""

    container_digest: str
    model_artifact_hash: str
    engine_version: str
    instance: dict
    host_facts: dict
    gpu_facts: dict
    container_cuda_runtime_version: str


def verify_live_provenance(
    *,
    run_tag: str,
    approved: dict,
    ledger: dict,
    artifact_dir: Path,
    digest_manifest: Path,
    serving_base_url: str,
    serving_container_name: str = "bwlab-vllm",
    runner: telemetry.CommandRunner = telemetry.run_command,
    http_get: HttpGetJson = default_http_get_json,
    http_request: HttpRequestJson = default_http_request_json,
    host_facts: dict | None = None,
    gpu_facts: dict | None = None,
) -> ObservedProvenance:
    """Observes and cross-checks every provenance fact before one cell.

    ``approved`` is the owner-approved pilot configuration — the expectation,
    never the source of manifest values. Raises :class:`ProvenanceError` on
    the first mismatch between an observation and the approved values or the
    lifecycle ledger.
    """
    mismatches: list[str] = []

    observed_digest = telemetry.resolve_container_digest(
        approved["serving"]["image"], runner=runner
    )
    if observed_digest != approved["serving"]["container_digest"]:
        mismatches.append("container digest differs from the approved pilot configuration")

    observed_model_hash = telemetry.verify_model_artifact(artifact_dir, digest_manifest)
    if observed_model_hash != approved["model"]["artifact_hash"]:
        mismatches.append("model artifact hash differs from the approved pilot configuration")

    observed_engine_version = observe_engine_version(serving_base_url, http_get=http_get)
    if observed_engine_version != approved["serving"]["engine_version"]:
        mismatches.append("running engine version differs from the approved pilot configuration")

    observed_container_cuda = telemetry.observe_container_cuda_version(
        serving_container_name, runner=runner
    )
    expected_cuda = approved["serving"].get("container_cuda_runtime_version")
    if expected_cuda and observed_container_cuda != expected_cuda:
        mismatches.append("container CUDA runtime differs from the approved pilot configuration")

    instance = observe_instance_identity(http_request=http_request)
    ledger_instances = [
        r for r in ledger.get("resources", []) if r.get("type") == "linode_instance"
    ]
    if len(ledger_instances) != 1:
        raise ProvenanceError(
            "the lifecycle ledger does not record exactly one instance; "
            "provenance cannot be verified"
        )
    recorded = ledger_instances[0]
    if instance["provider_id"] != recorded.get("provider_id"):
        mismatches.append("instance provider id differs from the lifecycle ledger")
    if instance["region"] != recorded.get("region"):
        mismatches.append("instance region differs from the lifecycle ledger")
    if instance["instance_type"] != approved["cloud"]["instance_type"]:
        mismatches.append("instance type differs from the approved pilot configuration")
    if instance["region"] != approved["cloud"]["region"]:
        mismatches.append("instance region differs from the approved pilot configuration")
    observed_tags = set(instance["tags"])
    if f"run:{run_tag}" not in observed_tags or "blackwell-lab" not in observed_tags:
        mismatches.append("instance run/project tags do not match the run")

    observed_host = (
        host_facts
        if host_facts is not None
        else telemetry.collect_host_facts(
            storage_description=approved["host"]["storage_description"],
            network_description=approved["host"]["network_description"],
            virtualization=approved["host"].get("virtualization"),
        )
    )
    observed_gpu = gpu_facts if gpu_facts is not None else telemetry.collect_gpu_facts(runner)
    expected_gpu_model = approved.get("expected_gpu_model")
    if expected_gpu_model and expected_gpu_model not in observed_gpu.get("gpu_model", ""):
        mismatches.append("observed GPU model differs from the approved pilot configuration")

    if mismatches:
        raise ProvenanceError(
            "live provenance verification FAILED; the cell will not run. "
            "Mismatches: " + "; ".join(sorted(mismatches))
        )
    return ObservedProvenance(
        container_digest=observed_digest,
        model_artifact_hash=observed_model_hash,
        engine_version=observed_engine_version,
        instance=instance,
        host_facts=observed_host,
        gpu_facts=observed_gpu,
        container_cuda_runtime_version=observed_container_cuda,
    )
