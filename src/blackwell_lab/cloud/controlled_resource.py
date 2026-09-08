"""Joint cgroup-v2 / systemd-slice enforcement for controlled-resource mode.

The envelope is one SHARED maximum across the serving container and the
benchmark process together (decision D-0017 / D-0004):

- 14 vCPUs total
- 100 GiB system memory total
- swap frozen at 0 (no silent swap-based escape)

Applying 14 vCPUs / 100 GiB independently to each workload would be a
fabrication of the joint envelope and is rejected. Provider-native mode
must observe that no controlled slice, Docker resource limit, or residual
cap remains.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

JOINT_VCPU_LIMIT = 14
JOINT_MEMORY_GIB = 100
JOINT_MEMORY_MAX_BYTES = JOINT_MEMORY_GIB * 1024 * 1024 * 1024
# cgroup v2 cpu.max: ``$MAX $PERIOD``. 14 CPUs * 100000 us period.
CPU_PERIOD_US = 100_000
CPU_MAX_QUOTA_US = JOINT_VCPU_LIMIT * CPU_PERIOD_US
CPU_MAX_VALUE = f"{CPU_MAX_QUOTA_US} {CPU_PERIOD_US}"
SWAP_MAX_BYTES = 0
CONTROLLED_SLICE = "bwlab-controlled.slice"
SERVING_CONTAINER_NAME = "bwlab-vllm"

_CGROUP_LINE_RE = re.compile(r"^(?:0|1):\s*([^:]*):(/.*)$")


class ControlledResourceError(RuntimeError):
    """Live enforcement observation failed; sanitized (no pids, paths, or ids)."""


@dataclass(frozen=True)
class CgroupView:
    """Sanitized, observed cgroup facts. Never includes PIDs or host paths."""

    cpu_max: str | None
    memory_max_bytes: int | None
    swap_max_bytes: int | None
    oom_kill: int | None
    slice_present: bool
    serving_in_slice: bool
    benchmark_in_slice: bool
    docker_cpu_limit: int
    docker_memory_limit: int


Reader = Callable[[Path], str]
Existence = Callable[[Path], bool]


def declared_resource_limits() -> dict:
    """The frozen joint envelope recorded in gpu-mode manifests."""
    return {
        "vcpu_limit": JOINT_VCPU_LIMIT,
        "memory_limit_gib": JOINT_MEMORY_GIB,
    }


def sanitized_enforcement_facts(view: CgroupView, *, mode: str) -> dict:
    """Facts safe to persist in an external manifest (no identifiers)."""
    return {
        "verified": True,
        "mode": mode,
        "slice": CONTROLLED_SLICE,
        "joint": True,
        "cpu_max": view.cpu_max,
        "memory_max_bytes": view.memory_max_bytes,
        "swap_max_bytes": view.swap_max_bytes,
        "oom_kill": view.oom_kill,
        "serving_in_slice": view.serving_in_slice,
        "benchmark_in_slice": view.benchmark_in_slice,
        "docker_cpu_limit": view.docker_cpu_limit,
        "docker_memory_limit": view.docker_memory_limit,
        "controlled_slice_present": view.slice_present,
    }


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _exists(path: Path) -> bool:
    return path.exists()


def _parse_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = raw.strip()
    if text in {"", "max"}:
        return None
    try:
        return int(text.split()[0])
    except ValueError:
        return None


def _parse_oom_kill(raw: str) -> int | None:
    for line in raw.splitlines():
        if line.startswith("oom_kill "):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _cgroup_path_for_pid(proc_root: Path, pid: int, reader: Reader) -> str:
    text = reader(proc_root / str(pid) / "cgroup")
    last = ""
    for line in text.splitlines():
        match = _CGROUP_LINE_RE.match(line.strip())
        if match is not None:
            last = match.group(2)
    return last


def _in_controlled_slice(cgroup_path: str) -> bool:
    return CONTROLLED_SLICE in cgroup_path.split("/")


def observe_cgroup(
    *,
    serving_pid: int | None,
    benchmark_pid: int | None,
    docker_inspect: Mapping[str, object] | None = None,
    cgroup_root: Path | None = None,
    proc_root: Path | None = None,
    reader: Reader = _read_text,
    exists: Existence = _exists,
) -> CgroupView:
    """Read live cgroup / Docker facts. Inject roots and readers in tests."""
    root = cgroup_root or Path("/sys/fs/cgroup")
    proc = proc_root or Path("/proc")
    slice_dir = root / CONTROLLED_SLICE
    slice_present = exists(slice_dir)
    cpu_max = None
    memory_max = None
    swap_max = None
    oom_kill = None
    if slice_present:
        with _suppress_read():
            cpu_max = reader(slice_dir / "cpu.max").strip()
        with _suppress_read():
            memory_max = _parse_int(reader(slice_dir / "memory.max"))
        with _suppress_read():
            swap_max = _parse_int(reader(slice_dir / "memory.swap.max"))
            if swap_max is None and exists(slice_dir / "memory.swap.max"):
                raw = reader(slice_dir / "memory.swap.max").strip()
                swap_max = 0 if raw == "0" else _parse_int(raw)
        with _suppress_read():
            oom_kill = _parse_oom_kill(reader(slice_dir / "memory.events"))

    serving_in = False
    benchmark_in = False
    if serving_pid is not None:
        with _suppress_read():
            serving_in = _in_controlled_slice(_cgroup_path_for_pid(proc, serving_pid, reader))
    if benchmark_pid is not None:
        with _suppress_read():
            benchmark_in = _in_controlled_slice(_cgroup_path_for_pid(proc, benchmark_pid, reader))

    inspect = docker_inspect or {}
    host_config = inspect.get("HostConfig") if isinstance(inspect.get("HostConfig"), dict) else {}
    docker_cpu = int(host_config.get("NanoCpus") or 0) if isinstance(host_config, dict) else 0
    docker_memory = int(host_config.get("Memory") or 0) if isinstance(host_config, dict) else 0

    return CgroupView(
        cpu_max=cpu_max,
        memory_max_bytes=memory_max,
        swap_max_bytes=swap_max,
        oom_kill=oom_kill,
        slice_present=slice_present,
        serving_in_slice=serving_in,
        benchmark_in_slice=benchmark_in,
        docker_cpu_limit=docker_cpu,
        docker_memory_limit=docker_memory,
    )


class _suppress_read:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, (OSError, ValueError))


def verify_controlled_resource(view: CgroupView) -> dict:
    """Fail closed unless the joint slice is correctly applied to BOTH PIDs."""
    problems: list[str] = []
    if not view.slice_present:
        problems.append("controlled slice is missing")
    if view.cpu_max != CPU_MAX_VALUE:
        problems.append("cpu.max does not equal the joint 14-vCPU quota")
    if view.memory_max_bytes != JOINT_MEMORY_MAX_BYTES:
        problems.append("memory.max does not equal the joint 100 GiB cap")
    if view.swap_max_bytes != SWAP_MAX_BYTES:
        problems.append("swap is not frozen at 0")
    if not view.serving_in_slice:
        problems.append("serving container is not a descendant of the joint slice")
    if not view.benchmark_in_slice:
        problems.append("benchmark process is not a descendant of the joint slice")
    if view.docker_cpu_limit or view.docker_memory_limit:
        problems.append("Docker per-container resource limits would split the joint envelope")
    if view.oom_kill not in (0, None):
        problems.append("an OOM kill was observed under the controlled slice")
    if problems:
        raise ControlledResourceError(
            "controlled-resource enforcement failed: " + "; ".join(problems)
        )
    return sanitized_enforcement_facts(view, mode="controlled-resource")


def verify_provider_native(view: CgroupView) -> dict:
    """Fail closed if any controlled slice, Docker cap, or residual limit remains."""
    problems: list[str] = []
    if view.slice_present:
        problems.append("a controlled slice is still present")
    if view.serving_in_slice or view.benchmark_in_slice:
        problems.append("a process remains in the controlled slice")
    if view.docker_cpu_limit or view.docker_memory_limit:
        problems.append("a Docker resource cap remains")
    if problems:
        raise ControlledResourceError(
            "provider-native mode is invalidated by residual caps: " + "; ".join(problems)
        )
    return sanitized_enforcement_facts(view, mode="provider-native")


def parse_docker_inspect(raw: str) -> dict:
    """Parse ``docker inspect`` JSON; never echoes the payload on failure."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ControlledResourceError("docker inspect output was unparseable") from exc
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            return first
    if isinstance(payload, dict):
        return payload
    raise ControlledResourceError("docker inspect output was empty")


def current_pid() -> int:
    return os.getpid()
