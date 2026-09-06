"""Shared test doubles: a deterministic virtual clock.

``FakeClock`` implements the workload ``Clock`` protocol with a virtual
monotonic timeline: ``sleep`` advances time instantly instead of blocking, so
tests that exercise consumed tool delays, timeouts, and pacing stay fast and
fully deterministic. It is thread-safe because the bounded scheduler runs
workers concurrently.
"""

from __future__ import annotations

import threading


class FakeClock:
    """Thread-safe virtual monotonic clock (sleep advances virtual time)."""

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._now += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)
