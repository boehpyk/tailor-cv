"""AC-14, AC-15 (CHECKs), AC-16: migration `7e43a47327ec` and the schema it creates (slice 3.1, T19,
test-after).

**Everything is read from the catalogue by name** (`pg_constraint`, `pg_index`, `pg_indexes`,
`information_schema`), never from the migration's own comments. Plan §5 and AC-14 are the source of
truth for what must exist.

- **Up/down/up** on an empty table, and **the downgrade refuses while any row exists** — the
  opposite promise to `b1b518fe84b1` (one-time tokens, dropped freely): a card is something a user
  chose to keep (2.2's and 2.3's precedent). The refused downgrade must leave the schema and the row
  where they were. Recovery to head is unconditional, and the committed seed is deleted afterwards.
- **The offline render** carries the refusal in SQL (a `DO` block), as 2.3's does.
- **No pinned head.** "At head" means `ScriptDirectory.get_current_head()`; this revision id appears
  only in tests that are about *this* revision (CLAUDE.md, 2.5's `0cc192c`).
- **Autogenerate against the mapping is empty**, with the options `alembic/env.py` uses.
- **AC-16's schema guard**: `tracking_application` has **no `guest_session_id` column**, and
  `user_id` is `NOT NULL`. This is the type-level decision of contrast 2, mirrored in the schema;
  a column added later turns it red by name.
- **AC-15's CHECKs**: a raw `INSERT` violating each fails on its **named constraint**, recognised
  through `violated_constraint`, never by message (which the engine withholds on purpose).

Plain `def` tests for the up/down/up (Alembic's `env.py` runs its own `asyncio.run`, which refuses to
nest inside pytest-asyncio's loop), `async def` for catalogue reads.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.registry import metadata
from tailorcraft.infrastructure.settings import Settings

_REVISION: Final = "7e43a47327ec"
_DOWN_REVISION: Final = "b1b518fe84b1"
_TABLE: Final = "tracking_application"
_INDEX: Final = "ix_tracking_application_user_id_stage_changed_at"

_NOW: Final = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
_PHC: Final = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"

_INSERT: Final = (
    "INSERT INTO tracking_application "
    "(id, user_id, tailoring_run_id, stage, title, tracked_at, stage_changed_at, version) "
    "VALUES (:i, :u, :r, :s, :t, :a, :c, 1)"
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


async def _scalar(url: str, sql: str, **params: object) -> Any:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar_one()
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


def _table_exists(url: str) -> bool:
    return bool(
        asyncio.run(_scalar(url, "SELECT to_regclass('public.tracking_application') IS NOT NULL"))
    )


def _revision(url: str) -> str:
    return str(asyncio.run(_scalar(url, "SELECT version_num FROM alembic_version")))


async def _seed_user(session: AsyncSession, user_id: UUID) -> None:
    await session.execute(
        text(
            "INSERT INTO identity_user (id, email, password_hash, created_at, password_updated_at) "
            "VALUES (:i, :e, :p, :t, :t)"
        ),
        {"i": user_id, "e": f"migration-{user_id}@example.com", "p": _PHC, "t": _NOW},
    )


async def _seed_card(session: AsyncSession, user_id: UUID, **overrides: Any) -> UUID:
    params: dict[str, Any] = {
        "i": uuid4(),
        "u": user_id,
        "r": uuid4(),
        "s": "to_apply",
        "t": None,
        "a": _NOW,
        "c": _NOW,
    }
    params.update(overrides)
    await session.execute(text(_INSERT), params)
    return UUID(str(params["i"]))


# --- up -> down -> up, and the refusing downgrade -----------------------------------------------


@pytest.mark.usefixtures("_migrated")
def test_migration_7e43a47327ec_up_down_up_on_an_empty_table(settings: Settings) -> None:
    """At head the table and its index exist; one step down drops both; head again restores both.
    Recovery to head is unconditional, whatever happened in between."""
    url = _test_url(settings)
    config = _config(url)
    assert _table_exists(url), "the table must exist at head"
    failure: BaseException | None = None
    try:
        command.downgrade(config, _DOWN_REVISION)
        assert not _table_exists(url), "the downgrade must drop the table"
        assert asyncio.run(
            _scalar(
                url,
                "SELECT to_regclass('public.ix_tracking_application_user_id_stage_changed_at') IS NULL",
            )
        )
        assert _revision(url) == _DOWN_REVISION
    except BaseException as exc:  # recovery below must run whatever happened
        failure = exc
    finally:
        command.upgrade(config, "head")
    assert _table_exists(url), "schema must be back at head"
    assert _revision(url) == _head()
    assert (
        asyncio.run(_scalar(url, "SELECT count(*) FROM pg_indexes WHERE indexname = :n", n=_INDEX))
        == 1
    )
    if failure is not None:
        raise failure


@pytest.mark.usefixtures("_migrated")
def test_the_downgrade_refuses_while_a_card_exists_and_deletes_nothing(settings: Settings) -> None:
    """AC-14: a refusal that names its reason; the table, the row and the recorded revision are
    exactly where they were afterwards (a kept card is never deleted to make a downgrade fit)."""
    url = _test_url(settings)
    user_id = uuid4()
    holder: dict[str, UUID] = {}

    async def seed(session: AsyncSession) -> None:
        await _seed_user(session, user_id)
        holder["card"] = await _seed_card(session, user_id)

    async def clean(session: AsyncSession) -> None:
        await session.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})

    asyncio.run(_commit(url, seed))
    try:
        with pytest.raises(
            RuntimeError, match=f"refusing to downgrade {_REVISION}: tracking_application holds"
        ):
            command.downgrade(_config(url), _DOWN_REVISION)
        assert _revision(url) == _head(), "a refused downgrade moved the schema anyway"
        assert _table_exists(url)
        assert (
            asyncio.run(
                _scalar(
                    url, "SELECT count(*) FROM tracking_application WHERE id = :i", i=holder["card"]
                )
            )
            == 1
        ), "the refused downgrade must never delete the row it refused over"
    finally:
        asyncio.run(_commit(url, clean))


def test_the_offline_downgrade_renders_the_refusal_in_sql(settings: Settings) -> None:
    """`alembic downgrade 7e43a47327ec:b1b518fe84b1 --sql`: no connection to ask, so the refusal
    travels as a `DO` block that raises — and it comes before any `DROP`."""
    buffer = io.StringIO()
    command.downgrade(
        _config(_test_url(settings), output_buffer=buffer),
        f"{_REVISION}:{_DOWN_REVISION}",
        sql=True,
    )
    script = buffer.getvalue()

    assert (
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM tracking_application) THEN RAISE EXCEPTION "  # noqa: S608 -- expected output, never executed
        f"'refusing to downgrade {_REVISION}: tracking_application holds"
    ) in script
    assert script.index("RAISE EXCEPTION") < script.index("DROP TABLE tracking_application")


@pytest.mark.usefixtures("_migrated")
def test_the_migration_descends_directly_from_the_one_time_token_migration(
    settings: Settings,
) -> None:
    """A fork or a skipped parent would let the deploy run two heads."""
    script = ScriptDirectory.from_config(_config(settings.test_database_url))
    revision = script.get_revision(_REVISION)
    assert revision is not None
    assert revision.down_revision == _DOWN_REVISION
    assert len(script.get_heads()) == 1, (
        "two alembic heads: the deploy's `upgrade head` would refuse"
    )


async def test_autogenerate_against_head_produces_an_empty_diff(
    connection: AsyncConnection,
) -> None:
    """AC-14: the mapped metadata and the migrated database agree exactly, with the options
    `alembic/env.py` runs autogenerate with."""

    def _diff(sync_conn: Connection) -> Sequence[object]:
        context = MigrationContext.configure(
            sync_conn, opts={"compare_type": True, "compare_server_default": True}
        )
        return list(compare_metadata(context, metadata))

    assert await connection.run_sync(_diff) == []


# --- the catalogue, by name (AC-14) -------------------------------------------------------------


async def _constraint(session: AsyncSession, name: str) -> Any:
    return (
        await session.execute(
            text(
                "SELECT con.contype::text AS kind, con.convalidated, "
                "  con.confdeltype::text AS on_delete, target.relname AS target, "
                "  pg_get_constraintdef(con.oid) AS definition "
                "FROM pg_constraint con "
                "JOIN pg_class rel ON rel.oid = con.conrelid "
                "LEFT JOIN pg_class target ON target.oid = con.confrelid "
                "WHERE rel.relname = :t AND con.conname = :n"
            ),
            {"t": _TABLE, "n": name},
        )
    ).one()


async def test_the_primary_key_is_named(session: AsyncSession) -> None:
    row = await _constraint(session, "pk_tracking_application")
    assert row.kind == "p"
    assert row.definition == "PRIMARY KEY (id)"


async def test_the_user_foreign_key_is_named_validated_and_cascades(
    session: AsyncSession,
) -> None:
    row = await _constraint(session, "fk_tracking_application_user_id_identity_user")
    assert row.kind == "f"
    assert row.target == "identity_user"
    assert row.on_delete == "c", "erasure takes the cards by cascade"
    assert row.convalidated is True
    assert row.definition.startswith("FOREIGN KEY (user_id) REFERENCES identity_user(id)")


async def test_the_run_column_is_unique_by_name(session: AsyncSession) -> None:
    row = await _constraint(session, "uq_tracking_application_tailoring_run_id")
    assert row.kind == "u"
    assert row.definition == "UNIQUE (tailoring_run_id)"


async def test_the_run_column_has_no_foreign_key_of_any_kind(session: AsyncSession) -> None:
    """ADR-0014/0016/0023: contexts' tables are not fused. The race an FK would close is closed by
    the repository's two locks (AC-17, AC-18), so an FK appearing here would be a design change."""
    foreign = (
        (
            await session.execute(
                text(
                    "SELECT pg_get_constraintdef(con.oid) FROM pg_constraint con "
                    "JOIN pg_class rel ON rel.oid = con.conrelid "
                    "WHERE rel.relname = :t AND con.contype = 'f'"
                ),
                {"t": _TABLE},
            )
        )
        .scalars()
        .all()
    )
    assert len(foreign) == 1, foreign
    assert "tailoring_run" not in foreign[0]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "ck_tracking_application_title_length",
            "CHECK (((title IS NULL) OR ((char_length(title) >= 1) AND (char_length(title) <= 120))))",
        ),
        (
            "ck_tracking_application_stage_changed_after_tracked",
            "CHECK ((stage_changed_at >= tracked_at))",
        ),
    ],
)
async def test_each_check_exists_by_name_is_validated_and_says_what_the_spec_says(
    session: AsyncSession, name: str, expected: str
) -> None:
    row = await _constraint(session, name)
    assert row.kind == "c"
    assert row.convalidated is True
    assert row.definition == expected


