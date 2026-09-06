"""Read-only Akamai Cloud (Linode) preflight checks.

Run **locally by the operator** in their authenticated environment. The
hosted Cloud Agent must not run credential-dependent preflight checks
(AGENTS.md, section 3) — this module is imported there only by offline tests
that inject fake fetchers.

Safe by construction:

- Issues only HTTP GET requests; contains no create/update/delete calls.
- The plan-type catalog check works unauthenticated; the authenticated
  readiness workflow requires a ``LINODE_TOKEN`` with read-only scope and a
  ``--region`` selection.
- Sanitized output only: never prints token values, account identifiers,
  raw API responses, or raw exception text. Failures produce generic,
  actionable messages.

Truthful readiness (decision D-0013): the authenticated workflow exits
nonzero unless ALL of the following are confirmed for the selected region —
the exact one-GPU Blackwell plan is visible to the account, the region
supports GPU Linodes and is not account-restricted, the region reports the
exact plan as deployable, and the applicable regional hourly price is
observed. Generic GPU-capable regions are never described as confirmed
Blackwell deployability, and a successful public-catalog check never makes
the authenticated workflow succeed. Each authenticated run writes a
sanitized receipt (exact plan, region, observed price, retrieval time, and
readiness decisions) to the external private ``LAB_RESULTS_DIR``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone

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

#: Injectable fetcher: (path, token) -> decoded JSON (object or, for some
#: endpoints such as ``GET /regions/{region}/availability``, a top-level array).
Fetch = Callable[..., dict | list]


class PreflightResponseError(ValueError):
    """A provider response was malformed and must not be treated as empty."""


def get_json(path: str, token: str | None = None) -> dict:
    request = urllib.request.Request(f"{API}{path}")  # noqa: S310 - fixed https host
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


def parse_list_response(payload: dict | list, *, context: str = "response") -> list[dict]:
    """Parse a Linode list payload: top-level array OR paginated ``{"data": [...]}``.

    Malformed payloads raise :class:`PreflightResponseError` — they are never
    silently treated as an empty list.
    """
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and "data" in payload:
        data = payload["data"]
        if not isinstance(data, list):
            raise PreflightResponseError(f"{context}: paginated 'data' is not a list")
        items = data
    else:
        raise PreflightResponseError(
            f"{context}: expected a top-level array or a paginated object with 'data'"
        )
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise PreflightResponseError(f"{context}: entry {index} is not an object")
    return items


def parse_region_availability(payload: dict | list, *, region: str) -> list[dict]:
    """Parse ``GET /v4/regions/{region}/availability`` (top-level array in the live API).

    Each entry must include ``region``, ``plan``, and a boolean ``available``.
    Duplicate ``(region, plan)`` pairs with conflicting ``available`` values fail.
    """
    context = f"/regions/{region}/availability"
    items = parse_list_response(payload, context=context)
    seen: dict[tuple[str, str], bool] = {}
    validated: list[dict] = []
    for index, item in enumerate(items):
        for field in ("region", "plan", "available"):
            if field not in item:
                raise PreflightResponseError(f"{context}: entry {index} missing '{field}'")
        if not isinstance(item["available"], bool):
            raise PreflightResponseError(f"{context}: entry {index} 'available' is not a boolean")
        key = (str(item["region"]), str(item["plan"]))
        available = item["available"]
        if key in seen and seen[key] != available:
            raise PreflightResponseError(
                f"{context}: conflicting availability records for plan {key[1]!r} in {key[0]!r}"
            )
        seen[key] = available
        validated.append(item)
    return validated


def _paginated(fetch: Fetch, path: str, token: str | None = None) -> list[dict]:
    """Collects every page of a paginated Linode list endpoint (read-only GETs)."""
    results: list[dict] = []
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        payload = fetch(f"{path}{separator}page={page}", token)
        if not isinstance(payload, dict):
            raise PreflightResponseError(
                f"{path}: paginated list endpoints must return an object, not an array"
            )
        page_items = parse_list_response(payload, context=f"{path}?page={page}")
        results.extend(page_items)
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
    """Prints Blackwell plans from the unauthenticated catalog; True on success.

    Informational only: a successful catalog check NEVER makes the complete
    authenticated readiness workflow succeed.
    """
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


# -- authenticated readiness decisions ------------------------------------------


def check_plan_entitlement(token: str, fetch: Fetch | None = None) -> dict:
    """Authenticated, read-only check for exact Blackwell plan entitlement.

    Decision semantics (truthful): the check PASSES only when the
    account-visible catalog contains the exact single-GPU RTX PRO 6000
    Blackwell plan. Visibility of larger multi-GPU plans does not pass.
    Returns a sanitized decision dict; prints plan facts only — never
    account identifiers.
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
        return {"completed": False, "plan_visible": False, "plan": None}

    plans = _blackwell_plans(types)
    if not plans:
        print(
            "ENTITLEMENT: NOT VISIBLE — no RTX PRO 6000 Blackwell plan appears "
            "in this account's catalog. The plan is limited-availability: "
            "request onboarding via Akamai support before any authorized "
            "Phase 3B apply."
        )
        return {"completed": True, "plan_visible": False, "plan": None}

    print(f"ENTITLEMENT: {len(plans)} RTX PRO 6000 Blackwell plan(s) visible to this account:")
    for plan in plans:
        print(f"{_describe_plan(plan)} (account-visible price)")
    single_gpu = [p for p in plans if p.get("gpus") == TARGET_GPU_COUNT]
    if not single_gpu:
        print(
            f"TARGET PLAN MISSING: no visible Blackwell plan has exactly "
            f"{TARGET_GPU_COUNT} GPU. Do not substitute a larger plan without "
            "an owner decision."
        )
        return {"completed": True, "plan_visible": False, "plan": None}
    target = single_gpu[0]
    print(
        f"TARGET PLAN VISIBLE: {target['id']} (exactly {TARGET_GPU_COUNT} GPU) "
        "is visible to this account. Region deployability and the applicable "
        "regional price are verified separately — plan visibility alone is "
        "not readiness."
    )
    return {"completed": True, "plan_visible": True, "plan": target}


