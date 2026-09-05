#!/usr/bin/env python3
"""Read-only AWS feasibility preflight for single-GPU EC2 G7e instances.

Safe by construction: only ``describe``/``get`` calls. Never creates,
modifies, starts, stops, or terminates anything (AGENTS.md forbids AWS
mutation until the AWS phase is authorized), and never prints credentials or
account identifiers. Requires the AWS CLI with a read-only identity.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

CANDIDATE_TYPES = ["g7e.2xlarge", "g7e.4xlarge", "g7e.8xlarge"]
# EC2 "Running On-Demand G and VT instances" vCPU quota.
G_VT_QUOTA_CODE = "L-DB2E81BA"


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def main() -> int:
    print("== AWS preflight (read-only) ==")

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
        print("Provide read-only credentials (see .env.example); never commit them.")
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
