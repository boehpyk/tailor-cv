"""Shared helpers for slice 2.4's concurrency proofs (T23, AC-13 … AC-20).

Every race in the spec's actor table (technical plan §0.6) needs the same three things, and a
subtle trap is in each:

1. **Real, pinned connections.** A `Session` bound to an *Engine* may be handed a different pooled
   connection after each commit, so a `SET lock_timeout` issued once protects only the connection
   that happened to be checked out (1.6's twenty-minute CI hang). `pinned_session` binds a session
   to **one** `AsyncConnection` and commits the `SET` (a bare `SET` is itself transactional).
   `lock_timeout` is the backstop: a regression that turns "blocks, then proceeds" into "blocks for
   ever" fails in seconds with a message that names a lock, not in CI's own timeout with nothing.
2. **Proof the overlap happened.** `wait_for_lock_waiter` reads `pg_stat_activity` from a third
   connection until a backend whose `wait_event_type = 'Lock'` runs a statement containing every
   given fragment. A `sleep` and "the task is not done yet" cannot tell "waiting on the lock" from
   "has not been scheduled yet" (2.3's H-33 lesson: stage the race at the moment the spec names).
3. **Committed seeds and explicit cleanup.** Rows the races write are real; nothing here rolls back.
   `drop_rows` deletes the session and user rows the test created (the cascades take the rest) and
   every assertion is scoped to ids the test created.

The upload volume is always a per-test `tmp_path`, never the shared `settings.upload_dir`: a sweep
walks its whole root and aged files would leak between tests.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from tailorcraft.domain.export.value_objects import ExportJobId
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.ownership import GuestOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.intake.value_objects import BaseCvId
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)
from tailorcraft.infrastructure.persistence.repositories.posting.job_posting import (
    SqlAlchemyJobPostingRepository,
)
from tailorcraft.infrastructure.persistence.repositories.tailoring.tailoring_run import (
    SqlAlchemyTailoringRunRepository,
)
from tailorcraft.infrastructure.settings import Settings
from tests.integration.owners import (
    extracted_cv,
    pasted_posting,
    ready_export,
    succeeded_run,
)
from tests.integration.persistence.owner_rows import persist_user

LOCK_TIMEOUT_MS: Final = 8_000
STATEMENT_TIMEOUT_MS: Final = 15_000
OWNED_TABLES: Final = ("intake_base_cv", "posting_job_posting", "tailoring_run", "export_job")


def assert_test_database(settings: Settings) -> None:
    assert "_test" in settings.database_url, (
        f"refusing to run a committing race test against {settings.database_url!r}"
    )


@asynccontextmanager
async def pinned_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session on **one** physical connection, with `lock_timeout` and `statement_timeout` set
    and the `SET` committed, so both survive every later commit on it."""
    async with engine.connect() as conn:
        await conn.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT_MS}ms'"))
        await conn.execute(text(f"SET statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'"))
        await conn.commit()
        factory = async_sessionmaker(bind=conn, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            yield session


async def bound_cleanup_locks(conn: AsyncConnection) -> None:
    """`SET LOCAL lock_timeout` for a teardown that deletes committed rows on its own connection.

    /verify r1: with a wrong intermediate lock mode in `save_issued` (`FOR NO KEY UPDATE`), a race
    test failed and left a session idle-in-transaction on the user row; the fixture's `DELETE FROM
    identity_user` then waited behind it with no `lock_timeout`, and the combined run hung for over
    ten minutes. A hanging teardown names nothing and CI sits on it until its own timeout. Local,
    because a bare `SET` is itself transactional and must live exactly as long as the deletes."""
    await conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'"))


async def wait_for_lock_waiter(
    engine: AsyncEngine, *fragments: str, within: float = 5.0
) -> list[str]:
    """Block until some backend is **waiting on a lock** while running a statement whose text
    contains every fragment (case-insensitive); return those statements. Raises `AssertionError`
    after `within` seconds — the overlap never happened, so the test would prove nothing."""
    wanted = [fragment.lower() for fragment in fragments]
    deadline = time.monotonic() + within
    seen: list[str] = []
    async with engine.connect() as probe:
        while time.monotonic() < deadline:
            rows = await probe.execute(
                text(
                    "SELECT query FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                    "AND wait_event_type = 'Lock'"
                )
            )
            seen = [str(query) for (query,) in rows.all()]
            matching = [q for q in seen if all(f in q.lower() for f in wanted)]
            if matching:
                return matching
            await probe.rollback()  # a fresh snapshot of pg_stat_activity each turn
            await asyncio.sleep(0.02)
    raise AssertionError(
        f"no backend was ever waiting on a lock in a statement containing {fragments!r} within "
        f"{within}s — the race was never staged. Lock waiters seen at the end: {seen!r}"
    )


async def new_user(engine: AsyncEngine, clock: FixedClock) -> UserId:
    """A committed registered user (no password route involved)."""
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        owner = await persist_user(session, clock)
        await session.commit()
        return owner.user_id


@dataclass(frozen=True, slots=True)
class SeededGuest:
    """One committed guest session and everything it owns, files included."""

    guest: GuestSessionId
    token_hash: str
    cv_id: BaseCvId
    cv_ref: FileRef
    posting_id: JobPostingId
    run_id: TailoringRunId
    export_id: ExportJobId
    export_ref: FileRef

    @property
    def files(self) -> tuple[FileRef, FileRef]:
        return (self.cv_ref, self.export_ref)


async def seed_guest_with_work(
    engine: AsyncEngine, files: LocalFileStore, *, started_at: datetime
) -> SeededGuest:
    """A guest session started at `started_at` (24 h ttl), owning a CV, a posting, a succeeded run
    and a ready export; the CV's and the export's bytes are really on `files`'s volume."""
    token_hash = secrets.token_hex(32)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        guests = SqlAlchemyGuestSessionRepository(session)
        guest = GuestSession.start(
            id=guests.next_identity(), token_hash=token_hash, at=started_at, ttl_hours=24
        )
        await guests.add(guest)
        owner = GuestOwner(guest.id)
        cv = extracted_cv(owner, started_at)
        await SqlAlchemyBaseCvRepository(session).add(cv)
        posting = pasted_posting(owner, started_at)
        await SqlAlchemyJobPostingRepository(session).add(posting)
        run = succeeded_run(owner, started_at, base_cv_id=cv.id, job_posting_id=posting.id)
        await SqlAlchemyTailoringRunRepository(session).add(run)
        export = ready_export(owner, run, started_at)
        await SqlAlchemyExportJobRepository(session).add(export)
        await session.commit()
        await files.put(cv.file, b"%PDF-1.4 claimed cv bytes")
        await files.put(export.storage_ref, b"%PDF-1.7 claimed export bytes")
        return SeededGuest(
            guest=guest.id,
            token_hash=token_hash,
            cv_id=cv.id,
            cv_ref=cv.file,
            posting_id=posting.id,
            run_id=run.id,
            export_id=export.id,
            export_ref=export.storage_ref,
        )


async def owned_row_counts(
    engine: AsyncEngine, *, guest: GuestSessionId | None = None, user: UserId | None = None
) -> dict[str, int]:
    """`{table: rows}` for one owner, over the four owned tables, on a fresh connection."""
    assert (guest is None) != (user is None)
    column, value = (
        ("guest_session_id", guest.value) if guest is not None else ("user_id", user.value)  # type: ignore[union-attr]
    )
    async with engine.connect() as conn:
        return {
            table: int(
                (
                    await conn.execute(
                        text(f"SELECT count(*) FROM {table} WHERE {column} = :v"),  # noqa: S608 -- test-owned names
                        {"v": value},
                    )
                ).scalar_one()
            )
            for table in OWNED_TABLES
        }


async def session_exists(engine: AsyncEngine, guest: GuestSessionId | UUID) -> bool:
    raw = guest.value if isinstance(guest, GuestSessionId) else guest
    async with engine.connect() as conn:
        return bool(
            (
                await conn.execute(
                    text("SELECT count(*) FROM identity_guest_session WHERE id = :i"), {"i": raw}
                )
            ).scalar_one()
        )


async def drop_rows(
    engine: AsyncEngine,
    *,
    guests: Sequence[GuestSessionId | UUID] = (),
    users: Sequence[UserId | UUID] = (),
) -> None:
    """Delete what a test committed; the foreign-key cascades take every owned row."""
    async with engine.begin() as conn:
        for guest in guests:
            raw = guest.value if isinstance(guest, GuestSessionId) else guest
            await conn.execute(text("DELETE FROM identity_guest_session WHERE id = :i"), {"i": raw})
        for user in users:
            raw_user = user.value if isinstance(user, UserId) else user
            await conn.execute(text("DELETE FROM identity_user WHERE id = :i"), {"i": raw_user})
