"""Pinned, isolated qualification environment gate (decision D-0031)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from blackwell_lab.cloud import qual_env
from blackwell_lab.cloud.qual_env import (
    MARKER_KIND,
    REQUIRED_DISTRIBUTIONS,
    REQUIRED_FORMATS,
    format_checker_report,
    in_virtual_environment,
    inspect_environment,
    installed_versions,
    pinned_versions,
    require_qualification_environment,
    write_marker,
)
from blackwell_lab.workload.validation import ConfigError

REPO = Path(__file__).resolve().parents[1]


class TestPins:
    def test_constraints_pin_every_required_distribution_exactly(self):
        pins = pinned_versions()
        for name in REQUIRED_DISTRIBUTIONS:
            assert qual_env.normalize(name) in pins, name
        assert pins["jsonschema"] == "4.26.0"
        assert pins["rfc3339-validator"] == "0.1.4"

    def test_python_version_markers_select_one_rpds_pin(self):
        new = pinned_versions(python_version=(3, 12))
        old = pinned_versions(python_version=(3, 10))
        assert new["rpds-py"] == "2026.6.3"
        assert old["rpds-py"] == "0.30.0"

    def test_non_exact_pins_and_unknown_markers_are_refused(self, tmp_path):
        loose = tmp_path / "c.txt"
        loose.write_text("jsonschema>=4\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="non-exact"):
            pinned_versions(loose)
        odd = tmp_path / "d.txt"
        odd.write_text('jsonschema==4.26.0; sys_platform == "linux"\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="unsupported environment marker"):
            pinned_versions(odd)

    def test_format_checkers_are_registered_here(self):
        report = format_checker_report()
        assert set(report) == set(REQUIRED_FORMATS) == {"date", "date-time"}
        assert all(report.values())


class TestInspection:
    def test_a_bootstrapped_environment_is_ok(self, tmp_path):
        marker = tmp_path / "marker.json"
        document = write_marker(marker)
        assert document["kind"] == MARKER_KIND
        assert document["installed_versions"] == installed_versions()
        report = inspect_environment(prefix="/venv", base_prefix="/usr", marker=marker)
        assert report["ok"] is True
        assert report["problems"] == []
        assert report["marker_valid"] is True
        assert report["format_checkers"] == {"date": True, "date-time": True}
        blob = json.dumps(report)
        assert str(tmp_path) not in blob and "/venv" not in blob

    def test_system_python_is_refused(self, tmp_path, monkeypatch):
        # The session fixture assumes isolation; restore the real predicate here.
        monkeypatch.setattr(qual_env, "in_virtual_environment", in_virtual_environment)
        marker = tmp_path / "marker.json"
        write_marker(marker)
        report = inspect_environment(prefix="/usr", base_prefix="/usr", marker=marker)
        assert report["ok"] is False
        assert "not_a_virtual_environment" in report["problems"]
        assert in_virtual_environment("/usr", "/usr") is False
        assert in_virtual_environment("/venv", "/usr") is True

    def test_missing_and_stale_markers_are_reported(self, tmp_path):
        report = inspect_environment(prefix="/venv", base_prefix="/usr", marker=tmp_path / "none")
        assert "marker_missing" in report["problems"] and report["marker_present"] is False
        marker = tmp_path / "marker.json"
        document = write_marker(marker)
        document["installed_versions"]["jsonschema"] = "0.0.0"
        marker.write_text(json.dumps(document), encoding="utf-8")
        report = inspect_environment(prefix="/venv", base_prefix="/usr", marker=marker)
        assert "marker_stale" in report["problems"] and report["marker_valid"] is False

    def test_version_drift_and_missing_distributions_are_reported(self, tmp_path, monkeypatch):
        marker = tmp_path / "marker.json"
        write_marker(marker)
        drifted = tmp_path / "c.txt"
        lines = [
            f"{name}=={'9.9.9' if name == 'jsonschema' else version}"
            for name, version in pinned_versions().items()
        ]
        drifted.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report = inspect_environment(
            constraints=drifted, prefix="/venv", base_prefix="/usr", marker=marker
        )
        assert "version_drift" in report["problems"]
        assert report["mismatches"]["jsonschema"]["pinned"] == "9.9.9"

        def missing(names=REQUIRED_DISTRIBUTIONS):
            found = installed_versions(names)
            found["rfc3339-validator"] = None
            return found

        monkeypatch.setattr(qual_env, "installed_versions", missing)
        report = inspect_environment(prefix="/venv", base_prefix="/usr", marker=marker)
        assert "missing_distribution" in report["problems"]


class TestGate:
    def test_live_mode_fails_closed_and_validate_only_reports(self, monkeypatch, tmp_path):
        monkeypatch.setattr(qual_env, "marker_path", lambda prefix=None: tmp_path / "absent")
        with pytest.raises(ConfigError, match="qualification environment is incomplete"):
            require_qualification_environment(validate_only=False)
        report = require_qualification_environment(validate_only=True)
        assert report["ok"] is False and "marker_missing" in report["problems"]

    def test_gate_passes_with_the_session_marker(self):
        report = require_qualification_environment(validate_only=False)
        assert report["ok"] is True

    def test_qualify_agent_refuses_before_reading_the_config(self, monkeypatch, tmp_path, capsys):
        from blackwell_lab.cloud.cli import main

        # Isolate this assertion from the earlier hosted-execution guard.
        # GitHub Actions sets CI and GITHUB_ACTIONS before the test runs.
        for name in (
            "CI",
            "GITHUB_ACTIONS",
            "CLOUD_AGENT",
            "CURSOR_AGENT_SOCKET",
            "CURSOR_AGENT_WORKER_ID",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(qual_env, "marker_path", lambda prefix=None: tmp_path / "absent")
        reads: list[str] = []
        monkeypatch.setattr(
            "blackwell_lab.cloud.qualification.require_external_config",
            lambda *a, **k: reads.append("config") or (_ for _ in ()).throw(AssertionError()),
        )
        code = main(
            [
                "qualify-agent",
                "--run-tag",
                "p3-qual-20260918a",
                "--run-label",
                "qual-a",
                "--candidate",
                "W1",
                "--stage",
                "development",
                "--config",
                str(tmp_path / "cfg.json"),
                "--approve",
                "x",
            ]
        )
        assert code == 1
        assert "qualification environment is incomplete" in capsys.readouterr().err
        assert reads == []


class TestBootstrapScript:
    SCRIPT = REPO / "scripts" / "bootstrap_qual_env.sh"

    def test_script_parses_and_is_executable(self):
        assert self.SCRIPT.is_file()
        subprocess.run(["/bin/bash", "-n", str(self.SCRIPT)], check=True)
        text = self.SCRIPT.read_text(encoding="utf-8")
        assert "constraints.txt" in text
        assert "write_marker" in text
        assert "-m venv" in text
        for forbidden in ("sudo", "pip install --user", "--break-system-packages"):
            assert forbidden not in text

    def test_script_refuses_an_environment_inside_the_repository(self):
        result = subprocess.run(
            ["/bin/bash", str(self.SCRIPT)],
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/nonexistent",
                "BWLAB_QUAL_ENV_DIR": str(REPO / ".qual-env"),
                "BWLAB_QUAL_PYTHON": sys.executable,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "outside the repository" in result.stderr
        assert not (REPO / ".qual-env").exists()

    def test_header_does_not_require_the_environment_for_analysis(self):
        header = " ".join(
            self.SCRIPT.read_text(encoding="utf-8").split("set -euo pipefail", 1)[0].split()
        )
        assert "qualify-agent" in header and "canary-agent" in header
        assert "analyze-qualification" in header
        assert "does not use this environment" in header

    def test_script_refuses_relative_nested_and_symlink_paths(self, tmp_path):
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "BWLAB_QUAL_PYTHON": sys.executable,
        }

        def run(directory: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["/bin/bash", str(self.SCRIPT)],
                cwd=cwd or tmp_path,
                env={**env, "BWLAB_QUAL_ENV_DIR": directory},
                capture_output=True,
                text=True,
                check=False,
            )

        relative = run(".qual-env-relative", cwd=REPO)
        assert relative.returncode == 1
        assert "outside the repository" in relative.stderr
        assert not (REPO / ".qual-env-relative").exists()

        nested = run(str(REPO / "src" / ".." / ".qual-env-nested"))
        assert nested.returncode == 1
        assert "outside the repository" in nested.stderr
        assert not (REPO / ".qual-env-nested").exists()

        link = tmp_path / "repo-link"
        link.symlink_to(REPO)
        mediated = run(str(link / "qual-env"))
        assert mediated.returncode == 1
        assert "outside the repository" in mediated.stderr
        assert not (REPO / "qual-env").exists()

        parent = tmp_path / "parent-link"
        parent.symlink_to(REPO / "tests")
        child = run(str(parent / "qual-env"))
        assert child.returncode == 1
        assert not (REPO / "tests" / "qual-env").exists()

    def test_an_outside_path_passes_the_containment_check(self, tmp_path):
        stub = tmp_path / "python-stub"
        stub.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-m" ]; then\n'
            '  echo "VENV_ATTEMPTED" >&2\n'
            "  exit 42\n"
            "fi\n"
            f'exec "{sys.executable}" "$@"\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
        outside = tmp_path / "outside-env"
        result = subprocess.run(
            ["/bin/bash", str(self.SCRIPT)],
            cwd=tmp_path,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path / "home"),
                "BWLAB_QUAL_PYTHON": str(stub),
                "BWLAB_QUAL_ENV_DIR": str(outside),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 42
        assert "VENV_ATTEMPTED" in result.stderr
        assert "outside the repository" not in result.stderr
        assert not outside.exists()
