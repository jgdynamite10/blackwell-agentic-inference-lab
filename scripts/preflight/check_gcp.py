#!/usr/bin/env python3
"""Read-only Google Cloud feasibility preflight for g4-standard-48.

Safe by construction: only ``gcloud`` describe/list commands. Never creates,
modifies, or deletes anything, and never prints credentials. Requires a local
``gcloud`` installation authenticated with a read-only identity.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

MACHINE_TYPE = "g4-standard-48"
QUOTA_METRIC = "NVIDIA_RTX_PRO_6000_GPUS"


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def main() -> int:
    print("== Google Cloud preflight (read-only) ==")

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
        print("Provide read-only credentials (see .env.example); never commit them.")
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
