#!/usr/bin/env python3
"""Read-only Google Cloud feasibility preflight for g4-standard-48.

Intended to be run LOCALLY by the operator in their authenticated
environment. The hosted Cloud Agent must not run credential-dependent
preflight checks (AGENTS.md, section 3).

Safe by construction: only ``gcloud`` list/describe commands; no
create/update/delete calls; never prints credentials or identity values.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

MACHINE_TYPE = "g4-standard-48"
QUOTA_METRIC = "NVIDIA_RTX_PRO_6000_GPUS"

READ_ONLY_BANNER = (
    "== Google Cloud preflight — READ-ONLY ==\n"
    "This tool only runs gcloud list/describe commands. It cannot provision,\n"
    "update, stop, or delete resources, and it never prints secrets or\n"
    "account/project identifiers."
)


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "READ-ONLY Google Cloud feasibility preflight. Reports zones offering "
            f"{MACHINE_TYPE} and the regional {QUOTA_METRIC} quota overview using "
            "gcloud list commands only. Performs no create/update/delete operations "
            "and never prints secrets. Run locally; not from the hosted Cloud Agent."
        )
    )
    parser.parse_args(argv)
    print(READ_ONLY_BANNER)

    gcloud = shutil.which("gcloud")
    if not gcloud:
        print("BLOCKED: gcloud CLI is not installed.")
        print(
            "Missing capability: cannot verify project quota "
            f"({QUOTA_METRIC}) or zone availability for {MACHINE_TYPE}."
        )
        return 1

    code, out = run([gcloud, "auth", "list", "--filter=status:ACTIVE", "--format=value(status)"])
    if code != 0 or not out:
        print("BLOCKED: gcloud is installed but no active credentials were found.")
        print("Provide read-only credentials locally (see .env.example); never commit them.")
        return 1
    print("Active gcloud credentials detected (identity not printed).")

    code, out = run(
        [
            gcloud,
            "compute",
            "machine-types",
            "list",
            f"--filter=name={MACHINE_TYPE}",
            "--format=value(zone)",
        ]
    )
    if code != 0:
        print(f"machine-types list failed: {out}")
        return 1
    zones = sorted(set(out.splitlines())) if out else []
    print(f"Zones offering {MACHINE_TYPE}: {len(zones)}")
    for zone in zones:
        print(f"  - {zone}")

    code, out = run(
        [
            gcloud,
            "compute",
            "regions",
            "list",
            f"--format=table(name,quotas.filter(metric={QUOTA_METRIC}))",
        ]
    )
    if code == 0:
        print(f"Regional {QUOTA_METRIC} quota overview:")
        print(out)
    else:
        print(f"Quota listing failed (read-only role may lack compute.regions.list): {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
