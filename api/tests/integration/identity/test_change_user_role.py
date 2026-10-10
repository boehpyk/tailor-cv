"""Application tests for `ChangeUserRole` (slice 4.1, T9 RED -- AC-7 (a)-(d)).

Real `SqlAlchemyUserRepository`, a fixed clock and a recording event publisher. Exceptions are
captured with `_attempt` and asserted by exact type (`NotImplementedError` is a `RuntimeError`).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.identity.change_user_role import ChangeUserRole
from tailorcraft.application.identity.results import RoleChange
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.events import UserRoleChanged
from tailorcraft.domain.identity.value_objects import Role, UserId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import RecordingEventPublisher
from tests.integration.identity._role_support import SpyUsers, seed_user


async def _attempt(
    use_case: ChangeUserRole, user_id: UserId, to: Role, *, dry_run: bool
) -> RoleChange | Exception:
    try:
        return await use_case(user_id, to, dry_run=dry_run)
    except Exception as exc:
        return exc


async def _stored_role(session: AsyncSession, user_id: UserId) -> Role:
    session.expunge_all()
    return (await SpyUsers(session).get(user_id)).role


@pytest.mark.parametrize(
    ("start", "target"), [(Role.USER, Role.ADMIN), (Role.ADMIN, Role.USER)], ids=["grant", "revoke"]
)
async def test_a_real_change_locks_saves_and_publishes_the_event(
    session: AsyncSession, clock: FixedClock, start: Role, target: Role
) -> None:
    user_id = await seed_user(session, clock, start)
    users, events = SpyUsers(session), RecordingEventPublisher()
    clock.advance(5)

    outcome = await _attempt(ChangeUserRole(users, clock, events), user_id, target, dry_run=False)

    assert outcome == RoleChange(user_id, start, target, True)
    assert users.calls[0] == "get_for_update"
    assert "save" in users.calls
    assert len(events.published) == 1
    event = events.published[0]
    assert isinstance(event, UserRoleChanged)
    assert (event.user_id, event.from_role, event.to_role) == (user_id, start, target)
    assert await _stored_role(session, user_id) is target


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
async def test_the_role_it_already_has_changes_nothing_and_records_nothing(
    session: AsyncSession, clock: FixedClock, role: Role
) -> None:
    user_id = await seed_user(session, clock, role)
    users, events = SpyUsers(session), RecordingEventPublisher()

    outcome = await _attempt(ChangeUserRole(users, clock, events), user_id, role, dry_run=False)

    assert outcome == RoleChange(user_id, role, role, False)
    assert "save" not in users.calls
    assert events.published == []


@pytest.mark.parametrize(
    ("start", "target", "would_change"),
    [(Role.USER, Role.ADMIN, True), (Role.ADMIN, Role.USER, True), (Role.USER, Role.USER, False)],
    ids=["grant", "revoke", "no-op"],
)
async def test_a_dry_run_reports_what_a_real_run_would_do_and_writes_nothing(
    session: AsyncSession, clock: FixedClock, start: Role, target: Role, would_change: bool
) -> None:
    user_id = await seed_user(session, clock, start)
    users, events = SpyUsers(session), RecordingEventPublisher()

    outcome = await _attempt(ChangeUserRole(users, clock, events), user_id, target, dry_run=True)

    assert outcome == RoleChange(user_id, start, target, would_change)
    assert "save" not in users.calls
    assert events.published == []
    assert await _stored_role(session, user_id) is start


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry"])
async def test_an_unknown_id_raises_user_not_found(
    session: AsyncSession, clock: FixedClock, dry_run: bool
) -> None:
    users, events = SpyUsers(session), RecordingEventPublisher()

    outcome = await _attempt(
        ChangeUserRole(users, clock, events), UserId(value=uuid4()), Role.ADMIN, dry_run=dry_run
    )

    assert type(outcome) is UserNotFound
    assert "save" not in users.calls
    assert events.published == []
