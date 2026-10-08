"""AC-11, AC-12, AC-13: migration `b10d1c777b0a` and `export_job.layout_template` (3.2, T14,
test-after).

Everything is read from the catalogue by name, never from the migration's own comments. Plan §0.12
and AC-11 are the source of truth for what must exist.

- **Back-fill** is proven on rows inserted in the *previous* schema's shape (no `layout_template`
  column) *before* upgrading: downgrade to `7e43a47327ec`, insert, upgrade.
- **Up/down/up** on a database holding guest and user jobs of both formats; the **downgrade
  refuses** while a non-classic row exists (nothing dropped, nothing deleted), and drops the CHECK
  and the column cleanly otherwise. Recovery to head is unconditional and committed seeds are
  deleted afterwards.
- **No pinned head**: "at head" means `ScriptDirectory.get_current_head()`.
- `LayoutTemplateType` refuses an unknown stored value with a `ValueError` naming the column.
- `find_latest_for_key` has `IS NOT DISTINCT FROM` semantics (a `None` lookup finds DOCX jobs, a
  layout lookup never finds one) and the plan uses `ix_export_job_tailoring_run_id`.

Plain `def` for the migration tests (Alembic's `env.py` runs its own `asyncio.run`).
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
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId, LayoutTemplate
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.persistence.database import violated_constraint
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.settings import Settings

_REVISION: Final = "b10d1c777b0a"
_DOWN_REVISION: Final = "7e43a47327ec"
_CHECK: Final = "ck_export_job_layout_template_only_for_pdf"
_NOW: Final = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
_PHC: Final = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"

_OLD_SHAPE_INSERT: Final = (
    "INSERT INTO export_job (id, user_id, tailoring_run_id, document, format, run_version, "
    "status, requested_at, version) "
    "VALUES (:i, :u, :r, 'cv', :f, 1, 'queued', :t, 1)"
)
_NEW_SHAPE_INSERT: Final = (
    "INSERT INTO export_job (id, user_id, tailoring_run_id, document, format, run_version, "
    "status, requested_at, version, layout_template) "
    "VALUES (:i, :u, :r, 'cv', :f, 1, 'queued', :t, 1, :l)"
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


def _column_exists(url: str) -> bool:
    return bool(
        asyncio.run(
            _scalar(
                url,
                "SELECT count(*) = 1 FROM information_schema.columns "
                "WHERE table_name = 'export_job' AND column_name = 'layout_template'",
            )
        )
    )


def _check_exists(url: str) -> bool:
    return bool(
        asyncio.run(
            _scalar(url, "SELECT count(*) = 1 FROM pg_constraint WHERE conname = :n", n=_CHECK)
        )
    )


def _revision(url: str) -> str:
    return str(asyncio.run(_scalar(url, "SELECT version_num FROM alembic_version")))


async def _seed_user(session: AsyncSession, user_id: UUID) -> None:
    await session.execute(
        text(
            "INSERT INTO identity_user (id, email, password_hash, created_at, password_updated_at) "
            "VALUES (:i, :e, :p, :t, :t)"
        ),
        {"i": user_id, "e": f"layout-migration-{user_id}@example.com", "p": _PHC, "t": _NOW},
    )


def _delete_user(url: str, user_id: UUID) -> None:
    async def clean(session: AsyncSession) -> None:
        # `export_job.user_id` cascades.
        await session.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})

    asyncio.run(_commit(url, clean))


# --- AC-11: back-fill on rows written in the previous schema's shape ----------------------------


@pytest.mark.usefixtures("_migrated")
def test_upgrade_back_fills_classic_on_pdf_rows_written_before_it_and_leaves_docx_null(
    settings: Settings,
) -> None:
    url = _test_url(settings)
    config = _config(url)
    user_id = uuid4()
    pdf_id, docx_id = uuid4(), uuid4()

    async def seed(session: AsyncSession) -> None:
        await _seed_user(session, user_id)
        for job_id, fmt in ((pdf_id, "pdf"), (docx_id, "docx")):
            await session.execute(
                text(_OLD_SHAPE_INSERT),
                {"i": job_id, "u": user_id, "r": uuid4(), "f": fmt, "t": _NOW},
            )

    failure: BaseException | None = None
    try:
        command.downgrade(config, _DOWN_REVISION)
        assert not _column_exists(url)
        asyncio.run(_commit(url, seed))
        command.upgrade(config, "head")
        got = {
            row.id: row.layout_template
            for row in asyncio.run(
                _rows(
                    url,
                    "SELECT id, layout_template FROM export_job WHERE user_id = :u",
                    u=user_id,
                )
            )
        }
        assert got == {pdf_id: "classic", docx_id: None}
    except BaseException as exc:  # recovery below must run whatever happened
        failure = exc
    finally:
        if _revision(url) != _head():
            command.upgrade(config, "head")
        _delete_user(url, user_id)
    if failure is not None:
        raise failure


@pytest.mark.usefixtures("_migrated")
def test_up_down_up_on_a_database_holding_guest_and_user_jobs_of_both_formats(
    settings: Settings,
) -> None:
    """Classic-only data: the downgrade drops the CHECK then the column; head restores both and the
    back-fill keeps the PDFs classic. (Guest-owned rows: the guest FK is nullable on this table, and
    the 'exactly one owner' CHECK is satisfied by a user row; a guest session row is seeded too.)"""
    url = _test_url(settings)
    config = _config(url)
    user_id, guest_id = uuid4(), uuid4()

    async def seed(session: AsyncSession) -> None:
        await _seed_user(session, user_id)
        await session.execute(
            text(
                "INSERT INTO identity_guest_session (id, token_hash, created_at, expires_at) "
                "VALUES (:i, :h, :t, :e)"
            ),
            {
                "i": guest_id,
                "h": "ab" * 32,
                "t": _NOW,
                "e": datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC),
            },
        )
        for fmt in ("pdf", "docx"):
            await session.execute(
                text(_NEW_SHAPE_INSERT),
                {
                    "i": uuid4(),
                    "u": user_id,
                    "r": uuid4(),
                    "f": fmt,
                    "t": _NOW,
                    "l": "classic" if fmt == "pdf" else None,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO export_job (id, guest_session_id, tailoring_run_id, document, "
                    "format, run_version, status, requested_at, version, layout_template) "
                    "VALUES (:i, :g, :r, 'cv', :f, 1, 'queued', :t, 1, :l)"
                ),
                {
                    "i": uuid4(),
                    "g": guest_id,
                    "r": uuid4(),
                    "f": fmt,
                    "t": _NOW,
                    "l": "classic" if fmt == "pdf" else None,
                },
            )

    asyncio.run(_commit(url, seed))
    failure: BaseException | None = None
    try:
        assert _column_exists(url)
        assert _check_exists(url)
        command.downgrade(config, _DOWN_REVISION)
        assert not _column_exists(url), "the downgrade must drop the column"
        assert not _check_exists(url), "the downgrade must drop the CHECK"
        assert _revision(url) == _DOWN_REVISION
        assert (
            asyncio.run(
                _scalar(
                    url,
                    "SELECT count(*) FROM export_job WHERE user_id = :u OR guest_session_id = :g",
                    u=user_id,
                    g=guest_id,
                )
            )
            == 4
        ), "a clean downgrade deletes no job"
        command.upgrade(config, "head")
        assert _column_exists(url)
        assert _check_exists(url)
        layouts = asyncio.run(
            _rows(
                url,
                "SELECT format, layout_template FROM export_job "
                "WHERE user_id = :u OR guest_session_id = :g",
                u=user_id,
                g=guest_id,
            )
        )
        assert sorted((r.format, r.layout_template) for r in layouts) == [
            ("docx", None),
            ("docx", None),
            ("pdf", "classic"),
            ("pdf", "classic"),
        ]
    except BaseException as exc:
        failure = exc
    finally:
        if _revision(url) != _head():
            command.upgrade(config, "head")

        async def clean(session: AsyncSession) -> None:
            await session.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": user_id})
            await session.execute(
                text("DELETE FROM identity_guest_session WHERE id = :i"), {"i": guest_id}
            )

        asyncio.run(_commit(url, clean))
    if failure is not None:
        raise failure


@pytest.mark.usefixtures("_migrated")
def test_the_downgrade_refuses_while_a_formal_row_exists_deletes_nothing_and_then_goes_clean(
    settings: Settings,
) -> None:
    url = _test_url(settings)
    config = _config(url)
    user_id, formal_id = uuid4(), uuid4()

    async def seed(session: AsyncSession) -> None:
        await _seed_user(session, user_id)
        await session.execute(
            text(_NEW_SHAPE_INSERT),
            {"i": formal_id, "u": user_id, "r": uuid4(), "f": "pdf", "t": _NOW, "l": "formal"},
        )

    asyncio.run(_commit(url, seed))
    failure: BaseException | None = None
    try:
        with pytest.raises(
            RuntimeError, match=f"refusing to downgrade {_REVISION}: export_job holds 1 "
        ):
            command.downgrade(config, _DOWN_REVISION)
        assert _revision(url) == _head(), "a refused downgrade moved the schema anyway"
        assert _column_exists(url), "the refused downgrade dropped the column"
        assert _check_exists(url), "the refused downgrade dropped the CHECK"
        assert (
            asyncio.run(
                _scalar(
                    url,
                    "SELECT layout_template FROM export_job WHERE id = :i",
                    i=formal_id,
                )
            )
            == "formal"
        ), "the refused downgrade must never touch the row it refused over"

        # The clean path: the operator removes the offending job, then the downgrade goes through.
        async def remove(session: AsyncSession) -> None:
            await session.execute(text("DELETE FROM export_job WHERE id = :i"), {"i": formal_id})

        asyncio.run(_commit(url, remove))
        command.downgrade(config, _DOWN_REVISION)
        assert not _column_exists(url)
        assert not _check_exists(url)
    except BaseException as exc:
        failure = exc
    finally:
        if _revision(url) != _head():
            command.upgrade(config, "head")
        _delete_user(url, user_id)
    if failure is not None:
        raise failure


def test_the_offline_downgrade_renders_the_refusal_in_sql_before_any_drop(
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
    assert f"refusing to downgrade {_REVISION}" in script
    assert script.index("RAISE EXCEPTION") < script.index("DROP CONSTRAINT")
    assert script.index("DROP CONSTRAINT") < script.index("DROP COLUMN")


@pytest.mark.usefixtures("_migrated")
def test_the_migration_descends_directly_from_the_tracking_migration_and_there_is_one_head(
    settings: Settings,
) -> None:
    script = ScriptDirectory.from_config(_config(settings.test_database_url))
    revision = script.get_revision(_REVISION)
    assert revision is not None
    assert revision.down_revision == _DOWN_REVISION
    assert len(script.get_heads()) == 1


# --- AC-11/AC-12: the catalogue, by name ---------------------------------------------------------


async def test_the_column_is_a_nullable_varchar_32(session: AsyncSession) -> None:
    row = (
        await session.execute(
            text(
                "SELECT data_type, character_maximum_length, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'export_job' "
                "AND column_name = 'layout_template'"
            )
        )
    ).one()
    assert (row.data_type, row.character_maximum_length, row.is_nullable) == (
        "character varying",
        32,
        "YES",
    )
    assert row.column_default is None


async def test_the_check_exists_by_name_is_validated_and_pairs_format_with_layout(
    session: AsyncSession,
) -> None:
    row = (
        await session.execute(
            text(
                "SELECT con.contype::text AS kind, con.convalidated, "
                "pg_get_constraintdef(con.oid) AS definition "
                "FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid "
                "WHERE rel.relname = 'export_job' AND con.conname = :n"
            ),
            {"n": _CHECK},
        )
    ).one()
    assert row.kind == "c"
    assert row.convalidated is True
    # Read the semantics, not PostgreSQL's parenthesisation.
    assert "format" in row.definition
    assert "pdf" in row.definition
    assert "layout_template IS NOT NULL" in row.definition


async def test_no_value_check_on_the_layout_ids_exists(session: AsyncSession) -> None:
    """Plan §0.12: the decorator is the authority on which ids exist, so a fourth layout is not a
    migration. The only CHECK that mentions the column is the format pairing."""
    definitions = (
        (
            await session.execute(
                text(
                    "SELECT pg_get_constraintdef(con.oid) FROM pg_constraint con "
                    "JOIN pg_class rel ON rel.oid = con.conrelid "
                    "WHERE rel.relname = 'export_job' AND con.contype = 'c' "
                    "AND pg_get_constraintdef(con.oid) LIKE '%layout_template%'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(definitions) == 1, definitions
    assert "classic" not in definitions[0]


async def test_no_other_index_was_added_by_the_migration(session: AsyncSession) -> None:
    names = set(
        (
            await session.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'export_job'")
            )
        )
        .scalars()
        .all()
    )
    assert not {n for n in names if "layout" in n}


# --- AC-12: the CHECK refuses by name; the type refuses an unknown value -------------------------


async def _user(session: AsyncSession) -> UUID:
    user_id = uuid4()
    await _seed_user(session, user_id)
    return user_id


async def _refused_by(session: AsyncSession, **params: Any) -> str:
    with pytest.raises(IntegrityError) as raised:
        async with session.begin_nested():
            await session.execute(text(_NEW_SHAPE_INSERT), params)
    name = violated_constraint(raised.value)
    assert name is not None
    return name


async def test_a_pdf_row_with_no_layout_is_refused_by_the_check(session: AsyncSession) -> None:
    user_id = await _user(session)
    assert (
        await _refused_by(session, i=uuid4(), u=user_id, r=uuid4(), f="pdf", t=_NOW, l=None)
        == _CHECK
    )


async def test_a_docx_row_with_a_layout_is_refused_by_the_check(session: AsyncSession) -> None:
    user_id = await _user(session)
    assert (
        await _refused_by(session, i=uuid4(), u=user_id, r=uuid4(), f="docx", t=_NOW, l="classic")
        == _CHECK
    )


@pytest.mark.parametrize("layout", list(LayoutTemplate), ids=lambda m: m.value)
async def test_every_layout_is_accepted_on_a_pdf_row_and_a_docx_row_with_none(
    session: AsyncSession, layout: LayoutTemplate
) -> None:
    user_id = await _user(session)
    await session.execute(
        text(_NEW_SHAPE_INSERT),
        {"i": uuid4(), "u": user_id, "r": uuid4(), "f": "pdf", "t": _NOW, "l": layout.value},
    )
    await session.execute(
        text(_NEW_SHAPE_INSERT),
        {"i": uuid4(), "u": user_id, "r": uuid4(), "f": "docx", "t": _NOW, "l": None},
    )


async def test_a_stored_unknown_layout_is_refused_on_load_naming_the_column_not_the_value(
    session: AsyncSession,
) -> None:
    """The database accepts any string on a PDF row (no value CHECK, on purpose); the type refuses
    it on the way out."""
    user_id = await _user(session)
    job_id = uuid4()
    await session.execute(
        text(_NEW_SHAPE_INSERT),
        {"i": job_id, "u": user_id, "r": uuid4(), "f": "pdf", "t": _NOW, "l": "brutalist"},
    )
    repo = SqlAlchemyExportJobRepository(session)
    session.expunge_all()

    with pytest.raises(ValueError, match=r"export_job\.layout_template") as raised:
        await repo.get(ExportJobId(value=job_id))

    assert "brutalist" not in str(raised.value)
    assert str(job_id) not in str(raised.value)


# --- AC-13: IS NOT DISTINCT FROM semantics and the plan -----------------------------------------


async def _raw_job(
    session: AsyncSession, user_id: UUID, run_id: UUID, fmt: str, layout: str | None
) -> UUID:
    job_id = uuid4()
    await session.execute(
        text(_NEW_SHAPE_INSERT),
        {"i": job_id, "u": user_id, "r": run_id, "f": fmt, "t": _NOW, "l": layout},
    )
    return job_id


async def test_a_docx_lookup_with_none_finds_the_docx_job_and_never_a_pdf(
    session: AsyncSession,
) -> None:
    user_id = await _user(session)
    run_id = uuid4()
    docx = await _raw_job(session, user_id, run_id, "docx", None)
    await _raw_job(session, user_id, run_id, "pdf", "classic")
    repo = SqlAlchemyExportJobRepository(session)

    found = await repo.find_latest_for_key(
        TailoringRunId(value=run_id), TailoredDocumentKind.CV, ExportFormat.DOCX, None
    )

    assert found is not None
    assert found.id.value == docx
    assert found.layout_template is None


async def test_a_pdf_lookup_matches_only_its_own_layout(session: AsyncSession) -> None:
    user_id = await _user(session)
    run_id = uuid4()
    await _raw_job(session, user_id, run_id, "pdf", "classic")
    formal = await _raw_job(session, user_id, run_id, "pdf", "formal")
    repo = SqlAlchemyExportJobRepository(session)
    run = TailoringRunId(value=run_id)

    got_formal = await repo.find_latest_for_key(
        run, TailoredDocumentKind.CV, ExportFormat.PDF, LayoutTemplate.FORMAL
    )
    got_modern = await repo.find_latest_for_key(
        run, TailoredDocumentKind.CV, ExportFormat.PDF, LayoutTemplate.MODERN
    )

    assert got_formal is not None
    assert got_formal.id.value == formal
    assert got_modern is None


async def test_a_none_lookup_for_a_pdf_finds_nothing_because_a_pdf_always_has_a_layout(
    session: AsyncSession,
) -> None:
    user_id = await _user(session)
    run_id = uuid4()
    await _raw_job(session, user_id, run_id, "pdf", "classic")
    repo = SqlAlchemyExportJobRepository(session)

    found = await repo.find_latest_for_key(
        TailoringRunId(value=run_id), TailoredDocumentKind.CV, ExportFormat.PDF, None
    )

    assert found is None


async def test_the_lookup_is_planned_through_the_run_index_with_other_runs_rows_seeded_first(
    session: AsyncSession, connection: AsyncConnection
) -> None:
    """Other runs' rows are seeded first and the table analysed, so the predicate is selective and
    the index is the planner's own choice (an EXPLAIN over one run's rows correctly seq-scans)."""
    from sqlalchemy import event

    user_id = await _user(session)
    run_id = uuid4()
    await _raw_job(session, user_id, run_id, "pdf", "classic")
    await session.execute(
        text(
            "INSERT INTO export_job (id, user_id, tailoring_run_id, document, format, run_version, "
            "status, requested_at, version, layout_template) "
            "SELECT gen_random_uuid(), :u, gen_random_uuid(), 'cv', 'pdf', 1, 'queued', :t, 1, "
            "'classic' FROM generate_series(1, 400)"
        ),
        {"u": user_id, "t": _NOW},
    )
    await session.execute(text("ANALYZE export_job"))
    repo = SqlAlchemyExportJobRepository(session)

    captured: list[tuple[str, Any]] = []

    def capture(conn: Any, cursor: Any, statement: str, parameters: Any, *_a: Any) -> None:
        captured.append((statement, parameters))

    sync_engine = connection.sync_connection.engine  # type: ignore[union-attr]
    event.listen(sync_engine, "before_cursor_execute", capture)
    try:
        await repo.find_latest_for_key(
            TailoringRunId(value=run_id),
            TailoredDocumentKind.CV,
            ExportFormat.PDF,
            LayoutTemplate.CLASSIC,
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture)

    selects = [c for c in captured if c[0].lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1
    statement, parameters = selects[0]
    plan = await connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)
    text_plan = "\n".join(row[0] for row in plan)
    assert "ix_export_job_tailoring_run_id" in text_plan, text_plan
    assert "Seq Scan on export_job" not in text_plan, text_plan
