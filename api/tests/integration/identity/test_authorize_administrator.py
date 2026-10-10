"""Application tests for `AuthorizeAdministrator` (slice 4.1, T9 RED -- AC-6).

Real `SqlAlchemyUserRepository` over the rolled-back test database. `NotImplementedError` subclasses
`RuntimeError`, so failures are captured with `_attempt` and asserted by exact type.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.identity.authorize_administrator import AuthorizeAdministrator
from tailorcraft.domain.identity.errors import NotAnAdministrator, UserNotFound
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import Role, UserId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.identity._role_support import SpyUsers, seed_user


async def _attempt(use_case: AuthorizeAdministrator, user_id: UserId) -> User | Exception:
    try:
        return await use_case(user_id)
    except Exception as exc:
        return exc


async def test_an_administrator_is_returned(session: AsyncSession, clock: FixedClock) -> None:
    user_id = await seed_user(session, clock, Role.ADMIN)

    outcome = await _attempt(AuthorizeAdministrator(SpyUsers(session)), user_id)

    assert isinstance(outcome, User)
    assert outcome.id == user_id
    assert outcome.is_admin


async def test_a_plain_user_is_refused_with_not_an_administrator(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await seed_user(session, clock, Role.USER)

    outcome = await _attempt(AuthorizeAdministrator(SpyUsers(session)), user_id)

    assert type(outcome) is NotAnAdministrator


async def test_a_missing_user_propagates_user_not_found(session: AsyncSession) -> None:
    outcome = await _attempt(AuthorizeAdministrator(SpyUsers(session)), UserId(value=uuid4()))

    assert type(outcome) is UserNotFound


async def test_it_reads_through_get_only_and_writes_and_records_nothing(
    session: AsyncSession, clock: FixedClock
) -> None:
    user_id = await seed_user(session, clock, Role.ADMIN)
    users = SpyUsers(session)

    outcome = await _attempt(AuthorizeAdministrator(users), user_id)

    assert isinstance(outcome, User)
    assert users.calls == ["get"]
    assert outcome.release_events() == ()
