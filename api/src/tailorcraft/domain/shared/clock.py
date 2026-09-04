"""Time as a dependency.

`datetime.now()` inside the domain makes every rule that touches time untestable without either
sleeping or freezing the process. So time arrives through a port, like any other external fact.

The contract has one unusual clause, and it is deliberate: **the clock is whole-second**. Timestamps
are stored as `TIMESTAMP WITH TIME ZONE` and round-tripped through PostgreSQL and JSON, and a value
that carries microseconds on the way in but not on the way out turns every equality assertion into a
coin flip that lands wrong on a day you have no time for it (ADR-0007). Truncating at the source
means the value the domain sees is already the value the database will return.

Any test double must honour the same contract — `FixedClock` in `tailorcraft.infrastructure.clock`
asserts it rather than trusting it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """The only source of "now" anything in the domain or application layer may use."""

    def now(self) -> datetime:
        """Return the current instant: timezone-aware, UTC, and truncated to a whole second."""
        ...
