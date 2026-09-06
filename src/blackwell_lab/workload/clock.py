"""Injectable monotonic clock and sleeper.

All workload timing flows through a :class:`Clock` so production code uses the
real monotonic clock while tests inject a deterministic fake. Simulated tool
latencies are *consumed* through ``Clock.sleep`` — they occupy task duration,
timeout budget, pacing, and cell wall time exactly like real delays.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Monotonic time source plus sleeper (injectable for tests)."""

    def monotonic(self) -> float:
        """Current monotonic time in seconds."""
        ...  # pragma: no cover - protocol

    def sleep(self, seconds: float) -> None:
        """Blocks (or virtually advances) for ``seconds``."""
        ...  # pragma: no cover - protocol


class SystemClock:
    """The real monotonic clock and real sleeping."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


SYSTEM_CLOCK = SystemClock()
