"""AC-16, AC-18: migration `b1b518fe84b1` and the CHECKs it creates (slice 2.5, T23, test-after).

**Everything is read from the catalogue by name** (`pg_constraint`, `pg_indexes`), never from the
migration's own comments. Plan §5 is the source of truth for what must exist.

- **Up/down/up** with real rows present: the downgrade "drops what the upgrade created and refuses
  nothing" (AC-16, plan §5), unlike `1a2676aa3759` and `03494836ce30`, which refuse because they
  would lose kept data. This is the opposite promise, so the test seeds a pending registration, a
  reset (both kinds) and a login **before** the downgrade and asserts it goes through. Rows the test
  commits to the shared `identity_user`/`identity_login` tables are deleted afterwards (the
  `finally` recovers the schema to head first, then cleans).
- **Pinned to the revision**, not `"head"` (`test_identity_migration.py`'s reason): the snapshot of
  pre-existing tables is taken at `b1b518fe84b1` and at `03494836ce30` and must be equal, so "no
  column is added to an existing table" is a comparison and not a promise. The one permitted change
  is `ix_identity_login_expires_at`.
- **Autogenerate after the migration is empty**, with the same options `alembic/env.py` uses.

Plain `def` tests for the up/down/up (Alembic's `env.py` runs its own `asyncio.run`, which refuses to
nest inside pytest-asyncio's loop — `test_identity_migration.py`'s account), `async def` for reads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from tailorcraft.infrastructure.persistence.registry import metadata
from tailorcraft.infrastructure.settings import Settings

_REVISION: Final = "b1b518fe84b1"
_DOWN_REVISION: Final = "03494836ce30"
_NEW_TABLES: Final = ("identity_pending_registration", "identity_password_reset")
_PRE_EXISTING: Final = (
    "identity_user",
    "identity_login",
    "identity_retired_refresh_token",
    "identity_guest_session",
    "intake_base_cv",
    "posting_job_posting",
    "tailoring_run",
    "export_job",
)

_CHECKS: Final = (
    "ck_identity_pending_registration_issued_together",
    "ck_identity_pending_registration_expires_after_request",
    "ck_identity_password_reset_exactly_one_target",
    "ck_identity_password_reset_issued_with_account",
)
_UNIQUES_AND_PKS: Final = (
    ("identity_pending_registration", "pk_identity_pending_registration", "p"),
    ("identity_pending_registration", "uq_identity_pending_registration_email", "u"),
    ("identity_pending_registration", "uq_identity_pending_registration_token_hash", "u"),
    ("identity_password_reset", "pk_identity_password_reset", "p"),
    ("identity_password_reset", "uq_identity_password_reset_token_hash", "u"),
)
_INDEXES: Final = (
    ("identity_pending_registration", "ix_identity_pending_registration_expires_at", "expires_at"),
    ("identity_password_reset", "ix_identity_password_reset_user_id", "user_id"),
    ("identity_password_reset", "ix_identity_password_reset_email", "email"),
    ("identity_password_reset", "ix_identity_password_reset_expires_at", "expires_at"),
    ("identity_login", "ix_identity_login_expires_at", "expires_at"),
)

_NOW: Final = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
_HASH_A: Final = "a" * 64
_HASH_B: Final = "b" * 64
_PHC: Final = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"


def _config(settings: Settings) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.test_database_url)
    return config


async def _exists(url: str, table: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return bool(
                (
                    await conn.execute(
                        text("SELECT to_regclass(:q) IS NOT NULL"), {"q": f"public.{table}"}
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()


async def _snapshot(url: str, tables: Sequence[str]) -> dict[str, dict[str, list[tuple[Any, ...]]]]:
    """Columns, constraint definitions and index definitions per table. Indexes named in
    `_INDEXES` are filtered out: they are the migration's one permitted change to a live table."""
    permitted = {name for _, name, _ in _INDEXES}
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result: dict[str, dict[str, list[tuple[Any, ...]]]] = {}
            for table in tables:
                columns = (
                    await conn.execute(
                        text(
                            "SELECT column_name, data_type, is_nullable, character_maximum_length, "
                            "datetime_precision, column_default FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = :t ORDER BY column_name"
                        ),
                        {"t": table},
                    )
                ).all()
                constraints = (
                    await conn.execute(
                        text(
                            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                            "WHERE conrelid = to_regclass(:q) ORDER BY conname"
                        ),
                        {"q": f"public.{table}"},
                    )
                ).all()
                indexes = (
                    await conn.execute(
                        text(
                            "SELECT indexname, indexdef FROM pg_indexes "
                            "WHERE schemaname = 'public' AND tablename = :t ORDER BY indexname"
                        ),
                        {"t": table},
                    )
                ).all()
                result[table] = {
                    "columns": [tuple(r) for r in columns],
                    "constraints": [tuple(r) for r in constraints],
                    "indexes": [tuple(r) for r in indexes if r[0] not in permitted],
                }
            return result
    finally:
        await engine.dispose()


