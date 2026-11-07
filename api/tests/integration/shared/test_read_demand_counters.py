"""Application tests for `ReadDemandCounters` (slice 5.2, T15 RED, AC-29, P-5, ADR-0043).

The use case owns two rules the adapter must not: the **range** (both ends inclusive, `from <= to`,
at most 92 days, else `InvalidCounterRange`, and then the query is never run) and the **zero fill**
(the query returns only the pairs that have a row; every day of the range and every counter in
`DemandCounter` comes back, zero when nothing was counted).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta

import pytest

from tailorcraft.application.shared.read_demand_counters import (
    ReadDemandCounters,
    ReadDemandCountersQuery,
)
from tailorcraft.domain.shared.counters import DemandCounter, InvalidCounterRange

_DAY = date(2026, 11, 1)


class _Query:
    """A `DemandCounterQuery` over a dict: only pairs with a row, as the real adapter returns."""

    def __init__(self, rows: Mapping[tuple[date, DemandCounter], int] | None = None) -> None:
        self._rows = dict(rows or {})
        self.ranges: list[tuple[date, date]] = []

    async def between(
        self, from_day: date, to_day: date
    ) -> Mapping[tuple[date, DemandCounter], int]:
        self.ranges.append((from_day, to_day))
        return {k: v for k, v in self._rows.items() if from_day <= k[0] <= to_day}


async def _read(query: _Query, from_day: date, to_day: date):  # type: ignore[no-untyped-def]
    return await ReadDemandCounters(query)(
        ReadDemandCountersQuery(from_day=from_day, to_day=to_day)
    )


async def test_a_day_with_nothing_counted_has_every_counter_at_zero() -> None:
    result = await _read(_Query(), _DAY, _DAY)

    (day,) = result.days
    assert day.day == _DAY
    assert dict(day.counts) == dict.fromkeys(DemandCounter, 0)
    assert dict(result.totals) == dict.fromkeys(DemandCounter, 0)


async def test_every_day_of_the_range_is_present_oldest_first_with_zeros_filled() -> None:
    query = _Query(
        {
            (_DAY, DemandCounter.SIGN_UPS): 2,
            (_DAY + timedelta(days=2), DemandCounter.SIGN_UPS): 5,
            (_DAY + timedelta(days=2), DemandCounter.PACK_CLICKS): 1,
        }
    )

    result = await _read(query, _DAY, _DAY + timedelta(days=3))

    assert [d.day for d in result.days] == [_DAY + timedelta(days=n) for n in range(4)]
    assert [d.counts[DemandCounter.SIGN_UPS] for d in result.days] == [2, 0, 5, 0]
    assert [d.counts[DemandCounter.PACK_CLICKS] for d in result.days] == [0, 0, 1, 0]
    assert all(set(d.counts) == set(DemandCounter) for d in result.days)


async def test_the_totals_sum_each_counter_across_the_range() -> None:
    query = _Query(
        {
            (_DAY, DemandCounter.FIT_SCORES): 30,
            (_DAY + timedelta(days=1), DemandCounter.FIT_SCORES): 12,
            (_DAY + timedelta(days=1), DemandCounter.RUNS_FAILED): 1,
        }
    )

    result = await _read(query, _DAY, _DAY + timedelta(days=1))

    assert result.totals[DemandCounter.FIT_SCORES] == 42
    assert result.totals[DemandCounter.RUNS_FAILED] == 1
    assert result.totals[DemandCounter.SIGN_UPS] == 0
    assert set(result.totals) == set(DemandCounter)


async def test_the_range_is_inclusive_at_both_ends_and_is_what_the_query_is_asked() -> None:
    query = _Query(
        {
            (_DAY - timedelta(days=1), DemandCounter.SIGN_UPS): 9,
            (_DAY, DemandCounter.SIGN_UPS): 1,
            (_DAY + timedelta(days=1), DemandCounter.SIGN_UPS): 2,
            (_DAY + timedelta(days=2), DemandCounter.SIGN_UPS): 9,
        }
    )

    result = await _read(query, _DAY, _DAY + timedelta(days=1))

    assert query.ranges == [(_DAY, _DAY + timedelta(days=1))]
    assert result.totals[DemandCounter.SIGN_UPS] == 3


async def test_a_range_of_exactly_92_days_is_accepted() -> None:
    result = await _read(_Query(), _DAY, _DAY + timedelta(days=91))

    assert len(result.days) == 92


@pytest.mark.parametrize(
    ("from_day", "to_day"),
    [
        pytest.param(_DAY, _DAY + timedelta(days=92), id="93 days"),
        pytest.param(_DAY, _DAY + timedelta(days=400), id="far too long"),
        pytest.param(_DAY, _DAY - timedelta(days=1), id="from after to"),
    ],
)
async def test_a_bad_range_is_refused_and_the_query_never_runs(
    from_day: date, to_day: date
) -> None:
    query = _Query()

    with pytest.raises(InvalidCounterRange):
        await _read(query, from_day, to_day)

    assert query.ranges == []
