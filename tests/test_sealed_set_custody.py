"""Adversarial tests for sealed-set custody. Payloads are artificial.

These tests construct placeholder bytes in this module. They do not open
scenario catalogs, accepted answers, workload prompts, or private results.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import jsonschema
import pytest

from blackwell_lab.paths import repository_root
from blackwell_lab.sealed_sets.controller import main
from blackwell_lab.sealed_sets.custody import (
    CustodyReceipt,
    import_authorized_set,
    load_bundle_directory,
    load_manifest_schema,
    report_receipt,
    validate_public_manifest,
    verify_custody,
)
from blackwell_lab.sealed_sets.model import (
    APPROVAL_TEMPLATE,
    INTEGRITY_STATEMENT,
    CustodyError,
    OpaqueTask,
    approval_phrase,
    build_public_manifest,
    canonical_manifest_bytes,
    controller_source_digest,
    sha256_digest,
    synthetic_placeholders,
    validate_bundles,
)

GIT = shutil.which("git")
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "blackwell_lab" / "sealed_sets"
SENTINEL_BODY = b"ARTIFICIAL-BODY-SENTINEL\n"
SENTINEL_ID = "d00placeholder"

if GIT is None:
    pytest.skip("git is required", allow_module_level=True)


@dataclass
class Fixture:
    root: Path
    repo: Path
    commit: str
    output: Path


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        [GIT, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def init_fixture_repo(path: Path) -> str:
    path.mkdir(parents=True)
    subprocess.run(
        [GIT, "-C", str(path), "init", "--quiet"],
        check=True,
        capture_output=True,
        text=True,
    )
    identity = [
        "-c",
        "user.email=custody-test@example.invalid",
        "-c",
        "user.name=Custody Test",
        "-c",
        "commit.gpgsign=false",
    ]
    (path / "README").write_text("synthetic fixture\n", encoding="utf-8")
    subprocess.run(
        [GIT, "-C", str(path), *identity, "add", "README"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [GIT, "-C", str(path), *identity, "commit", "-m", "synthetic fixture"],
        check=True,
        capture_output=True,
        text=True,
    )
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    repo = tmp_path / "fixture-repo"
    return Fixture(
        root=tmp_path,
        repo=repo,
        commit=init_fixture_repo(repo),
        output=tmp_path / "custody-output",
    )


def stage_tasks(prefix: str, label: str, count: int = 20) -> tuple[OpaqueTask, ...]:
    return tuple(
        OpaqueTask(
            f"{prefix}{index:02d}placeholder",
            f"artificial-{label}-{index:02d}\n".encode(),
        )
        for index in range(count)
    )


def bundles(
    dev_count: int = 20, hold_count: int = 20
) -> tuple[tuple[OpaqueTask, ...], tuple[OpaqueTask, ...]]:
    return stage_tasks("d", "dev", dev_count), stage_tasks("h", "hold", hold_count)


def attempt(
    fixture: Fixture,
    development: Sequence[OpaqueTask] | None = None,
    holdout: Sequence[OpaqueTask] | None = None,
    *,
    controller_digest: str | None = None,
    commit: str | None = None,
    approval: str | None = None,
    output: Path | str | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> CustodyReceipt:
    if development is None or holdout is None:
        development, holdout = bundles()
    digest = controller_source_digest()
    return import_authorized_set(
        repo=fixture.repo,
        output_root=fixture.output if output is None else output,
        expected_commit=fixture.commit if commit is None else commit,
        expected_controller_digest=digest if controller_digest is None else controller_digest,
        approval=approval_phrase(fixture.commit, digest) if approval is None else approval,
        development=development,
        holdout=holdout,
        checkpoint=checkpoint,
    )


def assert_no_valid_finalized(path: Path) -> None:
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        validate_public_manifest(document)
    except (OSError, UnicodeError, json.JSONDecodeError, CustodyError):
        return
    raise AssertionError("a schema-valid finalized manifest remained")


def expect_blocked(fixture: Fixture, reason: str, **kwargs: object) -> None:
    output = kwargs.get("output", fixture.output)
    with pytest.raises(CustodyError) as caught:
        attempt(fixture, **kwargs)  # type: ignore[arg-type]
    assert caught.value.reason == reason
    assert caught.value.__cause__ is None
    assert str(caught.value) == reason
    if isinstance(output, Path):
        assert_no_valid_finalized(output)


def write_bundle(directory: Path, tasks: Sequence[OpaqueTask]) -> None:
    os.mkdir(directory, 0o700)
    os.chmod(directory, 0o700)
    for task in tasks:
        fd = os.open(directory / task.task_id, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, task.content)
        finally:
            os.close(fd)
        os.chmod(directory / task.task_id, 0o600)


def run_controller(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "blackwell_lab.sealed_sets.controller", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def assert_modes(root: Path) -> None:
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for dirpath, _dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        assert not current.is_symlink()
        assert stat.S_IMODE(current.stat().st_mode) == 0o700
        for name in filenames:
            child = current / name
            assert not child.is_symlink()
            assert stat.S_IMODE(child.stat().st_mode) == 0o600


def test_sources_do_not_import_catalog_or_provider_modules() -> None:
    banned_prefixes = (
        "blackwell_lab.workload",
        "blackwell_lab.cloud",
        "blackwell_lab.qualification",
        "blackwell_lab.agent",
        "blackwell_lab.evaluator",
    )
    banned_constants = (
        "scenarios.py",
        "accepted_diagnos",
        "accepted_remediat",
        "SYSTEM_PROMPT",
    )
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for banned in banned_constants:
                    assert banned not in node.value
                continue
            else:
                continue
            for name in names:
                for banned in banned_prefixes:
                    assert not name.startswith(banned)


def test_synthetic_validation_is_in_memory_and_content_free() -> None:
    development, holdout = synthetic_placeholders()
    validation = validate_bundles(development, holdout)
    manifest = build_public_manifest(
        commit="a" * 40,
        controller_digest=controller_source_digest(),
        validation=validation,
    )
    validate_public_manifest(manifest)
    encoded = json.dumps(manifest)
    assert "synthetic-placeholder" not in encoded
    assert "ph00devplaceholder" not in encoded
    assert "ph00holdplaceholder" not in encoded
    assert manifest["development_task_count"] == 20
    assert manifest["holdout_task_count"] == 20
    assert len(manifest["file_digests"]) == 40
    assert manifest["integrity_statement"] == INTEGRITY_STATEMENT
    assert manifest["finalized"] is True
    schema = load_manifest_schema()
    jsonschema.validators.validator_for(schema).check_schema(schema)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(manifest)


def test_approval_phrase_is_defined_and_does_not_import(tmp_path: Path) -> None:
    commit = "589d4f4bfe53367edddaefd18caf55899622510d"
    digest = controller_source_digest()
    phrase = approval_phrase(commit, digest)
    assert phrase == (
        "I approve sealed qualification-set generation at canonical commit "
        f"{commit} using controller digest {digest}"
    )
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(CustodyError) as caught:
        approval_phrase("not-a-commit", digest)
    assert caught.value.reason == "approval-mismatch"


def test_validate_synthetic_and_approval_phrase_commands() -> None:
    synthetic = run_controller("validate-synthetic")
    assert synthetic.returncode == 0
    assert synthetic.stderr == ""
    for secret in ("synthetic-placeholder", "ph00devplaceholder", "ph00holdplaceholder"):
        assert secret not in synthetic.stdout
    payload = json.loads(synthetic.stdout)
    assert payload["status"] == "pass"
    assert payload["operation"] == "validate-synthetic"
    assert payload["development_task_count"] == 20
    assert payload["holdout_task_count"] == 20
    assert payload["controller_commit"] == ""
    assert payload["controller_digest"] == controller_source_digest()
    phrase = run_controller("approval-phrase")
    assert phrase.returncode == 0
    assert phrase.stdout == APPROVAL_TEMPLATE + "\n"
    assert "sha256:" not in phrase.stdout
    with pytest.raises(SystemExit) as caught:
        main([])
    assert caught.value.code == 2


def test_successful_import_binds_counts_modes_and_digests(fixture: Fixture) -> None:
    development, holdout = bundles()
    receipt = attempt(fixture, development, holdout)
    assert receipt.status == "pass"
    assert receipt.operation == "import"
    assert receipt.development_task_count == 20
    assert receipt.holdout_task_count == 20
    assert receipt.controller_commit == fixture.commit
    assert receipt.controller_digest == controller_source_digest()
    assert_modes(fixture.output)
    manifest = json.loads((fixture.output / "manifest.json").read_text(encoding="utf-8"))
    validate_public_manifest(manifest)
    public = (fixture.output / "manifest.json").read_text(encoding="utf-8")
    receipt_text = (fixture.output / "receipt.json").read_text(encoding="utf-8")
    index = json.loads((fixture.output / "private" / "index.json").read_text(encoding="utf-8"))
    dev_ids = set()
    hold_ids = set()
    dev_digests = set()
    hold_digests = set()
    for stage, id_set, digest_set in (
        ("development", dev_ids, dev_digests),
        ("holdout", hold_ids, hold_digests),
    ):
        rows = index[stage]
        assert len(rows) == 20
        lines = []
        for row in rows:
            task_id = row["task_id"]
            digest = row["content_digest"]
            id_set.add(task_id)
            digest_set.add(digest)
            blob = fixture.output / "private" / "blobs" / digest.removeprefix("sha256:")
            payload = blob.read_bytes()
            assert sha256_digest(payload) == digest
            assert task_id not in public
            assert task_id not in receipt_text
            assert payload.decode("utf-8") not in public
            lines.append(f"{task_id}\t{digest}\n")
        aggregate = sha256_digest("".join(lines).encode("utf-8"))
        assert aggregate == manifest[f"{stage}_aggregate_digest"]
    assert not dev_ids & hold_ids
    assert not dev_digests & hold_digests
    assert manifest["file_digests"] == sorted(dev_digests | hold_digests)
    assert manifest["custody_manifest_digest"] == sha256_digest(canonical_manifest_bytes(manifest))
    assert manifest["controller_commit"] == fixture.commit
    assert manifest["controller_digest"] == controller_source_digest()
    other = fixture.root / "custody-output-2"
    again = attempt(fixture, development, holdout, output=other)
    assert again.custody_manifest_digest == receipt.custody_manifest_digest
    verified = verify_custody(fixture.output, repo=fixture.repo, expected_commit=fixture.commit)
    assert verified.operation == "verify"
    assert verified.custody_manifest_digest == receipt.custody_manifest_digest
    stored = json.loads((fixture.output / "receipt.json").read_text(encoding="utf-8"))
    assert stored["operation"] == "import"
    reported = report_receipt(fixture.output, repo=fixture.repo, expected_commit=fixture.commit)
    assert reported.operation == "receipt"
    assert stored == json.loads((fixture.output / "receipt.json").read_text(encoding="utf-8"))


def test_cli_import_verify_and_receipt_do_not_leak(fixture: Fixture) -> None:
    development, holdout = bundles()
    development = (OpaqueTask(SENTINEL_ID, SENTINEL_BODY), *development[1:])
    dev_dir = fixture.root / "bundle-dev"
    hold_dir = fixture.root / "bundle-hold"
    write_bundle(dev_dir, development)
    write_bundle(hold_dir, holdout)
    digest = controller_source_digest()
    phrase = approval_phrase(fixture.commit, digest)
    blocked = run_controller(
        "import-bundles",
        "--repo",
        str(fixture.repo),
        "--commit",
        fixture.commit,
        "--controller-digest",
        digest,
        "--development",
        str(dev_dir),
        "--holdout",
        str(hold_dir),
        "--output",
        str(fixture.output),
        "--approve",
        "not-the-phrase",
    )
    assert blocked.returncode == 1
    assert blocked.stdout == ""
    assert blocked.stderr == "BLOCKED: approval-mismatch\n"
    assert not fixture.output.exists()
    imported = run_controller(
        "import-bundles",
        "--repo",
        str(fixture.repo),
        "--commit",
        fixture.commit,
        "--controller-digest",
        digest,
        "--development",
        str(dev_dir),
        "--holdout",
        str(hold_dir),
        "--output",
        str(fixture.output),
        "--approve",
        phrase,
    )
    assert imported.returncode == 0
    assert imported.stderr == ""
    verified = run_controller(
        "verify",
        "--repo",
        str(fixture.repo),
        "--commit",
        fixture.commit,
        "--output",
        str(fixture.output),
    )
    reported = run_controller(
        "receipt",
        "--repo",
        str(fixture.repo),
        "--commit",
        fixture.commit,
        "--output",
        str(fixture.output),
    )
    secrets = [
        SENTINEL_BODY.decode("utf-8"),
        SENTINEL_ID,
        "h00placeholder",
        str(fixture.output),
        str(dev_dir),
        str(hold_dir),
        "index.json",
        "blobs",
    ]
    for proc in (blocked, imported, verified, reported):
        for secret in secrets:
            assert secret not in proc.stdout
            assert secret not in proc.stderr
    payload = json.loads(imported.stdout)
    assert payload["status"] == "pass"
    assert payload["development_task_count"] == 20
    assert payload["holdout_task_count"] == 20
    assert payload["controller_commit"] == fixture.commit
    assert payload["controller_digest"] == digest
    assert json.loads(verified.stdout)["operation"] == "verify"
    assert json.loads(reported.stdout)["operation"] == "receipt"
    public = (fixture.output / "manifest.json").read_text(encoding="utf-8")
    assert SENTINEL_ID not in public
    assert "ARTIFICIAL-BODY-SENTINEL" not in public


@pytest.mark.parametrize(
    ("dev_count", "hold_count"),
    [(19, 20), (21, 20), (20, 19), (20, 21)],
)
def test_wrong_task_count_leaves_no_manifest(
    fixture: Fixture, dev_count: int, hold_count: int
) -> None:
    development, holdout = bundles(dev_count, hold_count)
    expect_blocked(fixture, "task-count", development=development, holdout=holdout)
    assert not fixture.output.exists()


def test_duplicate_ids_leave_no_manifest(fixture: Fixture) -> None:
    development, holdout = bundles()
    development = (
        development[0],
        OpaqueTask(development[0].task_id, b"artificial-dev-extra\n"),
        *development[2:],
    )
    expect_blocked(fixture, "duplicate-id", development=development, holdout=holdout)
    assert not fixture.output.exists()


def test_duplicate_content_within_stage_leaves_no_manifest(fixture: Fixture) -> None:
    development, holdout = bundles()
    development = (
        development[0],
        OpaqueTask(development[1].task_id, development[0].content),
        *development[2:],
    )
    expect_blocked(fixture, "duplicate-content", development=development, holdout=holdout)
    holdout = (
        holdout[0],
        OpaqueTask(holdout[1].task_id, holdout[0].content),
        *holdout[2:],
    )
    expect_blocked(fixture, "duplicate-content", development=bundles()[0], holdout=holdout)
    assert not fixture.output.exists()


def test_development_content_in_holdout_leaves_no_manifest(fixture: Fixture) -> None:
    development, holdout = bundles()
    holdout = (OpaqueTask(holdout[0].task_id, development[0].content), *holdout[1:])
    expect_blocked(fixture, "stage-separation", development=development, holdout=holdout)
    assert not fixture.output.exists()


def test_duplicate_ids_across_stages_leave_no_manifest(fixture: Fixture) -> None:
    development, holdout = bundles()
    holdout = (
        OpaqueTask(development[0].task_id, b"artificial-hold-other\n"),
        *holdout[1:],
    )
    expect_blocked(fixture, "stage-separation", development=development, holdout=holdout)
    assert not fixture.output.exists()


def test_relative_and_repository_paths_are_rejected(fixture: Fixture) -> None:
    relative = "sealed-relative-output"
    try:
        expect_blocked(fixture, "relative-path", output=relative)
        assert not Path(relative).exists()
    finally:
        leftover = Path(relative)
        if leftover.exists():
            shutil.rmtree(leftover)
    inside = fixture.repo / "inside-output"
    expect_blocked(fixture, "repository-path", output=inside)
    assert not inside.exists()
    other = fixture.root / "other-repo"
    init_fixture_repo(other)
    nested = other / "nested-output"
    expect_blocked(fixture, "repository-path", output=nested)
    assert not nested.exists()
    forbidden = repository_root() / "sealed-set-must-not-exist"
    assert not forbidden.exists()
    try:
        expect_blocked(fixture, "repository-path", output=forbidden)
        assert not forbidden.exists()
    finally:
        if forbidden.exists():
            shutil.rmtree(forbidden)


def test_symlink_escape_is_rejected(fixture: Fixture) -> None:
    target = fixture.root / "link-target"
    target.mkdir(mode=0o700)
    link = fixture.root / "link-output"
    link.symlink_to(target, target_is_directory=True)
    expect_blocked(fixture, "symlink-escape", output=link)
    assert not (target / "manifest.json").exists()
    development, _holdout = bundles()
    directory = fixture.root / "bundle-with-link"
    write_bundle(directory, development[:19])
    sentinel = fixture.root / "sentinel-target"
    sentinel.write_bytes(SENTINEL_BODY)
    (directory / "d19placeholder").symlink_to(sentinel)
    with pytest.raises(CustodyError) as caught:
        load_bundle_directory(directory, repo=fixture.repo)
    assert caught.value.reason == "symlink-escape"
    assert "ARTIFICIAL-BODY-SENTINEL" not in str(caught.value)


def test_nonempty_and_permissive_destinations_are_rejected(fixture: Fixture) -> None:
    permissive = fixture.root / "permissive-output"
    permissive.mkdir(mode=0o755)
    os.chmod(permissive, 0o755)  # noqa: S103
    expect_blocked(fixture, "permissive-mode", output=permissive)
    assert list(permissive.iterdir()) == []
    occupied = fixture.root / "occupied-output"
    occupied.mkdir(mode=0o700)
    os.chmod(occupied, 0o700)
    marker = occupied / "marker"
    marker.write_bytes(b"keep\n")
    os.chmod(marker, 0o600)
    expect_blocked(fixture, "destination-nonempty", output=occupied)
    assert marker.read_bytes() == b"keep\n"
    assert not (occupied / "manifest.json").exists()
    loose = fixture.root / "loose-bundle"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)  # noqa: S103
    with pytest.raises(CustodyError) as caught:
        load_bundle_directory(loose, repo=fixture.repo)
    assert caught.value.reason == "permissive-mode"
    files = fixture.root / "loose-files"
    write_bundle(files, bundles()[0])
    os.chmod(next(files.iterdir()), 0o644)
    with pytest.raises(CustodyError) as mode_error:
        load_bundle_directory(files, repo=fixture.repo)
    assert mode_error.value.reason == "permissive-mode"


def test_dirty_tree_wrong_commit_and_wrong_digest(fixture: Fixture) -> None:
    (fixture.repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    expect_blocked(fixture, "dirty-checkout")
    assert not fixture.output.exists()
    (fixture.repo / "untracked.txt").unlink()
    other = "0123456789abcdef0123456789abcdef01234567"
    assert other != fixture.commit
    expect_blocked(fixture, "commit-mismatch", commit=other)
    assert not fixture.output.exists()
    bad = "sha256:" + ("ab" * 32)
    assert bad != controller_source_digest()
    expect_blocked(fixture, "controller-digest-mismatch", controller_digest=bad)
    assert not fixture.output.exists()


def test_malformed_and_content_bearing_manifests_fail(fixture: Fixture) -> None:
    attempt(fixture)
    manifest_path = fixture.output / "manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    replacements = {
        "accepted_answers": "artificial-answer",
        "scenario_name": "artificial-scenario",
        "filename": "artificial-private-name",
        "task_body": "artificial-body",
    }
    for key, value in replacements.items():
        tainted = dict(document)
        tainted[key] = value
        with pytest.raises(CustodyError) as caught:
            validate_public_manifest(tainted)
        assert caught.value.reason == "manifest-invalid"
        assert value not in str(caught.value)
        assert caught.value.__cause__ is None
    encrypted = dict(document)
    encrypted["integrity_statement"] = "This set is encrypted and cryptographically sealed."
    with pytest.raises(CustodyError) as statement:
        validate_public_manifest(encrypted)
    assert statement.value.reason == "manifest-invalid"
    broken = dict(document)
    broken["custody_manifest_digest"] = "sha256:" + ("cd" * 32)
    with pytest.raises(CustodyError) as digest_error:
        validate_public_manifest(broken)
    assert digest_error.value.reason == "manifest-invalid"
    with pytest.raises(CustodyError):
        validate_public_manifest([])
    manifest_path.write_text('{"accepted_answers":"artificial-answer"}\n', encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    with pytest.raises(CustodyError) as on_disk:
        verify_custody(fixture.output, repo=fixture.repo, expected_commit=fixture.commit)
    assert on_disk.value.reason == "manifest-invalid"
    assert "artificial-answer" not in str(on_disk.value)
    assert_no_valid_finalized(fixture.output)


def test_interrupted_writes_leave_no_manifest(fixture: Fixture) -> None:
    seen: list[str] = []

    def checkpoint(name: str) -> None:
        seen.append(name)
        assert name == "before-manifest"
        assert not (fixture.output / "manifest.json").exists()
        assert (fixture.output / "private").is_dir()
        raise CustodyError("interrupted")

    expect_blocked(fixture, "interrupted", checkpoint=checkpoint)
    assert seen == ["before-manifest"]
    assert not fixture.output.exists()

    def explode(_name: str) -> None:
        raise RuntimeError("artificial-interrupt-sentinel")

    with pytest.raises(CustodyError) as caught:
        attempt(fixture, checkpoint=explode)
    assert caught.value.reason == "interrupted"
    assert caught.value.__cause__ is None
    assert "artificial-interrupt-sentinel" not in str(caught.value)
    assert not fixture.output.exists()


def test_tamper_after_finalization_is_rejected(fixture: Fixture) -> None:
    receipt = attempt(fixture)
    index = json.loads((fixture.output / "private" / "index.json").read_text(encoding="utf-8"))
    digest = index["development"][0]["content_digest"]
    blob = fixture.output / "private" / "blobs" / digest.removeprefix("sha256:")
    blob.write_bytes(b"tampered-artificial\n")
    os.chmod(blob, 0o600)
    with pytest.raises(CustodyError) as blob_error:
        verify_custody(fixture.output, repo=fixture.repo, expected_commit=fixture.commit)
    assert blob_error.value.reason == "tamper"
    fresh = fixture.root / "fresh-output"
    attempt(fixture, output=fresh)
    receipt_path = fresh / "receipt.json"
    stored = json.loads(receipt_path.read_text(encoding="utf-8"))
    stored["status"] = "fail"
    receipt_path.write_text(json.dumps(stored) + "\n", encoding="utf-8")
    os.chmod(receipt_path, 0o600)
    with pytest.raises(CustodyError) as receipt_error:
        verify_custody(fresh, repo=fixture.repo, expected_commit=fixture.commit)
    assert receipt_error.value.reason == "tamper"
    permissive = fixture.root / "mode-output"
    attempt(fixture, output=permissive)
    manifest_path = permissive / "manifest.json"
    os.chmod(manifest_path, 0o644)
    with pytest.raises(CustodyError) as mode_error:
        verify_custody(permissive, repo=fixture.repo, expected_commit=fixture.commit)
    assert mode_error.value.reason == "permissive-mode"
    assert receipt.custody_manifest_digest != ""


def test_rerun_against_finalized_set_does_not_replace_it(fixture: Fixture) -> None:
    receipt = attempt(fixture)
    before = (fixture.output / "manifest.json").read_bytes()
    with pytest.raises(CustodyError) as caught:
        attempt(fixture)
    assert caught.value.reason == "destination-nonempty"
    assert (fixture.output / "manifest.json").read_bytes() == before
    verified = verify_custody(fixture.output, repo=fixture.repo, expected_commit=fixture.commit)
    assert verified.custody_manifest_digest == receipt.custody_manifest_digest
    assert verified.status == "pass"


def test_directory_task_count_is_rejected(fixture: Fixture) -> None:
    short = fixture.root / "short-bundle"
    write_bundle(short, stage_tasks("d", "dev", 19))
    with pytest.raises(CustodyError) as caught:
        load_bundle_directory(short, repo=fixture.repo)
    assert caught.value.reason == "task-count"
    long = fixture.root / "long-bundle"
    write_bundle(long, stage_tasks("d", "dev", 21))
    with pytest.raises(CustodyError) as long_error:
        load_bundle_directory(long, repo=fixture.repo)
    assert long_error.value.reason == "task-count"
