"""Adapters for the `Clock` port."""

from __future__ import annotations

from datetime import UTC, datetime

from tailorcraft.domain.shared.clock import Clock


class SystemClock(Clock):
    """The real clock, truncated to whole seconds at the source.

    Truncating *here* rather than at the database boundary is the whole point: it means the value
    the domain sees is already the value PostgreSQL will hand back. Truncate later and you get an
    aggregate whose in-memory timestamp and persisted timestamp differ by microseconds — which
    passes every test that never reloads the row, and fails the one that does (ADR-0007).
    """

    def now(self) -> datetime:
        return datetime.now(UTC).replace(microsecond=0)


class FixedClock(Clock):
    """A clock that does not move, for tests.

    It **asserts** the whole-second contract rather than trusting the caller. A test double that
    quietly returns microseconds is how the production truncation rule gets discovered from a
    failing equality assertion instead of from this docstring.
    """

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock needs a timezone-aware instant")
        if at.microsecond:
            raise ValueError(
                "the Clock port is whole-second by contract (ADR-0007); "
                "pass an instant with microsecond=0"
            )
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, seconds: int) -> None:
        """Move the clock forward. Whole seconds only — the contract applies to test doubles too."""
        from datetime import timedelta

        self._at = self._at + timedelta(seconds=seconds)
