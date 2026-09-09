"""Persistence tests for `GuestSession` — the imperative mapping, `GuestSessionIdType` and
`SqlAlchemyGuestSessionRepository` against real PostgreSQL (T28, written **after**: this shape is
discovered against SQLAlchemy, not designed ahead of it — technical-plan.md's Test plan).

Every assertion below is stated from `docs/specs/intake-base-cv-upload/technical-plan.md`'s
persistence section and ADR-0007, not lifted from whatever a first run of the code produced.

Each test forces a real reload (`session.expunge_all()` before the second `get`/`find_by_token_hash`
call): without that, the ORM's identity map would happily hand back the very same Python object
`GuestSession.start()` built, which would prove nothing about `GuestSessionIdType` or the mapping —
only a genuine `SELECT` followed by `process_result_value` proves the round trip.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.errors import GuestSessionNotFound
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)


async def test_round_trip_preserves_value_object_types(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyGuestSessionRepository(session)
    started = GuestSession.start(
        id=repo.next_identity(), token_hash="a" * 64, at=clock.now(), ttl_hours=24
    )
    await repo.add(started)

    session.expunge_all()
    reloaded = await repo.get(started.id)

    assert isinstance(reloaded.id, GuestSessionId)
    assert reloaded.id == started.id
    assert isinstance(reloaded.token_hash, str)
    assert reloaded.token_hash == "a" * 64
    assert reloaded.created_at == started.created_at
    assert reloaded.expires_at == started.expires_at


async def test_round_trip_preserves_whole_second_timestamps(
    session: AsyncSession, clock: FixedClock
) -> None:
    """ADR-0007: the `Clock` port is whole-second by contract and the columns are `TIMESTAMP(0)`. A
    round trip through Postgres must never introduce a microsecond component — the failure mode this
    guards is an equality assertion that only fails on the day someone's test double stops
    truncating (CLAUDE.md)."""
    repo = SqlAlchemyGuestSessionRepository(session)
    started = GuestSession.start(
        id=repo.next_identity(), token_hash="b" * 64, at=clock.now(), ttl_hours=24
    )
    await repo.add(started)

    session.expunge_all()
    reloaded = await repo.get(started.id)

    assert reloaded.created_at.microsecond == 0
    assert reloaded.expires_at.microsecond == 0


async def test_get_raises_not_found_for_an_unknown_id(session: AsyncSession) -> None:
    repo = SqlAlchemyGuestSessionRepository(session)

    with pytest.raises(GuestSessionNotFound):
        await repo.get(repo.next_identity())


async def test_find_by_token_hash_returns_none_when_no_session_matches(
    session: AsyncSession,
) -> None:
    repo = SqlAlchemyGuestSessionRepository(session)

    assert await repo.find_by_token_hash("c" * 64) is None


async def test_find_by_token_hash_returns_the_matching_session(
    session: AsyncSession, clock: FixedClock
) -> None:
    repo = SqlAlchemyGuestSessionRepository(session)
    started = GuestSession.start(
        id=repo.next_identity(), token_hash="d" * 64, at=clock.now(), ttl_hours=24
    )
    await repo.add(started)

    session.expunge_all()
    found = await repo.find_by_token_hash("d" * 64)

    assert found is not None
    assert found.id == started.id


async def test_token_hash_is_unique(session: AsyncSession, clock: FixedClock) -> None:
    """`uq_identity_guest_session_token_hash` — the cookie lookup on every request depends on this
    being a real database constraint, not just an application-level convention (technical-plan.md's
    persistence table)."""
    repo = SqlAlchemyGuestSessionRepository(session)
    first = GuestSession.start(
        id=repo.next_identity(), token_hash="e" * 64, at=clock.now(), ttl_hours=24
    )
    await repo.add(first)

    duplicate = GuestSession.start(
        id=repo.next_identity(), token_hash="e" * 64, at=clock.now(), ttl_hours=24
    )
    session.add(duplicate)

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_expires_at_index_exists(session: AsyncSession) -> None:
    """Serves the 1.6 purge's `WHERE expires_at < now()` and `/health/ready`'s backlog count
    (technical-plan.md's "Data & migrations"). Asserted here so a future migration edit cannot drop
    it unnoticed until the purge job discovers a sequential scan the hard way."""
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'identity_guest_session' "
            "AND indexname = 'ix_identity_guest_session_expires_at'"
        )
    )
    assert result.scalar_one_or_none() == "ix_identity_guest_session_expires_at"
