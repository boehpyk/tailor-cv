"""R-3 / R-4's log lines at the **Celery task** entry point — `infrastructure/tasks/retention.py`'s
`_purge()` — against a real Postgres and a real Redis.

`test_retention_task.py` (the existing file beside this one) is deliberately schedule-and-wiring only
— its own module docstring says "no broker, no database, no Redis" — because AC-25…AC-29 are about
whether the beat entry exists and what it is named, never about what a real tick does. This file is
the sibling that file's docstring points away from: it drives `_purge()` itself, for real, the same
way `test_purge_cli.py` drives `purge_command._purge_guests` directly rather than through
`run_from_cli`.

**Why a separate file rather than new tests inside `test_retention_task.py`.** Every fixture in that
file is a pure helper or a `monkeypatch`'d `Settings` object; adding a database- and Redis-touching
test there would contradict its own opening claim the moment a future reader trusts it. This file
carries that cost instead, and is named so a reader immediately knows which kind of test they are
looking at.

**The one rule that dominates this file, from CLAUDE.md's 1.4 incident.** `get_settings()` under
`APP_ENV=test` still returns the **dev** `database_url` — only the `settings` fixture swaps in
`test_database_url`. `_purge()` and `purge_expired_guest_sessions_use_case()` each call `get_settings()`
themselves (they are composition roots, exactly like `run_from_cli`), so every test here monkeypatches
**both** call sites — `tasks/retention.py`'s own import and `tasks/container.py`'s own import — to the
already-swapped `settings` fixture, the same technique `test_retention_task.py` and
`test_celery_config.py` already use for `tasks/app.py`'s.

**Why `delete_session` and `LocalFileStore.delete` are monkeypatched rather than forced to fail
through a real lock or a real `ENOSPC`.** `test_purge_database.py`'s
`test_one_sessions_delete_failing_under_a_real_lock_timeout_leaves_the_batch_usable` already proves,
against a genuine Postgres lock timeout, that AC-13's SAVEPOINT absorbs a refused `DELETE` without
poisoning the rest of the batch — that is a claim about the **database adapter**. What this file
proves is a different claim, about the **entry point**: that `SessionPurgeFailure`/`FileUnlinkFailure`
reach `_purge()`'s two `log.warning(...)` calls with exactly the fields R-3/R-4 promise and none of
the ones they forbid. A monkeypatch that raises a distinctive, marked exception through the real
composition root exercises that claim directly, without needing a second connection held open for the
whole test.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.files import FileRef, FileStoreUnavailable
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.observability import configure_logging
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tailorcraft.infrastructure.settings import Settings
from tailorcraft.infrastructure.tasks import container as tasks_container_module
from tailorcraft.infrastructure.tasks import retention as retention_task_module

# --- Safety: the 1.4 guard, reproduced here rather than imported — every sibling retention test
# file keeps its own copy for the same stated reason (a tiny, private guard on the one operation
# this slice exists to gate is worth the duplication over a shared import that could itself drift). -


def _assert_test_database(settings: Settings) -> None:
    assert "_test" in settings.database_url, (
        "refusing to run a deleting retention-task test against a URL that is not the test "
        f"database: {settings.database_url!r}"
    )


def _wire_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """Point both of `_purge()`'s own `get_settings()` call sites — its own import and
    `tasks/container.py`'s — at the already-swapped test `Settings`, the same technique
    `test_retention_task.py` uses for `tasks/app.py`'s."""
    monkeypatch.setattr(retention_task_module, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks_container_module, "get_settings", lambda: settings)


@pytest.fixture(autouse=True)
def _configured_logging(settings: Settings) -> None:
    """`_purge()` is called directly, never through the Celery task wrapper, so nothing else in this
    module configures structlog — without this, `caplog` only captures output if some earlier test
    in the session happened to configure logging first (every sibling retention test file's identical
    fixture, same reason)."""
    configure_logging(settings)


def _a_past_instant(hours_ago: int) -> datetime:
    return datetime.now(UTC).replace(microsecond=0) - timedelta(hours=hours_ago)


async def _insert_expired_session(engine: AsyncEngine, *, expires_at: datetime) -> GuestSessionId:
    session_id = GuestSessionId(uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            guest_session_table.insert().values(
                id=session_id,
                token_hash=secrets.token_hex(32),
                created_at=expires_at - timedelta(hours=24),
                expires_at=expires_at,
            )
        )
    return session_id


