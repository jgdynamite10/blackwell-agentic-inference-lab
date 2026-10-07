"""Production-path loopback: native streamed tools on the real D-0014 CLI.

Uses the real ``blackwell-cloud pilot`` / ``verify-results`` path, the real
OpenAI client, agent loop, and evaluator. Only provider, GPU, container, and
inference-transport boundaries are stubbed. The suite-wide network ban is
replaced here with a loopback-only allowlist.
"""

from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from blackwell_lab.cloud import cli, lifecycle, provenance, telemetry
from blackwell_lab.cloud.cli import main
from blackwell_lab.cloud.telemetry import GpuSample, summarize_gpu_samples
from blackwell_lab.workload.native_tools import openai_tool_definitions
from blackwell_lab.workload.scenarios import catalog

RUN_TAG = "p3-native-20260908a"
RUN_LABEL = "native-tool-pilot"
ENGINE_VERSION = "0.27.1"
CONTAINER_DIGEST = "docker.io/vllm/vllm-openai@sha256:" + "cd" * 32
INSTANCE_TYPE = "g3-gpu-rtxpro6000-blackwell-1"
REGION = "us-sea"
PROVIDER_ID = "4242"
REPO_ROOT = Path(__file__).resolve().parents[1]


class NetworkAccessAttempted(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch):
    real_connect = socket.socket.connect
    real_create = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def _host(address) -> str:
        if isinstance(address, tuple) and address:
            return str(address[0])
        return ""

    def _allowed(host: str) -> bool:
        return host in {"127.0.0.1", "localhost", "::1"}

    def connect(self, address):
        if _allowed(_host(address)):
            return real_connect(self, address)
        raise NetworkAccessAttempted(f"non-loopback connect refused: {address!r}")

    def create_connection(address, *args, **kwargs):
        if _allowed(_host(address)):
            return real_create(address, *args, **kwargs)
        raise NetworkAccessAttempted(f"non-loopback create_connection refused: {address!r}")

    def getaddrinfo(host, *args, **kwargs):
        if _allowed(str(host)):
            return real_getaddrinfo(host, *args, **kwargs)
        raise NetworkAccessAttempted(f"non-loopback getaddrinfo refused: {host!r}")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


class FakeGpuSampler:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def summary(self, *, successful_tasks: int) -> dict:
        return summarize_gpu_samples(
            [
                GpuSample(100.0, 80.0, 70.0, 400.0, 65.0),
                GpuSample(101.0, 90.0, 75.0, 420.0, 66.0),
            ],
            window_started_monotonic_s=100.0,
            window_ended_monotonic_s=101.1,
            successful_tasks=successful_tasks,
        )


def _sse_line(payload: dict | str) -> bytes:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return f"data: {text}\n\n".encode()


def _fragmented_tool_call(*, call_id: str, name: str, arguments: dict) -> list[bytes]:
    raw = json.dumps(arguments, sort_keys=True)
    events = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": name}}]}}]},
    ]
    for start in range(0, len(raw), 3):
        piece = raw[start : start + 3]
        events.append(
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": piece}}]}}
                ]
            }
        )
    events.append({"choices": [], "usage": {"completion_tokens": 12}})
    events.append("[DONE]")
    return [_sse_line(event) for event in events]