async def _seed_one_of_each(url: str) -> dict[str, Any]:
    """Committed rows in both new tables and a login, so the downgrade has something to drop."""
    assert "_test" in url
    user_id, login_id = uuid4(), uuid4()
    marker = uuid4().hex
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO identity_user (id, email, password_hash, created_at, "
                    "password_updated_at) VALUES (:i, :e, :p, :t, :t)"
                ),
                {"i": user_id, "e": f"mig-{marker}@example.com", "p": _PHC, "t": _NOW},
            )
            await conn.execute(
                text(
                    "INSERT INTO identity_login (id, user_id, created_at, expires_at, generation, "
                    "current_token_hash, version) VALUES (:i, :u, :t, :x, 1, :h, 1)"
                ),
                {
                    "i": login_id,
                    "u": user_id,
                    "t": _NOW,
                    "x": _NOW + timedelta(days=30),
                    "h": _HASH_A,
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO identity_pending_registration (id, email, password_hash, "
                    "requested_at, expires_at) VALUES (:i, :e, :p, :t, :x)"
                ),
                {
                    "i": uuid4(),
                    "e": f"pending-{marker}@example.com",
                    "p": _PHC,
                    "t": _NOW,
                    "x": _NOW + timedelta(hours=24),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO identity_password_reset (id, email, requested_at, expires_at) "
                    "VALUES (:i, :e, :t, :x)"
                ),
                {
                    "i": uuid4(),
                    "e": f"reset-{marker}@example.com",
                    "t": _NOW,
                    "x": _NOW + timedelta(hours=1),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO identity_password_reset (id, user_id, token_hash, requested_at, "
                    "expires_at, issued_at) VALUES (:i, :u, :h, :t, :x, :t)"
                ),
                {
                    "i": uuid4(),
                    "u": user_id,
                    "h": _HASH_B,
                    "t": _NOW,
                    "x": _NOW + timedelta(hours=1),
                },
            )
    finally:
        await engine.dispose()
    return {"user_id": user_id}


async def _delete_user(url: str, user_id: Any) -> None:
    assert "_test" in url
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})
    finally:
        await engine.dispose()


@pytest.mark.usefixtures("_migrated")
def test_migration_b1b518fe84b1_up_down_up_with_rows_present_drops_exactly_what_it_created(
    settings: Settings,
) -> None:
    """AC-16. Seeds rows first, because the downgrade must **refuse nothing**; compares every
    pre-existing table before and after (no column added to an existing table, the one new index on
    `identity_login` excluded by name); recovers to head unconditionally."""
    url = settings.test_database_url
    config = _config(settings)
    seeded = asyncio.run(_seed_one_of_each(url))
    error: BaseException | None = None
    after: dict[str, dict[str, list[tuple[Any, ...]]]] | None = None
    try:
        for table in _NEW_TABLES:
            assert asyncio.run(_exists(url, table)), f"{table} must exist at head"
        command.downgrade(config, _REVISION)  # no-op if already at head's own revision
        after = asyncio.run(_snapshot(url, _PRE_EXISTING))
        # The downgrade must go through with rows in both new tables and in the login table.
        command.downgrade(config, _DOWN_REVISION)
        for table in _NEW_TABLES:
            assert not asyncio.run(_exists(url, table)), (
                f"{table} must be dropped by the downgrade, rows and all"
            )
        before = asyncio.run(_snapshot(url, _PRE_EXISTING))
        assert before == after, (
            "an existing table's columns, constraints or indexes differ across this migration: "
            "it must add no column to an existing table (AC-16)"
        )
        assert not asyncio.run(_index_exists(url, "ix_identity_login_expires_at")), (
            "the downgrade must drop the index it created on identity_login"
        )
    except BaseException as exc:
        error = exc
    try:
        command.upgrade(config, "head")
        for table in _NEW_TABLES:
            assert asyncio.run(_exists(url, table)), "schema must be back at head"
        assert asyncio.run(_index_exists(url, "ix_identity_login_expires_at"))
    except Exception as recovery:
        if error is not None:
            raise error from recovery
        raise
    finally:
        asyncio.run(_delete_user(url, seeded["user_id"]))
    if error is not None:
        raise error


