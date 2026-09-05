#!/usr/bin/env python3
"""Read-only Akamai Cloud (Linode) feasibility preflight.

Intended to be run LOCALLY by the operator in their authenticated
environment. The hosted Cloud Agent must not run credential-dependent
preflight checks (AGENTS.md, section 3).

Safe by construction:
- Issues only HTTP GET requests; contains no create/update/delete calls.
- Unauthenticated endpoints for the plan-type catalog.
- With ``LINODE_TOKEN`` (read-only scope recommended), checks account-level
  region availability.
- Never prints token values or account identifiers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

API = "https://api.linode.com/v4"
GPU_KEYWORDS = ("rtxpro6000", "rtx-pro-6000", "blackwell")

READ_ONLY_BANNER = (
    "== Akamai Cloud preflight — READ-ONLY ==\n"
    "This tool only performs GET requests. It cannot provision, update, stop,\n"
    "or delete resources, and it never prints secrets or account identifiers."
)


def get_json(path: str, token: str | None = None) -> dict:
    request = urllib.request.Request(f"{API}{path}")  # noqa: S310 - fixed https host
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def report_gpu_catalog(fetch=None) -> bool:
    """Print RTX PRO 6000 Blackwell plans from the unauthenticated catalog; True on success."""
    fetch = fetch or get_json
    try:
        types = fetch("/linode/types")["data"]
    except Exception as exc:
        print(f"BLOCKED: could not reach the Linode API catalog: {exc}")
        return False

    gpu_plans = [
        t
        for t in types
        if t.get("class") == "gpu" and any(k in t.get("id", "").lower() for k in GPU_KEYWORDS)
    ]
    if gpu_plans:
        print(f"Found {len(gpu_plans)} RTX PRO 6000 Blackwell plan(s) in the catalog:")
        for plan in gpu_plans:
            hourly = plan.get("price", {}).get("hourly")
            print(
                f"  - {plan['id']}: {plan.get('vcpus')} vCPU, "
                f"{plan.get('memory', 0) / 1024:.0f} GB RAM, "
                f"{plan.get('gpus')} GPU(s), ${hourly}/hr (catalog price)"
            )
    else:
        print("No RTX PRO 6000 Blackwell plans visible in the unauthenticated catalog.")
        print("The plan is limited-availability; account onboarding may be required.")
    return True


def report_account_availability(token: str, fetch=None) -> bool:
    """Print account-level GPU-Linode region availability; True on success."""
    fetch = fetch or get_json
    try:
        availability = fetch("/account/availability", token)["data"]
    except Exception as exc:
        print(f"Account availability check failed (token may lack scope): {exc}")
        return False

    unavailable = [r["region"] for r in availability if "GPU Linodes" in r.get("unavailable", [])]
    print(f"Regions where GPU Linodes are UNAVAILABLE to this account: {len(unavailable)}")
    for region in unavailable:
        print(f"  - {region}")
    print("(Region names only; no account identifiers are printed.)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "READ-ONLY Akamai Cloud feasibility preflight. Lists RTX PRO 6000 "
            "Blackwell GPU plans from the public catalog and, when a read-only "
            "LINODE_TOKEN is present in the environment, reports account-level "
            "region availability. Performs no create/update/delete operations "
            "and never prints secrets. Run locally; not from the hosted Cloud Agent."
        )
    )
    parser.parse_args(argv)
    print(READ_ONLY_BANNER)

    if not report_gpu_catalog():
        return 1

    token = os.environ.get("LINODE_TOKEN")
    if not token:
        print("LINODE_TOKEN not set: skipping account-level availability check.")
        print("Missing capability: cannot verify this account's onboarding/eligible regions.")
        return 0
    return 0 if report_account_availability(token) else 1


if __name__ == "__main__":
    sys.exit(main())