class NativePilotHarness:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.records: list[dict] = []
        self.tokens_issued: list[str] = []
        self.chat_posts = 0
        self.call_ids: list[str] = []
        self.requests: list[dict] = []
        self.instance = {
            "id": PROVIDER_ID,
            "type": INSTANCE_TYPE,
            "region": REGION,
            "tags": ["blackwell-lab", f"run:{RUN_TAG}"],
        }
        self.served_model = "synthetic-nemotron"
        self._vllm: ThreadingHTTPServer | None = None
        self._meta: ThreadingHTTPServer | None = None
        self._threads: list[threading.Thread] = []

    @property
    def vllm_origin(self) -> str:
        assert self._vllm is not None
        host, port = self._vllm.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def engine_base(self) -> str:
        return f"{self.vllm_origin}/v1"

    @property
    def meta_origin(self) -> str:
        assert self._meta is not None
        host, port = self._meta.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._vllm = ThreadingHTTPServer(("127.0.0.1", 0), self._vllm_handler())
        self._meta = ThreadingHTTPServer(("127.0.0.1", 0), self._meta_handler())
        for server in (self._vllm, self._meta):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        for server in (self._vllm, self._meta):
            if server is not None:
                server.shutdown()
                server.server_close()

    def remap_metadata_request(self, method: str, url: str, headers: dict[str, str]):
        if not url.startswith(provenance.METADATA_BASE):
            raise AssertionError(f"metadata transport received a non-metadata URL: {url}")
        rewritten = url.replace("http://169.254.169.254", self.meta_origin, 1)
        return provenance.default_http_request_json(method, rewritten, headers)

    def _vllm_handler(self):
        harness = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return None

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path == "/health":
                    self._json(200, {"status": "ok"})
                    return
                if path == "/v1/models":
                    self._json(200, {"data": [{"id": harness.served_model}]})
                    return
                if path == "/version":
                    self._json(200, {"version": ENGINE_VERSION})
                    return
                if path == "/v1/version":
                    self.send_error(404, "vLLM /version is not under /v1")
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                path = self.path.split("?", 1)[0]
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                body = json.loads(raw.decode("utf-8") or "{}")
                with harness.lock:
                    harness.chat_posts += 1
                    call_id = f"native-{harness.chat_posts}"
                    harness.call_ids.append(call_id)
                    harness.requests.append(body)
                    harness.records.append({"server": "vllm", "method": "POST", "path": path})
                _assert_native_request(body)
                payload = b"".join(_native_turn_sse(body, call_id))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _json(self, status: int, payload: dict) -> None:
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        return Handler

    def _meta_handler(self):
        harness = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return None

            def do_PUT(self) -> None:
                path = self.path.split("?", 1)[0]
                headers = {k: v for k, v in self.headers.items()}
                if path != "/v1/token":
                    self.send_error(404)
                    return
                if headers.get("Metadata-Token-Expiry-Seconds") != "60":
                    self.send_error(400, "malformed token request")
                    return
                with harness.lock:
                    token = f"meta-token-{len(harness.tokens_issued) + 1}"
                    harness.tokens_issued.append(token)
                body = json.dumps([token]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                token = self.headers.get("Metadata-Token")
                if path != "/v1/instance":
                    self.send_error(404)
                    return
                if token not in harness.tokens_issued:
                    self.send_error(401, "metadata token required")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(harness.instance).encode("utf-8"))

        return Handler


def _assert_native_request(body: dict) -> None:
    assert body["tools"] == openai_tool_definitions()
    assert body["tool_choice"] == "auto"
    assert body["parallel_tool_calls"] is False
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


def _scenario_from(body: dict):
    scenario_id = ""
    for message in body.get("messages") or []:
        content = message.get("content") or ""
        if not isinstance(content, str):
            continue
        for line in content.splitlines():
            if line.startswith("scenario_id:"):
                scenario_id = line.split(":", 1)[1].strip()
    scenarios = catalog()
    return scenarios.get(scenario_id) or next(iter(scenarios.values()))


def _native_turn_sse(body: dict, call_id: str) -> list[bytes]:
    tool_messages = [m for m in body.get("messages") or [] if m.get("role") == "tool"]
    if not tool_messages:
        return _fragmented_tool_call(call_id=call_id, name="get_service_health", arguments={})
    scenario = _scenario_from(body)
    return _fragmented_tool_call(
        call_id=call_id,
        name="recommend_remediation",
        arguments={
            "diagnosis_id": scenario.accepted_diagnoses[0],
            "rationale": scenario.root_cause_summary,
            "remediation_id": scenario.accepted_remediations[0],
        },
    )