async def _index_exists(url: str, name: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return bool(
                (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM pg_indexes "
                            "WHERE schemaname = 'public' AND indexname = :n"
                        ),
                        {"n": name},
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()


@pytest.mark.usefixtures("_migrated")
def test_the_migration_descends_directly_from_the_user_owner_migration(
    settings: Settings,
) -> None:
    """A fork or a skipped parent would let the deploy run two heads."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_config(settings))
    revision = script.get_revision(_REVISION)
    assert revision is not None
    assert revision.down_revision == _DOWN_REVISION
    assert len(script.get_heads()) == 1, (
        "two alembic heads: the deploy's `upgrade head` would refuse"
    )


async def test_autogenerate_against_head_produces_an_empty_diff(
    connection: AsyncConnection,
) -> None:
    """AC-16: the mapped metadata and the migrated database agree exactly, with the options
    `alembic/env.py` runs autogenerate with."""

    def _diff(sync_conn: Connection) -> Sequence[object]:
        context = MigrationContext.configure(
            sync_conn, opts={"compare_type": True, "compare_server_default": True}
        )
        return list(compare_metadata(context, metadata))

    assert await connection.run_sync(_diff) == []


# --- by name, from the catalogue ---------------------------------------------------------------


@pytest.mark.parametrize("name", _CHECKS)
async def test_each_check_constraint_exists_by_name_and_is_validated(
    session: AsyncSession, name: str
) -> None:
    row = (
        await session.execute(
            text(
                "SELECT contype::text, convalidated FROM pg_constraint "
                "WHERE conname = :n AND connamespace = 'public'::regnamespace"
            ),
            {"n": name},
        )
    ).one()
    assert row == ("c", True)


@pytest.mark.parametrize(("table", "name", "kind"), _UNIQUES_AND_PKS)
async def test_each_primary_key_and_unique_exists_by_name_on_its_table(
    session: AsyncSession, table: str, name: str, kind: str
) -> None:
    row = (
        await session.execute(
            text(
                "SELECT contype::text FROM pg_constraint "
                "WHERE conname = :n AND conrelid = to_regclass(:t)"
            ),
            {"n": name, "t": f"public.{table}"},
        )
    ).one()
    assert row[0] == kind


@pytest.mark.parametrize(("table", "name", "column"), _INDEXES)
async def test_each_index_exists_by_name_on_its_column(
    session: AsyncSession, table: str, name: str, column: str
) -> None:
    definition = (
        await session.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = :t AND indexname = :n"
            ),
            {"t": table, "n": name},
        )
    ).scalar_one()
    assert definition.endswith(f"USING btree ({column})")
    assert "UNIQUE" not in definition


async def test_the_reset_user_foreign_key_cascades_and_nothing_reaches_a_guest_session(
    session: AsyncSession,
) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT conname, confrelid::regclass::text, confdeltype::text FROM pg_constraint "
                "WHERE contype = 'f' AND conrelid = ANY(ARRAY["
                "to_regclass('public.identity_password_reset'), "
                "to_regclass('public.identity_pending_registration')])"
            )
        )
    ).all()
    assert [tuple(r) for r in rows] == [
        ("fk_identity_password_reset_user_id_identity_user", "identity_user", "c")
    ]


async def test_email_columns_are_varchar_254_and_the_password_hash_varchar_512(
    session: AsyncSession,
) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT table_name, column_name, data_type, character_maximum_length "
                "FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name IN ('identity_pending_registration', 'identity_password_reset') "
                "AND column_name IN ('email', 'password_hash', 'token_hash')"
            )
        )
    ).all()
    found = {(r[0], r[1]): (r[2], r[3]) for r in rows}
    assert found[("identity_pending_registration", "email")] == ("character varying", 254)
    assert found[("identity_password_reset", "email")] == ("character varying", 254)
    assert found[("identity_pending_registration", "password_hash")] == ("character varying", 512)
    assert found[("identity_pending_registration", "token_hash")] == ("character", 64)
    assert found[("identity_password_reset", "token_hash")] == ("character", 64)
    assert ("identity_password_reset", "password_hash") not in found, (
        "a reset must never hold a password hash"
    )


async def test_every_instant_on_the_new_tables_is_timestamptz_precision_0(
    session: AsyncSession,
) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT table_name, column_name, data_type, datetime_precision "
                "FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name IN ('identity_pending_registration', 'identity_password_reset') "
                "AND column_name LIKE '%\\_at'"
            )
        )
    ).all()
    assert len(rows) == 6  # requested_at, expires_at, issued_at on each
    for row in rows:
        assert (row.data_type, row.datetime_precision) == ("timestamp with time zone", 0), row


# --- AC-18: the CHECKs refuse each bad pairing, by constraint name -----------------------------


async def _refused_by(session: AsyncSession, sql: str, params: dict[str, Any]) -> str:
    """Run one hand-written INSERT and return the violated constraint's name, from the driver's
    diagnostics (the message itself is withheld by the engine's `handle_error` listener)."""
    from tailorcraft.infrastructure.persistence.database import violated_constraint

    with pytest.raises(IntegrityError) as raised:
        async with session.begin_nested():
            await session.execute(text(sql), params)
    name = violated_constraint(raised.value)
    assert name is not None
    return name


_INSERT_PENDING = (
    "INSERT INTO identity_pending_registration "
    "(id, email, password_hash, requested_at, expires_at, token_hash, issued_at) "
    "VALUES (:i, :e, :p, :t, :x, :h, :a)"
)


def _pending(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "i": uuid4(),
        "e": f"{uuid4().hex}@example.com",
        "p": _PHC,
        "t": _NOW,
        "x": _NOW + timedelta(hours=24),
        "h": None,
        "a": None,
    }
    return {**base, **overrides}


async def test_a_pending_row_with_a_token_hash_and_no_issued_at_is_refused(
    session: AsyncSession,
) -> None:
    name = await _refused_by(
        session,
        _INSERT_PENDING,
        _pending(h=_HASH_A),
    )
    assert name == "ck_identity_pending_registration_issued_together"


async def test_a_pending_row_with_an_issued_at_and_no_token_hash_is_refused(
    session: AsyncSession,
) -> None:
    name = await _refused_by(
        session,
        _INSERT_PENDING,
        _pending(a=_NOW),
    )
    assert name == "ck_identity_pending_registration_issued_together"


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=-1)])
async def test_a_pending_row_that_expires_at_or_before_its_request_is_refused(
    session: AsyncSession, delta: timedelta
) -> None:
    name = await _refused_by(
        session,
        _INSERT_PENDING,
        _pending(x=_NOW + delta),
    )
    assert name == "ck_identity_pending_registration_expires_after_request"


