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
        assert 'required_version = ">= 1.9.0, < 2.0.0"' in versions
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
        # Multi-GPU plans are rejected; the SSH key must be a public key.
        assert "x2|x4|x8" in variables
        assert "ssh-ed25519" in variables

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
            [terraform, "init", "-backend=false", "-input=false"],
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
            'VLLM_IMAGE=""\nMODEL_ARTIFACT="x"\nMODEL_DIR="/tmp/x"\n'
            'MODEL_DIGEST_MANIFEST="/tmp/x.sha256"\nNVIDIA_DRIVER_PACKAGE="d"\n'
            'MIN_DRIVER_BRANCH="580"\nDRIVER_MAX_CUDA_MAJOR="13"\n'
            'GPU_PROBE_IMAGE="ubuntu"\nSERVED_MODEL_NAME="m"\nSERVING_PORT="8000"\n'
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
        assert "required pin VLLM_IMAGE is empty" in completed.stdout + completed.stderr

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
        assert 'docker run --rm --gpus all "ubuntu:24.04"' not in script
        # Serving binds to loopback only.
        assert "127.0.0.1:${SERVING_PORT}:8000" in script

    def test_watchdog_documents_that_it_is_not_a_billing_control(self):
        watchdog = (BOOTSTRAP_DIR / "watchdog.sh").read_text(encoding="utf-8")
        assert "NOT A BILLING CONTROL" in watchdog
        env_example = (BOOTSTRAP_DIR / "bootstrap.env.example").read_text(encoding="utf-8")
        assert "STILL" in env_example and "BILLS" in env_example

    def test_pins_template_declares_candidate_versions(self):
        env_example = (BOOTSTRAP_DIR / "bootstrap.env.example").read_text(encoding="utf-8")
        assert "vllm-openai:v0.28.0" in env_example
        assert "UNVALIDATED CANDIDATE" in env_example
        assert "v0.27.1" in env_example  # the model card's documented recipe
        assert "NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16" in env_example
        assert 'MIN_DRIVER_BRANCH="580"' in env_example
        assert 'DRIVER_MAX_CUDA_MAJOR="13"' in env_example
        assert "REQUIRED_CONTAINER_CUDA_VERSION=" in env_example
        assert "GPU_PROBE_IMAGE_DIGEST=" in env_example
        assert "NVIDIA_DRIVER_PACKAGE=" in env_example
        # Digest fields exist but are deliberately unfrozen until the pilot.
        assert 'VLLM_IMAGE_DIGEST=""' in env_example
