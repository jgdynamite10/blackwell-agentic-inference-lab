"""Tests for the results-privacy path guard (docs/results-privacy.md)."""

from pathlib import Path

import pytest

from blackwell_lab.paths import (
    RESULTS_DIR_ENV_VAR,
    ResultsLocationError,
    RunMode,
    repository_root,
    resolve_results_dir,
)

REPO_ROOT = repository_root()


def test_repository_root_finds_git_checkout():
    assert (REPO_ROOT / "AGENTS.md").exists()


class TestRealMode:
    """Real-run mode fails closed and never falls back into the repository."""

    def test_unset_variable_is_rejected(self, monkeypatch):
        monkeypatch.delenv(RESULTS_DIR_ENV_VAR, raising=False)
        with pytest.raises(ResultsLocationError, match="fail closed"):
            resolve_results_dir()

    def test_blank_variable_is_rejected(self, monkeypatch):
        monkeypatch.setenv(RESULTS_DIR_ENV_VAR, "   ")
        with pytest.raises(ResultsLocationError, match="fail closed"):
            resolve_results_dir()

    def test_results_subdirectory_is_rejected(self):
        with pytest.raises(ResultsLocationError, match="inside the repository"):
            resolve_results_dir(str(REPO_ROOT / "results"))

    def test_repository_root_itself_is_rejected(self):
        with pytest.raises(ResultsLocationError, match="inside the repository"):
            resolve_results_dir(str(REPO_ROOT))

    def test_deeply_nested_repository_path_is_rejected(self):
        with pytest.raises(ResultsLocationError, match="inside the repository"):
            resolve_results_dir(str(REPO_ROOT / "src" / "blackwell_lab" / "deep"))

    def test_bare_relative_path_is_rejected_before_resolution(self):
        with pytest.raises(ResultsLocationError, match="relative path"):
            resolve_results_dir("relative-results")

    def test_parent_relative_path_is_rejected_even_if_it_would_resolve_outside(self, monkeypatch):
        # From the repo root, "../external-results" would resolve outside the
        # repository — it is rejected anyway because it is relative.
        monkeypatch.chdir(REPO_ROOT)
        with pytest.raises(ResultsLocationError, match="relative path"):
            resolve_results_dir("../external-results")

    def test_dot_relative_path_is_rejected(self, monkeypatch):
        monkeypatch.chdir(REPO_ROOT)
        with pytest.raises(ResultsLocationError, match="relative path"):
            resolve_results_dir("./results")

    def test_symlink_resolving_into_repository_is_rejected(self, tmp_path):
        link = tmp_path / "sneaky-link"
        link.symlink_to(REPO_ROOT / "results")
        with pytest.raises(ResultsLocationError, match="inside the repository"):
            resolve_results_dir(str(link))

    def test_nonexistent_external_path_is_accepted_but_not_created(self, tmp_path):
        candidate = tmp_path / "does-not-exist-yet" / "lab-results"
        resolved = resolve_results_dir(str(candidate))
        assert resolved == candidate.resolve()
        assert not resolved.exists(), "the guard must not create directories"

    def test_writable_external_directory_is_accepted(self, tmp_path):
        private_dir = tmp_path / "private-results"
        private_dir.mkdir()
        (private_dir / "probe.txt").write_text("writable")
        resolved = resolve_results_dir(str(private_dir))
        assert resolved == private_dir.resolve()

    def test_env_variable_with_external_path_is_accepted(self, monkeypatch, tmp_path):
        monkeypatch.setenv(RESULTS_DIR_ENV_VAR, str(tmp_path))
        assert resolve_results_dir() == Path(tmp_path).resolve()


class TestSyntheticMode:
    """Synthetic/test mode is explicit and still never writes into the repo."""

    def test_unset_variable_yields_no_persistence(self, monkeypatch):
        monkeypatch.delenv(RESULTS_DIR_ENV_VAR, raising=False)
        assert resolve_results_dir(mode=RunMode.SYNTHETIC) is None

    def test_repository_internal_path_is_still_rejected(self):
        with pytest.raises(ResultsLocationError, match="inside the repository"):
            resolve_results_dir(str(REPO_ROOT / "results"), mode=RunMode.SYNTHETIC)

    def test_relative_path_is_still_rejected(self):
        with pytest.raises(ResultsLocationError, match="relative path"):
            resolve_results_dir("relative-results", mode=RunMode.SYNTHETIC)

    def test_symlink_into_repository_is_still_rejected(self, tmp_path):
        link = tmp_path / "sneaky-link"
        link.symlink_to(REPO_ROOT)
        with pytest.raises(ResultsLocationError, match="inside the repository"):
            resolve_results_dir(str(link), mode=RunMode.SYNTHETIC)

    def test_external_path_is_accepted(self, tmp_path):
        resolved = resolve_results_dir(str(tmp_path), mode=RunMode.SYNTHETIC)
        assert resolved == Path(tmp_path).resolve()


def test_modes_are_explicit_enum_values():
    assert RunMode.REAL.value == "real"
    assert RunMode.SYNTHETIC.value == "synthetic"