async def test_a_pending_row_issued_together_and_unissued_are_both_accepted(
    session: AsyncSession,
) -> None:
    """The positive controls: the CHECKs refuse the bad pairings and nothing else."""
    for params in (_pending(), _pending(h=_HASH_A, a=_NOW)):
        await session.execute(
            text(_INSERT_PENDING),
            params,
        )


async def test_a_second_pending_row_for_one_address_is_refused_by_the_unique_email(
    session: AsyncSession,
) -> None:
    first = _pending()
    await session.execute(
        text(_INSERT_PENDING),
        first,
    )
    name = await _refused_by(
        session,
        _INSERT_PENDING,
        _pending(e=first["e"]),
    )
    assert name == "uq_identity_pending_registration_email"


_INSERT_RESET = (
    "INSERT INTO identity_password_reset "
    "(id, email, user_id, token_hash, requested_at, expires_at, issued_at) "
    "VALUES (:i, :e, :u, :h, :t, :x, :a)"
)


def _reset(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "i": uuid4(),
        "e": f"{uuid4().hex}@example.com",
        "u": None,
        "h": None,
        "t": _NOW,
        "x": _NOW + timedelta(hours=1),
        "a": None,
    }
    return {**base, **overrides}


async def _a_user(session: AsyncSession) -> Any:
    user_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO identity_user (id, email, password_hash, created_at, password_updated_at) "
            "VALUES (:i, :e, :p, :t, :t)"
        ),
        {"i": user_id, "e": f"{uuid4().hex}@example.com", "p": _PHC, "t": _NOW},
    )
    return user_id


