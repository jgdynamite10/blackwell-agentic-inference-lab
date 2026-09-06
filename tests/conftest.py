"""Shared test configuration.

The autouse guard below makes the offline requirement enforceable: any test
that attempts a real network connection fails immediately. Phase 2 code must
run entirely offline (no model download, no external API, no provider access).
"""

from __future__ import annotations

import socket

import pytest


class NetworkAccessAttempted(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch):
    """Fails any test that tries to open a network connection."""

    def _blocked(*_args, **_kwargs):
        raise NetworkAccessAttempted(
            "Network access is forbidden in this test suite: Phase 2 is offline-only."
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
