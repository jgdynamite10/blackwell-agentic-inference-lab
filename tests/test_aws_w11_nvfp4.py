"""Static pin for the D-0047 AWS NVFP4 cell."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_terraform_is_locked_to_the_nvfp4_run() -> None:
    variables = (ROOT / "infra/aws/variables.tf").read_text(encoding="utf-8")
    main = (ROOT / "infra/aws/main.tf").read_text(encoding="utf-8")
    assert 'var.run_tag == "aws-w11-nvfp4-20261015a"' in variables
    assert 'var.region == "us-east-1"' in variables
    assert 'var.instance_type == "g7e.4xlarge"' in variables
    assert 'contains(["us-east-1a", "us-east-1c"], var.availability_zone)' in variables
    assert "g7e.8xlarge" not in variables
    assert 'var.run_tag == "aws-diag-20261014a"' not in variables
    assert "aws-w11-nvfp4-20261015a" in main
    assert "g7e.4xlarge" in main
    assert 'phase   = "d-0047"' in main
    assert "delete_on_termination = true" in main
    assert 'http_tokens   = "required"' in main
    assert 'instance_initiated_shutdown_behavior = "terminate"' in main
    assert "ingress" not in main


def test_cell_matches_the_akamai_nvfp4_shape() -> None:
    cell = (ROOT / "scripts/aws_w11_nvfp4_cell.py").read_text(encoding="utf-8")
    remote = (ROOT / "scripts/aws_w11_nvfp4_remote.sh").read_text(encoding="utf-8")
    body = cell.split('"""', 2)[2]
    assert "_SCHEDULE_SEED = 20261023" in cell
    assert "seed=20260906" in cell
    assert "max_tokens=1024" in cell
    assert "reasoning_mode=True" in cell
    assert 'provider="aws"' in cell
    assert '"precision": "nvfp4"' in cell
    assert "bee7596271d1495f6992ae224aefde4410e816b8" in cell
    assert "_LIST_PRICE = 3.99816" in cell
    assert '_INSTANCE_TYPE = "g7e.4xlarge"' in cell
    assert "qualify-agent" not in body
    assert "bootstrap.nvfp4.env.example" in remote
    assert "verify_model_artifact" in remote
    assert "watchdog-disabled" in remote
    assert "bwlab-ttl" not in remote
    assert "aws_w11_nvfp4_cell.py" in remote


def test_nim_cell_waits_for_nvfp4_and_uses_the_adopted_bf16_identity() -> None:
    cell = (ROOT / "scripts/aws_nim_bf16_cell.py").read_text(encoding="utf-8")
    remote = (ROOT / "scripts/aws_nim_bf16_remote.sh").read_text(encoding="utf-8")
    body = cell.split('"""', 2)[2]
    assert "_SCHEDULE_SEED = 20261028" in cell
    assert "seed=20260906" in cell
    assert 'provider="aws"' in cell
    assert '"precision": "bf16"' in cell
    assert '_RUN_LABEL = "aws-nim-20261016a"' in cell
    assert "_LIST_PRICE = 3.99816" in cell
    assert '_INSTANCE_TYPE = "g7e.4xlarge"' in cell
    assert "qualify-agent" not in body
    assert "d74ba8ea77c6fa3a553400b56e1172846a3c073b0c57c2f24f3d48e71184fa39" in remote
    assert "nvfp4-result-missing" in remote
    assert "vllm-still-running" in remote
    assert "-e NGC_API_KEY" in remote
    assert "-e NGC_API_KEY=" not in remote
    assert "install -d -o root -g 0 -m 2770 /var/lib/bwlab/nim-cache" in remote
    assert "chmod 2770 /var/lib/bwlab/nim-cache" in remote
    assert "install -d -m 0700 /var/lib/bwlab/nim-cache" not in remote
    assert "nim-already-serving" in remote
    assert '"tool_choice": "auto"' in remote
    assert '"tool_choice": "required"' not in remote
    assert '"stream": True' in remote
    assert '"max_tokens": 1024' in remote
    assert remote.count('rm -f "${keyfile}"') == 1
    assert remote.index('rm -f "${keyfile}"') > remote.index('print("nim-tools-ok")')
    assert "nim-serve" in remote
    assert "--enable-auto-tool-choice" in remote
    assert "--tool-call-parser qwen3_coder" in remote
    assert "--reasoning-parser nemotron_v3" in remote
    assert "NIM_ENABLE_AUTO_TOOL_CHOICE" not in remote
    assert "results-export-nim.tar" in remote
