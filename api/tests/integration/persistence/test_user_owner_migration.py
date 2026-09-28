"""Migration and schema tests for `03494836ce30` — "add user owner to posting, run and export" (slice
2.3, T17, written after; AC-16, AC-17).

Three tables gain a `user_id` owner: `posting_job_posting`, `tailoring_run`, `export_job`. Each
gets `fk_<table>_user_id_identity_user` (validated, `ON DELETE CASCADE`), an index (the two listed
ones composite with `DESC` columns), `ck_<table>_exactly_one_owner` (validated), and a nullable
`guest_session_id`. Everything here is read from the catalog by name — never trusted from the
revision's comments.

The migration tests follow `test_base_cv_owner_migration.py` exactly: plain `def` tests (Alembic's
`env.py` runs its own `asyncio.run()`), no `session` fixture (DDL cannot run inside the fixture's
SAVEPOINT), committed data on short-lived engines against `settings.test_database_url` (asserted to
name a `_test` database before anything is written), and unconditional recovery to head so a
failure here never leaves the shared test database below head for every later test.

AC-17 (the running 2.2 code's INSERTs still succeed) is proven with the literal 2.2 shape: a real
row is written through the repository, read back as a column dict, deleted, and re-inserted
**without naming `user_id`** — every column 2.2 knew about and nothing else.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Final

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Table, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.user import User
from tailorcraft.domain.identity.value_objects import (
    EmailAddress,
    GuestSessionId,
    PasswordHash,
    UserId,
)
from tailorcraft.domain.tailoring.value_objects import TailoringFailureReason
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.user import (
    SqlAlchemyUserRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.integration.owners import pasted_posting, queued_export, running_run, succeeded_run

_TARGET: Final = "03494836ce30"
_DOWN: Final = "1a2676aa3759"
_TABLES: Final = ("posting_job_posting", "tailoring_run", "export_job")
_INDEXES: Final[dict[str, tuple[str, str]]] = {
    "posting_job_posting": (
        "ix_posting_job_posting_user_id_created_at",
        "(user_id, created_at DESC, id DESC)",
    ),
    "tailoring_run": (
        "ix_tailoring_run_user_id_requested_at",
        "(user_id, requested_at DESC, id DESC)",
    ),
    "export_job": ("ix_export_job_user_id", "(user_id)"),
}
_PASSWORD_HASH = PasswordHash("$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA")


def _test_url(settings: Settings) -> str:
    url = settings.test_database_url
    assert "_test" in url, f"refusing to write committed rows to {url!r}"
    return url


def _config(url: str, *, output_buffer: io.StringIO | None = None) -> Config:
    config = Config("alembic.ini", output_buffer=output_buffer)
    config.set_main_option("sqlalchemy.url", url)
    return config


async def _scalar(url: str, sql: str, **params: object) -> object:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar_one()
    finally:
        await engine.dispose()


def _schema_at(url: str) -> dict[str, object]:
    """The facts up/down/up moves, per table, plus the recorded revision."""
    facts: dict[str, object] = {
        "revision": asyncio.run(_scalar(url, "SELECT version_num FROM alembic_version"))
    }
    for table in _TABLES:
        facts[f"{table}.user_id"] = asyncio.run(
            _scalar(
                url,
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = 'user_id'",
                t=table,
            )
        )
        facts[f"{table}.guest_session_id nullable"] = asyncio.run(
            _scalar(
                url,
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = 'guest_session_id'",
                t=table,
            )
        )
        facts[f"{table}.check"] = asyncio.run(
            _scalar(
                url,
                "SELECT count(*) FROM pg_constraint WHERE conname = :n",
                n=f"ck_{table}_exactly_one_owner",
            )
        )
    return facts


_AT_HEAD: Final = {
    "revision": _TARGET,
    **{f"{t}.user_id": 1 for t in _TABLES},
    **{f"{t}.guest_session_id nullable": "YES" for t in _TABLES},
    **{f"{t}.check": 1 for t in _TABLES},
}
_BELOW: Final = {
    "revision": _DOWN,
    **{f"{t}.user_id": 0 for t in _TABLES},
    **{f"{t}.guest_session_id nullable": "NO" for t in _TABLES},
    **{f"{t}.check": 0 for t in _TABLES},
}


# --- The catalog, by name (AC-16) ----------------------------------------------------------------


@pytest.mark.parametrize("table", _TABLES)
async def test_the_user_foreign_key_is_named_validated_and_cascades(
    session: AsyncSession, table: str
) -> None:
    row = (
        await session.execute(
            text(
                "SELECT target.relname, con.confdeltype::text AS on_delete, con.convalidated, "
                "  pg_get_constraintdef(con.oid) AS definition "
                "FROM pg_constraint con "
                "JOIN pg_class rel ON rel.oid = con.conrelid "
                "JOIN pg_class target ON target.oid = con.confrelid "
                "WHERE rel.relname = :t AND con.conname = :n AND con.contype = 'f'"
            ),
            {"t": table, "n": f"fk_{table}_user_id_identity_user"},
        )
    ).one()
    assert row.relname == "identity_user"
    assert row.on_delete == "c"
    assert row.convalidated is True
    assert row.definition.startswith("FOREIGN KEY (user_id) REFERENCES identity_user(id)")


@pytest.mark.parametrize("table", _TABLES)
async def test_the_exactly_one_owner_check_is_named_and_validated(
    session: AsyncSession, table: str
) -> None:
    row = (
        await session.execute(
            text(
                "SELECT con.convalidated, pg_get_constraintdef(con.oid) AS definition "
                "FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid "
                "WHERE rel.relname = :t AND con.conname = :n AND con.contype = 'c'"
            ),
            {"t": table, "n": f"ck_{table}_exactly_one_owner"},
        )
    ).one()
    assert row.convalidated is True
    assert row.definition == "CHECK ((num_nonnulls(guest_session_id, user_id) = 1))"


@pytest.mark.parametrize("table", _TABLES)
async def test_the_user_index_carries_its_columns_and_directions(
    session: AsyncSession, table: str
) -> None:
    name, columns = _INDEXES[table]
    indexdef = (
        await session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE tablename = :t AND indexname = :n"),
            {"t": table, "n": name},
        )
    ).scalar_one()
    assert indexdef == f"CREATE INDEX {name} ON public.{table} USING btree {columns}"


# --- up -> down -> up, and the refusing downgrade -------------------------------------------------


@pytest.mark.usefixtures("_migrated")
def test_migration_03494836ce30_up_down_up(settings: Settings) -> None:
    """At head: `user_id` on all three, `guest_session_id` nullable, the CHECK present. At
    `1a2676aa3759`: all three undone, `guest_session_id` `NOT NULL` again. Head again after. Recovery
    to head is unconditional (the sibling 2.2 test's pattern)."""
    url = _test_url(settings)
    config = _config(url)
    assert _schema_at(url) == _AT_HEAD

    failure: BaseException | None = None
    try:
        command.downgrade(config, _DOWN)
        assert _schema_at(url) == _BELOW
    except BaseException as exc:  # recovery below must run whatever happened
        failure = exc
    finally:
        command.upgrade(config, "head")
    assert _schema_at(url) == _AT_HEAD
    if failure is not None:
        raise failure


async def _commit(url: str, work: Callable[[AsyncSession], Awaitable[None]]) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await work(session)
            await session.commit()
    finally:
        await engine.dispose()


def _refused_downgrade_leaves_head(url: str, match: str) -> None:
    with pytest.raises(RuntimeError, match=match):
        command.downgrade(_config(url), _DOWN)
    assert _schema_at(url) == _AT_HEAD, "a refused downgrade moved the schema anyway"


@pytest.mark.usefixtures("_migrated")
def test_downgrade_refuses_while_a_user_owned_row_exists(settings: Settings) -> None:
    url = _test_url(settings)
    now = datetime.now(UTC).replace(microsecond=0)
    user_id = UserId(uuid7())

    async def seed(session: AsyncSession) -> None:
        await SqlAlchemyUserRepository(session).add(
            User.register_with_password(
                id=user_id,
                email=EmailAddress.parse(f"refusal-{user_id.value}@example.com"),
                password_hash=_PASSWORD_HASH,
                at=now,
            )
        )
        await session.flush()
        await SqlAlchemyJobPostingRepository(session).add(pasted_posting(UserOwner(user_id), now))

    async def clean(session: AsyncSession) -> None:
        await session.execute(user_table.delete().where(user_table.c.id == user_id))

    asyncio.run(_commit(url, seed))
    try:
        _refused_downgrade_leaves_head(
            url,
            "refusing to downgrade 03494836ce30: posting_job_posting holds rows owned by "
            "registered users",
        )
        assert (
            asyncio.run(
                _scalar(
                    url,
                    "SELECT count(*) FROM posting_job_posting WHERE user_id = :u",
                    u=user_id.value,
                )
            )
            == 1
        ), "the refused downgrade must never delete the row it refused over"
    finally:
        asyncio.run(_commit(url, clean))


@pytest.mark.usefixtures("_migrated")
def test_downgrade_refuses_while_a_base_cv_deleted_run_exists(settings: Settings) -> None:
    """The second refusal is only reachable with no user-owned row anywhere, so the run is a
    guest's: a reason the previous release cannot load, whoever owns it."""
    url = _test_url(settings)
    now = datetime.now(UTC).replace(microsecond=0)
    session_id: list[GuestSessionId] = []

    async def seed(session: AsyncSession) -> None:
        sessions = SqlAlchemyGuestSessionRepository(session)
        guest = GuestSession.start(
            id=sessions.next_identity(), token_hash="9c" * 32, at=now, ttl_hours=24
        )
        await sessions.add(guest)
        await session.flush()
        session_id.append(guest.id)
        run = running_run(GuestOwner(guest.id), now)
        run.mark_failed(TailoringFailureReason.BASE_CV_DELETED, now)
        await SqlAlchemyTailoringRunRepository(session).add(run)

    async def clean(session: AsyncSession) -> None:
        await session.execute(
            guest_session_table.delete().where(guest_session_table.c.id == session_id[0])
        )

    asyncio.run(_commit(url, seed))
    try:
        _refused_downgrade_leaves_head(
            url,
            "refusing to downgrade 03494836ce30: tailoring_run holds runs that failed with "
            "base_cv_deleted",
        )
    finally:
        asyncio.run(_commit(url, clean))


def test_the_offline_downgrade_renders_both_refusals_in_sql(settings: Settings) -> None:
    """`alembic downgrade 03494836ce30:1a2676aa3759 --sql`: no connection to ask, so each refusal
    travels as a `DO` block that raises — and the script renders at all (a quote in a sentence
    would break the literal)."""
    buffer = io.StringIO()
    command.downgrade(
        _config(_test_url(settings), output_buffer=buffer), f"{_TARGET}:{_DOWN}", sql=True
    )
    script = buffer.getvalue()

    for table in _TABLES:
        assert (
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {table} WHERE user_id IS NOT NULL) THEN RAISE "  # noqa: S608 -- expected output, never executed
            f"EXCEPTION 'refusing to downgrade 03494836ce30: {table} holds rows owned by"
        ) in script
    assert (
        "IF EXISTS (SELECT 1 FROM tailoring_run WHERE failure_reason = 'base_cv_deleted') THEN "
        "RAISE EXCEPTION 'refusing to downgrade 03494836ce30: tailoring_run holds runs that failed"
    ) in script
    assert script.index("RAISE EXCEPTION") < script.index("DROP COLUMN user_id")


