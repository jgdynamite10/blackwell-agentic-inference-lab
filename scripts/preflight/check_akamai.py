#!/usr/bin/env python3
"""Read-only Akamai Cloud (Linode) feasibility preflight (thin wrapper).

The implementation lives in :mod:`blackwell_lab.cloud.preflight` so it can be
unit-tested offline with injected fetchers. Run this script LOCALLY in the
operator's authenticated environment (``pip install -e .`` first). The hosted
Cloud Agent must not run credential-dependent preflight checks (AGENTS.md,
section 3).

Checks (all HTTP GET only; no create/update/delete calls anywhere):

- unauthenticated RTX PRO 6000 Blackwell plan catalog;
- with a read-only ``LINODE_TOKEN``: exact Blackwell plan **entitlement** and
  the account-visible hourly price, plus eligible/unavailable **regions**.

Output is sanitized: no tokens, no account identifiers, no raw API payloads.
"""

from __future__ import annotations

import sys

try:
    from blackwell_lab.cloud.preflight import main
except ImportError:  # pragma: no cover - depends on the caller's environment
    print(
        "error: the blackwell_lab package is not importable. Install it first "
        '(from the repository root): pip install -e ".[dev]"',
        file=sys.stderr,
    )
    sys.exit(2)

if __name__ == "__main__":
    sys.exit(main())
