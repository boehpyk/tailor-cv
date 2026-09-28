"""Application tests for `resolve_owner` (slice 2.3, T10 RED, AC-7, H-9).

`resolve_owner(sessions, users, clock, requester) -> Owner` is the one `match` every requester-generic
use case starts with: a `GuestOwner` resolves the **active** session, a `UserOwner` the **existing**
user, and the variant comes back rebuilt from the resolved row. Each refusal is asserted by **exact
type** (`type(exc) is X`), because `NotImplementedError` subclasses `RuntimeError` and a loose
`pytest.raises` on a broad base would pass against a skeleton.

The guest arm is 1.1's `resolve_active_guest_session`, already in place, so its three tests are
green on arrival — kept as regression guards for the generalization. The user arm is the RED.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tailorcraft.application.identity.resolve_owner import resolve_owner
from tailorcraft.domain.identity.errors import (
    GuestSessionExpired,
    GuestSessionNotFound,
    UserNotFound,
)
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.infrastructure.clock import FixedClock
from tests.integration.fakes import (
    FakeGuestSessionRepository,
    FakeUserRepository,
    create_active_session,
)
from tests.integration.owners import seed_expired_session, seed_user

_REGISTERED_AT = datetime(2026, 9, 1, tzinfo=UTC)


async def test_an_active_guest_session_resolves_to_its_guest_owner(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    session = await create_active_session(sessions, clock)

    owner = await resolve_owner(sessions, FakeUserRepository(), clock, GuestOwner(session.id))

    assert owner == GuestOwner(session.id)


async def test_an_unknown_guest_session_raises_guest_session_not_found(clock: FixedClock) -> None:
    with pytest.raises(GuestSessionNotFound) as exc_info:
        await resolve_owner(
            FakeGuestSessionRepository(),
            FakeUserRepository(),
            clock,
            GuestOwner(GuestSessionId(value=uuid4())),
        )

    assert type(exc_info.value) is GuestSessionNotFound


async def test_an_expired_guest_session_raises_guest_session_expired(clock: FixedClock) -> None:
    sessions = FakeGuestSessionRepository()
    expired = await seed_expired_session(sessions, "expired", clock.now())

    with pytest.raises(GuestSessionExpired) as exc_info:
        await resolve_owner(sessions, FakeUserRepository(), clock, GuestOwner(expired.id))

    assert type(exc_info.value) is GuestSessionExpired


async def test_an_existing_user_resolves_to_its_user_owner(clock: FixedClock) -> None:
    users = FakeUserRepository()
    requester = await seed_user(users, "alex@example.com", _REGISTERED_AT)

    owner = await resolve_owner(FakeGuestSessionRepository(), users, clock, requester)

    assert owner == UserOwner(requester.user_id)
    assert type(owner) is UserOwner


async def test_an_erased_user_raises_user_not_found(clock: FixedClock) -> None:
    """H-9: a still-valid bearer for an account erased within the token's 15 minutes. The other
    user in the repository is the discriminating positive — the arm must look the id up, not merely
    notice that *some* user exists."""
    users = FakeUserRepository()
    await seed_user(users, "someone-else@example.com", _REGISTERED_AT)

    with pytest.raises(UserNotFound) as exc_info:
        await resolve_owner(
            FakeGuestSessionRepository(), users, clock, UserOwner(UserId(value=uuid4()))
        )

    assert type(exc_info.value) is UserNotFound


async def test_a_user_requester_never_consults_the_guest_sessions(clock: FixedClock) -> None:
    """The arms are disjoint: a `UserOwner` resolves against `users` alone, so an empty session
    repository (which would raise `GuestSessionNotFound` if touched) changes nothing."""
    users = FakeUserRepository()
    requester = await seed_user(users, "alex@example.com", _REGISTERED_AT)
    sessions = FakeGuestSessionRepository()  # empty

    owner = await resolve_owner(sessions, users, clock, requester)

    assert owner == requester
