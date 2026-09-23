"""Migration tests for `66c9e18acc5c` — "add identity user and login" (T22, after).

**AC-14.** Purely additive: exactly three new tables, and no pre-existing table's columns,
constraints or indexes differ before and after `upgrade head`. Proven by downgrading to
`fa1bef4468bf` and back within this one test, snapshotting `information_schema.columns`,
`pg_constraint` (via `pg_get_constraintdef`, which normalises column order and expression text so the
comparison is not fooled by cosmetic differences) and `pg_indexes` for every pre-existing table at
both revisions, and asserting the two snapshots are equal.

Follows `test_tailoring_run_repository.py`'s up/down/up precedent closely: a plain `def test_...`,
not `async def` (Alembic's `env.py` opens its own `asyncio.run()`, which refuses to nest inside
pytest-asyncio's session-scoped loop while that loop is mid-test — see that file's identical test for
the full account); no `session`/`connection` fixture (those bind to a SAVEPOINT held open on the
shared connection, and Alembic's DDL must run outside of any such transaction); and unconditional
recovery, because this suite migrates its **one** test database to head exactly once per session
(`conftest.py`'s `_migrated`) — a schema left below head here would silently break every test that
runs after this one, in this file and beyond.

**AC-15** is read from `pg_constraint`, never from the migration's own comments: no foreign key from
any of the three new tables reaches `identity_guest_session`, and both new foreign keys carry
`ON DELETE CASCADE`. These do not need the down/up dance — they are ordinary reads against the schema
`_migrated` already brought to head — so they run as plain `async def` tests against the shared
`session` fixture, like every other schema test in this package.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Final

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from tailorcraft.infrastructure.settings import Settings

_DOWN_REVISION: Final = "fa1bef4468bf"

_PRE_EXISTING_TABLES: Final[tuple[str, ...]] = (
    "identity_guest_session",
    "intake_base_cv",
    "posting_job_posting",
    "tailoring_run",
    "export_job",
)

_NEW_TABLES: Final[tuple[str, ...]] = (
    "identity_user",
    "identity_login",
    "identity_retired_refresh_token",
)


async def _table_exists(url: str, table_name: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT to_regclass(:qualified) IS NOT NULL"),
                {"qualified": f"public.{table_name}"},
            )
            return bool(result.scalar_one())
    finally:
        await engine.dispose()


async def _table_snapshot(url: str, table_name: str) -> dict[str, list[tuple[Any, ...]]]:
    """Columns, constraint definitions and index definitions for one table, each ordered so the
    comparison is insensitive to the order Postgres happens to return rows in."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            columns = (
                await conn.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable, character_maximum_length, "
                        "numeric_precision, datetime_precision, column_default "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :table_name "
                        "ORDER BY column_name"
                    ),
                    {"table_name": table_name},
                )
            ).all()
            constraints = (
                await conn.execute(
                    text(
                        "SELECT conname, contype::text, pg_get_constraintdef(oid) "
                        "FROM pg_constraint WHERE conrelid = to_regclass(:qualified) "
                        "ORDER BY conname"
                    ),
                    {"qualified": f"public.{table_name}"},
                )
            ).all()
            indexes = (
                await conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE schemaname = 'public' AND tablename = :table_name "
                        "ORDER BY indexname"
                    ),
                    {"table_name": table_name},
                )
            ).all()
            return {
                "columns": [tuple(row) for row in columns],
                "constraints": [tuple(row) for row in constraints],
                "indexes": [tuple(row) for row in indexes],
            }
    finally:
        await engine.dispose()


async def _snapshot_all(
    url: str, tables: Sequence[str]
) -> dict[str, dict[str, list[tuple[Any, ...]]]]:
    return {table: await _table_snapshot(url, table) for table in tables}


@pytest.mark.usefixtures("_migrated")
def test_migration_66c9e18acc5c_up_down_up_touches_no_pre_existing_table_and_adds_exactly_three(
    settings: Settings,
) -> None:
    """AC-14. `_migrated` is requested explicitly (not merely relied on transitively through some
    other fixture) so this file is self-contained even run alone.

    **Recovery is unconditional.** Every exit path — a `downgrade()` that raises, an assertion that
    fails, an `upgrade()` that itself raises during recovery — ends by restoring head, or by chaining
    the recovery failure onto whatever it was trying to report, exactly as
    `test_tailoring_run_repository.py`'s identical migration test does; a failure recovering the
    schema must never silently replace the real assertion in the report, and must never be lost
    either.
    """
    url = settings.test_database_url
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)

    for table in _NEW_TABLES:
        assert asyncio.run(_table_exists(url, table)) is True, f"{table} must exist at head"

    after_snapshot = asyncio.run(_snapshot_all(url, _PRE_EXISTING_TABLES))

    downgrade_error: Exception | None = None
    try:
        command.downgrade(config, _DOWN_REVISION)
    except Exception as exc:
        downgrade_error = exc

    if downgrade_error is not None:
        try:
            command.upgrade(config, "head")
            for table in _NEW_TABLES:
                assert asyncio.run(_table_exists(url, table)) is True
        except Exception as recovery_exc:
            raise downgrade_error from recovery_exc
        raise downgrade_error

    assertion_error: AssertionError | None = None
    try:
        for table in _NEW_TABLES:
            assert asyncio.run(_table_exists(url, table)) is False, (
                f"{table} must not exist below {_DOWN_REVISION} — downgrade() must drop it, "
                "and AC-14 says downgrade drops tables, never columns"
            )
        before_snapshot = asyncio.run(_snapshot_all(url, _PRE_EXISTING_TABLES))
        assert before_snapshot == after_snapshot, (
            "a pre-existing table's columns, constraints or indexes differ between the down "
            "revision and head — this migration must be purely additive (AC-14)"
        )
    except AssertionError as exc:
        assertion_error = exc

    try:
        command.upgrade(config, "head")
        for table in _NEW_TABLES:
            assert asyncio.run(_table_exists(url, table)) is True, (
                "the schema must be back at head before the next test in the session runs"
            )
    except Exception as recovery_exc:
        if assertion_error is not None:
            raise assertion_error from recovery_exc
        raise

    if assertion_error is not None:
        raise assertion_error


