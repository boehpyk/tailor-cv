"""AC-9 and AC-10: migration `3d26e05edffe` and `identity_user.role` (4.1, T7, test-after).

Same shape as `test_export_job_layout_migration.py`: catalogue facts by name, committed seeds
removed afterwards, recovery to head unconditional, "at head" via `get_current_head()`. Plain `def`
for the tests that run Alembic (its `env.py` runs its own `asyncio.run`).
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import EmailAddress, PasswordHash, Role, UserId
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.settings import Settings

_REVISION: Final = "3d26e05edffe"
_DOWN_REVISION: Final = "b10d1c777b0a"
_CHECK: Final = "ck_identity_user_role_known"
_NOW: Final = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
_PHC: Final = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"

# What the previous image does during the deploy window: no `role` column in the statement.
_OLD_SHAPE_INSERT: Final = (
    "INSERT INTO identity_user (id, email, password_hash, created_at, password_updated_at) "
    "VALUES (:i, :e, :p, :t, :t)"
)
_NEW_SHAPE_INSERT: Final = (
    "INSERT INTO identity_user (id, email, password_hash, created_at, password_updated_at, role) "
    "VALUES (:i, :e, :p, :t, :t, :r)"
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
    rows = asyncio.run(
        _query(
            url,
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'identity_user' AND column_name = 'role'",
        )
    )
    return bool(rows)


def _check_exists(url: str) -> bool:
    return bool(
        asyncio.run(_query(url, "SELECT 1 FROM pg_constraint WHERE conname = :n", n=_CHECK))
    )


def _revision(url: str) -> str:
    return str(asyncio.run(_query(url, "SELECT version_num FROM alembic_version"))[0][0])


def _email(user_id: UUID) -> str:
    return f"role-migration-{user_id}@example.com"


def _insert(url: str, user_id: UUID, role: str | None) -> None:
    async def seed(session: AsyncSession) -> None:
        if role is None:
            await session.execute(
                text(_OLD_SHAPE_INSERT), {"i": user_id, "e": _email(user_id), "p": _PHC, "t": _NOW}
            )
        else:
            await session.execute(
                text(_NEW_SHAPE_INSERT),
                {"i": user_id, "e": _email(user_id), "p": _PHC, "t": _NOW, "r": role},
            )

    asyncio.run(_commit(url, seed))


def _delete_user(url: str, user_id: UUID) -> None:
    async def clean(session: AsyncSession) -> None:
        await session.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})

    asyncio.run(_commit(url, clean))


def _recover(config: Config, url: str) -> None:
    if _revision(url) != _head():
        command.upgrade(config, "head")


# --- AC-9 ----------------------------------------------------------------------------------------


@pytest.mark.usefixtures("_migrated")
def test_ac9b_rows_written_before_the_upgrade_read_user_afterwards(settings: Settings) -> None:
    url = _test_url(settings)
    config = _config(url)
    user_id = uuid4()
    failure: BaseException | None = None
    try:
        command.downgrade(config, _DOWN_REVISION)
        assert not _column_exists(url)
        _insert(url, user_id, None)
        command.upgrade(config, "head")
        rows = asyncio.run(_query(url, "SELECT role FROM identity_user WHERE id = :i", i=user_id))
        assert [r.role for r in rows] == ["user"]
    except BaseException as exc:
        failure = exc
    finally:
        _recover(config, url)
        _delete_user(url, user_id)
    if failure is not None:
        raise failure


@pytest.mark.usefixtures("_migrated")
def test_ac9c_up_down_up_round_trips_on_a_table_with_only_user_rows(settings: Settings) -> None:
    url = _test_url(settings)
    config = _config(url)
    user_id = uuid4()
    _insert(url, user_id, "user")
    failure: BaseException | None = None
    try:
        assert _column_exists(url)
        assert _check_exists(url)
        command.downgrade(config, _DOWN_REVISION)
        assert not _column_exists(url), "the downgrade must drop the column"
        assert not _check_exists(url), "the downgrade must drop the CHECK"
        assert _revision(url) == _DOWN_REVISION
        assert asyncio.run(_query(url, "SELECT 1 FROM identity_user WHERE id = :i", i=user_id)), (
            "a clean downgrade deletes no user"
        )
        command.upgrade(config, "head")
        assert _column_exists(url)
        assert _check_exists(url)
        rows = asyncio.run(_query(url, "SELECT role FROM identity_user WHERE id = :i", i=user_id))
        assert [r.role for r in rows] == ["user"]
    except BaseException as exc:
        failure = exc
    finally:
        _recover(config, url)
        _delete_user(url, user_id)
    if failure is not None:
        raise failure


@pytest.mark.usefixtures("_migrated")
def test_ac9d_the_downgrade_refuses_while_an_admin_exists_naming_revoke_role_and_no_id_or_email(
    settings: Settings,
) -> None:
    url = _test_url(settings)
    config = _config(url)
    user_id = uuid4()
    _insert(url, user_id, "admin")
    failure: BaseException | None = None
    try:
        with pytest.raises(RuntimeError, match="revoke-role") as raised:
            command.downgrade(config, _DOWN_REVISION)
        message = str(raised.value)
        assert str(user_id) not in message
        assert _email(user_id) not in message
        assert "1 account(s)" in message

        assert _revision(url) == _head(), "a refused downgrade moved the schema anyway"
        assert _column_exists(url), "the refused downgrade dropped the column"
        assert _check_exists(url), "the refused downgrade dropped the CHECK"
        rows = asyncio.run(_query(url, "SELECT role FROM identity_user WHERE id = :i", i=user_id))
        assert [r.role for r in rows] == ["admin"], "the refusal must not touch the admin row"

        # Clean path: the operator revokes first, then the downgrade goes through.
        async def revoke(session: AsyncSession) -> None:
            await session.execute(
                text("UPDATE identity_user SET role = 'user' WHERE id = :i"), {"i": user_id}
            )

        asyncio.run(_commit(url, revoke))
        command.downgrade(config, _DOWN_REVISION)
        assert not _column_exists(url)
        assert not _check_exists(url)
    except BaseException as exc:
        failure = exc
    finally:
        _recover(config, url)
        _delete_user(url, user_id)
    if failure is not None:
        raise failure


def test_ac9d_the_offline_downgrade_renders_the_refusal_in_sql_before_any_drop(
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
    assert "revoke-role" in script
    assert script.index("RAISE EXCEPTION") < script.index("DROP CONSTRAINT")
    assert script.index("DROP CONSTRAINT") < script.index("DROP COLUMN")


@pytest.mark.usefixtures("_migrated")
def test_ac9e_the_database_sits_at_head_and_the_migration_descends_from_the_layout_one(
    settings: Settings,
) -> None:
    url = _test_url(settings)
    script = ScriptDirectory.from_config(_config(url))
    revision = script.get_revision(_REVISION)
    assert revision is not None
    assert revision.down_revision == _DOWN_REVISION
    assert len(script.get_heads()) == 1
    assert _revision(url) == script.get_current_head()


async def test_ac9a_the_column_is_a_not_null_varchar_16_defaulting_to_user(
    session: AsyncSession,
) -> None:
    row = (
        await session.execute(
            text(
                "SELECT data_type, character_maximum_length, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'identity_user' "
                "AND column_name = 'role'"
            )
        )
    ).one()
    assert (row.data_type, row.character_maximum_length, row.is_nullable) == (
        "character varying",
        16,
        "NO",
    )
    assert row.column_default is not None
    assert "'user'" in row.column_default


async def test_ac9a_the_check_exists_by_name_is_validated_and_lists_exactly_user_and_admin(
    session: AsyncSession,
) -> None:
    row = (
        await session.execute(
            text(
                "SELECT con.contype::text AS kind, con.convalidated, "
                "pg_get_constraintdef(con.oid) AS definition "
                "FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid "
                "WHERE rel.relname = 'identity_user' AND con.conname = :n"
            ),
            {"n": _CHECK},
        )
    ).one()
    assert (row.kind, row.convalidated) == ("c", True)
    assert "'user'" in row.definition
    assert "'admin'" in row.definition


# --- AC-10 ---------------------------------------------------------------------------------------


async def _raw_user(session: AsyncSession, role: str | None) -> UUID:
    user_id = uuid4()
    if role is None:
        await session.execute(
            text(_OLD_SHAPE_INSERT), {"i": user_id, "e": _email(user_id), "p": _PHC, "t": _NOW}
        )
    else:
        await session.execute(
            text(_NEW_SHAPE_INSERT),
            {"i": user_id, "e": _email(user_id), "p": _PHC, "t": _NOW, "r": role},
        )
    return user_id


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
async def test_ac10a_the_role_round_trips_through_the_repository(
    session: AsyncSession, role: Role
) -> None:
    users = SqlAlchemyUserRepository(session)
    user = User.register_with_password(
        users.next_identity(),
        EmailAddress.parse(f"round-trip-{uuid4().hex}@example.com"),
        PasswordHash(_PHC),
        _NOW,
    )
    user.change_role(role, _NOW)
    user.release_events()
    await users.add(user)
    await session.flush()
    session.expunge_all()

    loaded = await users.get(user.id)

    assert loaded.role is role
    assert loaded.is_admin is (role is Role.ADMIN)


async def test_ac10b_a_raw_insert_that_omits_role_stores_user(session: AsyncSession) -> None:
    user_id = await _raw_user(session, None)

    stored = (
        await session.execute(text("SELECT role FROM identity_user WHERE id = :i"), {"i": user_id})
    ).scalar_one()
    assert stored == "user"


async def test_ac10c_a_raw_update_to_an_unknown_role_is_refused_by_the_check(
    session: AsyncSession,
) -> None:
    user_id = await _raw_user(session, "user")

    with pytest.raises(IntegrityError) as raised:
        async with session.begin_nested():
            await session.execute(
                text("UPDATE identity_user SET role = 'superuser' WHERE id = :i"), {"i": user_id}
            )

    assert violated_constraint(raised.value) == _CHECK


async def test_ac10d_a_stored_unknown_role_is_refused_on_load_naming_the_column_not_the_value(
    session: AsyncSession,
) -> None:
    """The CHECK is dropped inside the test's own transaction (DDL is transactional in PostgreSQL,
    and the fixture rolls it back), so the type's Python half is what is under test."""
    await session.execute(text(f"ALTER TABLE identity_user DROP CONSTRAINT {_CHECK}"))
    user_id = await _raw_user(session, "superuser")
    users = SqlAlchemyUserRepository(session)
    session.expunge_all()

    with pytest.raises(ValueError, match=r"identity_user\.role") as raised:
        await users.get(UserId(user_id))

    assert "superuser" not in str(raised.value)
    assert str(user_id) not in str(raised.value)
