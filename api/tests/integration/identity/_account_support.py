"""Shared seeds and spies for slice 4.2's application tests (T11): real repositories on
`tailorcraft_test`, seeds that commit (2.3's fa40793), every assertion scoped to ids the test made."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.login import Login
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, TokenHash, UserId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.persistence.repositories.identity.login import (
    SqlAlchemyLoginRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)

PHC = PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA")


class SpyUsers(SqlAlchemyUserRepository):
    """The real repository appending `users.<method>` to a log shared with `SpyLogins`."""

    def __init__(self, session: AsyncSession, log: list[str]) -> None:
        super().__init__(session)
        self.log = log

    async def get(self, user_id: UserId) -> User:
        self.log.append("users.get")
        return await super().get(user_id)

    async def get_for_update(self, user_id: UserId) -> User:
        self.log.append("users.get_for_update")
        return await super().get_for_update(user_id)

    async def save(self, user: User) -> None:
        self.log.append("users.save")
        await super().save(user)


class SpyLogins(SqlAlchemyLoginRepository):
    def __init__(self, session: AsyncSession, log: list[str]) -> None:
        super().__init__(session)
        self.log = log

    async def remove_all_for_user(self, user_id: UserId) -> int:
        self.log.append("logins.remove_all_for_user")
        return await super().remove_all_for_user(user_id)

    async def add(self, login: Login) -> None:
        self.log.append("logins.add")
        await super().add(login)


def token_hash(seed: str) -> TokenHash:
    return TokenHash(value=hashlib.sha256(seed.encode()).hexdigest())


async def seed_user(
    session: AsyncSession,
    clock: FixedClock,
    *,
    email: str | None = None,
    disabled: bool = False,
    logins: int = 0,
) -> UserId:
    """Commit a user (optionally disabled, optionally holding `logins` logins), then forget it."""
    users = SqlAlchemyUserRepository(session)
    user = User.register_with_password(
        users.next_identity(),
        EmailAddress.parse(email or f"{uuid4().hex[:12]}@example.com"),
        PHC,
        clock.now(),
    )
    if disabled:
        user.disable(clock.now() + timedelta(seconds=1), 0)
    user.release_events()
    await users.add(user)
    repo = SqlAlchemyLoginRepository(session)
    for _ in range(logins):
        login = Login.start(
            repo.next_identity(), user.id, token_hash(uuid4().hex), clock.now(), timedelta(days=30)
        )
        login.release_events()
        await repo.add(login)
    await session.commit()
    session.expunge_all()
    return user.id


async def login_count(session: AsyncSession, user_id: UserId) -> int:
    return int(
        (
            await session.execute(
                text("SELECT count(*) FROM identity_login WHERE user_id = :u"),
                {"u": user_id.value},
            )
        ).scalar_one()
    )


async def stored_disabled_at(session: AsyncSession, user_id: UserId) -> object:
    return (
        await session.execute(
            text("SELECT disabled_at FROM identity_user WHERE id = :u"), {"u": user_id.value}
        )
    ).scalar_one()
