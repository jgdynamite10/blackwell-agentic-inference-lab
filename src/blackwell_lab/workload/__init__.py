"""Synthetic Cloud Operations Agent workload (Phase 2).

Everything in this package is synthetic and offline: fictional service names,
RFC 5737 documentation IP ranges, invented ``.example`` hostnames, and
generated logs. No credentials, provider APIs, model downloads, GPUs, or
network access are used anywhere in this package.
"""

from blackwell_lab.workload.scenarios import (
    INCIDENT_CLASSES,
    WORKLOAD_NAME,
    WORKLOAD_VERSION,
    catalog,
    catalog_digest,
)

__all__ = [
    "INCIDENT_CLASSES",
    "WORKLOAD_NAME",
    "WORKLOAD_VERSION",
    "catalog",
    "catalog_digest",
]