# --- AC-17: the running 2.2 code's INSERTs, and the two ways to break the CHECK --------------------


async def _guest(session: AsyncSession, clock: FixedClock, token: str) -> GuestSessionId:
    sessions = SqlAlchemyGuestSessionRepository(session)
    guest = GuestSession.start(
        id=sessions.next_identity(), token_hash=token * 32, at=clock.now(), ttl_hours=24
    )
    await sessions.add(guest)
    await session.flush()
    return guest.id


async def _user(session: AsyncSession, clock: FixedClock) -> UserId:
    users = SqlAlchemyUserRepository(session)
    user_id = users.next_identity()
    await users.add(
        User.register_with_password(
            id=user_id,
            email=EmailAddress.parse(f"ac17-{user_id.value}@example.com"),
            password_hash=_PASSWORD_HASH,
            at=clock.now(),
        )
    )
    await session.flush()
    return user_id


async def _a_2_2_row(
    session: AsyncSession, clock: FixedClock, table: str
) -> tuple[Table, dict[str, object]]:
    """A guest-owned row of `table`, written through its repository, read back and **removed**, as a
    column dict without `user_id` — the INSERT 2.2's code issues, ready to replay."""
    owner = GuestOwner(await _guest(session, clock, "5e"))
    run = succeeded_run(owner, clock.now())
    row_id: object
    if table == "posting_job_posting":
        posting = pasted_posting(owner, clock.now())
        await SqlAlchemyJobPostingRepository(session).add(posting)
        mapped, row_id = job_posting_table, posting.id
    elif table == "tailoring_run":
        await SqlAlchemyTailoringRunRepository(session).add(run)
        mapped, row_id = tailoring_run_table, run.id
    else:
        job = queued_export(owner, run, clock.now())
        await SqlAlchemyExportJobRepository(session).add(job)
        mapped, row_id = export_job_table, job.id
    await session.flush()
    values = dict(
        (await session.execute(select(mapped).where(mapped.c.id == row_id))).mappings().one()
    )
    session.expunge_all()
    await session.execute(mapped.delete().where(mapped.c.id == row_id))
    assert values.pop("user_id") is None
    return mapped, values