# --- AC-15: read from pg_constraint, never from the migration's comments ------------------------


async def test_ac15_no_foreign_key_from_any_new_table_reaches_identity_guest_session(
    session: AsyncSession,
) -> None:
    result = await session.execute(
        text(
            "SELECT conrelid::regclass::text, confrelid::regclass::text "
            "FROM pg_constraint "
            "WHERE contype = 'f' "
            "AND conrelid = ANY(ARRAY["
            "  to_regclass('public.identity_user'), "
            "  to_regclass('public.identity_login'), "
            "  to_regclass('public.identity_retired_refresh_token')"
            "]) "
            "AND confrelid = to_regclass('public.identity_guest_session')"
        )
    )
    assert result.all() == [], (
        "a foreign key from a new identity table reaches identity_guest_session — the purge's "
        "cascade would reach registered data (AC-15)"
    )


async def test_ac15_identity_login_user_id_cascades_from_identity_user_on_delete(
    session: AsyncSession,
) -> None:
    result = await session.execute(
        text(
            "SELECT con.confdeltype::text, refrel.relname "
            "FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_class refrel ON refrel.oid = con.confrelid "
            "JOIN pg_attribute att ON att.attrelid = rel.oid AND att.attnum = ANY(con.conkey) "
            "WHERE con.contype = 'f' AND rel.relname = 'identity_login' AND att.attname = 'user_id'"
        )
    )
    delete_type, referenced_table = result.one()
    assert referenced_table == "identity_user"
    assert delete_type == "c", (
        f"identity_login.user_id's FK is not ON DELETE CASCADE ({delete_type!r})"
    )


async def test_ac15_retired_refresh_token_login_id_cascades_from_identity_login_on_delete(
    session: AsyncSession,
) -> None:
    result = await session.execute(
        text(
            "SELECT con.confdeltype::text, refrel.relname "
            "FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "JOIN pg_class refrel ON refrel.oid = con.confrelid "
            "JOIN pg_attribute att ON att.attrelid = rel.oid AND att.attnum = ANY(con.conkey) "
            "WHERE con.contype = 'f' AND rel.relname = 'identity_retired_refresh_token' "
            "AND att.attname = 'login_id'"
        )
    )
    delete_type, referenced_table = result.one()
    assert referenced_table == "identity_login"
    assert delete_type == "c", (
        f"identity_retired_refresh_token.login_id's FK is not ON DELETE CASCADE ({delete_type!r})"
    )


# --- Exactly three new tables, all timestamp columns TIMESTAMP(0) WITH TIME ZONE ----------------


async def test_exactly_three_new_tables_exist_at_head(session: AsyncSession) -> None:
    stmt = text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name IN :tables"
    ).bindparams(bindparam("tables", expanding=True))
    result = await session.execute(stmt, {"tables": list(_NEW_TABLES)})
    assert {row[0] for row in result.all()} == set(_NEW_TABLES)


async def test_every_timestamp_column_on_the_three_new_tables_is_timestamptz_precision_0(
    session: AsyncSession,
) -> None:
    stmt = text(
        "SELECT table_name, column_name, data_type, datetime_precision "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name IN :tables "
        "AND data_type = 'timestamp with time zone'"
    ).bindparams(bindparam("tables", expanding=True))
    result = await session.execute(stmt, {"tables": list(_NEW_TABLES)})
    rows = result.all()
    # identity_user: created_at, password_updated_at (2) — identity_login: created_at, expires_at,
    # rotated_at (3) — identity_retired_refresh_token: retired_at (1) — six in total.
    assert len(rows) == 6, (
        f"expected 6 timestamptz columns across the three new tables, found {rows}"
    )
    for row in rows:
        assert row.data_type == "timestamp with time zone"
        assert row.datetime_precision == 0, f"{row.table_name}.{row.column_name} is not precision 0"