async def _insert_expired_session_with_base_cv(
    engine: AsyncEngine, files: LocalFileStore, *, expires_at: datetime
) -> tuple[GuestSessionId, FileRef]:
    """Deferred repository/aggregate imports — see `test_purge_cli.py`'s identical helper for why:
    `configure_mappings()` only runs at fixture setup, after this module has already been imported,
    and `repositories/intake/base_cv.py` reads a mapped attribute at its own import time."""
    from tailorcraft.domain.intake.base_cv import BaseCv
    from tailorcraft.domain.intake.value_objects import CvContentType, OriginalFilename
    from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
        SqlAlchemyBaseCvRepository,
    )

    session_id = await _insert_expired_session(engine, expires_at=expires_at)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        cvs = SqlAlchemyBaseCvRepository(session)
        cv_id = cvs.next_identity()
        ref = FileRef.for_base_cv(cv_id, CvContentType.PDF)
        await cvs.add(
            BaseCv.upload(
                id=cv_id,
                guest_session_id=session_id,
                original_filename=OriginalFilename("cv.pdf"),
                content_type=CvContentType.PDF,
                size_bytes=8,
                file=ref,
                uploaded_at=expires_at - timedelta(hours=3),
            )
        )
        await session.commit()
    await files.put(ref, b"%PDF-1.4")
    return session_id, ref


async def _delete_guest_session_row(engine: AsyncEngine, session_id: GuestSessionId) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            guest_session_table.delete().where(guest_session_table.c.id == session_id)
        )


# --- R-3: a refused delete_session, at the task's own log.warning(...) call site -------------------


async def test_a_refused_delete_session_logs_the_session_failed_line_with_only_id_and_error_type(
    settings: Settings,
    engine: AsyncEngine,
    clear_redis: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R-3, at the second of the two entry points that must both emit it (`purge_command.py`'s
    `_log_batch_failures` is the CLI's, proved in `test_purge_cli.py`). The two are separately
    written and separately maintained — `tasks/retention.py`'s own module comment explains why the
    task is not a thin call into the CLI's code — so a passing CLI test cannot stand in for this one.
    """
    _assert_test_database(settings)
    _wire_settings(monkeypatch, settings)
    session_id = await _insert_expired_session(engine, expires_at=_a_past_instant(2))

    marker = "MARKER-do-not-let-this-travel-into-a-log-record"
    original = SqlAlchemyExpiredGuestData.delete_session

    async def _refuse(self: SqlAlchemyExpiredGuestData, sid: GuestSessionId) -> None:
        if sid == session_id:
            raise RuntimeError(marker)
        await original(self, sid)

    monkeypatch.setattr(SqlAlchemyExpiredGuestData, "delete_session", _refuse)

    try:
        with caplog.at_level(logging.WARNING):
            await retention_task_module._purge()

        async with engine.connect() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(guest_session_table)
                .where(guest_session_table.c.id == session_id)
            )
            assert result.scalar_one() == 1, "a refused DELETE must not remove the row"
        assert "retention.session_purge_failed" in caplog.text
        assert str(session_id.value) in caplog.text
        assert "RuntimeError" in caplog.text
        assert marker not in caplog.text, (
            f"the exception's message leaked into a log record: {caplog.text}"
        )
    finally:
        await _delete_guest_session_row(engine, session_id)


# --- R-4: a refused unlink, at the task's own log.warning(...) call site ---------------------------


async def test_a_refused_unlink_logs_the_file_failed_line_with_only_error_type(
    settings: Settings,
    engine: AsyncEngine,
    clear_redis: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _assert_test_database(settings)
    _wire_settings(monkeypatch, settings)
    files = LocalFileStore(settings.upload_dir)
    session_id, ref = await _insert_expired_session_with_base_cv(
        engine, files, expires_at=_a_past_instant(2)
    )

    message = "a storage key or path, which must never travel"

    async def _refuse(self: LocalFileStore, r: FileRef) -> None:
        raise FileStoreUnavailable(message)

    monkeypatch.setattr(LocalFileStore, "delete", _refuse)

    try:
        with caplog.at_level(logging.WARNING):
            await retention_task_module._purge()

        async with engine.connect() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(guest_session_table)
                .where(guest_session_table.c.id == session_id)
            )
            assert result.scalar_one() == 0, "R-4: the row is deleted even though the unlink failed"
        assert "retention.file_unlink_failed" in caplog.text
        assert "FileStoreUnavailable" in caplog.text
        assert ref.key not in caplog.text
        assert str(settings.upload_dir / ref.key) not in caplog.text
        assert message not in caplog.text, (
            f"the exception's message leaked into a log record: {caplog.text}"
        )
    finally:
        # The row is already gone (R-4's point); only the orphaned file needs cleaning up, since the
        # monkeypatched `delete` never actually removed it.
        (settings.upload_dir / ref.key).unlink(missing_ok=True)
