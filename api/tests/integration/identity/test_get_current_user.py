"""Application tests for `GetCurrentUser` (T13, RED).

Covers AC-12 against an **in-memory `UserRepository` fake** (`tests/integration/fakes.py`): the
found path and I-39 (a valid access token whose user row is gone). No clock, no events — this use
case reads one row and nothing else, per its own skeleton docstring.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tailorcraft.application.identity.get_current_user import GetCurrentUser
from tailorcraft.domain.identity.errors import UserNotFound
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, UserId
from tests.integration.fakes import FakeUserRepository


async def test_returns_the_user_for_a_known_id() -> None:
    users = FakeUserRepository()
    user = User.register_with_password(
        users.next_identity(),
        EmailAddress.parse("alex@example.com"),
        PasswordHash(value="$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$b2xkaGFzaA"),
        at=datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC),
    )
    user.release_events()
    await users.add(user)
    use_case = GetCurrentUser(users)

    result = await use_case(user.id)

    assert result is user


async def test_raises_user_not_found_for_an_id_with_no_row() -> None:
    """I-39: a valid access token whose user row is gone."""
    users = FakeUserRepository()
    use_case = GetCurrentUser(users)

    with pytest.raises(UserNotFound):
        await use_case(UserId(value=uuid4()))
