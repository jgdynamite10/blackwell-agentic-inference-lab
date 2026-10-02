"""Sealed qualification-set custody tooling.

SHA-256 digests provide integrity, not confidentiality. This package does
not encrypt payloads and does not apply a cryptographic seal. Key
management is outside this track.

The tooling accepts externally supplied opaque bundles. It does not
generate qualification tasks, read scenario catalogs, or contact a
provider.
"""

from blackwell_lab.sealed_sets.model import (
    INTEGRITY_STATEMENT,
    approval_phrase,
    running_controller_digest,
)

__all__ = [
    "INTEGRITY_STATEMENT",
    "approval_phrase",
    "running_controller_digest",
]
