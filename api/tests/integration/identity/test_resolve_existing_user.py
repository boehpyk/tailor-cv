"""Application tests for `resolve_existing_user` (T9, RED, slice 2.2, AC-8).

The user-side sibling of `resolve_active_guest_session` (1.1/1.4): the one "does this id still
resolve" step every use case acting for a registered user starts with. Two behaviours, mirroring
that helper's own test coverage — a live user round-trips, a gone one raises `UserNotFound` rather
than returning `None`, since every caller already believes the id it is handing in is real (a
still-valid access token, resolved by `require_user`, for an account that may since have been
erased).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tailorcraft.application.identity.resolve_existing_user import resolve_existing_user
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, UserId
from tests.integration.fakes import FakeUserRepository

_HASH = PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZWhhc2g")


async def _seed_user(users: FakeUserRepository, *, email: str = "alex@example.com") -> User:
    user = User.register_with_password(
        users.next_identity(),
        EmailAddress.parse(email),
        _HASH,
        at=datetime(2026, 9, 4, 0, 0, 0, tzinfo=UTC),
    )
    user.release_events()
    await users.add(user)
    return user


async def test_a_live_user_round_trips_by_id() -> None:
    users = FakeUserRepository()
    user = await _seed_user(users)

    resolved = await resolve_existing_user(users, user.id)

    assert resolved.id == user.id
    assert resolved.email == user.email


async def test_a_gone_user_raises_user_not_found() -> None:
    """A still-valid access token for an account erased within its 15-minute lifetime (S-2 and its
    siblings across every user use case): the defense-in-depth this helper exists for."""
    users = FakeUserRepository()  # empty: no such user
    gone_user_id = UserId(value=uuid4())

    with pytest.raises(UserNotFound):
        await resolve_existing_user(users, gone_user_id)
