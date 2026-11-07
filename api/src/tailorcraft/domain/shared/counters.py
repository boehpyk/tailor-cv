"""Demand counters: one anonymous count per UTC day and counter (ADR-0043).

In `shared` because four contexts count and none may import another. A row holds no personal data,
so it outlives the purge and erasure: that is its purpose.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from typing import Protocol


class DemandCounter(StrEnum):
    """A closed set, checked in the database. A new counter is a deliberate change to this enum, the
    CHECK and ADR-0043's table, never a free-form string."""

    FIT_SCORES = "fit_scores"
    FIT_SCORES_BUSY = "fit_scores_busy"
    SIGN_UPS = "sign_ups"
    PACK_CLICKS = "pack_clicks"
    PACK_CLICKERS = "pack_clickers"
    RUNS_FAILED = "runs_failed"
    RUNS_SUCCEEDED = "runs_succeeded"


class DailyCounterPort(Protocol):
    """Counts a fact **in the fact's own transaction**: the adapter never commits, so a rolled-back
    fact counts nothing. `day` is the UTC date of the fact's instant, read once from the `Clock`."""

    async def increment(self, counter: DemandCounter, day: date) -> None: ...


class DemandCounterQuery(Protocol):
    """The read side, for `/admin/demand`."""

    async def between(
        self, from_day: date, to_day: date
    ) -> Mapping[tuple[date, DemandCounter], int]:
        """The stored counts for days in `[from_day, to_day]`, both inclusive. A pair with no row is
        absent, never zero: filling zeros is the use case's job."""
        ...