def _regional_price(plan: dict, region: str) -> float | None:
    """The applicable account-visible hourly price for one region, if observed."""
    for entry in plan.get("region_prices") or []:
        if entry.get("id") == region:
            hourly = entry.get("hourly")
            return float(hourly) if hourly is not None else None
    hourly = (plan.get("price") or {}).get("hourly")
    return float(hourly) if hourly is not None else None


def check_region_deployability(
    token: str,
    region: str,
    plan: dict,
    fetch: Fetch | None = None,
) -> dict:
    """Authenticated, read-only deployability decision for ONE exact region.

    Truthful semantics: a region passes only when (a) it exists and carries
    the "GPU Linodes" capability, (b) the account is not restricted from GPU
    Linodes there, and (c) the region's availability listing reports the
    EXACT selected plan as available. A generic GPU-capable region is never
    described as confirmed Blackwell deployability.
    """
    fetch = fetch or get_json
    plan_id = plan.get("id", "")
    try:
        regions = _paginated(fetch, "/regions")
        availability = _paginated(fetch, "/account/availability", token)
        # GET /regions/{region}/availability returns a top-level JSON array in
        # the live API — never a paginated {"data": [...]} wrapper. Parse it
        # explicitly; malformed responses fail closed rather than looking empty.
        region_plans = parse_region_availability(
            fetch(f"/regions/{region}/availability", token), region=region
        )
    except (PreflightResponseError, Exception):
        print(
            "BLOCKED: the region deployability lookup failed. Likely causes: "
            "the token lacks account:read_only scope, the token is expired, "
            "the region id is invalid, or the API was unreachable. (No error "
            "payload is printed to avoid echoing account details.)"
        )
        return {
            "completed": False,
            "region_exists": False,
            "capability_ok": False,
            "account_allowed": False,
            "plan_deployable": False,
            "regional_price_usd_per_hour": None,
        }

    region_record = next((r for r in regions if r.get("id") == region), None)
    region_exists = region_record is not None
    capability_ok = bool(
        region_record and "GPU Linodes" in (region_record.get("capabilities") or [])
    )
    restricted = {
        r["region"] for r in availability if "GPU Linodes" in (r.get("unavailable") or [])
    }
    account_allowed = region_exists and region not in restricted
    plan_deployable = any(
        entry.get("plan") == plan_id and entry.get("available") is True for entry in region_plans
    )
    price = _regional_price(plan, region)

    print(f"REGION {region}:")
    print(f"  - region exists: {'yes' if region_exists else 'NO'}")
    print(f"  - GPU Linodes capability: {'yes' if capability_ok else 'NO'}")
    print(f"  - account allowed for GPU Linodes here: {'yes' if account_allowed else 'NO'}")
    deployable_text = (
        "yes" if plan_deployable else "NO (a GPU-capable region is not Blackwell deployability)"
    )
    print(f"  - EXACT plan {plan_id} deployable here: {deployable_text}")
    if price is not None:
        print(f"  - applicable regional price: ${price}/hr (account-visible)")
    else:
        print("  - applicable regional price: UNCONFIRMED")
    return {
        "completed": True,
        "region_exists": region_exists,
        "capability_ok": capability_ok,
        "account_allowed": account_allowed,
        "plan_deployable": plan_deployable,
        "regional_price_usd_per_hour": price,
    }


