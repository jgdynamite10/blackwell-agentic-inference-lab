"""Tests for the results-privacy path guard."""

from pathlib import Path

import pytest

from blackwell_lab.paths import (
    RESULTS_DIR_ENV_VAR,
    ResultsLocationError,
    repository_root,
    resolve_results_dir,
)

REPO_ROOT = repository_root()


def test_repository_root_finds_git_checkout():
    assert (REPO_ROOT / "AGENTS.md").exists()


def test_unset_variable_is_rejected(monkeypatch):
    monkeypatch.delenv(RESULTS_DIR_ENV_VAR, raising=False)
    with pytest.raises(ResultsLocationError, match="not set"):
        resolve_results_dir()


def test_empty_variable_is_rejected(monkeypatch):
    monkeypatch.setenv(RESULTS_DIR_ENV_VAR, "   ")
    with pytest.raises(ResultsLocationError, match="not set"):
        resolve_results_dir()


def test_path_inside_repository_is_rejected():
    with pytest.raises(ResultsLocationError, match="inside the public repository"):
        resolve_results_dir(str(REPO_ROOT / "results"))


def test_repository_root_itself_is_rejected():
    with pytest.raises(ResultsLocationError, match="inside the public repository"):
        resolve_results_dir(str(REPO_ROOT))


def test_relative_path_inside_repository_is_rejected(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    with pytest.raises(ResultsLocationError, match="inside the public repository"):
        resolve_results_dir("./results")


def test_symlink_into_repository_is_rejected(tmp_path):
    link = tmp_path / "sneaky-link"
    link.symlink_to(REPO_ROOT / "results")
    with pytest.raises(ResultsLocationError, match="inside the public repository"):
        resolve_results_dir(str(link))


def test_outside_path_is_accepted(tmp_path):
    private_dir = tmp_path / "private-results"
    resolved = resolve_results_dir(str(private_dir))
    assert resolved == private_dir.resolve()


def test_env_variable_outside_path_is_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv(RESULTS_DIR_ENV_VAR, str(tmp_path))
    assert resolve_results_dir() == Path(tmp_path).resolve()
