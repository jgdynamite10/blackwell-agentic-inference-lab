"""Static validation of the Akamai Terraform and bootstrap material.

Text-level checks always run; terraform fmt/validate run only when the
binary is present (and validate only when providers are already initialized,
so the suite never touches the network).
"""

from __future__ import annotations

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

    def test_exactly_one_resource_a_single_gpu_instance(self):
        main_tf = read("main.tf")
        assert main_tf.count('resource "') == 1
        assert 'resource "linode_instance" "gpu_baseline"' in main_tf

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
        ipv4_block = outputs.split('output "instance_ipv4"')[1]
        assert "sensitive   = true" in ipv4_block


class TestTerraformBinaryChecks:
    @pytest.fixture()
    def terraform(self):
        binary = shutil.which("terraform")
        if binary is None:
            pytest.skip("terraform binary not installed")
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

    def test_validate_passes_when_initialized(self, terraform):
        if not (INFRA_DIR / ".terraform").is_dir():
            pytest.skip("providers not initialized (validate would need the network)")
        completed = subprocess.run(
            [terraform, "validate", "-no-color"],
            cwd=INFRA_DIR,
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
        assert scripts, "no bootstrap scripts found"
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
            'MODEL_DIGEST_MANIFEST="/tmp/x.sha256"\nMIN_DRIVER_BRANCH="580"\n'
            'REQUIRED_CUDA_MAJOR="13"\nSERVED_MODEL_NAME="m"\nSERVING_PORT="8000"\n'
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

    def test_watchdog_documents_that_it_is_not_a_billing_control(self):
        watchdog = (BOOTSTRAP_DIR / "watchdog.sh").read_text(encoding="utf-8")
        assert "NOT A BILLING CONTROL" in watchdog
        env_example = (BOOTSTRAP_DIR / "bootstrap.env.example").read_text(encoding="utf-8")
        assert "STILL" in env_example and "BILLS" in env_example

    def test_pins_template_declares_candidate_versions(self):
        env_example = (BOOTSTRAP_DIR / "bootstrap.env.example").read_text(encoding="utf-8")
        assert "vllm-openai:v0.28.0" in env_example
        assert "NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16" in env_example
        assert 'MIN_DRIVER_BRANCH="580"' in env_example
        assert 'REQUIRED_CUDA_MAJOR="13"' in env_example
        # Digest fields exist but are deliberately unfrozen until the pilot.
        assert 'VLLM_IMAGE_DIGEST=""' in env_example
