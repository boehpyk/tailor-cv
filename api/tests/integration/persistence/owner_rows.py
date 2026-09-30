"""Persisted owners for slice 2.3's persistence tests (T17): a guest session and a registered user,
each written through its own repository so the FKs every owned row carries are satisfied for real.
"""

from __future__ import annotations

import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)

_PASSWORD_HASH = PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA")


async def persist_guest(session: AsyncSession, clock: FixedClock) -> GuestOwner:
    sessions = SqlAlchemyGuestSessionRepository(session)
    guest = GuestSession.start(
        id=sessions.next_identity(), token_hash=secrets.token_hex(32), at=clock.now(), ttl_hours=24
    )
    await sessions.add(guest)
    await session.flush()
    return GuestOwner(guest.id)


async def persist_user(session: AsyncSession, clock: FixedClock) -> UserOwner:
    users = SqlAlchemyUserRepository(session)
    user_id = users.next_identity()
    user = User.register_with_password(
        id=user_id,
        email=EmailAddress.parse(f"owner-{user_id.value}@example.com"),
        password_hash=_PASSWORD_HASH,
        at=clock.now(),
    )
    user.release_events()
    await users.add(user)
    await session.flush()
    return UserOwner(user_id)
