#!/usr/bin/env python3
"""Read-only Akamai Cloud (Linode) feasibility preflight.

Safe by construction:
- Public, unauthenticated endpoints for GPU plan types and regions.
- With ``LINODE_TOKEN`` (read-only scope recommended), checks account-level
  region availability. Never creates, modifies, or deletes anything.
- Never prints token values or account identifiers.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

API = "https://api.linode.com/v4"
GPU_KEYWORDS = ("rtxpro6000", "rtx-pro-6000", "blackwell")


def get_json(path: str, token: str | None = None) -> dict:
    request = urllib.request.Request(f"{API}{path}")  # noqa: S310 - fixed https host
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def main() -> int:
    print("== Akamai Cloud preflight (read-only) ==")

    try:
        types = get_json("/linode/types")["data"]
    except Exception as exc:
        print(f"BLOCKED: could not reach public Linode API: {exc}")
        return 1

    gpu_plans = [
        t
        for t in types
        if t.get("class") == "gpu" and any(k in t.get("id", "").lower() for k in GPU_KEYWORDS)
    ]
    if gpu_plans:
        print(f"Found {len(gpu_plans)} RTX PRO 6000 Blackwell plan(s) in the public catalog:")
        for plan in gpu_plans:
            hourly = plan.get("price", {}).get("hourly")
            print(
                f"  - {plan['id']}: {plan.get('vcpus')} vCPU, "
                f"{plan.get('memory', 0) / 1024:.0f} GB RAM, "
                f"{plan.get('gpus')} GPU(s), ${hourly}/hr (catalog price)"
            )
    else:
        print("No RTX PRO 6000 Blackwell plans visible in the public catalog.")
        print("The plan is limited-availability; account onboarding may be required.")

    token = os.environ.get("LINODE_TOKEN")
    if not token:
        print("LINODE_TOKEN not set: skipping account-level availability check.")
        print("Missing capability: cannot verify this account's onboarding/eligible regions.")
        return 0

    try:
        availability = get_json("/account/availability", token)["data"]
    except Exception as exc:
        print(f"Account availability check failed (token may lack scope): {exc}")
        return 1

    unavailable = [r["region"] for r in availability if "GPU Linodes" in r.get("unavailable", [])]
    print(f"Regions where GPU Linodes are UNAVAILABLE to this account: {len(unavailable)}")
    for region in unavailable:
        print(f"  - {region}")
    print("(Region names only; no account identifiers are printed.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
