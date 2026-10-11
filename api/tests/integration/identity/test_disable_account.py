"""Application tests for `DisableAccount` (slice 4.2, T11 RED, AC-7 (a)(b)(c)).

Real repositories on `tailorcraft_test`, a fixed clock and a recording publisher. Exceptions are
captured and asserted by exact type (`NotImplementedError` is a `RuntimeError`).
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.application.identity.disable_account import DisableAccount
from tailorcraft.application.identity.results import AccountStatusChange
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.events import UserDisabled
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import RecordingEventPublisher
from tests.integration.identity._account_support import (
    SpyLogins,
    SpyUsers,
    login_count,
    seed_user,
    stored_disabled_at,
)


async def _attempt(use_case: DisableAccount, target: UserId) -> AccountStatusChange | Exception:
    try:
        return await use_case(target)
    except Exception as exc:
        return exc


def _build(session: AsyncSession, clock: FixedClock) -> tuple[DisableAccount, list[str], object]:
    log: list[str] = []
    events = RecordingEventPublisher()
    return (
        DisableAccount(SpyUsers(session, log), SpyLogins(session, log), clock, events),
        log,
        events,
    )


async def test_disabling_an_enabled_user_locks_first_revokes_every_login_and_publishes(
    session: AsyncSession, clock: FixedClock
) -> None:
    target = await seed_user(session, clock, logins=3)
    other = await seed_user(session, clock, logins=2)
    use_case, log, events = _build(session, clock)
    assert isinstance(events, RecordingEventPublisher)
    clock.advance(5)

    outcome = await _attempt(use_case, target)

    assert outcome == AccountStatusChange(target, clock.now(), True, 3)
    assert log[0] == "users.get_for_update", "the user row is locked before anything else"
    assert log.index("users.get_for_update") < log.index("logins.remove_all_for_user")
    assert log.index("logins.remove_all_for_user") < log.index("users.save")
    assert await login_count(session, target) == 0
    assert await login_count(session, other) == 2, "another user's logins are not touched"
    assert await stored_disabled_at(session, target) == clock.now()
    assert len(events.published) == 1
    event = events.published[0]
    assert isinstance(event, UserDisabled)
    assert (event.user_id, event.logins_revoked, event.occurred_at) == (target, 3, clock.now())


async def test_disabling_a_user_with_no_logins_reports_zero_revoked(
    session: AsyncSession, clock: FixedClock
) -> None:
    target = await seed_user(session, clock)
    use_case, _, _ = _build(session, clock)

    outcome = await _attempt(use_case, target)

    assert isinstance(outcome, AccountStatusChange)
    assert (outcome.changed, outcome.logins_revoked) == (True, 0)


async def test_disabling_a_disabled_user_changes_nothing_and_records_nothing(
    session: AsyncSession, clock: FixedClock
) -> None:
    target = await seed_user(session, clock, disabled=True)
    original = await stored_disabled_at(session, target)
    use_case, log, events = _build(session, clock)
    assert isinstance(events, RecordingEventPublisher)
    clock.advance(60)

    outcome = await _attempt(use_case, target)

    assert outcome == AccountStatusChange(target, original, False, 0)  # type: ignore[arg-type]
    assert "users.save" not in log
    assert events.published == []
    assert await stored_disabled_at(session, target) == original


async def test_disabling_a_disabled_user_still_deletes_a_login_found_for_it(
    session: AsyncSession, clock: FixedClock
) -> None:
    """A login of a disabled user should not exist; if one does, the cure is still deletion."""
    target = await seed_user(session, clock, disabled=True, logins=2)
    use_case, log, events = _build(session, clock)
    assert isinstance(events, RecordingEventPublisher)

    outcome = await _attempt(use_case, target)

    assert isinstance(outcome, AccountStatusChange)
    assert outcome.changed is False
    assert await login_count(session, target) == 0
    assert "users.save" not in log
    assert events.published == []


async def test_an_unknown_id_raises_user_not_found_and_records_nothing(
    session: AsyncSession, clock: FixedClock
) -> None:
    use_case, log, events = _build(session, clock)
    assert isinstance(events, RecordingEventPublisher)

    outcome = await _attempt(use_case, UserId(value=uuid4()))

    assert type(outcome) is UserNotFound
    assert "users.save" not in log
    assert "logins.remove_all_for_user" not in log
    assert events.published == []