async def test_the_stage_check_lists_exactly_the_six_stages_the_domain_declares(
    session: AsyncSession,
) -> None:
    """Read the literals out of the definition rather than comparing PostgreSQL's deparsed text,
    which differs between minor versions in its parentheses."""
    import re

    from tailorcraft.domain.tracking.value_objects import ApplicationStage

    row = await _constraint(session, "ck_tracking_application_stage_known")
    assert row.kind == "c"
    assert row.convalidated is True
    assert re.findall(r"'([a-z_]+)'::character varying", row.definition) == [
        stage.value for stage in ApplicationStage
    ]


async def test_the_board_index_carries_its_columns_and_directions(session: AsyncSession) -> None:
    indexdef = (
        await session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE tablename = :t AND indexname = :n"),
            {"t": _TABLE, "n": _INDEX},
        )
    ).scalar_one()
    assert indexdef == (
        f"CREATE INDEX {_INDEX} ON public.{_TABLE} USING btree "
        "(user_id, stage_changed_at DESC, id DESC)"
    )


async def test_the_table_has_exactly_the_planned_columns_with_the_planned_types(
    session: AsyncSession,
) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT column_name, data_type, is_nullable, character_maximum_length, "
                "datetime_precision, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t ORDER BY column_name"
            ),
            {"t": _TABLE},
        )
    ).all()
    by_name = {row.column_name: row for row in rows}
    assert set(by_name) == {
        "id",
        "user_id",
        "tailoring_run_id",
        "stage",
        "title",
        "tracked_at",
        "stage_changed_at",
        "version",
    }
    for uuid_column in ("id", "user_id", "tailoring_run_id"):
        assert (by_name[uuid_column].data_type, by_name[uuid_column].is_nullable) == ("uuid", "NO")
    assert (by_name["stage"].data_type, by_name["stage"].character_maximum_length) == (
        "character varying",
        16,
    )
    assert by_name["stage"].is_nullable == "NO"
    assert (by_name["title"].data_type, by_name["title"].is_nullable) == ("text", "YES")
    for instant in ("tracked_at", "stage_changed_at"):
        row = by_name[instant]
        assert (row.data_type, row.datetime_precision, row.is_nullable) == (
            "timestamp with time zone",
            0,
            "NO",
        ), instant
    assert (by_name["version"].data_type, by_name["version"].is_nullable) == ("integer", "NO")
    assert by_name["version"].column_default == "1"


