"""The `Clock` port's whole-second contract (ADR-0007).

This looks like a fussy rule until the first time it is violated. Timestamps round-trip through
`TIMESTAMP WITH TIME ZONE` and JSON; a value carrying microseconds on the way in and not on the way
out turns every equality assertion into a coin flip. Truncating at the source means the value the
domain holds is already the value the database will return.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tailorcraft.infrastructure.clock import FixedClock, SystemClock


def test_system_clock_returns_whole_seconds() -> None:
    assert SystemClock().now().microsecond == 0


def test_system_clock_returns_an_aware_utc_instant() -> None:
    """A naive datetime is the other half of the same bug: it compares wrong and stores wrong."""
    now = SystemClock().now()

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_fixed_clock_does_not_move() -> None:
    at = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    clock = FixedClock(at)

    assert clock.now() == at
    assert clock.now() == at


def test_fixed_clock_refuses_a_microsecond_instant() -> None:
    """A test double must obey the contract production obeys.

    If `FixedClock` accepted microseconds, the truncation rule would be discovered from a mystifying
    equality failure weeks later instead of from this line.
    """
    with pytest.raises(ValueError, match="whole-second"):
        FixedClock(datetime(2026, 9, 4, 12, 0, 0, 123456, tzinfo=UTC))


def test_fixed_clock_refuses_a_naive_instant() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 9, 4, 12, 0, 0))


def test_advancing_a_fixed_clock_keeps_whole_seconds() -> None:
    clock = FixedClock(datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC))

    clock.advance(seconds=90)

    assert clock.now() == datetime(2026, 9, 4, 12, 1, 30, tzinfo=UTC)
