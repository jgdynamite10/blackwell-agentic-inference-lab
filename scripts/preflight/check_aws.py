#!/usr/bin/env python3
"""Read-only AWS feasibility preflight for single-GPU EC2 G7e instances.

Intended to be run LOCALLY by the operator in their authenticated
environment. The hosted Cloud Agent must not run credential-dependent
preflight checks (AGENTS.md, section 3).

Safe by construction: only ``describe``/``get`` calls; no create, modify,
start, stop, or terminate operations (AGENTS.md forbids AWS mutation until
the AWS phase is authorized); never prints credentials, account identifiers,
raw CLI stdout/stderr on failure, or raw exception text.

Scope (exactly two account-level checks; this script does NOT query the AWS
Pricing API and makes no pricing claims):

1. ``ec2 describe-instance-type-offerings`` — which candidate G7e types are
   *offered* in the current region. An offering means the type can be
   requested there; it does **not** prove immediate/live capacity.
2. ``service-quotas get-service-quota`` — the account's On-Demand G/VT vCPU
   quota.

Exit codes: 0 only when both checks completed; any blocked or failed check
exits nonzero.
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
    "modify, stop, or terminate resources, and it never prints secrets,\n"
    "account identifiers, or raw error output."
)


def run(cmd: list[str]) -> tuple[int, str]:
    """Run a read-only aws command; stdout is returned for parsing only.

    Raw stdout/stderr is never printed by callers on failure — failure
    messages are sanitized because CLI errors can embed ARNs, account IDs,
    and credential file paths.
    """
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    return proc.returncode, (proc.stdout or "").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "READ-ONLY AWS feasibility preflight. Reports which single-GPU G7e "
            "instance types are OFFERED in the current region (an offering does "
            "not prove immediate/live capacity) and the account's On-Demand "
            "G/VT vCPU service quota, using aws describe/get commands only. It "
            "does not query the AWS Pricing API. Performs no create/modify/stop/"
            "terminate operations and never prints secrets, account identifiers, "
            "or raw error output. Run locally; not from the hosted Cloud Agent."
        )
    )
    parser.parse_args(argv)
    print(READ_ONLY_BANNER)

    aws = shutil.which("aws")
    if not aws:
        print("BLOCKED: aws CLI is not installed.")
        print(
            "Missing capability: cannot verify G7e instance-type offerings or "
            "the G/VT-instance vCPU service quota."
        )
        return 1

    code, _ = run([aws, "sts", "get-caller-identity", "--output", "json"])
    if code != 0:
        print("BLOCKED: aws CLI is installed but no working credentials were found.")
        print("Provide read-only credentials locally (see .env.example); never commit them.")
        return 1
    print("Working AWS credentials detected (account identity not printed).")

    ok = True

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
        try:
            offerings = json.loads(out).get("InstanceTypeOfferings", [])
        except ValueError:
            print(
                "BLOCKED: the instance-type offerings response could not be "
                "parsed. Re-run locally; no raw output is printed."
            )
            ok = False
        else:
            found = sorted({o["InstanceType"] for o in offerings})
            print(f"G7e single-GPU types offered in the current region: {found or 'none'}")
            print(
                "(An offering means the type can be requested in this region; "
                "it does not prove immediate/live capacity.)"
            )
    else:
        print(
            "BLOCKED: describe-instance-type-offerings failed. Likely causes: "
            "the role lacks ec2:DescribeInstanceTypeOfferings, or no default "
            "region is configured. Fix locally and retry. (Raw CLI error output "
            "is suppressed because it can contain account details.)"
        )
        ok = False

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
        try:
            quota = json.loads(out).get("Quota", {})
        except ValueError:
            print(
                "BLOCKED: the service-quota response could not be parsed. "
                "Re-run locally; no raw output is printed."
            )
            ok = False
        else:
            print(
                f"On-Demand G/VT vCPU quota: {quota.get('Value')} "
                f"(g7e.4xlarge needs 16, g7e.8xlarge needs 32)"
            )
    else:
        print(
            "BLOCKED: the service-quota lookup failed. Likely cause: the role "
            "lacks servicequotas:GetServiceQuota. Grant read-only quota access "
            "locally and retry. (Raw CLI error output is suppressed.)"
        )
        ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