# --- AC-16: no guest column, and the owner is NOT NULL ------------------------------------------


async def test_the_table_has_no_guest_session_id_column_and_user_id_is_not_null(
    session: AsyncSession,
) -> None:
    """AC-16's schema guard. 2.2's "exactly one owner" test and 2.4's "the claim re-keys every
    table with a `guest_session_id`" test enumerate tables **by that column**; this table stays
    outside both only while it has none. A `guest_session_id` added later (the purge, which deletes
    by that FK, could then reach a card) fails here by name."""
    columns = (
        await session.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": _TABLE},
        )
    ).all()
    names = {row.column_name for row in columns}
    assert "guest_session_id" not in names
    assert {row.column_name: row.is_nullable for row in columns}["user_id"] == "NO"


async def test_no_foreign_key_from_the_table_reaches_a_guest_table(session: AsyncSession) -> None:
    targets = (
        (
            await session.execute(
                text(
                    "SELECT target.relname FROM pg_constraint con "
                    "JOIN pg_class rel ON rel.oid = con.conrelid "
                    "JOIN pg_class target ON target.oid = con.confrelid "
                    "WHERE rel.relname = :t AND con.contype = 'f'"
                ),
                {"t": _TABLE},
            )
        )
        .scalars()
        .all()
    )
    assert list(targets) == ["identity_user"]


