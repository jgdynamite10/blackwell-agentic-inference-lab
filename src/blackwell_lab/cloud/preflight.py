"""Read-only Akamai Cloud (Linode) preflight checks.

Run **locally by the operator** in their authenticated environment. The
hosted Cloud Agent must not run credential-dependent preflight checks
(AGENTS.md, section 3) — this module is imported there only by offline tests
that inject fake fetchers.

Safe by construction:

- Issues only HTTP GET requests; contains no create/update/delete calls.
- The plan-type catalog check works unauthenticated; the **plan entitlement**
  (exact Blackwell plan visible to this account, with the account-visible
  hourly price) and **eligible regions** checks require a ``LINODE_TOKEN``
  with read-only scope.
- Sanitized output only: never prints token values, account identifiers,
  raw API responses, or raw exception text. Failures produce generic,
  actionable messages.

Exit codes (``main``): 0 only when every requested check completed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections.abc import Callable

API = "https://api.linode.com/v4"
GPU_PLAN_KEYWORDS = ("rtxpro6000", "rtx-pro-6000", "blackwell")

#: The exact target: one RTX PRO 6000 Blackwell SE GPU (Phase 3 baseline).
TARGET_GPU_COUNT = 1

READ_ONLY_BANNER = (
    "== Akamai Cloud preflight — READ-ONLY ==\n"
    "This tool only performs GET requests. It cannot provision, update, stop,\n"
    "or delete resources, and it never prints secrets, account identifiers,\n"
    "or raw error payloads."
)

#: Injectable fetcher: (path, token) -> decoded JSON object.
Fetch = Callable[..., dict]


def get_json(path: str, token: str | None = None) -> dict:
    request = urllib.request.Request(f"{API}{path}")  # noqa: S310 - fixed https host
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def _paginated(fetch: Fetch, path: str, token: str | None = None) -> list[dict]:
    """Collects every page of a Linode list endpoint (read-only GETs)."""
    results: list[dict] = []
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        payload = fetch(f"{path}{separator}page={page}", token)
        results.extend(payload.get("data", []))
        if page >= int(payload.get("pages", 1)):
            return results
        page += 1


def _blackwell_plans(types: list[dict]) -> list[dict]:
    return [
        t
        for t in types
        if t.get("class") == "gpu" and any(k in t.get("id", "").lower() for k in GPU_PLAN_KEYWORDS)
    ]


def _describe_plan(plan: dict) -> str:
    hourly = plan.get("price", {}).get("hourly")
    return (
        f"  - {plan['id']}: {plan.get('vcpus')} vCPU, "
        f"{plan.get('memory', 0) / 1024:.0f} GB RAM, "
        f"{plan.get('gpus')} GPU(s), ${hourly}/hr"
    )


def report_gpu_catalog(fetch: Fetch | None = None) -> bool:
    """Prints Blackwell plans from the unauthenticated catalog; True on success."""
    fetch = fetch or get_json
    try:
        types = _paginated(fetch, "/linode/types")
    except Exception:
        # Deliberately no exception text: it could echo URLs, proxy details,
        # or response fragments. The message is generic and actionable.
        print(
            "BLOCKED: could not retrieve the public Linode plan catalog "
            "(network error or API unavailable). Check connectivity and retry."
        )
        return False

    gpu_plans = _blackwell_plans(types)
    if gpu_plans:
        print(f"Found {len(gpu_plans)} RTX PRO 6000 Blackwell plan(s) in the public catalog:")
        for plan in gpu_plans:
            print(f"{_describe_plan(plan)} (catalog price)")
    else:
        print("No RTX PRO 6000 Blackwell plans visible in the unauthenticated catalog.")
        print("The plan is limited-availability; account onboarding may be required.")
    return True


def report_plan_entitlement(token: str, fetch: Fetch | None = None) -> bool:
    """Authenticated, read-only check for exact Blackwell plan entitlement.

    Confirms whether the **account-visible** plan catalog contains the exact
    single-GPU RTX PRO 6000 Blackwell plan (limited-availability plans appear
    only for onboarded accounts) and prints the account-visible hourly price
    for each visible Blackwell plan. Prints plan facts only — never account
    identifiers. Returns True when the check itself completed.
    """
    fetch = fetch or get_json
    try:
        types = _paginated(fetch, "/linode/types", token)
    except Exception:
        print(
            "BLOCKED: the authenticated plan-catalog lookup failed. Likely "
            "causes: the token lacks read scope, the token is expired, or the "
            "API was unreachable. Fix the token locally and retry. (No error "
            "payload is printed to avoid echoing account details.)"
        )
        return False

    plans = _blackwell_plans(types)
    if not plans:
        print(
            "ENTITLEMENT: NOT VISIBLE — no RTX PRO 6000 Blackwell plan appears "
            "in this account's catalog. The plan is limited-availability: "
            "request onboarding via Akamai support before Phase 3B."
        )
        return True

    print(f"ENTITLEMENT: {len(plans)} RTX PRO 6000 Blackwell plan(s) visible to this account:")
    for plan in plans:
        print(f"{_describe_plan(plan)} (account-visible price)")
    single_gpu = [p for p in plans if p.get("gpus") == TARGET_GPU_COUNT]
    if single_gpu:
        target = single_gpu[0]
        hourly = target.get("price", {}).get("hourly")
        print(
            f"TARGET PLAN OK: {target['id']} (exactly {TARGET_GPU_COUNT} GPU) is "
            f"available to this account at ${hourly}/hr (account-visible price; "
            "regional surcharges may apply — verify the per-region price before "
            "approval)."
        )
    else:
        print(
            f"TARGET PLAN MISSING: no visible Blackwell plan has exactly "
            f"{TARGET_GPU_COUNT} GPU. Do not substitute a larger plan without "
            "an owner decision."
        )
    return True


def report_eligible_regions(token: str, fetch: Fetch | None = None) -> bool:
    """Authenticated, read-only region eligibility for GPU Linodes.

    Cross-references the public region list (GPU Linodes capability) with the
    account's availability restrictions. Prints region identifiers only.
    """
    fetch = fetch or get_json
    try:
        regions = _paginated(fetch, "/regions")
        availability = _paginated(fetch, "/account/availability", token)
    except Exception:
        print(
            "BLOCKED: the region eligibility lookup failed. Likely causes: the "
            "token lacks account:read_only scope, the token is expired, or the "
            "API was unreachable. Fix the token locally and retry. (No error "
            "payload is printed to avoid echoing account details.)"
        )
        return False

    gpu_capable = {r["id"] for r in regions if "GPU Linodes" in (r.get("capabilities") or [])}
    unavailable = {
        r["region"] for r in availability if "GPU Linodes" in (r.get("unavailable") or [])
    }
    eligible = sorted(gpu_capable - unavailable)
    print(f"Regions eligible for GPU Linodes on this account: {len(eligible)}")
    for region in eligible:
        print(f"  - {region}")
    if unavailable:
        print(f"Regions where GPU Linodes are UNAVAILABLE to this account: {len(unavailable)}")
        for region in sorted(unavailable):
            print(f"  - {region} (unavailable)")
    print("(Region identifiers only; no account identifiers are printed.)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_akamai",
        description=(
            "READ-ONLY Akamai Cloud feasibility preflight. Lists RTX PRO 6000 "
            "Blackwell GPU plans from the public catalog and, unless "
            "--public-only is given, requires a read-only LINODE_TOKEN in the "
            "environment to report exact plan entitlement, the account-visible "
            "hourly price, and eligible regions. Performs no "
            "create/update/delete operations and never prints secrets or raw "
            "error payloads. Run locally; not from the hosted Cloud Agent."
        ),
    )
    parser.add_argument(
        "--public-only",
        action="store_true",
        help=(
            "Run only the unauthenticated public-catalog check and treat it as "
            "the complete requested scope. Without this flag, the "
            "account-level entitlement and region checks are required and a "
            "missing LINODE_TOKEN fails."
        ),
    )
    args = parser.parse_args(argv)
    print(READ_ONLY_BANNER)

    if not report_gpu_catalog():
        return 1

    if args.public_only:
        print("--public-only: account-level checks intentionally skipped.")
        return 0

    token = os.environ.get("LINODE_TOKEN")
    if not token:
        print(
            "BLOCKED: LINODE_TOKEN is not set, so the required account-level "
            "entitlement and region checks cannot run. Set a read-only token "
            "locally and retry, or pass --public-only to request the catalog "
            "check alone."
        )
        print("Missing capability: cannot verify this account's onboarding/eligible regions.")
        return 1
    entitlement_ok = report_plan_entitlement(token)
    regions_ok = report_eligible_regions(token)
    return 0 if (entitlement_ok and regions_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
