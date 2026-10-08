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


@pytest.fixture(scope="session")
def qualification_environment_marker(tmp_path_factory):
    """A genuine D-0031 environment marker for the interpreter running the tests.

    The marker records the versions actually installed here, so the gate still
    fails if the test environment itself drifts from constraints.txt.
    """
    from blackwell_lab.cloud import qual_env

    marker = tmp_path_factory.mktemp("qual-env") / qual_env.MARKER_NAME
    qual_env.write_marker(marker)
    return marker


@pytest.fixture(autouse=True)
def _qualification_environment(qualification_environment_marker):
    """Point the environment gate at the session marker (read-only otherwise).

    A private MonkeyPatch instance is used so a test calling
    ``monkeypatch.undo()`` on the shared fixture does not drop this binding.
    """
    from blackwell_lab.cloud import qual_env

    patch = pytest.MonkeyPatch()
    patch.setattr(qual_env, "marker_path", lambda prefix=None: qualification_environment_marker)
    patch.setattr(qual_env, "in_virtual_environment", lambda *_a, **_k: True)
    try:
        yield
    finally:
        patch.undo()


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