async def test_a_reset_with_both_an_address_and_an_account_is_refused(
    session: AsyncSession,
) -> None:
    user_id = await _a_user(session)
    name = await _refused_by(
        session,
        _INSERT_RESET,
        _reset(u=user_id, h=_HASH_A, a=_NOW),
    )
    assert name == "ck_identity_password_reset_exactly_one_target"


async def test_a_reset_with_neither_an_address_nor_an_account_is_refused(
    session: AsyncSession,
) -> None:
    name = await _refused_by(
        session,
        _INSERT_RESET,
        _reset(e=None),
    )
    assert name == "ck_identity_password_reset_exactly_one_target"


async def test_an_issued_reset_without_an_account_is_refused(session: AsyncSession) -> None:
    """A token hash and `issued_at` on an addressed reset: issued ⇔ account."""
    name = await _refused_by(
        session,
        _INSERT_RESET,
        _reset(h=_HASH_A, a=_NOW),
    )
    assert name == "ck_identity_password_reset_issued_with_account"


async def test_an_account_reset_without_a_token_hash_is_refused(session: AsyncSession) -> None:
    user_id = await _a_user(session)
    name = await _refused_by(
        session,
        _INSERT_RESET,
        _reset(e=None, u=user_id),
    )
    assert name == "ck_identity_password_reset_issued_with_account"


async def test_an_account_reset_with_a_hash_but_no_issued_at_is_refused(
    session: AsyncSession,
) -> None:
    user_id = await _a_user(session)
    name = await _refused_by(
        session,
        _INSERT_RESET,
        _reset(e=None, u=user_id, h=_HASH_A),
    )
    assert name == "ck_identity_password_reset_issued_with_account"


async def test_addressed_and_issued_resets_are_both_accepted(session: AsyncSession) -> None:
    """Positive controls for the two CHECKs, and the cascade: deleting the user takes the issued one."""
    user_id = await _a_user(session)
    await session.execute(
        text(_INSERT_RESET),
        _reset(),
    )
    issued = _reset(e=None, u=user_id, h=_HASH_A, a=_NOW)
    await session.execute(
        text(_INSERT_RESET),
        issued,
    )
    await session.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})
    remaining = (
        await session.execute(
            text("SELECT count(*) FROM identity_password_reset WHERE id = :i"), {"i": issued["i"]}
        )
    ).scalar_one()
    assert remaining == 0


async def test_a_reset_for_a_user_that_does_not_exist_is_refused_by_the_foreign_key(
    session: AsyncSession,
) -> None:
    name = await _refused_by(
        session,
        _INSERT_RESET,
        _reset(e=None, u=uuid4(), h=_HASH_A, a=_NOW),
    )
    assert name == "fk_identity_password_reset_user_id_identity_user"


async def test_two_resets_cannot_share_a_token_hash_but_any_number_may_have_none(
    session: AsyncSession,
) -> None:
    for _ in range(3):
        await session.execute(
            text(_INSERT_RESET),
            _reset(),
        )
    user_a, user_b = await _a_user(session), await _a_user(session)
    await session.execute(
        text(_INSERT_RESET),
        _reset(e=None, u=user_a, h=_HASH_A, a=_NOW),
    )
    name = await _refused_by(
        session,
        _INSERT_RESET,
        _reset(e=None, u=user_b, h=_HASH_A, a=_NOW),
    )
    assert name == "uq_identity_password_reset_token_hash"
