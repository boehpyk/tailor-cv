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


# --- T30: the rest of the migration's catalogue -----------------------------------------------------

_TABLES = ("billing_credit_account", "billing_credit_entry", "demand_daily_counter")


def _load_revision() -> Any:
    import importlib.util
    from pathlib import Path

    (path,) = Path("alembic/versions").glob(f"*-{_REVISION}_*.py")
    spec = importlib.util.spec_from_file_location("billing_revision_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _rerun_backfill(url: str) -> None:
    """The revision's own `_backfill`, run again on a connection, with the two names it reads from
    Alembic (`op.get_bind`, `context.is_offline_mode`) answered by the connection and `False`: a second
    upgrade cannot be issued (the tables exist), so idempotence is shown on the function itself."""
    from types import SimpleNamespace

    module = _load_revision()
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:

            def run(sync_conn: Any) -> None:
                module.op = SimpleNamespace(get_bind=lambda: sync_conn)
                module.context = SimpleNamespace(is_offline_mode=lambda: False)
                module._backfill()

            await conn.run_sync(run)
    finally:
        await engine.dispose()


@pytest.mark.usefixtures("_migrated")
def test_the_backfill_is_idempotent_and_leaves_an_existing_account_and_its_entries_alone(
    settings: Settings,
) -> None:
    url = _test_url(settings)
    bare, opened = uuid4(), uuid4()

    async def seed_opened(session: AsyncSession) -> None:
        await _seed_user(opened)(session)
        await session.execute(
            text("INSERT INTO billing_credit_account (user_id, opened_at) VALUES (:u, :t)"),
            {"u": opened, "t": _NOW},
        )
        await session.execute(
            text(
                "INSERT INTO billing_credit_entry (id, user_id, kind, credits, created_at) "
                "VALUES (:i, :u, 'welcome', 1, :t)"
            ),
            {"i": uuid4(), "u": opened, "t": _NOW},
        )

    asyncio.run(_commit(url, _seed_user(bare)))
    asyncio.run(_commit(url, seed_opened))
    try:
        asyncio.run(_rerun_backfill(url))
        asyncio.run(_rerun_backfill(url))

        sql = (
            "SELECT e.kind, count(*) FROM billing_credit_entry e WHERE e.user_id = :u "
            "GROUP BY e.kind"
        )
        assert [tuple(r) for r in asyncio.run(_rows(url, sql, u=bare))] == [("existing_account", 1)]
        assert [tuple(r) for r in asyncio.run(_rows(url, sql, u=opened))] == [("welcome", 1)]
        for user in (bare, opened):
            accounts = asyncio.run(
                _rows(url, "SELECT count(*) FROM billing_credit_account WHERE user_id = :u", u=user)
            )
            assert accounts[0][0] == 1
        # AC-2's catalogue over what this test created: no user without an account.
        orphans = asyncio.run(
            _rows(
                url,
                "SELECT u.id FROM identity_user u LEFT JOIN billing_credit_account a "
                "ON a.user_id = u.id WHERE a.user_id IS NULL AND u.id IN (:a, :b)",
                a=bare,
                b=opened,
            )
        )
        assert orphans == []
    finally:
        asyncio.run(_commit(url, _delete_user(bare)))
        asyncio.run(_commit(url, _delete_user(opened)))


@pytest.mark.usefixtures("_migrated")
def test_a_draft_score_blocks_the_downgrade_and_deletes_nothing(settings: Settings) -> None:
    url = _test_url(settings)
    user_id, score_id = uuid4(), uuid4()

    async def seed(session: AsyncSession) -> None:
        await _seed_user(user_id)(session)
        await session.execute(
            text(
                "INSERT INTO scoring_fit_score (id, user_id, base_cv_id, job_posting_id, "
                "cv_fingerprint, posting_fingerprint, score, gaps, model, prompt_version, "
                "input_tokens, output_tokens, unmetered_attempts, llm_duration_ms, "
                "cost_microusd, scored_at, tailoring_run_id) VALUES (:id, :u, :cv, :p, :f1, :f2, "
                "50, CAST('[]' AS jsonb), 'm', '1', 1, 1, 0, 1, 1, :t, :run)"
            ),
            {
                "id": score_id,
                "u": user_id,
                "cv": uuid4(),
                "p": uuid4(),
                "f1": "a" * 64,
                "f2": "b" * 64,
                "t": _NOW,
                "run": uuid4(),
            },
        )

    asyncio.run(_commit(url, seed))
    try:
        with pytest.raises(RuntimeError, match=f"refusing to downgrade {_REVISION}"):
            command.downgrade(_config(url), _DOWN_REVISION)
        assert asyncio.run(_rows(url, "SELECT 1 FROM scoring_fit_score WHERE id = :i", i=score_id))
        assert asyncio.run(
            _rows(
                url,
                "SELECT 1 FROM information_schema.tables WHERE table_name = 'billing_credit_entry'",
            )
        )
    finally:
        asyncio.run(_commit(url, _delete_user(user_id)))


@pytest.mark.usefixtures("_migrated")
def test_the_downgrade_with_only_opening_entries_drops_the_tables_and_the_column_and_upgrades_back(
    settings: Settings,
) -> None:
    url = _test_url(settings)
    config = _config(url)
    user_id = uuid4()
    failure: BaseException | None = None
    try:
        command.downgrade(config, _DOWN_REVISION)
        asyncio.run(_commit(url, _seed_user(user_id)))
        command.upgrade(config, _REVISION)  # backfill opens one existing_account entry
        command.downgrade(config, _DOWN_REVISION)

        present = {
            r[0]
            for r in asyncio.run(
                _rows(
                    url,
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = ANY(:names)",
                    names=list(_TABLES),
                )
            )
        }
        column = asyncio.run(
            _rows(
                url,
                "SELECT 1 FROM information_schema.columns WHERE table_name = 'scoring_fit_score' "
                "AND column_name = 'tailoring_run_id'",
            )
        )
        assert present == set(), "an opening entry is derived data and must not block the downgrade"
        assert column == []
        assert (
            asyncio.run(_rows(url, "SELECT version_num FROM alembic_version"))[0][0]
            == _DOWN_REVISION
        )
    except BaseException as exc:  # recovery below must run whatever happened
        failure = exc
    finally:
        command.upgrade(config, "head")
        asyncio.run(_commit(url, _delete_user(user_id)))
    if failure is not None:
        raise failure
    assert asyncio.run(_rows(url, "SELECT version_num FROM alembic_version"))[0][0] == _head()


@pytest.mark.usefixtures("_migrated")
def test_an_offline_upgrade_refuses_rather_than_render_a_schema_with_no_accounts(
    settings: Settings,
) -> None:
    config = _config(_test_url(settings))

    with pytest.raises(RuntimeError, match="needs a connection"):
        command.upgrade(config, f"{_DOWN_REVISION}:{_REVISION}", sql=True)


@pytest.mark.usefixtures("_migrated")
def test_an_offline_downgrade_carries_the_refusal_in_the_script_it_renders(
    settings: Settings,
) -> None:
    import io

    config = _config(_test_url(settings))
    config.output_buffer = io.StringIO()

    command.downgrade(config, f"{_REVISION}:{_DOWN_REVISION}", sql=True)

    script = config.output_buffer.getvalue()
    assert "RAISE EXCEPTION" in script
    assert f"refusing to downgrade {_REVISION}" in script
    assert script.index("RAISE EXCEPTION") < script.index("DROP TABLE billing_credit_entry")
