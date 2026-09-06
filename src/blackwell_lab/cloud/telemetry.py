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
- **GPU**: model name, memory, host driver version, and the **maximum CUDA
  version supported by the driver** (the ``nvidia-smi`` banner value — this
  is deliberately NOT called the runtime CUDA version; the container's
  actual CUDA runtime is observed separately via
  :func:`observe_container_cuda_version`), plus sampled utilization /
  memory / power / temperature series summarized into the result schema's
  ``gpu`` block.
- **Sampling accuracy**: every GPU sample carries a monotonic-clock
  timestamp; energy is integrated over the actual elapsed intervals
  (trapezoidal rule between consecutive samples, no extrapolation beyond
  the first/last sample); the summary records sampling start/end, actual
  duration, sample count, coverage, interval statistics, peak temperature,
  and the integration method, and fails visibly when coverage is
  insufficient.
- **Provenance digests**: the immutable serving-container digest
  (``repo@sha256:...``) from ``docker inspect``, and the model-artifact
  aggregate hash verified file-by-file against a pinned digest manifest.

All process execution goes through an injectable ``runner`` so tests are
fully offline and hosts without the tools produce explicit
:class:`TelemetryUnavailable` errors rather than invented values.
"""

from __future__ import annotations

import hashlib
import itertools
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

from blackwell_lab.workload.clock import SYSTEM_CLOCK, Clock

#: Injectable command runner: (argv) -> stdout text. Raises on failure.
CommandRunner = Callable[[list[str]], str]

#: Default sampling interval for the GPU sampler (seconds).
DEFAULT_GPU_SAMPLE_INTERVAL_S = 1.0

#: Minimum fraction of the sampling window the samples must cover for the
#: summary to be usable in an approved measurement.
MIN_SAMPLING_COVERAGE = 0.8

#: The documented integration method recorded in every summary.
ENERGY_INTEGRATION_METHOD = (
    "trapezoidal over actual monotonic sample intervals; no extrapolation "
    "beyond the first/last sample (boundary intervals outside the sampled "
    "span are excluded from the integral and reported via coverage)"
)


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

    The banner's "CUDA Version" is recorded as ``driver_max_cuda_version``:
    it is the **maximum CUDA version the installed driver supports**, not
    the CUDA runtime any container actually uses. The container runtime is
    observed separately (:func:`observe_container_cuda_version`).
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
        raise TelemetryUnavailable(
            "the driver's max supported CUDA version was not reported by nvidia-smi"
        )

    return {
        "gpu_model": name,
        "gpu_count": 1,
        "gpu_memory_gb": memory_gb,
        "driver_version": driver_version,
        "driver_max_cuda_version": match.group(1),
    }


def observe_container_cuda_version(container_name: str, runner: CommandRunner = run_command) -> str:
    """The ACTUAL CUDA runtime version inside the running serving container.

    Observed via ``torch.version.cuda`` inside the container — never taken
    from the ``nvidia-smi`` banner, which reports the driver's maximum
    supported CUDA version, a different fact.
    """
    output = runner(
        [
            "docker",
            "exec",
            container_name,
            "python3",
            "-c",
            "import torch; print(torch.version.cuda)",
        ]
    ).strip()
    version = output.splitlines()[-1].strip() if output else ""
    if not re.fullmatch(r"[0-9]+\.[0-9]+", version):
        raise TelemetryUnavailable(
            "the container's CUDA runtime version could not be observed "
            "(torch.version.cuda did not return a plain version)"
        )
    return version


@dataclass(frozen=True)
class GpuSample:
    """One genuine GPU telemetry sample, timestamped on the monotonic clock."""

    monotonic_s: float
    utilization_pct: float
    memory_used_gib: float
    power_watts: float
    temperature_c: float


def read_gpu_sample(
    runner: CommandRunner = run_command, *, clock: Clock = SYSTEM_CLOCK
) -> GpuSample:
    """Reads one utilization/memory/power/temperature sample via nvidia-smi.

    The sample is stamped with the injectable monotonic clock so energy can
    be integrated over the ACTUAL elapsed intervals rather than a nominal
    sampling interval.
    """
    output = runner(
        ["nvidia-smi", f"--query-gpu={_NVIDIA_SMI_SAMPLE_QUERY}", "--format=csv,noheader,nounits"]
    )
    stamped_at = clock.monotonic()
    rows = [line.strip() for line in output.splitlines() if line.strip()]
    if not rows:
        raise TelemetryUnavailable("nvidia-smi returned no telemetry sample")
    parts = [p.strip() for p in rows[0].split(",")]
    if len(parts) != 4:
        raise TelemetryUnavailable("nvidia-smi returned an unparseable telemetry sample")
    try:
        return GpuSample(
            monotonic_s=stamped_at,
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
    window_started_monotonic_s: float,
    window_ended_monotonic_s: float,
    successful_tasks: int,
    min_coverage: float = MIN_SAMPLING_COVERAGE,
) -> dict:
    """Builds the result schema's available ``gpu`` block from genuine samples.

    Energy is integrated with the trapezoidal rule over the ACTUAL elapsed
    monotonic intervals between consecutive samples. Boundary handling is
    documented and conservative: nothing is extrapolated beyond the first or
    last sample, so the integral covers only the sampled span; the gap
    between the sampling window boundaries and the sampled span is exposed
    through ``coverage_fraction`` and never silently filled in.

    Fails visibly (:class:`TelemetryUnavailable`) when fewer than two samples
    exist, timestamps are not strictly increasing, or coverage is below
    ``min_coverage`` — an approved measurement never proceeds on
    insufficient telemetry.
    """
    if len(samples) < 2:
        raise TelemetryUnavailable(
            "insufficient GPU telemetry: at least two timestamped samples are "
            "required to integrate energy over actual intervals"
        )
    if window_ended_monotonic_s <= window_started_monotonic_s:
        raise TelemetryUnavailable("the GPU sampling window has a non-positive duration")
    timestamps = [s.monotonic_s for s in samples]
    if any(b <= a for a, b in itertools.pairwise(timestamps)):
        raise TelemetryUnavailable(
            "GPU telemetry timestamps are not strictly increasing; the sample "
            "series cannot be integrated"
        )

    window_s = window_ended_monotonic_s - window_started_monotonic_s
    covered_s = timestamps[-1] - timestamps[0]
    coverage = covered_s / window_s
    if coverage < min_coverage:
        raise TelemetryUnavailable(
            f"GPU telemetry coverage {coverage:.2f} is below the required "
            f"minimum {min_coverage:.2f} for an approved measurement; the "
            "sampled span does not adequately cover the measurement window"
        )

    intervals = [b - a for a, b in itertools.pairwise(timestamps)]
    energy_joules = sum(
        (samples[i].power_watts + samples[i + 1].power_watts) / 2.0 * intervals[i]
        for i in range(len(intervals))
    )
    utilizations = sorted(s.utilization_pct for s in samples)
    p95_rank = max(1, -(-len(utilizations) * 95 // 100))  # ceil without math import
    return {
        "telemetry_available": True,
        "utilization_mean_pct": round(sum(utilizations) / len(utilizations), 2),
        "utilization_p95_pct": round(utilizations[p95_rank - 1], 2),
        "memory_peak_gib": round(max(s.memory_used_gib for s in samples), 3),
        "power_mean_watts": round(energy_joules / covered_s, 2),
        "energy_joules": round(energy_joules, 1),
        "energy_per_successful_task_joules": (
            round(energy_joules / successful_tasks, 1) if successful_tasks > 0 else None
        ),
        "sampling": {
            "sample_count": len(samples),
            "window_duration_s": round(window_s, 3),
            "covered_duration_s": round(covered_s, 3),
            "coverage_fraction": round(coverage, 4),
            "interval_mean_s": round(sum(intervals) / len(intervals), 4),
            "interval_max_s": round(max(intervals), 4),
            "peak_temperature_c": round(max(s.temperature_c for s in samples), 1),
            "integration_method": ENERGY_INTEGRATION_METHOD,
        },
    }


class GpuSamplerThread:
    """Background sampler collecting genuine GPU telemetry during a pass.

    ``start()``/``stop()`` bracket one repetition and record the sampling
    window boundaries on the monotonic clock. Sampling failures are recorded
    (first reason wins) and surface as :class:`TelemetryUnavailable` from
    :meth:`summary` — a run with broken telemetry fails visibly instead of
    producing a partial or fabricated summary.
    """

    def __init__(
        self,
        runner: CommandRunner = run_command,
        *,
        interval_s: float = DEFAULT_GPU_SAMPLE_INTERVAL_S,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be > 0")
        self._runner = runner
        self._interval_s = interval_s
        self._clock = clock
        self._samples: list[GpuSample] = []
        self._failure_reason: str | None = None
        self._window_started: float | None = None
        self._window_ended: float | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("sampler already started")
        self._stop_event.clear()
        self._window_started = self._clock.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None
        self._window_ended = self._clock.monotonic()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._samples.append(read_gpu_sample(self._runner, clock=self._clock))
            except TelemetryUnavailable as exc:
                if self._failure_reason is None:
                    self._failure_reason = str(exc)
                return
            self._stop_event.wait(self._interval_s)

    def summary(self, *, successful_tasks: int) -> dict:
        if self._failure_reason is not None:
            raise TelemetryUnavailable(f"GPU sampling failed mid-run: {self._failure_reason}")
        if self._window_started is None or self._window_ended is None:
            raise TelemetryUnavailable("the GPU sampling window was never started and stopped")
        return summarize_gpu_samples(
            self._samples,
            window_started_monotonic_s=self._window_started,
            window_ended_monotonic_s=self._window_ended,
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