async def test_the_guest_work_tables_enumerated_by_the_catalogue_exclude_this_one(
    session: AsyncSession,
) -> None:
    """The very enumeration 2.4's claim test uses: every table carrying `guest_session_id`."""
    tables = (
        (
            await session.execute(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'guest_session_id'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert _TABLE not in tables
    assert tables, "the enumeration found nothing, so this test would pass vacuously"


# --- AC-15: each CHECK refuses on its own name --------------------------------------------------


async def _refused_by(session: AsyncSession, params: dict[str, Any]) -> str:
    """Run one hand-written INSERT and return the violated constraint's name, from the driver's
    diagnostics (the message itself is withheld by the engine's `handle_error` listener)."""
    with pytest.raises(IntegrityError) as raised:
        async with session.begin_nested():
            await session.execute(text(_INSERT), params)
    name = violated_constraint(raised.value)
    assert name is not None
    return name


def _row(user_id: UUID, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "i": uuid4(),
        "u": user_id,
        "r": uuid4(),
        "s": "to_apply",
        "t": None,
        "a": _NOW,
        "c": _NOW,
    }
    params.update(overrides)
    return params


async def _user(session: AsyncSession) -> UUID:
    user_id = uuid4()
    await _seed_user(session, user_id)
    return user_id


async def test_an_unknown_stage_is_refused_by_the_stage_known_check(session: AsyncSession) -> None:
    user_id = await _user(session)
    assert (
        await _refused_by(session, _row(user_id, s="ghosted"))
        == "ck_tracking_application_stage_known"
    )


async def test_every_stage_the_domain_declares_is_accepted(session: AsyncSession) -> None:
    """The CHECK's list and `ApplicationStage` agree: a member added to one only is a migration."""
    from tailorcraft.domain.tracking.value_objects import ApplicationStage

    user_id = await _user(session)
    for stage in ApplicationStage:
        await _seed_card(session, user_id, s=stage.value)  # raises if refused


@pytest.mark.parametrize("title", ["", "x" * 121], ids=["empty", "121-chars"])
async def test_a_title_outside_one_to_120_characters_is_refused_by_the_length_check(
    session: AsyncSession, title: str
) -> None:
    user_id = await _user(session)
    assert (
        await _refused_by(session, _row(user_id, t=title)) == "ck_tracking_application_title_length"
    )


@pytest.mark.parametrize("title", [None, "x", "x" * 120], ids=["null", "one", "120"])
async def test_a_null_title_and_the_boundary_lengths_are_accepted(
    session: AsyncSession, title: str | None
) -> None:
    user_id = await _user(session)
    await _seed_card(session, user_id, t=title)


async def test_a_stage_change_before_the_card_was_tracked_is_refused_by_its_check(
    session: AsyncSession,
) -> None:
    user_id = await _user(session)
    assert (
        await _refused_by(session, _row(user_id, c=_NOW - timedelta(seconds=1)))
        == "ck_tracking_application_stage_changed_after_tracked"
    )


async def test_a_stage_change_at_the_same_instant_as_tracking_is_accepted(
    session: AsyncSession,
) -> None:
    user_id = await _user(session)
    await _seed_card(session, user_id, c=_NOW)


async def test_a_second_card_for_one_run_is_refused_by_the_unique_constraint(
    session: AsyncSession,
) -> None:
    user_id = await _user(session)
    run = uuid4()
    await _seed_card(session, user_id, r=run)
    assert await _refused_by(session, _row(user_id, r=run)) == (
        "uq_tracking_application_tailoring_run_id"
    )


async def test_a_card_for_a_user_that_does_not_exist_is_refused_by_the_foreign_key(
    session: AsyncSession,
) -> None:
    assert await _refused_by(session, _row(uuid4())) == (
        "fk_tracking_application_user_id_identity_user"
    )


async def test_a_card_with_no_user_is_refused_as_a_not_null_violation(
    session: AsyncSession,
) -> None:
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(text(_INSERT), _row(uuid4(), u=None))
