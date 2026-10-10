"""Shared seed and spy for the slice 4.1 role use-case tests (T9)."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, Role, UserId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.mapping.identity import (
    user as _user_mapping,  # noqa: F401
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)

_HASH = PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA")


class SpyUsers(SqlAlchemyUserRepository):
    """The real repository, recording which methods the use case called."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.calls: list[str] = []

    async def get(self, user_id: UserId) -> User:
        self.calls.append("get")
        return await super().get(user_id)

    async def get_for_update(self, user_id: UserId) -> User:
        self.calls.append("get_for_update")
        return await super().get_for_update(user_id)

    async def save(self, user: User) -> None:
        self.calls.append("save")
        await super().save(user)


async def seed_user(session: AsyncSession, clock: FixedClock, role: Role) -> UserId:
    """Commit a user holding `role` (seeds commit: 2.3's fa40793), loaded fresh afterwards."""
    users = SqlAlchemyUserRepository(session)
    user = User.register_with_password(
        users.next_identity(),
        EmailAddress.parse(f"{uuid4().hex[:12]}@example.com"),
        _HASH,
        clock.now(),
    )
    if role is not Role.USER:
        user.change_role(role, clock.now())
    user.release_events()
    await users.add(user)
    await session.commit()
    session.expunge_all()
    return user.id
