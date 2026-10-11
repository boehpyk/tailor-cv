"""Application tests for `GetAccountOverview` (slice 4.2, T11 RED, AC-11)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.retention.get_account_overview import GetAccountOverview
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.persistence.retention.account_data import SqlAlchemyAccountData
from tests.integration.identity._account_support import seed_user


def _use_case(session: AsyncSession) -> GetAccountOverview:
    return GetAccountOverview(SqlAlchemyUserRepository(session), SqlAlchemyAccountData(session))


@contextmanager
def _captured(session: AsyncSession) -> Iterator[list[str]]:
    statements: list[str] = []
    engine = session.get_bind()

    def capture(conn: Connection, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


async def test_it_returns_the_user_and_the_seven_counts(
    session: AsyncSession, clock: FixedClock
) -> None:
    target = await seed_user(session, clock, logins=2)
    other = await seed_user(session, clock, logins=5)

    overview = await _use_case(session)(target)

    assert overview.user.id == target
    assert overview.counts.logins == 2, f"{other} must not leak into this count"
    assert overview.counts.base_cvs == 0
    assert overview.counts.files == 0
    assert overview.counts.tailoring_runs == 0
    assert overview.counts.job_postings == 0
    assert overview.counts.export_jobs == 0
    assert overview.counts.tracked_applications == 0


async def test_it_shows_a_disabled_user_as_disabled(
    session: AsyncSession, clock: FixedClock
) -> None:
    target = await seed_user(session, clock, disabled=True)

    overview = await _use_case(session)(target)

    assert overview.user.is_disabled is True


async def test_an_unknown_id_raises_user_not_found(session: AsyncSession) -> None:
    with pytest.raises(UserNotFound):
        await _use_case(session)(UserId(value=uuid4()))


async def test_it_takes_no_row_lock(session: AsyncSession, clock: FixedClock) -> None:
    target = await seed_user(session, clock, logins=1)

    with _captured(session) as statements:
        await _use_case(session)(target)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert selects, "premise: the use case ran statements"
    locking = [s for s in selects if re.search(r"\bFOR (NO KEY )?(UPDATE|SHARE|KEY SHARE)\b", s)]
    assert locking == []
