"""The `ReadDemandCounters` use case: `/admin/demand`'s table (ADR-0043)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from tailorcraft.domain.shared.counters import (
    DemandCounter,
    DemandCounterQuery,
    InvalidCounterRange,
)

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
        days_in_range = (query.to_day - query.from_day).days + 1
        if not 1 <= days_in_range <= MAX_RANGE_DAYS:
            raise InvalidCounterRange(
                f"a range must run forwards and hold at most {MAX_RANGE_DAYS} days"
            )
        stored = await self._counts.between(query.from_day, query.to_day)
        days = tuple(
            DemandDay(day=day, counts={c: stored.get((day, c), 0) for c in DemandCounter})
            for day in (query.from_day + timedelta(days=n) for n in range(days_in_range))
        )
        totals = {c: sum(d.counts[c] for d in days) for c in DemandCounter}
        return DemandCounters(days=days, totals=totals)
