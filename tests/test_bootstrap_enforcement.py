"""Adversarial enforcement tests for the real bootstrap and fetch-model paths."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from blackwell_lab.cloud.bootstrap_pins import (
    APPROVED_DRIVER_PACKAGE,
    PROPRIETARY_DRIVER_PACKAGE,
    validate_candidate_pins,
)

BOOTSTRAP_DIR = Path(__file__).resolve().parents[1] / "infra" / "akamai" / "bootstrap"


@pytest.fixture()
def bash():
    binary = shutil.which("bash")
    if binary is None:
        pytest.skip("bash not available")
    return binary


def _stage_bootstrap(tmp_path: Path, env_text: str | None = None) -> Path:
    dest = tmp_path / "bootstrap"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("bootstrap.sh", "fetch-model.sh", "pins.sh", "bootstrap.env.example"):
        shutil.copy2(BOOTSTRAP_DIR / name, dest / name)
    example = (dest / "bootstrap.env.example").read_text(encoding="utf-8")
    (dest / "bootstrap.env").write_text(
        env_text if env_text is not None else example,
        encoding="utf-8",
    )
    return dest


_SHA256SUM_FALLBACK = r"""#!/usr/bin/env python3
import hashlib
import sys

args = sys.argv[1:]
check = "--check" in args
quiet = "--quiet" in args
status = "--status" in args
files = [arg for arg in args if not arg.startswith("-")]


