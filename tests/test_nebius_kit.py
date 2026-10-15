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


def test_shape_is_one_rtx_pro_6000_in_us_central1():
    main = _text("main.tf")
    variables = _text("variables.tf")
    assert 'default     = "gpu-rtx6000"' in variables
    assert 'default     = "1gpu-24vcpu-218gb"' in variables
    assert 'default     = "us-central1"' in variables
    assert "ubuntu24.04-cuda13.0" in main
    assert "size_gibibytes" in main and "400" in main
    assert "recovery_policy" in main and '"FAIL"' in main
    assert "8gpu-192vcpu-1744gb" not in main
    assert "gpu-rtx6000-a" not in main


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
    assert "_SCHEDULE_SEED = 20261020" in source
    assert "_LIST_PRICE = 1.80" in source
    assert "qualify-agent" not in source.split('"""', 2)[2]


def test_operator_blocks_apply_without_the_phrase(monkeypatch):
    operator = _load_operator()
    monkeypatch.delenv("BWLAB_APPROVAL", raising=False)
    assert operator.main(["nebius_w11_operator.py", "apply"]) == 2
    assert operator.gate("plan", None) is None
    assert operator.gate("apply", operator.APPROVAL_PHRASE) is None


def test_operator_stops_before_terraform_when_the_token_is_missing(monkeypatch):
    operator = _load_operator()
    monkeypatch.setenv("BWLAB_APPROVAL", operator.APPROVAL_PHRASE)
    monkeypatch.delenv("NEBIUS_IAM_TOKEN", raising=False)
    assert operator.main(["nebius_w11_operator.py", "apply"]) == 2