def _write_model(directory: Path) -> str:
    artifact_dir = directory / "model"
    artifact_dir.mkdir()
    (artifact_dir / "config.json").write_text('{"arch":"synthetic"}\n', encoding="utf-8")
    (artifact_dir / "weights.bin").write_bytes(b"synthetic-weights-v1")
    manifest = directory / "model.sha256"
    entries = []
    for path in sorted(p for p in artifact_dir.rglob("*") if p.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append((digest, path.relative_to(artifact_dir).as_posix()))
    manifest.write_text(
        "\n".join(f"{digest}  {name}" for digest, name in entries) + "\n",
        encoding="utf-8",
    )
    canonical = "\n".join(
        f"{digest}  {name}" for digest, name in sorted(entries, key=lambda item: item[1])
    )
    aggregate = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{aggregate}"


def _approved_config(directory: Path, *, engine_base: str, aggregate: str) -> Path:
    config = {
        "endpoint": {"base_url": engine_base, "model": "synthetic-nemotron"},
        "cloud": {
            "instance_type": INSTANCE_TYPE,
            "region": REGION,
            "list_price_usd_per_hour": 3.0,
            "price_source_date": "2026-09-06",
        },
        "model": {
            "artifact": "synthetic/nemotron",
            "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "artifact_hash": aggregate,
            "precision": "bf16",
        },
        "serving": {
            "engine": "vllm",
            "engine_version": ENGINE_VERSION,
            "image": "docker.io/vllm/vllm-openai:v0.27.1",
            "container_digest": CONTAINER_DIGEST,
            "container_cuda_runtime_version": "13.0",
        },
        "host": {
            "storage_description": "plan NVMe",
            "network_description": "plan default networking",
        },
        "comparison_mode": "provider-native",
        "expected_gpu_model": "RTX PRO 6000 Blackwell",
        "model_verification": {
            "artifact_dir": str(directory / "model"),
            "digest_manifest": str(directory / "model.sha256"),
        },
        "cells": [
            {"profile": "interactive", "concurrency": 1},
            {"profile": "batch-heavy", "concurrency": 4},
            {"profile": "batch-heavy", "concurrency": 8},
        ],
        "warmup_passes": 1,
        "repetitions": 1,
        "tasks_per_repetition": 20,
    }
    path = directory / "pilot.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _ready_ledger() -> dict:
    return {
        "run_tag": RUN_TAG,
        "reconciled": True,
        "reconciliation": {"provider_checked": True},
        "resources": [
            {
                "address": "linode_instance.gpu_baseline",
                "type": "linode_instance",
                "provider_id": PROVIDER_ID,
                "region": REGION,
            },
            {
                "address": "linode_firewall.gpu_baseline",
                "type": "linode_firewall",
                "provider_id": "555",
            },
        ],
    }


def _install_production_stubs(monkeypatch, harness: NativePilotHarness) -> None:
    monkeypatch.setattr(lifecycle, "refuse_hosted_execution", lambda environ=None: None)
    monkeypatch.setattr(telemetry, "resolve_container_digest", lambda *a, **k: CONTAINER_DIGEST)
    monkeypatch.setattr(telemetry, "observe_container_cuda_version", lambda *a, **k: "13.0")
    monkeypatch.setattr(
        telemetry,
        "collect_gpu_facts",
        lambda *a, **k: {
            "gpu_model": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
            "gpu_count": 1,
            "gpu_memory_gb": 96.0,
            "driver_version": "580.65.06",
            "driver_max_cuda_version": "13.0",
        },
    )
    monkeypatch.setattr(telemetry, "GpuSamplerThread", FakeGpuSampler)
    real_verify = provenance.verify_live_provenance

    def verify_with_metadata_transport(**kwargs):
        kwargs.setdefault("http_request", harness.remap_metadata_request)
        return real_verify(**kwargs)

    monkeypatch.setattr(provenance, "verify_live_provenance", verify_with_metadata_transport)


def _external_dirs():
    results = Path(tempfile.mkdtemp(prefix="bwlab-native-results-", dir="/tmp"))
    config_dir = Path(tempfile.mkdtemp(prefix="bwlab-native-config-", dir="/tmp"))
    assert not str(results.resolve()).startswith(str(REPO_ROOT))
    assert not str(config_dir.resolve()).startswith(str(REPO_ROOT))
    return results, config_dir


def _forbidden_needles() -> tuple[str, ...]:
    return (
        "TOOL_CALL:",
        "TOOL_RESULT:",
        "private chain",
        "LINODE_TOKEN",
        "HF_TOKEN",
        "/home/",
        "BEGIN PRIVATE",
    )


class TestNativeToolProductionPath:
    def test_three_d0014_cells_complete_with_native_round_trips(self, monkeypatch, capsys):
        harness = NativePilotHarness()
        harness.start()
        try:
            results, config_dir = _external_dirs()
            monkeypatch.setenv("LAB_RESULTS_DIR", str(results))
            aggregate = _write_model(config_dir)
            config_path = _approved_config(
                config_dir, engine_base=harness.engine_base, aggregate=aggregate
            )
            paths = lifecycle.lifecycle_paths(results, RUN_TAG)
            lifecycle.write_private_json(paths.ledger_path, _ready_ledger())
            _install_production_stubs(monkeypatch, harness)
            phrase = cli.PILOT_APPROVAL_TEMPLATE.format(
                run_tag=RUN_TAG,
                run_label=RUN_LABEL,
                config_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
            )
            for path in ("/health", "/v1/models"):
                request = urllib.request.Request(f"{harness.vllm_origin}{path}")  # noqa: S310
                with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
                    assert response.status == 200

            assert (
                main(
                    [
                        "pilot",
                        "--run-tag",
                        RUN_TAG,
                        "--run-label",
                        RUN_LABEL,
                        "--config",
                        str(config_path),
                        "--approve",
                        phrase,
                    ]
                )
                == 0
            )
            report = json.loads(capsys.readouterr().out)
            assert report["workflow"] == "pilot"
            assert [(c["profile"], c["concurrency"]) for c in report["cells"]] == [
                ("interactive", 1),
                ("batch-heavy", 4),
                ("batch-heavy", 8),
            ]
            assert all(cell["tasks"]["attempted"] == 20 for cell in report["cells"])
            assert all(cell["tasks"]["errored"] == 0 for cell in report["cells"])
            assert all(cell["tasks"]["timed_out"] == 0 for cell in report["cells"])

            # 20 tasks x 2 turns x (1 warmup + 1 measured) x 3 cells
            assert harness.chat_posts == 240
            assert len(set(harness.call_ids)) == 240
            second_turns = [
                req
                for req in harness.requests
                if any(message.get("role") == "tool" for message in req["messages"])
            ]
            assert len(second_turns) == 120
            for request in second_turns:
                tool_msg = next(m for m in request["messages"] if m["role"] == "tool")
                assistant = next(m for m in request["messages"] if m.get("tool_calls"))
                assert tool_msg["tool_call_id"] == assistant["tool_calls"][0]["id"]
                assert tool_msg["tool_call_id"] in harness.call_ids

            result_paths = sorted((results / "real-runs").rglob("*.result.json"))
            observation_paths = sorted((results / "real-runs").rglob("*observations.json"))
            manifest_paths = sorted((results / "real-runs").rglob("*.manifest.json"))
            assert len(result_paths) == 3
            assert len(manifest_paths) == 3
            completed = 0
            for path in observation_paths:
                document = json.loads(path.read_text(encoding="utf-8"))
                for observation in document["observations"]:
                    assert observation["status"] == "completed"
                    assert observation["error_category"] is None
                    completed += 1
                    blob = json.dumps(observation)
                    for needle in _forbidden_needles():
                        assert needle not in blob
            assert completed == 120

            for path in manifest_paths:
                serving = json.loads(path.read_text(encoding="utf-8"))["serving"]
                assert serving["tool_call_transport"] == "openai-native-tools"
                assert serving["tool_call_parser"] == "qwen3_coder"
                assert serving["reasoning_parser"] == "nemotron_v3"
                generation = json.loads(path.read_text(encoding="utf-8"))["generation"]
                assert generation["reasoning_mode"] is True

            assert main(["verify-results", "--subdirectory", "real-runs"]) == 0
            verification = json.loads(capsys.readouterr().out)
            assert verification["ok"] is True
            assert verification["verified"] == 3
        finally:
            harness.stop()
