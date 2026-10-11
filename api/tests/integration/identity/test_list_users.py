"""Application tests for `ListUsers` (slice 4.2, T11 RED, AC-10).

The real directory query on `tailorcraft_test`; every search is scoped by a per-test token so users
other tests left behind cannot change an assertion.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.identity.list_users import ListUsers
from tailorcraft.domain.identity.user_directory import (
    PAGE_SIZE_MAX,
    EmailSearch,
    UserDirectoryCursor,
    UserDirectoryEntry,
)
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.queries.user_directory import (
    SqlAlchemyUserDirectoryQuery,
)
from tests.integration.identity._account_support import seed_user


def _search(raw: str) -> EmailSearch:
    parsed = EmailSearch.parse(raw)
    assert parsed is not None
    return parsed


async def test_a_page_is_newest_first_and_the_cursor_is_the_last_entry_or_none(
    session: AsyncSession, clock: FixedClock
) -> None:
    token = uuid4().hex
    ids = []
    for i in range(3):
        ids.append(await seed_user(session, clock, email=f"{token}-{i}@example.com"))
        clock.advance(10)
    use_case = ListUsers(SqlAlchemyUserDirectoryQuery(session))

    first = await use_case(_search(token), None, 2)
    assert [e.id for e in first.entries] == [ids[2], ids[1]]
    assert first.next_cursor == UserDirectoryCursor(first.entries[-1].created_at, ids[1])

    last = await use_case(_search(token), first.next_cursor, 2)
    assert [e.id for e in last.entries] == [ids[0]]
    assert last.next_cursor is None


async def test_a_search_returns_only_emails_containing_it(
    session: AsyncSession, clock: FixedClock
) -> None:
    token = uuid4().hex
    hit = await seed_user(session, clock, email=f"x.{token}@example.com")
    await seed_user(session, clock, email=f"{uuid4().hex}@example.com")

    page = await ListUsers(SqlAlchemyUserDirectoryQuery(session))(_search(token), None, 20)

    assert [e.id for e in page.entries] == [hit]


async def test_percent_and_underscore_in_a_search_match_only_themselves(
    session: AsyncSession, clock: FixedClock
) -> None:
    token = uuid4().hex
    literal = await seed_user(session, clock, email=f"{token}a%b_c@example.com")
    clock.advance(1)
    await seed_user(session, clock, email=f"{token}aXbYc@example.com")
    use_case = ListUsers(SqlAlchemyUserDirectoryQuery(session))

    page = await use_case(_search(f"{token}a%b_c"), None, 20)

    assert [e.id for e in page.entries] == [literal]


async def test_no_entry_carries_a_password_hash(session: AsyncSession, clock: FixedClock) -> None:
    token = uuid4().hex
    await seed_user(session, clock, email=f"{token}@example.com")

    page = await ListUsers(SqlAlchemyUserDirectoryQuery(session))(_search(token), None, 20)

    assert len(page.entries) == 1
    fields = {f.name for f in dataclasses.fields(UserDirectoryEntry)}
    assert fields == {"id", "email", "role", "created_at", "disabled_at"}
    assert "password" not in repr(page.entries[0]).lower()


@pytest.mark.parametrize("limit", [1, 20, PAGE_SIZE_MAX])
async def test_a_limit_inside_the_bounds_is_accepted(session: AsyncSession, limit: int) -> None:
    page = await ListUsers(SqlAlchemyUserDirectoryQuery(session))(None, None, limit)

    assert len(page.entries) <= limit


@pytest.mark.parametrize("limit", [0, -1, PAGE_SIZE_MAX + 1])
async def test_a_limit_outside_the_bounds_raises_invariant_violated(
    session: AsyncSession, limit: int
) -> None:
    with pytest.raises(InvariantViolated):
        await ListUsers(SqlAlchemyUserDirectoryQuery(session))(None, None, limit)


async def test_an_entry_shows_the_disabled_state(session: AsyncSession, clock: FixedClock) -> None:
    token = uuid4().hex
    await seed_user(session, clock, email=f"{token}-on@example.com", disabled=True)
    await seed_user(session, clock, email=f"{token}-off@example.com")

    page = await ListUsers(SqlAlchemyUserDirectoryQuery(session))(_search(token), None, 20)

    by_email = {e.email.value: e for e in page.entries}
    assert by_email[f"{token}-on@example.com"].disabled_at == clock.now() + timedelta(seconds=1)
    assert by_email[f"{token}-off@example.com"].disabled_at is None