@pytest.mark.parametrize("table", _TABLES)
async def test_the_2_2_insert_shape_still_succeeds(
    session: AsyncSession, clock: FixedClock, table: str
) -> None:
    mapped, values = await _a_2_2_row(session, clock, table)

    await session.execute(mapped.insert().values(**values))  # no user_id named

    stored = (
        await session.execute(select(mapped.c.user_id).where(mapped.c.id == values["id"]))
    ).scalar_one()
    assert stored is None


@pytest.mark.parametrize("table", _TABLES)
async def test_a_row_with_both_owners_is_refused_by_the_check_by_name(
    session: AsyncSession, clock: FixedClock, table: str
) -> None:
    mapped, values = await _a_2_2_row(session, clock, table)
    user_id = await _user(session, clock)

    with pytest.raises(IntegrityError) as exc_info:
        async with session.begin_nested():
            await session.execute(mapped.insert().values(**values, user_id=user_id))

    assert violated_constraint(exc_info.value) == f"ck_{table}_exactly_one_owner"


@pytest.mark.parametrize("table", _TABLES)
async def test_a_row_with_neither_owner_is_refused_by_the_check_by_name(
    session: AsyncSession, clock: FixedClock, table: str
) -> None:
    mapped, values = await _a_2_2_row(session, clock, table)
    values["guest_session_id"] = None

    with pytest.raises(IntegrityError) as exc_info:
        async with session.begin_nested():
            await session.execute(mapped.insert().values(**values))

    assert violated_constraint(exc_info.value) == f"ck_{table}_exactly_one_owner"