def authenticated_readiness(
    token: str,
    region: str,
    fetch: Fetch | None = None,
) -> dict:
    """The complete authenticated readiness decision (sanitized receipt).

    ``ready`` is true ONLY when every decision passed: exact one-GPU plan
    visible, selected region confirmed for that exact plan and account, GPU
    deployment capability present, and the applicable regional price
    observed. The public catalog plays no part in this decision.
    """
    entitlement = check_plan_entitlement(token, fetch)
    plan = entitlement.get("plan")
    if entitlement["completed"] and entitlement["plan_visible"] and plan is not None:
        region_decision = check_region_deployability(token, region, plan, fetch)
    else:
        region_decision = {
            "completed": False,
            "region_exists": False,
            "capability_ok": False,
            "account_allowed": False,
            "plan_deployable": False,
            "regional_price_usd_per_hour": None,
        }
        print(
            "REGION CHECK SKIPPED: without the exact one-GPU plan there is "
            "nothing to confirm deployability for."
        )

    price = region_decision["regional_price_usd_per_hour"]
    ready = bool(
        entitlement["completed"]
        and entitlement["plan_visible"]
        and region_decision["completed"]
        and region_decision["region_exists"]
        and region_decision["capability_ok"]
        and region_decision["account_allowed"]
        and region_decision["plan_deployable"]
        and price is not None
    )
    receipt = {
        "workflow": "akamai-authenticated-preflight",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "selected_plan_id": (plan or {}).get("id"),
        "selected_region": region,
        "observed_hourly_price_usd": price,
        "decisions": {
            "entitlement_check_completed": entitlement["completed"],
            "exact_single_gpu_plan_visible": entitlement["plan_visible"],
            "region_check_completed": region_decision["completed"],
            "region_exists": region_decision["region_exists"],
            "gpu_capability_in_region": region_decision["capability_ok"],
            "account_allowed_in_region": region_decision["account_allowed"],
            "exact_plan_deployable_in_region": region_decision["plan_deployable"],
            "regional_price_observed": price is not None,
        },
        "ready": ready,
        "note": (
            "Sanitized receipt: no account identifiers or tokens. 'ready' is "
            "true only when every decision passed; the public catalog check "
            "never substitutes for these account-level decisions."
        ),
    }
    return receipt


def _write_receipt(receipt: dict) -> str | None:
    """Writes the sanitized receipt to the external private results dir."""
    from blackwell_lab.cloud.artifacts import write_private_json
    from blackwell_lab.paths import RunMode, resolve_results_dir

    resolved = resolve_results_dir(mode=RunMode.REAL)
    if resolved is None:  # pragma: no cover - REAL mode never returns None
        return None
    stamp = receipt["generated_at_utc"].replace(":", "").replace("+", "Z")
    name = f"preflight-receipt-{stamp}.json"
    write_private_json(resolved / "preflight-receipts" / name, receipt)
    return name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_akamai",
        description=(
            "READ-ONLY Akamai Cloud feasibility preflight. Lists RTX PRO 6000 "
            "Blackwell GPU plans from the public catalog and, unless "
            "--public-only is given, requires a read-only LINODE_TOKEN in the "
            "environment plus --region to decide account-level readiness: "
            "exact one-GPU plan entitlement, confirmed deployability of that "
            "exact plan in the selected region, and the applicable regional "
            "price. Exit 0 ONLY when every readiness decision passed. Writes "
            "a sanitized receipt to the external private LAB_RESULTS_DIR. "
            "Performs no create/update/delete operations and never prints "
            "secrets or raw error payloads. Run locally; not from the hosted "
            "Cloud Agent."
        ),
    )
    parser.add_argument(
        "--public-only",
        action="store_true",
        help=(
            "Run only the unauthenticated public-catalog check. Informational "
            "only: it never constitutes authenticated readiness."
        ),
    )
    parser.add_argument(
        "--region",
        help="Exact region id (e.g. us-ord) to confirm deployability for.",
    )
    args = parser.parse_args(argv)
    print(READ_ONLY_BANNER)

    catalog_ok = report_gpu_catalog()

    if args.public_only:
        print(
            "--public-only: account-level checks intentionally skipped. This "
            "output is informational and does NOT constitute readiness."
        )
        return 0 if catalog_ok else 1

    token = os.environ.get("LINODE_TOKEN")
    if not token:
        print(
            "BLOCKED: LINODE_TOKEN is not set, so the required account-level "
            "readiness decisions cannot run. Set a read-only token locally "
            "and retry, or pass --public-only for the informational catalog "
            "check alone."
        )
        return 1
    if not args.region:
        print(
            "BLOCKED: --region is required for authenticated readiness: "
            "deployability is confirmed for one exact region, never assumed "
            "from generic GPU capability."
        )
        return 1

    receipt = authenticated_readiness(token, args.region)
    try:
        receipt_name = _write_receipt(receipt)
    except Exception:
        receipt_name = None
    if receipt_name is None:
        print(
            "BLOCKED: the sanitized receipt could not be written externally. "
            "Set LAB_RESULTS_DIR to an absolute private path outside the "
            "repository and retry (the receipt is required)."
        )
        return 1
    print(f"RECEIPT: written externally as {receipt_name} (path not printed).")
    if receipt["ready"]:
        print("READY: every authenticated readiness decision passed.")
        return 0
    print(
        "NOT READY: one or more readiness decisions failed (see the decisions "
        "above and the external receipt). The exit status is nonzero."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
