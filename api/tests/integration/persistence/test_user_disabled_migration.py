"""AC-13, AC-14 and AC-16's storage half: migration `669b75917559` and `identity_user.disabled_at`
(slice 4.2, T6, test-after).

Same shape as `test_user_role_migration.py`: catalogue facts by name, committed seeds removed
afterwards, recovery to head unconditional, "at head" via `get_current_head()`. Plain `def` for the
tests that run Alembic (its `env.py` runs its own `asyncio.run`).

`is_disabled` is asserted to **bypass the identity map**: a `User` loaded before a concurrent disable
committed stays stale, and the repository's answer must not.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, UserId
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.settings import Settings

_REVISION: Final = "669b75917559"
_DOWN_REVISION: Final = "3d26e05edffe"
_NOW: Final = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)
_LATER: Final = datetime(2026, 10, 11, 13, 0, 0, tzinfo=UTC)
_PHC: Final = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"

# What the previous image does during the deploy window: no `disabled_at` in the statement.
_OLD_SHAPE_INSERT: Final = (
    "INSERT INTO identity_user (id, email, password_hash, created_at, password_updated_at, role) "
    "VALUES (:i, :e, :p, :t, :t, 'user')"
)


def _test_url(settings: Settings) -> str:
    url = settings.test_database_url
    assert "_test" in url, f"refusing to write committed rows to {url!r}"
    return url


def _config(url: str, *, output_buffer: io.StringIO | None = None) -> Config:
    config = Config("alembic.ini", output_buffer=output_buffer)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _head() -> str | None:
    return ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()


async def _query(url: str, sql: str, **params: object) -> list[Any]:
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


def _column_exists(url: str) -> bool:
    return bool(
        asyncio.run(
            _query(
                url,
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'identity_user' AND column_name = 'disabled_at'",
            )
        )
    )


def _revision(url: str) -> str:
    return str(asyncio.run(_query(url, "SELECT version_num FROM alembic_version"))[0][0])


def _email(user_id: UUID) -> str:
    return f"disabled-migration-{user_id}@example.com"


def _insert(url: str, user_id: UUID, disabled_at: datetime | None) -> None:
    async def seed(session: AsyncSession) -> None:
        await session.execute(
            text(_OLD_SHAPE_INSERT), {"i": user_id, "e": _email(user_id), "p": _PHC, "t": _NOW}
        )
        if disabled_at is not None:
            await session.execute(
                text("UPDATE identity_user SET disabled_at = :d WHERE id = :i"),
                {"d": disabled_at, "i": user_id},
            )

    asyncio.run(_commit(url, seed))


def _set_disabled(url: str, user_id: UUID, disabled_at: datetime | None) -> None:
    async def work(session: AsyncSession) -> None:
        await session.execute(
            text("UPDATE identity_user SET disabled_at = :d WHERE id = :i"),
            {"d": disabled_at, "i": user_id},
        )

    asyncio.run(_commit(url, work))


def _delete_user(url: str, user_id: UUID) -> None:
    async def clean(session: AsyncSession) -> None:
        await session.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})

    asyncio.run(_commit(url, clean))


def _recover(config: Config, url: str) -> None:
    if _revision(url) != _head():
        command.upgrade(config, "head")


# --- AC-13 ---------------------------------------------------------------------------------------


async def test_ac13a_the_column_is_a_nullable_timestamptz_precision_0_with_no_default_and_no_index(
    session: AsyncSession,
) -> None:
    row = (
        await session.execute(
            text(
                "SELECT data_type, datetime_precision, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'identity_user' "
                "AND column_name = 'disabled_at'"
            )
        )
    ).one()
    assert (row.data_type, row.datetime_precision, row.is_nullable) == (
        "timestamp with time zone",
        0,
        "YES",
    )
    assert row.column_default is None

    indexed = (
        await session.execute(
            text(
                "SELECT i.indexrelid::regclass::text AS name FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = 'identity_user'::regclass AND a.attname = 'disabled_at'"
            )
        )
    ).all()
    assert indexed == [], f"disabled_at must carry no index (OQ-9), found {indexed}"


@pytest.mark.usefixtures("_migrated")
def test_ac13b_rows_written_before_the_upgrade_read_null_afterwards(settings: Settings) -> None:
    url = _test_url(settings)
    config = _config(url)
    user_id = uuid4()
    failure: BaseException | None = None
    try:
        command.downgrade(config, _DOWN_REVISION)
        assert not _column_exists(url)
        _insert(url, user_id, None)
        command.upgrade(config, "head")
        rows = asyncio.run(
            _query(url, "SELECT disabled_at FROM identity_user WHERE id = :i", i=user_id)
        )
        assert [r.disabled_at for r in rows] == [None]
    except BaseException as exc:
        failure = exc
    finally:
        _recover(config, url)
        _delete_user(url, user_id)
    if failure is not None:
        raise failure


@pytest.mark.usefixtures("_migrated")
def test_ac13c_up_down_up_round_trips_on_a_table_with_no_disabled_row(settings: Settings) -> None:
    url = _test_url(settings)
    config = _config(url)
    user_id = uuid4()
    _insert(url, user_id, None)
    failure: BaseException | None = None
    try:
        assert _column_exists(url)
        command.downgrade(config, _DOWN_REVISION)
        assert not _column_exists(url), "the downgrade must drop the column"
        assert _revision(url) == _DOWN_REVISION
        assert asyncio.run(_query(url, "SELECT 1 FROM identity_user WHERE id = :i", i=user_id)), (
            "a clean downgrade deletes no user"
        )
        command.upgrade(config, "head")
        assert _column_exists(url)
        rows = asyncio.run(
            _query(url, "SELECT disabled_at FROM identity_user WHERE id = :i", i=user_id)
        )
        assert [r.disabled_at for r in rows] == [None]
    except BaseException as exc:
        failure = exc
    finally:
        _recover(config, url)
        _delete_user(url, user_id)
    if failure is not None:
        raise failure


@pytest.mark.usefixtures("_migrated")
def test_ac13d_the_downgrade_refuses_while_an_account_is_disabled_naming_enable_and_no_id_or_email(
    settings: Settings,
) -> None:
    url = _test_url(settings)
    config = _config(url)
    user_id = uuid4()
    _insert(url, user_id, _LATER)
    failure: BaseException | None = None
    try:
        with pytest.raises(RuntimeError, match="enable them") as raised:
            command.downgrade(config, _DOWN_REVISION)
        message = str(raised.value)
        assert str(user_id) not in message
        assert _email(user_id) not in message
        assert "1 account(s)" in message

        assert _revision(url) == _head(), "a refused downgrade moved the schema anyway"
        assert _column_exists(url), "the refused downgrade dropped the column"
        rows = asyncio.run(
            _query(url, "SELECT disabled_at FROM identity_user WHERE id = :i", i=user_id)
        )
        assert [r.disabled_at for r in rows] == [_LATER], "the refusal must not touch the row"

        # Clean path: the operator enables first, then the downgrade goes through.
        _set_disabled(url, user_id, None)
        command.downgrade(config, _DOWN_REVISION)
        assert not _column_exists(url)
    except BaseException as exc:
        failure = exc
    finally:
        _recover(config, url)
        _delete_user(url, user_id)
    if failure is not None:
        raise failure


def test_ac13d_the_offline_downgrade_renders_the_refusal_in_sql_before_the_drop(
    settings: Settings,
) -> None:
    buffer = io.StringIO()
    command.downgrade(
        _config(_test_url(settings), output_buffer=buffer),
        f"{_REVISION}:{_DOWN_REVISION}",
        sql=True,
    )
    script = buffer.getvalue()

    assert "RAISE EXCEPTION" in script
    assert "enable them" in script
    assert script.index("RAISE EXCEPTION") < script.index("DROP COLUMN")


@pytest.mark.usefixtures("_migrated")
def test_ac13e_the_database_sits_at_head_and_the_migration_descends_from_the_role_one(
    settings: Settings,
) -> None:
    url = _test_url(settings)
    script = ScriptDirectory.from_config(_config(url))
    revision = script.get_revision(_REVISION)
    assert revision is not None
    assert revision.down_revision == _DOWN_REVISION
    assert len(script.get_heads()) == 1
    assert _revision(url) == script.get_current_head()


# --- AC-14 ---------------------------------------------------------------------------------------


def _registered(users: SqlAlchemyUserRepository) -> User:
    user = User.register_with_password(
        users.next_identity(),
        EmailAddress.parse(f"disabled-{uuid4().hex}@example.com"),
        PasswordHash(_PHC),
        _NOW,
    )
    return user


async def test_ac14_a_disabled_account_round_trips_whole_second_and_timezone_aware(
    session: AsyncSession,
) -> None:
    users = SqlAlchemyUserRepository(session)
    user = _registered(users)
    user.disable(_LATER, 0)
    user.release_events()
    await users.add(user)
    await session.flush()
    session.expunge_all()

    loaded = await users.get(user.id)

    assert loaded.disabled_at == _LATER
    assert loaded.disabled_at is not None
    assert loaded.disabled_at.utcoffset() is not None
    assert loaded.disabled_at.microsecond == 0
    assert loaded.is_disabled is True


async def test_ac14_an_active_account_round_trips_as_none(session: AsyncSession) -> None:
    users = SqlAlchemyUserRepository(session)
    user = _registered(users)
    user.release_events()
    await users.add(user)
    await session.flush()
    session.expunge_all()

    loaded = await users.get(user.id)

    assert loaded.disabled_at is None
    assert loaded.is_disabled is False


async def test_ac14_saving_a_disable_then_an_enable_persists_each_state(
    session: AsyncSession,
) -> None:
    users = SqlAlchemyUserRepository(session)
    user = _registered(users)
    user.release_events()
    await users.add(user)
    await session.flush()

    user.disable(_LATER, 0)
    await users.save(user)
    await session.flush()
    stored = (
        await session.execute(
            text("SELECT disabled_at FROM identity_user WHERE id = :i"), {"i": user.id.value}
        )
    ).scalar_one()
    assert stored == _LATER

    user.enable(_LATER)
    await users.save(user)
    await session.flush()
    stored = (
        await session.execute(
            text("SELECT disabled_at FROM identity_user WHERE id = :i"), {"i": user.id.value}
        )
    ).scalar_one()
    assert stored is None


async def test_ac14_a_raw_insert_that_omits_disabled_at_stores_null(session: AsyncSession) -> None:
    user_id = uuid4()
    await session.execute(
        text(_OLD_SHAPE_INSERT), {"i": user_id, "e": _email(user_id), "p": _PHC, "t": _NOW}
    )

    stored = (
        await session.execute(
            text("SELECT disabled_at FROM identity_user WHERE id = :i"), {"i": user_id}
        )
    ).scalar_one()
    assert stored is None


# --- is_disabled ---------------------------------------------------------------------------------


async def test_is_disabled_is_true_for_a_disabled_account(session: AsyncSession) -> None:
    users = SqlAlchemyUserRepository(session)
    user = _registered(users)
    user.disable(_LATER, 0)
    user.release_events()
    await users.add(user)
    await session.flush()

    assert await users.is_disabled(user.id) is True


async def test_is_disabled_is_false_for_an_active_account(session: AsyncSession) -> None:
    users = SqlAlchemyUserRepository(session)
    user = _registered(users)
    user.release_events()
    await users.add(user)
    await session.flush()

    assert await users.is_disabled(user.id) is False


async def test_is_disabled_is_false_for_a_missing_user_and_does_not_raise(
    session: AsyncSession,
) -> None:
    users = SqlAlchemyUserRepository(session)

    assert await users.is_disabled(UserId(value=uuid4())) is False


async def test_is_disabled_sees_a_disable_committed_after_the_aggregate_was_loaded(
    engine: AsyncEngine,
) -> None:
    """The identity map would answer `False` from the loaded `User`; the statement must not."""
    user_id = uuid4()
    async with engine.connect() as seed:
        await seed.execute(
            text(_OLD_SHAPE_INSERT), {"i": user_id, "e": _email(user_id), "p": _PHC, "t": _NOW}
        )
        await seed.commit()
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as reader:
            users = SqlAlchemyUserRepository(reader)
            loaded = await users.get(UserId(value=user_id))
            assert loaded.is_disabled is False

            async with engine.connect() as other:
                await other.execute(
                    text("UPDATE identity_user SET disabled_at = :d WHERE id = :i"),
                    {"d": _LATER, "i": user_id},
                )
                await other.commit()

            assert loaded.is_disabled is False, "premise: the loaded aggregate is stale"
            assert (await users.get(UserId(value=user_id))).is_disabled is False, (
                "premise: a second get is served from the identity map"
            )
            assert await users.is_disabled(UserId(value=user_id)) is True
    finally:
        async with engine.connect() as clean:
            await clean.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})
            await clean.commit()
