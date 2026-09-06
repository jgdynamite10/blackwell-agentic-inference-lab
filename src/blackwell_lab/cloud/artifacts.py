"""Atomic, private persistence for external lifecycle/preflight artifacts.

Every artifact this package writes outside Git — ledgers, pending records,
plan metadata, session records, orphan reports, preflight receipts — goes
through these helpers: temp-file-plus-rename atomic writes, directory mode
0700, file mode 0600. Nothing is ever written into the repository tree.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path


def write_private_json(target: Path, document: dict) -> str:
    """Atomic private write (0600 file, 0700 parent); returns SHA-256 hex."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(target.parent, 0o700)
    payload = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return hashlib.sha256(payload).hexdigest()


def write_private_text(target: Path, text: str) -> None:
    """Atomic private write of a text artifact (0600 file, 0700 parent)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(target.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
