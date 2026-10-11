"""Application tests for `EnableAccount` (slice 4.2, T11 RED, AC-8 (a)(b)(c))."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.identity.enable_account import EnableAccount
from tailorcraft.application.identity.results import AccountStatusChange
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.events import UserEnabled
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import RecordingEventPublisher
from tests.integration.identity._account_support import (
    SpyUsers,
    login_count,
    seed_user,
    stored_disabled_at,
)


async def _attempt(use_case: EnableAccount, target: UserId) -> AccountStatusChange | Exception:
    try:
        return await use_case(target)
    except Exception as exc:
        return exc


async def test_enabling_a_disabled_user_clears_it_publishes_and_creates_no_login(
    session: AsyncSession, clock: FixedClock
) -> None:
    target = await seed_user(session, clock, disabled=True)
    log: list[str] = []
    events = RecordingEventPublisher()
    clock.advance(5)

    outcome = await _attempt(EnableAccount(SpyUsers(session, log), clock, events), target)

    assert outcome == AccountStatusChange(target, None, True, 0)
    assert log[0] == "users.get_for_update"
    assert "users.save" in log
    assert await stored_disabled_at(session, target) is None
    assert await login_count(session, target) == 0, "an enable restores no session"
    assert len(events.published) == 1
    event = events.published[0]
    assert isinstance(event, UserEnabled)
    assert (event.user_id, event.occurred_at) == (target, clock.now())


async def test_enabling_an_enabled_user_changes_nothing_and_records_nothing(
    session: AsyncSession, clock: FixedClock
) -> None:
    target = await seed_user(session, clock)
    log: list[str] = []
    events = RecordingEventPublisher()

    outcome = await _attempt(EnableAccount(SpyUsers(session, log), clock, events), target)

    assert outcome == AccountStatusChange(target, None, False, 0)
    assert "users.save" not in log
    assert events.published == []


async def test_an_unknown_id_raises_user_not_found_and_records_nothing(
    session: AsyncSession, clock: FixedClock
) -> None:
    log: list[str] = []
    events = RecordingEventPublisher()

    outcome = await _attempt(
        EnableAccount(SpyUsers(session, log), clock, events), UserId(value=uuid4())
    )

    assert type(outcome) is UserNotFound
    assert "users.save" not in log
    assert events.published == []
