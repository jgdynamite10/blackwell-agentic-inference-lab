#!/usr/bin/env python3
"""Local operator for the D-0045 Nebius kit.

``plan`` is allowed against the private tfvars file. ``apply`` and
``destroy`` refuse to start unless BWLAB_APPROVAL is the exact D-0045
phrase. Credentials stay in NEBIUS_IAM_TOKEN. This script never prints
that token, the private tfvars, or provider identifiers.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

RUN_TAG = "nebius-w11-20261015b"
APPROVAL_PHRASE = "I approve the Nebius W11 freeze replication for run nebius-w11-20261015b"
MODULE = Path(__file__).resolve().parents[1] / "infra" / "nebius"


def state_dir() -> Path:
    return Path.home() / ".local" / "share" / "blackwell-lab" / RUN_TAG


def gate(command: str, approval: str | None) -> str | None:
    """Return a refusal reason, or None when the command may run."""
    if command not in {"plan", "apply", "destroy"}:
        return "usage: nebius_w11_operator.py plan|apply|destroy"
    if command == "plan":
        return None
    if approval != APPROVAL_PHRASE:
        return "BLOCKED: apply and destroy require the D-0045 approval phrase in BWLAB_APPROVAL"
    return None


def terraform_bin() -> str:
    override = os.environ.get("BWLAB_TERRAFORM")
    if override:
        return override
    found = shutil.which("terraform")
    if found:
        return found
    pinned = Path.home() / ".local/share/blackwell-lab/terraform-1.9.8/terraform"
    if pinned.is_file():
        return str(pinned)
    raise SystemExit("terraform 1.9.8 is not on PATH")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(gate("nope", None), file=sys.stderr)
        return 2
    command = argv[1]
    refusal = gate(command, os.environ.get("BWLAB_APPROVAL"))
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    if not os.environ.get("NEBIUS_IAM_TOKEN"):
        print("BLOCKED: NEBIUS_IAM_TOKEN is not set", file=sys.stderr)
        return 2
    private = state_dir()
    tfvars = private / "terraform.tfvars"
    if not tfvars.is_file():
        print("BLOCKED: private terraform.tfvars is missing", file=sys.stderr)
        return 2
    private.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_DATA_DIR"] = str(private / "tfdata")
    binary = terraform_bin()
    state = private / "terraform.tfstate"
    init = subprocess.run(
        [
            binary,
            f"-chdir={MODULE}",
            "init",
            "-input=false",
            "-lockfile=readonly",
            f"-backend-config=path={state}",
        ],
        env=env,
        check=False,
    )
    if init.returncode != 0:
        return init.returncode
    plan_path = private / f"{command}.tfplan"
    plan_args = [
        binary,
        f"-chdir={MODULE}",
        "plan",
        "-input=false",
        f"-var-file={tfvars}",
        f"-out={plan_path}",
    ]
    if command == "destroy":
        plan_args.insert(4, "-destroy")
    plan = subprocess.run(plan_args, env=env, check=False)
    if plan.returncode != 0 or command == "plan":
        return plan.returncode
    apply = subprocess.run(
        [binary, f"-chdir={MODULE}", "apply", "-input=false", str(plan_path)],
        env=env,
        check=False,
    )
    return apply.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
