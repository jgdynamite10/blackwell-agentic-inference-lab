"""Offline checks for the D-0045 Nebius kit. No provider calls."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "infra" / "nebius"


def _load_operator():
    path = ROOT / "scripts" / "nebius_w11_operator.py"
    spec = importlib.util.spec_from_file_location("nebius_w11_operator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _text(name: str) -> str:
    return (MODULE / name).read_text(encoding="utf-8")


def test_shape_is_one_rtx_pro_6000_in_eu_south1():
    main = _text("main.tf")
    variables = _text("variables.tf")
    assert 'default     = "gpu-rtx6000-a"' in variables
    assert 'default     = "1gpu-24vcpu-218gb"' in variables
    assert 'default     = "eu-south1"' in variables
    assert 'default     = "nebius-nim-20261015a"' in variables
    assert "ubuntu24.04-cuda13.0" in main
    family = main.split("source_image_family", 1)[1].split("}", 1)[0]
    assert "parent_id" not in family
    assert "size_gibibytes" in main and "400" in main
    assert "recovery_policy" in main and '"FAIL"' in main
    assert "8gpu-192vcpu-1744gb" not in main
    assert "uk-south2" not in main
    assert 'default     = "gpu-rtx6000"\n' not in variables
    assert 'default     = "us-central1"' not in variables


def test_private_identifiers_are_not_committed():
    example = _text("terraform.tfvars.example")
    assert "REPLACE_PROJECT_ID" in example
    assert "REPLACE_NETWORK_ID" in example
    assert "REPLACE_SUBNET_ID" in example
    committed = "\n".join(path.read_text(encoding="utf-8") for path in MODULE.glob("*.tf"))
    assert "REPLACE_PROJECT_ID" not in committed
    assert "project-" not in committed


def test_manifest_schema_accepts_nebius():
    schema = json.loads((ROOT / "schemas" / "run-manifest.schema.json").read_text())
    providers = schema["properties"]["cloud"]["properties"]["provider"]["enum"]
    assert "nebius" in providers


def test_cell_matches_the_freeze_shape():
    source = (ROOT / "scripts" / "nebius_w11_full_cell.py").read_text(encoding="utf-8")
    assert 'provider="nebius"' in source
    assert "tasks_per_repetition=200" in source
    assert "warmup_passes=1" in source
    assert "_SCHEDULE_SEED = 20261024" in source
    assert '_RUN_LABEL = "nebius-w11-20261015b"' in source
    assert 'region="eu-south1"' in source
    assert "_LIST_PRICE = 1.80" in source
    assert "qualify-agent" not in source.split('"""', 2)[2]


def test_nim_cell_uses_the_adopted_bf16_identity():
    source = (ROOT / "scripts" / "nebius_nim_bf16_cell.py").read_text(encoding="utf-8")
    remote = (ROOT / "scripts" / "nebius_nim_bf16_remote.sh").read_text(encoding="utf-8")
    assert "_SCHEDULE_SEED = 20261027" in source
    assert '_RUN_LABEL = "nebius-nim-20261015a"' in source
    assert 'workload_version="2.13.0"' in source
    assert 'controller="workflow-controller-v3"' in source
    assert "qualify-agent" not in source.split('"""', 2)[2]
    assert "d74ba8ea77c6fa3a553400b56e1172846a3c073b0c57c2f24f3d48e71184fa39" in remote
    assert "4816da181f28c435aaa2efa0e2302309c28f2c08c9534070e89f67854a521d72" not in remote
    assert "127.0.0.1:8000:8000" in remote
    assert "-e NGC_API_KEY" in remote
    assert "-e NGC_API_KEY=" not in remote


def test_operator_blocks_apply_without_the_phrase(monkeypatch):
    operator = _load_operator()
    monkeypatch.delenv("BWLAB_APPROVAL", raising=False)
    assert operator.main(["nebius_w11_operator.py", "apply"]) == 2
    assert operator.gate("plan", None) is None
    assert operator.gate("apply", operator.APPROVAL_PHRASE) is None
    for old in (
        "I approve the Nebius W11 freeze replication for run nebius-w11-20261015a",
        "I approve the Nebius W11 freeze replication for run nebius-w11-20261015b",
    ):
        assert operator.gate("apply", old) is not None
        assert operator.gate("destroy", old) is not None


def test_operator_stops_before_terraform_when_the_token_is_missing(monkeypatch):
    operator = _load_operator()
    monkeypatch.setenv("BWLAB_APPROVAL", operator.APPROVAL_PHRASE)
    monkeypatch.delenv("NEBIUS_IAM_TOKEN", raising=False)
    assert operator.main(["nebius_w11_operator.py", "apply"]) == 2
