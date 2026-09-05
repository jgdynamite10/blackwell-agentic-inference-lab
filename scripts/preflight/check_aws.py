#!/usr/bin/env python3
"""Read-only AWS feasibility preflight for single-GPU EC2 G7e instances.

Intended to be run LOCALLY by the operator in their authenticated
environment. The hosted Cloud Agent must not run credential-dependent
preflight checks (AGENTS.md, section 3).

Safe by construction: only ``describe``/``get`` calls; no create, modify,
start, stop, or terminate operations (AGENTS.md forbids AWS mutation until
the AWS phase is authorized); never prints credentials or account
identifiers.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

CANDIDATE_TYPES = ["g7e.2xlarge", "g7e.4xlarge", "g7e.8xlarge"]
# EC2 "Running On-Demand G and VT instances" vCPU quota.
G_VT_QUOTA_CODE = "L-DB2E81BA"

READ_ONLY_BANNER = (
    "== AWS preflight — READ-ONLY ==\n"
    "This tool only runs aws describe/get commands. It cannot provision,\n"
    "modify, stop, or terminate resources, and it never prints secrets or\n"
    "account identifiers."
)


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "READ-ONLY AWS feasibility preflight. Reports which single-GPU G7e "
            "instance types are offered in the current region and the account's "
            "On-Demand G/VT vCPU service quota, using aws describe/get commands "
            "only. Performs no create/modify/stop/terminate operations and never "
            "prints secrets or account identifiers. Run locally; not from the "
            "hosted Cloud Agent."
        )
    )
    parser.parse_args(argv)
    print(READ_ONLY_BANNER)

    aws = shutil.which("aws")
    if not aws:
        print("BLOCKED: aws CLI is not installed.")
        print(
            "Missing capability: cannot verify G7e instance-type offerings, the "
            "G/VT-instance vCPU service quota, or Pricing API rates."
        )
        return 1

    code, _ = run([aws, "sts", "get-caller-identity", "--output", "json"])
    if code != 0:
        print("BLOCKED: aws CLI is installed but no working credentials were found.")
        print("Provide read-only credentials locally (see .env.example); never commit them.")
        return 1
    print("Working AWS credentials detected (account identity not printed).")

    code, out = run(
        [
            aws,
            "ec2",
            "describe-instance-type-offerings",
            "--location-type",
            "region",
            "--filters",
            f"Name=instance-type,Values={','.join(CANDIDATE_TYPES)}",
            "--output",
            "json",
        ]
    )
    if code == 0:
        offerings = json.loads(out).get("InstanceTypeOfferings", [])
        found = sorted({o["InstanceType"] for o in offerings})
        print(f"G7e single-GPU types offered in the current region: {found or 'none'}")
    else:
        print(f"describe-instance-type-offerings failed: {out}")

    code, out = run(
        [
            aws,
            "service-quotas",
            "get-service-quota",
            "--service-code",
            "ec2",
            "--quota-code",
            G_VT_QUOTA_CODE,
            "--output",
            "json",
        ]
    )
    if code == 0:
        quota = json.loads(out).get("Quota", {})
        print(
            f"On-Demand G/VT vCPU quota: {quota.get('Value')} "
            f"(g7e.4xlarge needs 16, g7e.8xlarge needs 32)"
        )
    else:
        print(f"service-quotas lookup failed (role may lack servicequotas:GetServiceQuota): {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
