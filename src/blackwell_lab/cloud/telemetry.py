"""Host and GPU telemetry collection for genuine (gpu-mode) runs.

Truthfulness contract (measurement contract; result schema 2.x): every fact
reported here is either **genuinely observed** on the host — via ``/proc``,
``nvidia-smi``, or ``docker inspect`` — or **explicitly unavailable with a
reason**. Nothing is fabricated, zero-filled, or defaulted. Mock execution
never calls this module; the mock result builder records GPU telemetry as
unavailable by construction.

Collected facts:

- **Host**: operating system, CPU model, vCPU count, system memory,
  storage/network descriptions (free-text, no volume identifiers or
  addresses).
- **GPU**: model name, memory, driver version, CUDA runtime version (from
  ``nvidia-smi``), plus sampled utilization / memory / power / temperature
  series summarized into the result schema's ``gpu`` block.
- **Provenance digests**: the immutable serving-container digest
  (``repo@sha256:...``) from ``docker inspect``, and the model-artifact
  aggregate hash verified file-by-file against a pinned digest manifest.

All process execution goes through an injectable ``runner`` so tests are
fully offline and hosts without the tools produce explicit
:class:`TelemetryUnavailable` errors rather than invented values.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: Injectable command runner: (argv) -> stdout text. Raises on failure.
CommandRunner = Callable[[list[str]], str]

#: Default sampling interval for the GPU sampler (seconds).
DEFAULT_GPU_SAMPLE_INTERVAL_S = 1.0


class TelemetryUnavailable(RuntimeError):
    """A telemetry source is genuinely unavailable (reason in ``str(exc)``)."""


def run_command(argv: list[str]) -> str:
    """Default command runner: captures stdout, raises on any failure."""
    executable = shutil.which(argv[0])
    if executable is None:
        raise TelemetryUnavailable(f"required tool not found on this host: {argv[0]}")
    completed = subprocess.run(
        [executable, *argv[1:]],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise TelemetryUnavailable(f"{argv[0]} exited with status {completed.returncode}")
    return completed.stdout


# -- host facts -------------------------------------------------------------


def _cpu_model_from_proc(proc_cpuinfo: str) -> str | None:
    for line in proc_cpuinfo.splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return None


def collect_host_facts(
    *,
    storage_description: str,
    network_description: str,
    virtualization: str | None = None,
    proc_cpuinfo_path: str = "/proc/cpuinfo",
) -> dict:
    """Genuine host facts for the run-manifest ``host`` block (no GPU fields).

    ``storage_description`` and ``network_description`` are operator-supplied
    plain descriptions of the purchasable configuration (e.g. plan NVMe size,
    advertised network tier) — never volume identifiers or addresses.
    """
    cpu_model: str | None = None
    try:
        cpu_model = _cpu_model_from_proc(Path(proc_cpuinfo_path).read_text(encoding="utf-8"))
    except OSError:
        cpu_model = None
    if cpu_model is None:
        cpu_model = platform.processor() or platform.machine() or None
    if not cpu_model:
        raise TelemetryUnavailable("CPU model could not be determined from /proc or platform")

    vcpu_count = os.cpu_count()
    if not vcpu_count:
        raise TelemetryUnavailable("vCPU count could not be determined")

    try:
        memory_gib = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024**3)
    except (ValueError, OSError, AttributeError) as exc:
        raise TelemetryUnavailable("system memory size could not be determined") from exc

    facts = {
        "operating_system": platform.platform(),
        "cpu_model": cpu_model,
        "vcpu_count": vcpu_count,
        "system_memory_gib": round(memory_gib, 2),
        "storage_description": storage_description,
        "network_description": network_description,
    }
    if virtualization:
        facts["virtualization"] = virtualization
    return facts


# -- GPU facts and sampling ---------------------------------------------------

_NVIDIA_SMI_FACT_QUERY = "name,memory.total,driver_version"
_NVIDIA_SMI_SAMPLE_QUERY = "utilization.gpu,memory.used,power.draw,temperature.gpu"
_CUDA_VERSION_RE = re.compile(r"CUDA Version\s*:?\s*([0-9]+\.[0-9]+)")


def collect_gpu_facts(runner: CommandRunner = run_command) -> dict:
    """GPU facts for the manifest ``host`` block, from ``nvidia-smi`` only.

    Raises :class:`TelemetryUnavailable` when ``nvidia-smi`` is missing or
    reports anything unparseable — GPU facts are never guessed.
    """
    output = runner(
        ["nvidia-smi", f"--query-gpu={_NVIDIA_SMI_FACT_QUERY}", "--format=csv,noheader,nounits"]
    )
    rows = [line.strip() for line in output.splitlines() if line.strip()]
    if len(rows) != 1:
        raise TelemetryUnavailable(
            f"expected exactly one GPU, nvidia-smi reported {len(rows)} "
            "(the Phase 3 baseline is a single-GPU instance)"
        )
    parts = [p.strip() for p in rows[0].split(",")]
    if len(parts) != 3:
        raise TelemetryUnavailable("nvidia-smi returned an unparseable GPU fact row")
    name, memory_mib_text, driver_version = parts
    try:
        memory_gb = round(int(memory_mib_text) / 1024.0, 2)
    except ValueError as exc:
        raise TelemetryUnavailable("nvidia-smi GPU memory value was not an integer") from exc

    banner = runner(["nvidia-smi"])
    match = _CUDA_VERSION_RE.search(banner)
    if match is None:
        raise TelemetryUnavailable("CUDA version not reported by nvidia-smi")

    return {
        "gpu_model": name,
        "gpu_count": 1,
        "gpu_memory_gb": memory_gb,
        "driver_version": driver_version,
        "cuda_version": match.group(1),
    }


@dataclass(frozen=True)
class GpuSample:
    """One genuine GPU telemetry sample."""

    utilization_pct: float
    memory_used_gib: float
    power_watts: float
    temperature_c: float


def read_gpu_sample(runner: CommandRunner = run_command) -> GpuSample:
    """Reads one utilization/memory/power/temperature sample via nvidia-smi."""
    output = runner(
        ["nvidia-smi", f"--query-gpu={_NVIDIA_SMI_SAMPLE_QUERY}", "--format=csv,noheader,nounits"]
    )
    rows = [line.strip() for line in output.splitlines() if line.strip()]
    if not rows:
        raise TelemetryUnavailable("nvidia-smi returned no telemetry sample")
    parts = [p.strip() for p in rows[0].split(",")]
    if len(parts) != 4:
        raise TelemetryUnavailable("nvidia-smi returned an unparseable telemetry sample")
    try:
        return GpuSample(
            utilization_pct=float(parts[0]),
            memory_used_gib=round(float(parts[1]) / 1024.0, 3),
            power_watts=float(parts[2]),
            temperature_c=float(parts[3]),
        )
    except ValueError as exc:
        raise TelemetryUnavailable("nvidia-smi telemetry sample was not numeric") from exc


def summarize_gpu_samples(
    samples: list[GpuSample],
    *,
    sample_interval_s: float,
    successful_tasks: int,
) -> dict:
    """Builds the result schema's available ``gpu`` block from genuine samples.

    Energy is integrated as mean power x sampled wall time (rectangle rule at
    the sampling interval) — an estimate derived from genuine samples, with
    the method recorded by the caller in the result notes.
    """
    if not samples:
        raise TelemetryUnavailable("no GPU telemetry samples were collected during the run")
    if sample_interval_s <= 0:
        raise ValueError("sample_interval_s must be > 0")
    utilizations = sorted(s.utilization_pct for s in samples)
    powers = [s.power_watts for s in samples]
    p95_rank = max(1, -(-len(utilizations) * 95 // 100))  # ceil without math import
    energy_joules = sum(p * sample_interval_s for p in powers)
    block = {
        "telemetry_available": True,
        "utilization_mean_pct": round(sum(utilizations) / len(utilizations), 2),
        "utilization_p95_pct": round(utilizations[p95_rank - 1], 2),
        "memory_peak_gib": round(max(s.memory_used_gib for s in samples), 3),
        "power_mean_watts": round(sum(powers) / len(powers), 2),
        "energy_joules": round(energy_joules, 1),
        "energy_per_successful_task_joules": (
            round(energy_joules / successful_tasks, 1) if successful_tasks > 0 else None
        ),
    }
    return block


class GpuSamplerThread:
    """Background sampler collecting genuine GPU telemetry during a pass.

    ``start()``/``stop()`` bracket one repetition. Sampling failures are
    recorded (first reason wins) and surface as :class:`TelemetryUnavailable`
    from :meth:`summary` — a run with broken telemetry fails visibly instead
    of producing a partial or fabricated summary.
    """

    def __init__(
        self,
        runner: CommandRunner = run_command,
        *,
        interval_s: float = DEFAULT_GPU_SAMPLE_INTERVAL_S,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        self._runner = runner
        self._interval_s = interval_s
        self._samples: list[GpuSample] = []
        self._failure_reason: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("sampler already started")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._samples.append(read_gpu_sample(self._runner))
            except TelemetryUnavailable as exc:
                if self._failure_reason is None:
                    self._failure_reason = str(exc)
                return
            self._stop_event.wait(self._interval_s)

    def summary(self, *, successful_tasks: int) -> dict:
        if self._failure_reason is not None:
            raise TelemetryUnavailable(f"GPU sampling failed mid-run: {self._failure_reason}")
        return summarize_gpu_samples(
            self._samples,
            sample_interval_s=self._interval_s,
            successful_tasks=successful_tasks,
        )


# -- provenance digests -------------------------------------------------------


def resolve_container_digest(image: str, runner: CommandRunner = run_command) -> str:
    """The immutable ``repo@sha256:...`` digest of a locally pulled image."""
    output = runner(["docker", "inspect", "--format", "{{index .RepoDigests 0}}", image]).strip()
    if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", output):
        raise TelemetryUnavailable(
            "docker inspect did not return an immutable repo@sha256 digest for the image"
        )
    return output


class ArtifactVerificationError(RuntimeError):
    """The model artifact on disk does not match its pinned digest manifest."""


def verify_model_artifact(artifact_dir: Path, digest_manifest: Path) -> str:
    """Verifies every pinned file digest, returning the aggregate hash.

    ``digest_manifest`` is the frozen ``sha256sum``-format file (one
    ``<64-hex>  <relative-path>`` line per model file) produced when the
    baseline is frozen. Every listed file must exist and hash to its pinned
    value; the aggregate is the SHA-256 of the sorted manifest lines and is
    recorded as the manifest's ``model.artifact_hash`` (``sha256:<hex>``).
    Serving must not start when this raises.
    """
    try:
        lines = [
            line.strip()
            for line in digest_manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError as exc:
        raise ArtifactVerificationError("digest manifest is missing or unreadable") from exc
    if not lines:
        raise ArtifactVerificationError("digest manifest is empty")

    entries: list[tuple[str, str]] = []
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})[ *]+(\S.*)", line)
        if match is None:
            raise ArtifactVerificationError("digest manifest has a malformed line")
        entries.append((match.group(1), match.group(2)))

    for expected_hex, relative_name in entries:
        if relative_name.startswith("/") or ".." in Path(relative_name).parts:
            raise ArtifactVerificationError("digest manifest contains an unsafe path")
        file_path = artifact_dir / relative_name
        digest = hashlib.sha256()
        try:
            with open(file_path, "rb") as handle:
                for block in iter(lambda h=handle: h.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise ArtifactVerificationError(
                f"model file missing or unreadable: {relative_name}"
            ) from exc
        if digest.hexdigest() != expected_hex:
            raise ArtifactVerificationError(f"digest mismatch for model file: {relative_name}")

    aggregate = hashlib.sha256(
        "\n".join(f"{h}  {n}" for h, n in sorted(entries, key=lambda e: e[1])).encode("utf-8")
    ).hexdigest()
    return f"sha256:{aggregate}"


def utc_epoch_seconds() -> float:
    """Wall-clock seconds (correlation only; durations use the monotonic clock)."""
    return time.time()