def digest(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


if check:
    lines = open(files[0], encoding="utf-8").readlines() if files else sys.stdin.readlines()
    ok = True
    for raw in lines:
        line = raw.rstrip("\n")
        if not line:
            continue
        hexdigest, name = line.split(None, 1)
        if name.startswith("*"):
            name = name[1:]
        try:
            actual = digest(name)
        except OSError:
            ok = False
            if not status:
                print(f"{name}: FAILED open", file=sys.stderr)
            continue
        if actual != hexdigest:
            ok = False
            if not status and not quiet:
                print(f"{name}: FAILED")
        elif not status and not quiet:
            print(f"{name}: OK")
    raise SystemExit(0 if ok else 1)

targets = files or ["-"]
for path in targets:
    if path == "-":
        print(f"{hashlib.sha256(sys.stdin.buffer.read()).hexdigest()}  -")
    else:
        print(f"{digest(path)}  {path}")
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _ensure_sha256sum(bin_dir: Path) -> None:
    if shutil.which("sha256sum") is not None:
        return
    _write_exec(bin_dir / "sha256sum", _SHA256SUM_FALLBACK)


def _mutation_path(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "mut-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "mutations.log"
    for name in (
        "apt-get",
        "curl",
        "gpg",
        "dpkg",
        "dpkg-query",
        "docker",
        "systemctl",
        "systemd",
    ):
        _write_exec(
            bin_dir / name,
            f"#!/usr/bin/env bash\nprintf '%s %s\\n' '{name}' \"$*\" >> '{log}'\nexit 0\n",
        )
    return bin_dir, log


def _run(
    bash: str, script: Path, env: dict[str, str], timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bash, str(script)],
        cwd=script.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class TestDriverPackage:
    def test_example_accepts_open_and_python_rejects_proprietary(self):
        example = (BOOTSTRAP_DIR / "bootstrap.env.example").read_text(encoding="utf-8")
        assert APPROVED_DRIVER_PACKAGE in example
        assert f'NVIDIA_DRIVER_PACKAGE="{PROPRIETARY_DRIVER_PACKAGE}"' not in example
        assert validate_candidate_pins(example) == []
        proprietary = example.replace(
            f'NVIDIA_DRIVER_PACKAGE="{APPROVED_DRIVER_PACKAGE}"',
            f'NVIDIA_DRIVER_PACKAGE="{PROPRIETARY_DRIVER_PACKAGE}"',
        )
        problems = validate_candidate_pins(proprietary)
        assert any("proprietary" in problem for problem in problems)

    def test_bootstrap_rejects_proprietary_driver_before_mutation(self, bash, tmp_path):
        example = (BOOTSTRAP_DIR / "bootstrap.env.example").read_text(encoding="utf-8")
        dest = _stage_bootstrap(
            tmp_path,
            example.replace(
                f'NVIDIA_DRIVER_PACKAGE="{APPROVED_DRIVER_PACKAGE}"',
                f'NVIDIA_DRIVER_PACKAGE="{PROPRIETARY_DRIVER_PACKAGE}"',
            ),
        )
        bin_dir, log = _mutation_path(tmp_path)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
        completed = _run(bash, dest / "bootstrap.sh", env)
        assert completed.returncode == 1
        output = completed.stdout + completed.stderr
        assert "open kernel modules" in output or "proprietary" in output
        assert not log.exists()


class TestPinValidationStopsMutation:
    @pytest.mark.parametrize(
        ("old", "new", "needle"),
        [
            (
                'NVIDIA_DRIVER_PACKAGE_VERSION="580.173.02-0ubuntu0.24.04.1"',
                'NVIDIA_DRIVER_PACKAGE_VERSION=""',
                "empty",
            ),
            (
                'NVIDIA_CTK_PACKAGE_VERSION="1.20.0-1"',
                'NVIDIA_CTK_PACKAGE_VERSION="latest"',
                "floating",
            ),
            (
                'NVIDIA_CTK_PACKAGE_VERSION="1.20.0-1"',
                'NVIDIA_CTK_PACKAGE_VERSION="1.20.*"',
                "wildcard",
            ),
            (
                'NVIDIA_CTK_PACKAGE_VERSION="1.20.0-1"',
                'NVIDIA_CTK_PACKAGE_VERSION=">=1.20.0-1"',
                "range",
            ),
            (
                'VLLM_IMAGE_DIGEST="sha256:c2f3b1b964e47809b722b5e75b61b1e7b39a50f70388cf2bf2418f16a9f31da2"',
                'VLLM_IMAGE_DIGEST="sha256:deadbeef"',
                "digest",
            ),
            (
                'MODEL_REVISION="a9904d24bcc1d289a1950fa9d2b978c47cf903b9"',
                'MODEL_REVISION="not-a-revision"',
                "40-character",
            ),
            (
                'NVIDIA_DRIVER_PACKAGE="nvidia-driver-580-server-open"',
                'NVIDIA_DRIVER_PACKAGE="nvidia-driver-550"',
                "nvidia-driver-580-server-open",
            ),
            (
                'MIN_DRIVER_BRANCH="580"',
                'MIN_DRIVER_BRANCH="570"',
                "reviewed candidate baseline",
            ),
            (
                'REQUIRED_OS_ID="ubuntu"',
                'REQUIRED_OS_ID="debian"',
                "reviewed candidate baseline",
            ),
            (
                'REQUIRED_OS_VERSION="24.04"',
                'REQUIRED_OS_VERSION="22.04"',
                "reviewed candidate baseline",
            ),
            (
                'SERVED_MODEL_NAME="nemotron-3.5-lightning-30b-a3b-bf16"',
                'SERVED_MODEL_NAME="other-model"',
                "reviewed candidate baseline",
            ),
            (
                'SERVING_PORT="8000"',
                'SERVING_PORT="9000"',
                "reviewed candidate baseline",
            ),
            (
                'SERVING_PORT="8000"',
                'SERVING_PORT="70000"',
                "1-65535",
            ),
            (
                "--max-num-seqs 128 --enable-prefix-caching",
                "--max-num-seqs 1 --enable-prefix-caching",
                "reviewed candidate baseline",
            ),
            (
                'WATCHDOG_IDLE_MINUTES="45"',
                'WATCHDOG_IDLE_MINUTES="30"',
                "reviewed candidate baseline",
            ),
            (
                'WATCHDOG_IDLE_MINUTES="45"',
                'WATCHDOG_IDLE_MINUTES="60"',
                "no greater than 45",
            ),
        ],
    )
    def test_invalid_pins_do_not_call_mutators(self, bash, tmp_path, old, new, needle):
        example = (BOOTSTRAP_DIR / "bootstrap.env.example").read_text(encoding="utf-8")
        for script_name in ("bootstrap.sh", "fetch-model.sh"):
            dest = _stage_bootstrap(
                tmp_path / script_name.replace(".sh", ""),
                example.replace(old, new),
            )
            bin_dir, log = _mutation_path(tmp_path / f"{script_name}-mut")
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
            completed = _run(bash, dest / script_name, env)
            assert completed.returncode == 1
            assert needle in (completed.stdout + completed.stderr)
            assert not log.exists()


class TestInstalledVersionsAndMarkers:
    def test_wrong_preinstalled_version_is_not_silently_accepted(self, bash, tmp_path):
        dest = _stage_bootstrap(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "mutations.log"
        helper = tmp_path / "converge.sh"
        helper.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
export BWLAB_BOOTSTRAP_STATE_DIR="{tmp_path / "state"}"
export BWLAB_KEYRING_DIR="{tmp_path / "keyrings"}"
export BWLAB_APT_LIST_DIR="{tmp_path / "lists"}"
. "{dest / "bootstrap.sh"}"
load_and_validate_bootstrap_env "${{ENV_FILE}}" "${{EXAMPLE_FILE}}"
verify_installed_gpu_packages
""",
            encoding="utf-8",
        )
        helper.chmod(0o755)
        _write_exec(
            bin_dir / "dpkg-query",
            "#!/usr/bin/env bash\nprintf 'wrong-version\\n'\n",
        )
        _write_exec(
            bin_dir / "apt-get",
            f"#!/usr/bin/env bash\nprintf 'apt-get %s\\n' \"$*\" >> '{log}'\nexit 0\n",
        )
        _ensure_sha256sum(bin_dir)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
        completed = _run(bash, helper, env)
        assert completed.returncode == 1
        output = completed.stdout + completed.stderr
        assert "never accepted" in output or "differs from reviewed" in output
        assert log.exists()
        text = log.read_text(encoding="utf-8")
        assert "nvidia-driver-580-server-open=580.173.02-0ubuntu0.24.04.1" in text
        assert "latest" not in text

    def test_completion_marker_does_not_skip_version_verification(self, bash, tmp_path):
        dest = _stage_bootstrap(tmp_path)
        script = (dest / "bootstrap.sh").read_text(encoding="utf-8")
        assert "still verifying exact installed versions" in script
        assert "step_done gpu-stack-installed" in script
        assert "verify_installed_gpu_packages" in script
        assert "install_verified_nvidia_key_and_list" in script
        helper = tmp_path / "marker.sh"
        helper.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
export BWLAB_BOOTSTRAP_STATE_DIR="{tmp_path / "state"}"
export BWLAB_KEYRING_DIR="{tmp_path / "keyrings"}"
export BWLAB_APT_LIST_DIR="{tmp_path / "lists"}"
. "{dest / "bootstrap.sh"}"
load_and_validate_bootstrap_env "${{ENV_FILE}}" "${{EXAMPLE_FILE}}"
step_done gpu-stack-installed && echo "marker-would-have-skipped"
verify_installed_gpu_packages
""",
            encoding="utf-8",
        )
        helper.chmod(0o755)
        (tmp_path / "state").mkdir()
        (tmp_path / "state" / "gpu-stack-installed.done").write_text("", encoding="utf-8")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "mutations.log"
        _write_exec(bin_dir / "dpkg-query", "#!/usr/bin/env bash\nprintf 'wrong-version\\n'\n")
        _write_exec(
            bin_dir / "apt-get",
            f"#!/usr/bin/env bash\nprintf 'apt-get %s\\n' \"$*\" >> '{log}'\nexit 0\n",
        )
        _ensure_sha256sum(bin_dir)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
        completed = _run(bash, helper, env)
        assert completed.returncode == 1
        output = completed.stdout + completed.stderr
        assert "marker-would-have-skipped" in output
        assert "never accepted" in output or "differs from reviewed" in output


class TestRepositoryKey:
    def test_key_mismatch_performs_no_installation(self, bash, tmp_path):
        dest = _stage_bootstrap(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "mutations.log"
        keyring = tmp_path / "keyrings"
        lists = tmp_path / "lists"
        keyring.mkdir()
        lists.mkdir()
        _write_exec(
            bin_dir / "curl",
            "#!/usr/bin/env bash\n"
            "out=''\n"
            "while [ $# -gt 0 ]; do\n"
            '  case "$1" in -o) out="$2"; shift 2 ;; *) shift ;; esac\n'
            "done\n"
            "printf 'NOT-THE-OFFICIAL-KEY\\n' > \"${out}\"\n",
        )
        _write_exec(
            bin_dir / "gpg",
            f"#!/usr/bin/env bash\nprintf 'gpg %s\\n' \"$*\" >> '{log}'\nexit 0\n",
        )
        _write_exec(
            bin_dir / "apt-get",
            f"#!/usr/bin/env bash\nprintf 'apt-get %s\\n' \"$*\" >> '{log}'\nexit 0\n",
        )
        helper = tmp_path / "keycheck.sh"
        helper.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
export BWLAB_KEYRING_DIR="{keyring}"
export BWLAB_APT_LIST_DIR="{lists}"
. "{dest / "bootstrap.sh"}"
load_and_validate_bootstrap_env "${{ENV_FILE}}" "${{EXAMPLE_FILE}}"
install_verified_nvidia_key_and_list
""",
            encoding="utf-8",
        )
        helper.chmod(0o755)
        _ensure_sha256sum(bin_dir)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
        completed = _run(bash, helper, env)
        assert completed.returncode == 1
        assert "mismatch" in (completed.stdout + completed.stderr)
        assert list(keyring.iterdir()) == []
        assert list(lists.iterdir()) == []
        assert not log.exists()


class TestFetchModelAtomic:
    def _valid_env(self, dest: Path, model_dir: Path) -> None:
        example = (dest / "bootstrap.env.example").read_text(encoding="utf-8")
        (dest / "bootstrap.env").write_text(
            example.replace(
                'MODEL_DIR="/opt/models/nemotron-3.5-lightning-30b-a3b-bf16"',
                f'MODEL_DIR="{model_dir}"',
            ).replace(
                'MODEL_DIGEST_MANIFEST="/opt/models/nemotron-3.5-lightning-30b-a3b-bf16.sha256"',
                f'MODEL_DIGEST_MANIFEST="{model_dir}.sha256"',
            ),
            encoding="utf-8",
        )

    def test_partial_directory_is_not_certified(self, bash, tmp_path):
        dest = _stage_bootstrap(tmp_path)
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        (model_dir / "partial.bin").write_bytes(b"incomplete")
        self._valid_env(dest, model_dir)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_exec(
            bin_dir / "docker",
            "#!/usr/bin/env bash\nexit 1\n",
        )
        _ensure_sha256sum(bin_dir)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "BWLAB_ALLOW_PATH_OVERRIDE": "1",
        }
        completed = _run(bash, dest / "fetch-model.sh", env)
        assert completed.returncode == 1
        assert not Path(f"{model_dir}.sha256").exists()
        output = completed.stdout + completed.stderr
        assert "untrusted" in output or "failed" in output
        assert "not-a-real-token" not in output

    def test_interrupted_download_cannot_produce_manifest(self, bash, tmp_path):
        dest = _stage_bootstrap(tmp_path)
        model_dir = tmp_path / "model"
        self._valid_env(dest, model_dir)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_exec(
            bin_dir / "docker",
            "#!/usr/bin/env bash\n"
            'if [ "$1" = image ]; then exit 0; fi\n'
            'stage=""\n'
            'for arg in "$@"; do\n'
            '  case "$arg" in MODEL_STAGE_DIR=*) stage="${arg#MODEL_STAGE_DIR=}" ;; esac\n'
            "done\n"
            '[ -n "$stage" ] || exit 1\n'
            'mkdir -p "$stage"\n'
            "printf 'partial' > \"$stage/weights.bin\"\n"
            "exit 1\n",
        )
        _ensure_sha256sum(bin_dir)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "BWLAB_ALLOW_PATH_OVERRIDE": "1",
        }
        completed = _run(bash, dest / "fetch-model.sh", env)
        assert completed.returncode == 1
        assert not Path(f"{model_dir}.sha256").exists()
        assert not model_dir.is_dir()

    def test_resumed_successful_download_can_be_certified(self, bash, tmp_path):
        dest = _stage_bootstrap(tmp_path)
        model_dir = tmp_path / "model"
        revision = "a9904d24bcc1d289a1950fa9d2b978c47cf903b9"
        staging = Path(f"{model_dir}.rev-{revision}.staging")
        staging.mkdir(parents=True)
        (staging / "config.json").write_text('{"ok": true}\n', encoding="utf-8")
        self._valid_env(dest, model_dir)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_exec(
            bin_dir / "docker",
            "#!/usr/bin/env bash\n"
            'if [ "$1" = image ]; then exit 0; fi\n'
            'stage=""\n'
            'for arg in "$@"; do\n'
            '  case "$arg" in MODEL_STAGE_DIR=*) stage="${arg#MODEL_STAGE_DIR=}" ;; esac\n'
            "done\n"
            '[ -n "$stage" ] || exit 1\n'
            "printf 'ok\\n' > \"$stage/config.json\"\n"
            "printf 'weights\\n' > \"$stage/weights.bin\"\n"
            "exit 0\n",
        )
        _ensure_sha256sum(bin_dir)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HF_TOKEN": "not-a-real-token",
            "BWLAB_ALLOW_PATH_OVERRIDE": "1",
        }
        completed = _run(bash, dest / "fetch-model.sh", env)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        manifest = Path(f"{model_dir}.sha256")
        assert model_dir.is_dir()
        assert manifest.is_file()
        sha_bin = shutil.which(
            "sha256sum",
            path=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        )
        assert sha_bin is not None
        check = subprocess.run(
            [sha_bin, "--check", "--strict", str(manifest)],
            cwd=model_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        assert check.returncode == 0
        output = completed.stdout + completed.stderr
        assert "not-a-real-token" not in output
        assert "HF_TOKEN=" not in output

    def test_token_never_appears_in_script_or_arguments(self, bash, tmp_path):
        dest = _stage_bootstrap(tmp_path)
        model_dir = tmp_path / "model"
        self._valid_env(dest, model_dir)
        argv_log = tmp_path / "docker.argv"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_exec(
            bin_dir / "docker",
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> '{argv_log}'\n"
            'if [ "$1" = image ]; then exit 0; fi\n'
            "exit 1\n",
        )
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HF_TOKEN": "not-a-real-token",
            "BWLAB_ALLOW_PATH_OVERRIDE": "1",
        }
        completed = _run(bash, dest / "fetch-model.sh", env)
        output = completed.stdout + completed.stderr
        assert "not-a-real-token" not in output
        if argv_log.exists():
            assert "not-a-real-token" not in argv_log.read_text(encoding="utf-8")
        script = (dest / "fetch-model.sh").read_text(encoding="utf-8")
        assert "${HF_TOKEN}" not in script
        assert "$HF_TOKEN" not in script
        assert "--token" not in script


class TestFetchModelStatic:
    def test_does_not_require_host_huggingface_cli(self):
        script = (BOOTSTRAP_DIR / "fetch-model.sh").read_text(encoding="utf-8")
        assert "huggingface-cli" not in script
        assert "command -v hf " not in script
        assert "digest-pinned" in script
        assert "--entrypoint python3" in script
        assert "huggingface_hub" in script
        assert "staging" in script
