"""Static validation of the Akamai Terraform and bootstrap material.

Text-level checks always run. terraform fmt / init -backend=false / validate
run whenever the binary is present; when ``BWLAB_REQUIRE_TERRAFORM`` is set
(the dedicated CI job sets it), a missing binary is a FAILURE, never a skip —
Terraform validation must not silently disappear from CI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

INFRA_DIR = Path(__file__).resolve().parents[1] / "infra" / "akamai"
BOOTSTRAP_DIR = INFRA_DIR / "bootstrap"


def read(name: str) -> str:
    return (INFRA_DIR / name).read_text(encoding="utf-8")


class TestStaticConfiguration:
    def test_required_files_exist(self):
        for name in ("versions.tf", "variables.tf", "main.tf", "outputs.tf", ".gitignore"):
            assert (INFRA_DIR / name).is_file(), name

    def test_provider_and_terraform_versions_are_pinned(self):
        versions = read("versions.tf")
        assert 'version = "4.1.0"' in versions  # exact provider pin
        assert 'required_version = "= 1.9.8"' in versions
        assert "-platform=darwin_arm64" in versions
        assert "-platform=linux_amd64" in versions
        assert "-lockfile=readonly" in versions
        lock = read(".terraform.lock.hcl")
        assert "linode/linode" in lock and 'version     = "4.1.0"' in lock

    def test_local_backend_is_declared_for_external_state(self):
        # The lifecycle wrapper configures the external state path at init;
        # the backend block must exist for -backend-config to apply.
        assert 'backend "local" {}' in read("versions.tf")

    def test_no_credentials_or_account_data_in_configuration(self):
        for name in ("versions.tf", "variables.tf", "main.tf", "outputs.tf"):
            for line in read(name).splitlines():
                code = line.split("#", 1)[0]
                # No token is ever assigned in configuration; the provider
                # reads LINODE_TOKEN from the environment only.
                assert "token" not in code.lower().replace("authorized", ""), (name, line)
        # The SSH key variable is sensitive and carries no default value.
        key_block = (
            read("variables.tf").split('variable "authorized_ssh_key"')[1].split("variable ")[0]
        )
        assert "sensitive   = true" in key_block
        assert "default" not in key_block

    def test_state_and_tfvars_are_excluded_from_git(self):
        gitignore = read(".gitignore")
        for pattern in ("*.tfstate", "*.tfvars", ".terraform/", "*.tfplan"):
            assert pattern in gitignore, pattern

    def test_exactly_one_gpu_instance_plus_its_firewall(self):
        main_tf = read("main.tf")
        assert main_tf.count('resource "') == 2
        assert 'resource "linode_instance" "gpu_baseline"' in main_tf
        assert 'resource "linode_firewall" "gpu_baseline"' in main_tf

    def test_firewall_default_drop_ssh_only_from_management_cidr(self):
        main_tf = read("main.tf")
        firewall = main_tf.split('resource "linode_firewall"')[1]
        assert 'inbound_policy  = "DROP"' in firewall
        assert 'ports    = "22"' in firewall
        assert "var.management_cidr" in firewall
        # No serving port is ever exposed through the firewall.
        assert "8000" not in firewall
        # The firewall is attached to the exact instance and carries the tags.
        assert "linode_instance.gpu_baseline.id" in firewall
        assert "local.tags" in firewall

    def test_management_cidr_rejects_zero_prefix(self):
        variables = read("variables.tf")
        cidr_block = variables.split('variable "management_cidr"')[1].split("variable ")[0]
        assert "0.0.0.0/0" in cidr_block  # documented rejection
        assert "::/0" in cidr_block
        # Prefix regex excludes /0 entirely.
        assert "[1-9]|[12][0-9]|3[0-2]" in cidr_block

    def test_run_and_project_tags_are_applied(self):
        main_tf = read("main.tf")
        assert '"blackwell-lab"' in read("main.tf") or "blackwell-lab" in main_tf
        assert "run:${var.run_tag}" in main_tf
        assert "ttl-hours:${var.ttl_hours}" in main_tf

    def test_variables_carry_safe_validation(self):
        variables = read("variables.tf")
        # Every variable declares a validation block.
        assert variables.count("variable ") == variables.count("validation {")
        assert "ssh-ed25519" in variables

    def test_variables_lock_authorized_pilot_identity(self):
        variables = read("variables.tf")
        assert 'var.region == "us-sea"' in variables
        assert "region must equal us-sea" in variables
        assert 'var.gpu_instance_type == "g3-gpu-rtxpro6000-blackwell-1"' in variables
        assert "gpu_instance_type must equal g3-gpu-rtxpro6000-blackwell-1" in variables
        assert "var.ttl_hours == 6" in variables
        assert "ttl_hours must equal 6" in variables

    def test_ip_output_is_sensitive(self):
        outputs = read("outputs.tf")
        ipv4_block = outputs.split('output "instance_ipv4"')[1].split("output ")[0]
        assert "sensitive   = true" in ipv4_block

    def test_firewall_outputs_feed_the_ledger(self):
        outputs = read("outputs.tf")
        assert 'output "firewall_id"' in outputs
        assert 'output "firewall_label"' in outputs


class TestTerraformBinaryChecks:
    @pytest.fixture()
    def terraform(self):
        binary = shutil.which("terraform")
        if binary is None:
            if os.environ.get("BWLAB_REQUIRE_TERRAFORM"):
                pytest.fail(
                    "terraform binary is REQUIRED (BWLAB_REQUIRE_TERRAFORM is "
                    "set); validation must not silently disappear"
                )
            pytest.skip("terraform binary not installed (CI runs the required job)")
        return binary

    def test_fmt_check_passes(self, terraform):
        completed = subprocess.run(
            [terraform, "fmt", "-check", "-recursive"],
            cwd=INFRA_DIR,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

    def test_init_backend_false_and_validate_pass(self, terraform, tmp_path):
        env = {**os.environ, "TF_DATA_DIR": str(tmp_path / "tf-data"), "TF_IN_AUTOMATION": "1"}
        init = subprocess.run(
            [terraform, "init", "-backend=false", "-input=false", "-lockfile=readonly"],
            cwd=INFRA_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if init.returncode != 0:
            if os.environ.get("BWLAB_REQUIRE_TERRAFORM"):
                pytest.fail(f"terraform init -backend=false failed: {init.stderr}")
            pytest.skip("terraform init could not download providers (offline)")
        completed = subprocess.run(
            [terraform, "validate", "-no-color"],
            cwd=INFRA_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

    def test_repeated_readonly_init_does_not_modify_committed_lockfile(self, terraform, tmp_path):
        lock = INFRA_DIR / ".terraform.lock.hcl"
        before = lock.read_bytes()
        for index in range(2):
            env = {
                **os.environ,
                "TF_DATA_DIR": str(tmp_path / f"tf-data-{index}"),
                "TF_IN_AUTOMATION": "1",
            }
            init = subprocess.run(
                [terraform, "init", "-backend=false", "-input=false", "-lockfile=readonly"],
                cwd=INFRA_DIR,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            if init.returncode != 0:
                if os.environ.get("BWLAB_REQUIRE_TERRAFORM"):
                    pytest.fail(f"terraform init -lockfile=readonly failed: {init.stderr}")
                pytest.skip("terraform init could not download providers (offline)")
        assert lock.read_bytes() == before

    def test_committed_lockfile_covers_both_supported_platforms(self):
        lock = read(".terraform.lock.hcl")
        assert 'version     = "4.1.0"' in lock
        assert 'constraints = "4.1.0"' in lock
        h1_hashes = [line.strip() for line in lock.splitlines() if '"h1:' in line]
        assert len(h1_hashes) >= 2, "darwin_arm64 and linux_amd64 each contribute an h1 checksum"
        zh_hashes = [line.strip() for line in lock.splitlines() if '"zh:' in line]
        assert len(zh_hashes) >= 12


class TestBootstrapScripts:
    @pytest.fixture()
    def bash(self):
        binary = shutil.which("bash")
        if binary is None:
            pytest.skip("bash not available")
        return binary

    def test_scripts_pass_bash_syntax_check(self, bash):
        scripts = sorted(BOOTSTRAP_DIR.glob("*.sh"))
        names = {s.name for s in scripts}
        assert {"bootstrap.sh", "fetch-model.sh", "watchdog.sh"} <= names
        for script in scripts:
            completed = subprocess.run(
                [bash, "-n", str(script)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            assert completed.returncode == 0, f"{script.name}: {completed.stderr}"

    def test_bootstrap_refuses_missing_env_file_before_any_action(self, bash, tmp_path):
        staged = tmp_path / "bootstrap.sh"
        staged.write_bytes((BOOTSTRAP_DIR / "bootstrap.sh").read_bytes())
        completed = subprocess.run(
            [bash, str(staged)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 1
        assert "bootstrap.env not found" in completed.stdout + completed.stderr

    def test_bootstrap_refuses_unset_pins_before_any_action(self, bash, tmp_path):
        staged = tmp_path / "bootstrap.sh"
        staged.write_bytes((BOOTSTRAP_DIR / "bootstrap.sh").read_bytes())
        # An env file with a deliberately empty required pin.
        (tmp_path / "bootstrap.env").write_text(
            'VLLM_IMAGE="img"\nMODEL_ARTIFACT="x"\nMODEL_DIR="/tmp/x"\n'
            'MODEL_DIGEST_MANIFEST="/tmp/x.sha256"\nNVIDIA_DRIVER_PACKAGE="d"\n'
            'NVIDIA_DRIVER_PACKAGE_VERSION=""\nNVIDIA_CTK_PACKAGE_VERSION="1.0"\n'
            'NVIDIA_REPO_KEY_URL="https://example/key"\nNVIDIA_REPO_LIST="deb ..."\n'
            'DOCKER_PACKAGE="docker.io"\nDOCKER_PACKAGE_VERSION=""\n'
            'MIN_DRIVER_BRANCH="580"\nDRIVER_MAX_CUDA_MAJOR="13"\n'
            'GPU_PROBE_IMAGE="nvcr.io/nvidia/cuda:13.0.0-base-ubuntu24.04"\n'
            'GPU_PROBE_EXPECTED_GPU="RTX PRO 6000"\n'
            'SERVED_MODEL_NAME="m"\nSERVING_PORT="8000"\n'
            'WATCHDOG_IDLE_MINUTES="45"\n',
            encoding="utf-8",
        )
        completed = subprocess.run(
            [bash, str(staged)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 1
        assert "required pin NVIDIA_DRIVER_PACKAGE_VERSION is empty" in (
            completed.stdout + completed.stderr
        )

    def test_fetch_model_refuses_token_arguments(self, bash, tmp_path):
        staged = tmp_path / "fetch-model.sh"
        staged.write_bytes((BOOTSTRAP_DIR / "fetch-model.sh").read_bytes())
        completed = subprocess.run(
            [bash, str(staged), "--token", "not-a-real-token"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 1
        assert "no arguments" in completed.stdout + completed.stderr

    def test_fetch_model_never_places_tokens_in_files_or_args(self):
        script = (BOOTSTRAP_DIR / "fetch-model.sh").read_text(encoding="utf-8")
        # The token is read from the environment by the CLI itself; the
        # script never interpolates it into a command line or a file.
        assert "--token" not in script
        assert "${HF_TOKEN}" not in script and "$HF_TOKEN" not in script
        assert "set +x" in script

    def test_bootstrap_documents_reproducible_gpu_stack_route(self):
        script = (BOOTSTRAP_DIR / "bootstrap.sh").read_text(encoding="utf-8")
        assert "install_gpu_stack" in script
        assert "REBOOT_REQUIRED_EXIT=2" in script
        assert "check_container_cuda" in script
        # Probe image is digest-pinned; a mutable tag is never used.
        assert "GPU_PROBE_IMAGE_DIGEST" in script
        assert "nvidia-smi --query-gpu=name" in script
        assert "GPU_PROBE_EXPECTED_GPU" in script
        assert 'docker run --rm --gpus all "${probe_ref}" true' not in script
        # Serving binds to loopback only.
        assert "127.0.0.1:${SERVING_PORT}:8000" in script

    def test_bootstrap_normalizes_gpu_probe_names_identically(self):
        script = (BOOTSTRAP_DIR / "bootstrap.sh").read_text(encoding="utf-8")
        assert "normalize_gpu_match_string" in script
        assert "gpu_probe_name_matches" in script
        assert "tr -d '[:space:]'" in script

    def test_gpu_probe_name_matching_accepts_rtx_pro_6000_and_rejects_others(self, bash, tmp_path):
        test_sh = tmp_path / "gpu_probe_match.sh"
        test_sh.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
normalize_gpu_match_string() {
  printf '%s' "$1" | tr -d '[:space:]'
}
gpu_probe_name_matches() {
  local observed="$1" expected="$2"
  case "$(normalize_gpu_match_string "${observed}")" in
    *"$(normalize_gpu_match_string "${expected}")"*) return 0 ;;
    *) return 1 ;;
  esac
}
gpu_probe_name_matches "NVIDIA RTX PRO 6000 Blackwell Server Edition" "RTX PRO 6000"
gpu_probe_name_matches "NVIDIA A100 PCIe" "RTX PRO 6000" && exit 1
exit 0
""",
            encoding="utf-8",
        )
        test_sh.chmod(0o755)
        completed = subprocess.run(
            [bash, str(test_sh)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

    def test_watchdog_documents_that_it_is_not_a_billing_control(self):
        watchdog = (BOOTSTRAP_DIR / "watchdog.sh").read_text(encoding="utf-8")
        assert "NOT A BILLING CONTROL" in watchdog
        env_example = (BOOTSTRAP_DIR / "bootstrap.env.example").read_text(encoding="utf-8")
        assert "STILL" in env_example and "BILLS" in env_example

    def test_pins_template_declares_verified_candidate_versions(self):
        from blackwell_lab.cloud.bootstrap_pins import (
            candidate_pins_are_valid,
            parse_env_assignments,
            validate_candidate_pins,
        )

        env_example = (BOOTSTRAP_DIR / "bootstrap.env.example").read_text(encoding="utf-8")
        assert validate_candidate_pins(env_example) == []
        assert candidate_pins_are_valid(env_example)
        pins = parse_env_assignments(env_example)
        assert pins["VLLM_IMAGE"] == "docker.io/vllm/vllm-openai:v0.27.1"
        assert pins["VLLM_IMAGE_DIGEST"].startswith("sha256:")
        assert pins["VLLM_IMAGE_INDEX_DIGEST"].startswith("sha256:")
        assert pins["VLLM_IMAGE_DIGEST"] != pins["VLLM_IMAGE_INDEX_DIGEST"]
        assert pins["MODEL_REVISION"] == "a9904d24bcc1d289a1950fa9d2b978c47cf903b9"
        assert pins["NVIDIA_DRIVER_PACKAGE"] == "nvidia-driver-580-server"
        assert pins["NVIDIA_DRIVER_PACKAGE_VERSION"] == "580.173.02-0ubuntu0.24.04.1"
        assert pins["NVIDIA_CTK_PACKAGE_VERSION"] == "1.20.0-1"
        assert pins["DOCKER_PACKAGE_VERSION"] == "29.1.3-0ubuntu3~24.04.2"
        assert pins["REQUIRED_CONTAINER_CUDA_VERSION"] == "13.0"
        assert "NOT been validated" in env_example
        assert "NVFP4" in env_example and "not part of this pilot" in env_example
        assert "HF_TOKEN" not in env_example
        assert "LINODE_TOKEN" not in env_example
        assert "LAB_RESULTS_DIR" not in env_example

    def test_pins_template_fails_closed_on_removed_or_floating_values(self):
        from blackwell_lab.cloud.bootstrap_pins import validate_candidate_pins

        env_example = (BOOTSTRAP_DIR / "bootstrap.env.example").read_text(encoding="utf-8")
        floating = env_example.replace(
            'NVIDIA_DRIVER_PACKAGE_VERSION="580.173.02-0ubuntu0.24.04.1"',
            'NVIDIA_DRIVER_PACKAGE_VERSION="latest"',
        )
        assert any("floating" in problem for problem in validate_candidate_pins(floating))
        emptied = env_example.replace(
            'NVIDIA_CTK_PACKAGE_VERSION="1.20.0-1"',
            'NVIDIA_CTK_PACKAGE_VERSION=""',
        )
        assert any("empty" in problem for problem in validate_candidate_pins(emptied))
        unpinned = env_example.replace(
            'VLLM_IMAGE_DIGEST="sha256:c2f3b1b964e47809b722b5e75b61b1e7b39a50f70388cf2bf2418f16a9f31da2"',
            'VLLM_IMAGE_DIGEST="sha256:deadbeef"',
        )
        assert any("immutable" in problem for problem in validate_candidate_pins(unpinned))
        latest_image = env_example.replace(
            'VLLM_IMAGE="docker.io/vllm/vllm-openai:v0.27.1"',
            'VLLM_IMAGE="docker.io/vllm/vllm-openai:latest"',
        )
        assert any("floating" in problem for problem in validate_candidate_pins(latest_image))
