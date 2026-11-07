"""The `ReadDemandCounters` use case: `/admin/demand`'s table (ADR-0043)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from tailorcraft.domain.shared.counters import DemandCounter, DemandCounterQuery

MAX_RANGE_DAYS = 92


@dataclass(frozen=True, slots=True)
class ReadDemandCountersQuery:
    from_day: date
    to_day: date


@dataclass(frozen=True, slots=True)
class DemandDay:
    """Every `DemandCounter` member is a key, zero when nothing was counted."""

    day: date
    counts: Mapping[DemandCounter, int]


@dataclass(frozen=True, slots=True)
class DemandCounters:
    """`days` is every day in the range, oldest first, zeros filled; `totals` sums them, every
    member a key."""

    days: tuple[DemandDay, ...]
    totals: Mapping[DemandCounter, int]


class ReadDemandCounters:
    """`InvalidCounterRange` when `from_day > to_day` or the range holds more than `MAX_RANGE_DAYS`
    days (both ends inclusive); otherwise one `query.between` and the zeros filled here."""

    def __init__(self, counts: DemandCounterQuery) -> None:
        self._counts = counts

    async def __call__(self, query: ReadDemandCountersQuery) -> DemandCounters:
        raise NotImplementedError
