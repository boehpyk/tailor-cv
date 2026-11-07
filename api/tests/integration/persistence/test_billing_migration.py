"""Migration `9d2cb5b8ec5f` (slice 5.2, T20, test-after): the backfill and the refusing downgrade.

The rest of the migration's catalogue (each CHECK and unique index refusing, the offline render, backfill
idempotence, AC-2/AC-28) is T30's. Plain `def` tests: Alembic's `env.py` runs its own `asyncio.run`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tailorcraft.infrastructure.settings import Settings

_REVISION: Final = "9d2cb5b8ec5f"
_DOWN_REVISION: Final = "8ddd7ee1ca93"
_NOW: Final = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
_PHC: Final = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"


def _test_url(settings: Settings) -> str:
    url = settings.test_database_url
    assert "_test" in url, f"refusing to write committed rows to {url!r}"
    return url


def _config(url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    return config


def _head() -> str | None:
    return ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()


async def _rows(url: str, sql: str, **params: object) -> list[Any]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(text(sql), params)).all())
    finally:
        await engine.dispose()


async def _commit(url: str, work: Callable[[AsyncSession], Awaitable[None]]) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await work(session)
            await session.commit()
    finally:
        await engine.dispose()


def _seed_user(user_id: UUID) -> Callable[[AsyncSession], Awaitable[None]]:
    async def seed(session: AsyncSession) -> None:
        await session.execute(
            text(
                "INSERT INTO identity_user (id, email, password_hash, created_at, "
                "password_updated_at) VALUES (:i, :e, :p, :t, :t)"
            ),
            {"i": user_id, "e": f"billing-{user_id}@example.com", "p": _PHC, "t": _NOW},
        )

    return seed


def _delete_user(user_id: UUID) -> Callable[[AsyncSession], Awaitable[None]]:
    async def clean(session: AsyncSession) -> None:
        await session.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})

    return clean


@pytest.mark.usefixtures("_migrated")
def test_the_upgrade_opens_one_account_with_one_existing_account_entry_per_user(
    settings: Settings,
) -> None:
    url = _test_url(settings)
    config = _config(url)
    user_id = uuid4()
    failure: BaseException | None = None
    try:
        command.downgrade(config, _DOWN_REVISION)
        asyncio.run(_commit(url, _seed_user(user_id)))
        command.upgrade(config, _REVISION)

        accounts = asyncio.run(
            _rows(url, "SELECT opened_at FROM billing_credit_account WHERE user_id = :u", u=user_id)
        )
        entries = asyncio.run(
            _rows(
                url,
                "SELECT kind, credits, tailoring_run_id, created_at, id FROM billing_credit_entry "
                "WHERE user_id = :u",
                u=user_id,
            )
        )
        assert len(accounts) == 1
        assert len(entries) == 1
        kind, credits, run, created_at, entry_id = entries[0]
        assert (kind, credits, run) == ("existing_account", 1, None)
        assert created_at == accounts[0].opened_at
        assert created_at.microsecond == 0, "whole-second, like every instant (ADR-0007)"
        assert UUID(str(entry_id)).version == 7
    except BaseException as exc:  # recovery below must run whatever happened
        failure = exc
    finally:
        command.upgrade(config, "head")
        asyncio.run(_commit(url, _delete_user(user_id)))
    if failure is not None:
        raise failure


@pytest.mark.usefixtures("_migrated")
def test_the_downgrade_refuses_while_a_grant_exists_and_deletes_nothing(
    settings: Settings,
) -> None:
    url = _test_url(settings)
    user_id = uuid4()
    grant_id = uuid4()

    async def seed(session: AsyncSession) -> None:
        await _seed_user(user_id)(session)
        await session.execute(
            text("INSERT INTO billing_credit_account (user_id, opened_at) VALUES (:u, :t)"),
            {"u": user_id, "t": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO billing_credit_entry (id, user_id, kind, credits, created_at) "
                "VALUES (:i, :u, 'grant', 3, :t)"
            ),
            {"i": grant_id, "u": user_id, "t": _NOW},
        )

    asyncio.run(_commit(url, seed))
    try:
        with pytest.raises(RuntimeError, match=f"refusing to downgrade {_REVISION}"):
            command.downgrade(_config(url), _DOWN_REVISION)
        assert _head() == asyncio.run(_rows(url, "SELECT version_num FROM alembic_version"))[0][0]
        assert asyncio.run(
            _rows(url, "SELECT 1 FROM billing_credit_entry WHERE id = :i", i=grant_id)
        )
    finally:
        asyncio.run(_commit(url, _delete_user(user_id)))
