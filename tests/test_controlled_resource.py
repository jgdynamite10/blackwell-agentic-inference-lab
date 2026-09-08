"""Offline adversarial tests for joint cgroup-v2 enforcement."""

from __future__ import annotations

from pathlib import Path

import pytest

from blackwell_lab.cloud.controlled_resource import (
    CPU_MAX_VALUE,
    JOINT_MEMORY_MAX_BYTES,
    ControlledResourceError,
    observe_cgroup,
    verify_controlled_resource,
    verify_provider_native,
)


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    cgroup = tmp_path / "sys"
    proc = tmp_path / "proc"
    slice_dir = cgroup / "bwlab-controlled.slice"
    slice_dir.mkdir(parents=True)
    (slice_dir / "cpu.max").write_text(CPU_MAX_VALUE + "\n", encoding="utf-8")
    (slice_dir / "memory.max").write_text(f"{JOINT_MEMORY_MAX_BYTES}\n", encoding="utf-8")
    (slice_dir / "memory.swap.max").write_text("0\n", encoding="utf-8")
    (slice_dir / "memory.events").write_text("oom_kill 0\n", encoding="utf-8")
    for pid in (11, 22):
        pid_dir = proc / str(pid)
        pid_dir.mkdir(parents=True)
        (pid_dir / "cgroup").write_text(
            f"0::/bwlab-controlled.slice/{pid}.scope\n", encoding="utf-8"
        )
    return cgroup, proc


def test_verified_joint_membership(tmp_path):
    cgroup, proc = _layout(tmp_path)
    view = observe_cgroup(
        serving_pid=11,
        benchmark_pid=22,
        docker_inspect={"HostConfig": {"NanoCpus": 0, "Memory": 0}},
        cgroup_root=cgroup,
        proc_root=proc,
    )
    facts = verify_controlled_resource(view)
    assert facts["verified"] is True
    assert facts["serving_in_slice"] is True
    assert facts["benchmark_in_slice"] is True
    assert facts["joint"] is True
    assert "11" not in str(facts)
    assert "22" not in str(facts)


def test_wrong_cpu_quota_fails_closed(tmp_path):
    cgroup, proc = _layout(tmp_path)
    (cgroup / "bwlab-controlled.slice" / "cpu.max").write_text("100000 100000\n", encoding="utf-8")
    view = observe_cgroup(
        serving_pid=11,
        benchmark_pid=22,
        docker_inspect={"HostConfig": {"NanoCpus": 0, "Memory": 0}},
        cgroup_root=cgroup,
        proc_root=proc,
    )
    with pytest.raises(ControlledResourceError, match=r"cpu\.max"):
        verify_controlled_resource(view)


def test_missing_memory_cap_fails_closed(tmp_path):
    cgroup, proc = _layout(tmp_path)
    (cgroup / "bwlab-controlled.slice" / "memory.max").write_text("max\n", encoding="utf-8")
    view = observe_cgroup(
        serving_pid=11,
        benchmark_pid=22,
        docker_inspect={"HostConfig": {"NanoCpus": 0, "Memory": 0}},
        cgroup_root=cgroup,
        proc_root=proc,
    )
    with pytest.raises(ControlledResourceError, match=r"memory\.max"):
        verify_controlled_resource(view)


def test_swap_escape_fails_closed(tmp_path):
    cgroup, proc = _layout(tmp_path)
    (cgroup / "bwlab-controlled.slice" / "memory.swap.max").write_text("max\n", encoding="utf-8")
    view = observe_cgroup(
        serving_pid=11,
        benchmark_pid=22,
        docker_inspect={"HostConfig": {"NanoCpus": 0, "Memory": 0}},
        cgroup_root=cgroup,
        proc_root=proc,
    )
    with pytest.raises(ControlledResourceError, match="swap"):
        verify_controlled_resource(view)


def test_split_docker_limits_fail_closed(tmp_path):
    cgroup, proc = _layout(tmp_path)
    view = observe_cgroup(
        serving_pid=11,
        benchmark_pid=22,
        docker_inspect={"HostConfig": {"NanoCpus": 14_000_000_000, "Memory": 100}},
        cgroup_root=cgroup,
        proc_root=proc,
    )
    with pytest.raises(ControlledResourceError, match="Docker"):
        verify_controlled_resource(view)


def test_pid_outside_slice_fails_closed(tmp_path):
    cgroup, proc = _layout(tmp_path)
    (proc / "22" / "cgroup").write_text("0::/user.slice\n", encoding="utf-8")
    view = observe_cgroup(
        serving_pid=11,
        benchmark_pid=22,
        docker_inspect={"HostConfig": {"NanoCpus": 0, "Memory": 0}},
        cgroup_root=cgroup,
        proc_root=proc,
    )
    with pytest.raises(ControlledResourceError, match="benchmark process"):
        verify_controlled_resource(view)


def test_oom_fails_closed(tmp_path):
    cgroup, proc = _layout(tmp_path)
    (cgroup / "bwlab-controlled.slice" / "memory.events").write_text(
        "oom_kill 1\n", encoding="utf-8"
    )
    view = observe_cgroup(
        serving_pid=11,
        benchmark_pid=22,
        docker_inspect={"HostConfig": {"NanoCpus": 0, "Memory": 0}},
        cgroup_root=cgroup,
        proc_root=proc,
    )
    with pytest.raises(ControlledResourceError, match="OOM"):
        verify_controlled_resource(view)


def test_provider_native_rejects_residual_slice(tmp_path):
    cgroup, proc = _layout(tmp_path)
    view = observe_cgroup(
        serving_pid=11,
        benchmark_pid=22,
        docker_inspect={"HostConfig": {"NanoCpus": 0, "Memory": 0}},
        cgroup_root=cgroup,
        proc_root=proc,
    )
    with pytest.raises(ControlledResourceError, match="residual"):
        verify_provider_native(view)


def test_provider_native_rejects_residual_docker_cap(tmp_path):
    cgroup = tmp_path / "sys"
    proc = tmp_path / "proc"
    cgroup.mkdir()
    (proc / "11").mkdir(parents=True)
    (proc / "11" / "cgroup").write_text("0::/system.slice/docker.service\n", encoding="utf-8")
    (proc / "22").mkdir(parents=True)
    (proc / "22" / "cgroup").write_text("0::/user.slice\n", encoding="utf-8")
    view = observe_cgroup(
        serving_pid=11,
        benchmark_pid=22,
        docker_inspect={"HostConfig": {"NanoCpus": 4_000_000_000, "Memory": 0}},
        cgroup_root=cgroup,
        proc_root=proc,
    )
    with pytest.raises(ControlledResourceError, match="Docker resource cap"):
        verify_provider_native(view)


def test_provider_native_accepts_clean_host(tmp_path):
    cgroup = tmp_path / "sys"
    proc = tmp_path / "proc"
    cgroup.mkdir()
    (proc / "11").mkdir(parents=True)
    (proc / "11" / "cgroup").write_text("0::/system.slice/docker.service\n", encoding="utf-8")
    (proc / "22").mkdir(parents=True)
    (proc / "22" / "cgroup").write_text("0::/user.slice\n", encoding="utf-8")
    view = observe_cgroup(
        serving_pid=11,
        benchmark_pid=22,
        docker_inspect={"HostConfig": {"NanoCpus": 0, "Memory": 0}},
        cgroup_root=cgroup,
        proc_root=proc,
    )
    facts = verify_provider_native(view)
    assert facts["controlled_slice_present"] is False
    assert facts["verified"] is True
