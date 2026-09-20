"""The database half of the purge's contract (T9b) and its persistence details (T32) — against a
**real** Postgres, driving `PurgeExpiredGuestSessions` with the real `SqlAlchemyExpiredGuestData`
(and `CommittingExpiredGuestDataAdapter` where the criterion is about durability rather than merely
about ordering).

`test_purge_expired_guest_sessions.py` (T9) proved the use case's own contract — ordering and
counting — against two hand-written doubles, and its module docstring explains why the criteria whose
truth lives in the database moved here: AC-9 (a `rendering` export job's derived key), AC-10 / AC-11
(the predicate sparing an unexpired session's rows *and files*, across all four tables), AC-12 (the
2.2 tripwire, proved in `test_schema.py` instead — a schema fact, not a use-case one), AC-16 (no
aggregate hydration, no PII column in any `SELECT`), and AC-13's *"no `MissingGreenlet` on the next
iteration"*.

**R-42 governs every assertion below: direct table queries and direct filesystem checks, never the
`PurgeReport` the code under test produced.** A report that agrees with itself proves nothing, so no
test here reads `PurgeReport`'s fields as its proof — several call the use case only for its effect
on the database and the volume.

**Why this file cannot use the ordinary rolled-back `session` fixture, and reuses
`test_purge_cli.py`'s approach instead.** AC-8's ordering claim — "this session's row is committed
before this session's first file is unlinked" — and AC-13's forced mid-batch failure both need a
transaction boundary that is *real*: `CommittingExpiredGuestDataAdapter.delete_session` calls
`session.commit()`, and on the SAVEPOINT-per-test `session` fixture that release is a strictly weaker
guarantee (conftest.py's own docstring), and AC-13 in particular needs a **second, independent**
connection to hold a real row lock while the purge's own connection is refused by it — two genuinely
separate database sessions, which the shared, single-connection `connection` fixture cannot provide.
So every test below builds its own session(s) directly off the session-scoped `engine` fixture and
cleans up whatever it committed at teardown, exactly as `test_purge_cli.py`'s `_CommittedRows` does.

**Why a session bound to the shared `engine` is not enough, and what changed here after a real
deadlock.** An early version of this file bound `committing_session` straight to `engine` (the same
shape `test_purge_cli.py`'s `_CommittedRows` uses). That is fine for `_CommittedRows`, because every
one of its operations is a single, self-contained `engine.begin()` block that neither needs nor
issues a session-level `SET`. AC-13 does: it sets `lock_timeout` once and then drives a use case that
commits **per session**, and a `Session` bound to an `Engine` (rather than to one specific
`Connection`) returns its connection to the pool at the end of every transaction and may be handed a
*different* one on the next — ordinary `QueuePool` behaviour, `create_engine`'s `pool_size=5`. The
`SET` protected only the connection that happened to be checked out when it ran; the `DELETE` that
needed the timeout landed on a connection that had never heard of it, and blocked forever on a row
lock held by a second, independent connection. That is the ~20-minute hang this file was parked over,
found by reading `pg_stat_activity` (`DELETE ... WHERE id = $1` waiting on `Lock / transactionid` and
`Lock / tuple`, against a connection sitting `idle in transaction`). `committing_connection` below
pins one physical connection for the whole test and carries a `statement_timeout` as a blanket
backstop; `committing_session` binds to *it*, not to `engine`, which is what makes a `SET` on that
connection — AC-13's `lock_timeout` included — apply to every statement for the rest of the test,
commits included. See that fixture's own docstring for the full account.

**On promoting `_CommittedRows` (from `test_purge_cli.py`) to a shared fixture: declined, on
purpose.** The two files' setup helpers solve different problems and only rhyme at the surface.
`_CommittedRows` is a minimal, single-table insert for CLI-level black-box tests, built from
`engine.begin()` blocks that never need to survive past one statement. `_Rig` below builds whole
aggregates — a `BaseCv`, an `ExportJob` in two different states, a `TailoringRun`, a `JobPosting` —
through their own repositories, and *does* need one connection's settings (`statement_timeout`, and
in AC-13's case `lock_timeout`) to survive across many commits. The piece that is genuinely shared
between the two files is not `_CommittedRows` or `_Rig`, it is the *underlying pattern* — "borrow a
connection off the session-scoped `engine` fixture, make it commit for real, clean up at teardown" —
and that pattern is already exactly as many lines as an import would be. Promoting either concrete
helper to `conftest.py` would force one file's shape onto the other's very different data-building
needs, for a saving smaller than this paragraph. Kept local, same call this file's own
`_assert_test_database` already made for the same reason.

**The one rule that dominates this file, from CLAUDE.md's 1.4 incident.** `get_settings()` under
`APP_ENV=test` still returns the **dev** `database_url`; only the `settings` fixture swaps in
`test_database_url`. `_assert_test_database` asserts `"_test"` is in the URL about to be used, before
the first statement, in every test that deletes.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import Table, event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from tailorcraft.application.retention.purge_expired_guest_sessions import (
    PurgeExpiredGuestSessions,
)
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.value_objects import BaseCvId, CvContentType, OriginalFilename
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId, JobPostingText
from tailorcraft.domain.retention.value_objects import RetentionWindow
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.files.local_file_store import LocalFileStore
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
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
from tailorcraft.infrastructure.persistence.retention.expired_guest_data import (
    SqlAlchemyExpiredGuestData,
)
from tailorcraft.infrastructure.redis_client import create_redis
from tailorcraft.infrastructure.retention.data_access import CommittingExpiredGuestDataAdapter
from tailorcraft.infrastructure.retention.heartbeat import RedisPurgeHeartbeat
from tailorcraft.infrastructure.settings import Settings

# --- Safety: the 1.4 guard, reproduced here rather than imported (test_purge_cli.py keeps its own
# copy too — a tiny, private guard on the one operation this slice exists to gate is worth the two
# lines of duplication over a shared import that could itself drift silently). -----------------------


def _assert_test_database(settings: Settings) -> None:
    assert "_test" in settings.database_url, (
        "refusing to run a deleting purge test against a URL that is not the test database: "
        f"{settings.database_url!r}"
    )


# --- Fixtures: a genuinely committing, PINNED session, and a small rig for building committed data -

_STATEMENT_TIMEOUT_MS: Final = 5_000
"""A blanket, fail-fast backstop for every connection this file pins for itself. Nothing in this
file is meant to take anywhere near 5 seconds; the value exists purely so that a test which
accidentally blocks on a lock — AC-13 forces exactly that, on purpose — fails in seconds instead of
the ~20 minutes this file cost before the hang was diagnosed, rather than sitting in CI until the
runner's own timeout with a failure that names nothing."""


@pytest_asyncio.fixture
async def committing_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """One real, physical connection, pinned for the whole test and carrying a `statement_timeout`.

    **This is the fix for the deadlock this file was parked over** (see the module docstring for the
    full diagnosis). A `Session` bound to an *Engine* — rather than to one specific `Connection` —
    checks its connection back into the pool at the end of every transaction and may be handed a
    *different* one on the next; that is ordinary `QueuePool` behaviour (`create_engine`'s
    `pool_size=5`), not a bug. A `SET` issued on such a session protects only the connection that
    happened to be checked out at that moment — and AC-13 needs its `lock_timeout` to survive several
    commits later, when the purge under test actually reaches the locked row.

    Binding a `Session` to a `Connection` object, rather than to the `Engine`, is what pins it: the
    connection is checked out exactly once, by this fixture's own `engine.connect()`, and is not
    returned to the pool until this fixture tears down — so a `SET` issued on it, once, applies to
    every statement any code sends through it for the rest of the test, commits included.

    The `SET` is followed by an explicit `commit()` because `SET` (unlike `SET LOCAL`) is itself
    transactional in PostgreSQL: issued inside a transaction that later rolls back, the setting
    reverts with it. Nothing here rolls back this connection's very first transaction, but committing
    it removes the question rather than relying on that.
    """
    async with engine.connect() as conn:
        await conn.execute(text(f"SET statement_timeout = '{_STATEMENT_TIMEOUT_MS}ms'"))
        await conn.commit()
        yield conn


@pytest_asyncio.fixture
async def committing_session(
    committing_connection: AsyncConnection,
) -> AsyncIterator[AsyncSession]:
    """A session bound to `committing_connection` — never the shared `engine` directly (see that
    fixture's docstring for why), and never the rolled-back `connection` fixture — so
    `CommittingExpiredGuestDataAdapter.delete_session`'s `session.commit()` is a real commit, exactly
    as it is in production, on a connection that keeps its `statement_timeout` across every one of
    those commits. Nothing here rolls back on its own; `_Rig.cleanup()` deletes whatever it created.
    """
    factory = async_sessionmaker(
        bind=committing_connection, expire_on_commit=False, autoflush=False
    )
    async with factory() as session:
        yield session


@dataclass(slots=True)
class _Rig:
    """Builds real, committed rows and real files for one test, and deletes what it created."""

    session: AsyncSession
    clock: FixedClock
    files: LocalFileStore
    _guest_session_ids: list[GuestSessionId] = field(default_factory=list)

    async def new_guest_session(
        self, *, expires_at: datetime, created_at: datetime | None = None
    ) -> GuestSessionId:
        session_id = GuestSessionId(uuid4())
        await self.session.execute(
            guest_session_table.insert().values(
                id=session_id,
                token_hash=secrets.token_hex(32),
                created_at=created_at or (expires_at - timedelta(hours=24)),
                expires_at=expires_at,
            )
        )
        await self.session.commit()
        self._guest_session_ids.append(session_id)
        return session_id

    async def add_base_cv(self, owner: GuestSessionId) -> FileRef:
        cvs = SqlAlchemyBaseCvRepository(self.session)
        cv_id = cvs.next_identity()
        ref = FileRef.for_base_cv(cv_id, CvContentType.PDF)
        await cvs.add(
            BaseCv.upload(
                id=cv_id,
                guest_session_id=owner,
                original_filename=OriginalFilename("cv.pdf"),
                content_type=CvContentType.PDF,
                size_bytes=8,
                file=ref,
                uploaded_at=self.clock.now(),
            )
        )
        await self.session.commit()
        await self.files.put(ref, b"%PDF-1.4")
        return ref

    async def add_ready_export_job(self, owner: GuestSessionId) -> FileRef:
        jobs = SqlAlchemyExportJobRepository(self.session)
        job = ExportJob.request(
            id=jobs.next_identity(),
            guest_session_id=owner,
            tailoring_run_id=TailoringRunId(value=uuid4()),
            document=TailoredDocumentKind.CV,
            format=ExportFormat.PDF,
            run_version=1,
            requested_at=self.clock.now(),
        )
        job.mark_started(self.clock.now())
        job.mark_ready(byte_size=9, render_duration_ms=50, at=self.clock.now())
        await jobs.add(job)
        await self.session.commit()
        await self.files.put(job.storage_ref, b"%PDF-rdy")
        return job.storage_ref

    async def add_rendering_export_job_with_bytes_on_disk(self, owner: GuestSessionId) -> FileRef:
        """A worker that wrote bytes and crashed before `mark_ready` — `file_key IS NULL`, and the
        key those bytes live under is derivable only from `(id, format)`. This is the exact case
        AC-9 exists to prove: ADR-0016's guarantee that a `rendering` job's file is findable without
        reading a row that does not yet name it.
        """
        jobs = SqlAlchemyExportJobRepository(self.session)
        job = ExportJob.request(
            id=jobs.next_identity(),
            guest_session_id=owner,
            tailoring_run_id=TailoringRunId(value=uuid4()),
            document=TailoredDocumentKind.COVER_LETTER,
            format=ExportFormat.DOCX,
            run_version=1,
            requested_at=self.clock.now(),
        )
        job.mark_started(self.clock.now())
        await jobs.add(job)
        await self.session.commit()
        ref = job.storage_ref
        await self.files.put(ref, b"partial render bytes")
        return ref

    async def add_tailoring_run(self, owner: GuestSessionId) -> TailoringRunId:
        runs = SqlAlchemyTailoringRunRepository(self.session)
        run = TailoringRun.request(
            id=runs.next_identity(),
            guest_session_id=owner,
            base_cv_id=BaseCvId(value=uuid4()),
            job_posting_id=JobPostingId(value=uuid4()),
            requested_at=self.clock.now(),
        )
        await runs.add(run)
        await self.session.commit()
        return run.id

    async def add_job_posting(self, owner: GuestSessionId) -> JobPostingId:
        postings = SqlAlchemyJobPostingRepository(self.session)
        posting = JobPosting.from_pasted_text(
            id=postings.next_identity(),
            guest_session_id=owner,
            text=JobPostingText("x" * 150),
            created_at=self.clock.now(),
        )
        await postings.add(posting)
        await self.session.commit()
        return posting.id

    async def cleanup(self) -> None:
        if not self._guest_session_ids:
            return
        await self.session.execute(
            guest_session_table.delete().where(
                guest_session_table.c.id.in_(self._guest_session_ids)
            )
        )
        await self.session.commit()


@pytest_asyncio.fixture
async def rig(
    committing_session: AsyncSession, clock: FixedClock, settings: Settings
) -> AsyncIterator[_Rig]:
    r = _Rig(session=committing_session, clock=clock, files=LocalFileStore(settings.upload_dir))
    try:
        yield r
    finally:
        await r.cleanup()


async def _run_purge(rig: _Rig, *, batch_limit: int, dry_run: bool = False) -> None:
    """Run one batch of the real purge, wired exactly as the CLI and the task wire it — the
    committing adapter, `LocalFileStore`, the same `FixedClock` the rig's rows were built against.
    The return value is deliberately discarded by every caller (R-42): what a test may assert on is
    the database and the filesystem, never this report.
    """
    data = CommittingExpiredGuestDataAdapter(SqlAlchemyExpiredGuestData(rig.session), rig.session)
    purge = PurgeExpiredGuestSessions(
        data=data,
        files=rig.files,
        clock=rig.clock,
        window=RetentionWindow(hours=24),
        batch_limit=batch_limit,
        dry_run=dry_run,
    )
    await purge()


async def _session_exists(conn: AsyncConnection, session_id: GuestSessionId) -> bool:
    result = await conn.execute(
        select(func.count())
        .select_from(guest_session_table)
        .where(guest_session_table.c.id == session_id)
    )
    return bool(result.scalar_one() > 0)


async def _count_owned(conn: AsyncConnection, table: Table, owner: GuestSessionId) -> int:
    result = await conn.execute(
        select(func.count()).select_from(table).where(table.c.guest_session_id == owner)
    )
    return result.scalar_one()


def _an_unreferenced_file_ref() -> FileRef:
    """A syntactically valid `FileRef` with nothing behind it and no row naming it — the same
    construction `test_purge_expired_guest_sessions.py`'s `_a_file_ref` uses."""
    generated = uuid4()
    hex_digits = generated.hex
    return FileRef(key=f"{hex_digits[0:2]}/{hex_digits[2:4]}/{generated}.pdf")


# --- AC-9: rows-first collection includes a `rendering` job's derived key --------------------------


async def test_purging_a_session_unlinks_its_base_cv_and_both_export_job_files(
    settings: Settings, engine: AsyncEngine, rig: _Rig, clock: FixedClock
) -> None:
    """AC-9. A session whose base CV and two export jobs are all on disk has all three keys
    unlinked — including a `rendering` job whose `file_key IS NULL`, whose key is *derived* from
    `(id, format)` rather than read off the row (ADR-0016's guarantee, consumed here for the first
    time). Asserted against the real filesystem and the real tables, never the `PurgeReport`.
    """
    _assert_test_database(settings)
    owner = await rig.new_guest_session(expires_at=clock.now() - timedelta(hours=1))
    cv_ref = await rig.add_base_cv(owner)
    ready_ref = await rig.add_ready_export_job(owner)
    rendering_ref = await rig.add_rendering_export_job_with_bytes_on_disk(owner)

    for ref in (cv_ref, ready_ref, rendering_ref):
        assert (settings.upload_dir / ref.key).exists(), f"test setup: {ref.key} was never written"

    await _run_purge(rig, batch_limit=10)

    async with engine.connect() as conn:
        assert not await _session_exists(conn, owner)
    for ref in (cv_ref, ready_ref, rendering_ref):
        assert not (settings.upload_dir / ref.key).exists(), f"{ref.key} survived the purge"


# --- AC-10: a session expiring in the future is never selected --------------------------------------


async def test_a_session_expiring_in_the_future_is_never_selected(
    settings: Settings, engine: AsyncEngine, rig: _Rig, clock: FixedClock
) -> None:
    """AC-10 (ADR-0006 §3, part 1). The future session is deliberately the **oldest** row
    (`created_at` far in the past) and owns the **most** data — the predicate must be `expires_at`
    alone, never `created_at`, row count or "has files"."""
    _assert_test_database(settings)
    future_owner = await rig.new_guest_session(
        expires_at=clock.now() + timedelta(hours=2),
        created_at=clock.now() - timedelta(days=30),
    )
    future_cv = await rig.add_base_cv(future_owner)
    future_export = await rig.add_ready_export_job(future_owner)
    await rig.add_tailoring_run(future_owner)
    await rig.add_job_posting(future_owner)

    expired_owner = await rig.new_guest_session(expires_at=clock.now() - timedelta(minutes=1))
    await rig.add_base_cv(expired_owner)

    await _run_purge(rig, batch_limit=10)

    async with engine.connect() as conn:
        assert await _session_exists(conn, future_owner), "the future session was purged"
        assert not await _session_exists(conn, expired_owner), "the expired session survived"
    assert (settings.upload_dir / future_cv.key).exists()
    assert (settings.upload_dir / future_export.key).exists()


# --- AC-11: a full run leaves every row and file of an unexpired session intact, all four tables ----


async def test_a_full_run_leaves_every_row_and_file_of_an_unexpired_session_intact(
    settings: Settings, engine: AsyncEngine, rig: _Rig, clock: FixedClock
) -> None:
    """AC-11 (ADR-0006 §3, part 2; amended at T8 — the bound is `batch_limit`, not `limit=None`).
    `batch_limit` below covers the whole backlog (one expired candidate), which is what "a full run"
    means for a use case whose signature has no unbounded shape. The unexpired session's rows in all
    four guest-owned tables, and its files, must be untouched.
    """
    _assert_test_database(settings)
    unexpired_owner = await rig.new_guest_session(expires_at=clock.now() + timedelta(hours=1))
    cv_ref = await rig.add_base_cv(unexpired_owner)
    export_ref = await rig.add_ready_export_job(unexpired_owner)
    await rig.add_tailoring_run(unexpired_owner)
    await rig.add_job_posting(unexpired_owner)

    expired_owner = await rig.new_guest_session(expires_at=clock.now() - timedelta(minutes=1))

    await _run_purge(rig, batch_limit=100)

    async with engine.connect() as conn:
        assert await _session_exists(conn, unexpired_owner)
        assert not await _session_exists(conn, expired_owner)
        assert await _count_owned(conn, base_cv_table, unexpired_owner) == 1
        assert await _count_owned(conn, export_job_table, unexpired_owner) == 1
        assert await _count_owned(conn, tailoring_run_table, unexpired_owner) == 1
        assert await _count_owned(conn, job_posting_table, unexpired_owner) == 1
    assert (settings.upload_dir / cv_ref.key).exists()
    assert (settings.upload_dir / export_ref.key).exists()


# --- AC-16: no aggregate hydration, no PII column in any SELECT the purge issues --------------------


async def test_the_purge_never_selects_a_pii_column_or_touches_another_contexts_table(
    settings: Settings, engine: AsyncEngine, rig: _Rig, clock: FixedClock
) -> None:
    """AC-16. Captures the actual SQL text the purge sends to Postgres (the same
    `before_cursor_execute` hook `test_tailoring_run_repository.py` uses) and asserts none of it
    names a column that could carry a person's document, and none of it touches `tailoring_run` or
    `posting_job_posting` at all — the purge reaches those tables only through the cascade, never
    through a `SELECT` of its own.
    """
    _assert_test_database(settings)
    owner = await rig.new_guest_session(expires_at=clock.now() - timedelta(hours=1))
    await rig.add_base_cv(owner)
    await rig.add_ready_export_job(owner)
    await rig.add_tailoring_run(owner)
    await rig.add_job_posting(owner)

    captured: list[str] = []

    def _capture(conn: object, cursor: object, statement: str, *_args: object) -> None:
        captured.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        await _run_purge(rig, batch_limit=10)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    joined = "\n".join(captured).lower()
    for forbidden_column in (
        "extracted_text",
        "posting_text",
        "tailored_cv",
        "cover_letter",
        "original_filename",
    ):
        assert forbidden_column not in joined, (
            f"{forbidden_column!r} appeared in a statement the purge sent to Postgres: {joined}"
        )
    for forbidden_table in ("tailoring_run", "posting_job_posting"):
        assert forbidden_table not in joined, (
            f"the purge's own SQL named {forbidden_table!r}: {joined}"
        )


# --- AC-13: a real per-session DELETE failure is SAVEPOINT-contained --------------------------------


async def test_one_sessions_delete_failing_under_a_real_lock_timeout_leaves_the_batch_usable(
    settings: Settings, engine: AsyncEngine, rig: _Rig, clock: FixedClock
) -> None:
    """AC-13, proved against a genuine refusal rather than a double (R-42; CLAUDE.md's
    expired-identity-map lesson, measured in 1.4). A second, independent connection holds a real row
    lock on the middle candidate via `SELECT ... FOR UPDATE`; the purge itself runs on a *third*
    connection, pinned for the whole run exactly as `committing_connection` is (see that fixture's
    docstring for why pinning, not just `SET`, is what makes a timeout dependable across several
    commits), so `delete_session(middle)` genuinely fails with a database error mid-batch after
    250ms rather than hanging.

    The batch must still reach the newest candidate afterwards — the proof that `begin_nested()`'s
    SAVEPOINT absorbs the failure rather than expiring the whole session's identity map, which is
    exactly the shape that produces `MissingGreenlet` on the next iteration if it is missing.

    **Belt and suspenders on the fail-fast requirement.** This connection carries both the 250ms
    `lock_timeout`, which is the condition under test and is expected to fire, and the file's usual
    5-second `statement_timeout` as a backstop in case the former were ever defeated by something
    this test did not anticipate — so a regression here still fails in single-digit seconds, never
    in the ~20 minutes the unpinned version of this file actually hung for.
    """
    _assert_test_database(settings)
    oldest = await rig.new_guest_session(expires_at=clock.now() - timedelta(hours=3))
    middle = await rig.new_guest_session(expires_at=clock.now() - timedelta(hours=2))
    newest = await rig.new_guest_session(expires_at=clock.now() - timedelta(hours=1))

    async with engine.connect() as lock_conn:
        lock_tx = await lock_conn.begin()
        try:
            await lock_conn.execute(
                select(guest_session_table.c.id)
                .where(guest_session_table.c.id == middle)
                .with_for_update()
            )

            async with engine.connect() as purge_conn:
                await purge_conn.execute(text("SET lock_timeout = '250ms'"))
                await purge_conn.execute(
                    text(f"SET statement_timeout = '{_STATEMENT_TIMEOUT_MS}ms'")
                )
                await purge_conn.commit()
                purge_session = async_sessionmaker(
                    bind=purge_conn, expire_on_commit=False, autoflush=False
                )()
                try:
                    data = CommittingExpiredGuestDataAdapter(
                        SqlAlchemyExpiredGuestData(purge_session), purge_session
                    )
                    purge = PurgeExpiredGuestSessions(
                        data=data,
                        files=LocalFileStore(settings.upload_dir),
                        clock=clock,
                        window=RetentionWindow(hours=24),
                        batch_limit=10,
                        dry_run=False,
                    )
                    await purge()
                finally:
                    await purge_session.close()
        finally:
            await lock_tx.rollback()

    async with engine.connect() as conn:
        assert not await _session_exists(conn, oldest), "the oldest candidate was not deleted"
        assert await _session_exists(conn, middle), (
            "the middle candidate, whose DELETE should have failed under the lock, is gone"
        )
        assert not await _session_exists(conn, newest), (
            "the batch did not reach the newest candidate after the middle one failed — "
            "the SAVEPOINT did not contain the failure"
        )


# --- T9's ordering promise, deferred here on purpose (T9's own note) --------------------------------


async def test_list_expired_returns_oldest_expires_at_first(
    settings: Settings, rig: _Rig, clock: FixedClock
) -> None:
    """AC-7's ordering promise, at the adapter it actually belongs to. `test_purge_expired_guest_
    sessions.py`'s doubles do not sort by `expires_at` at all (its own module docstring says so, and
    says this is where the real claim is proved)."""
    _assert_test_database(settings)
    newest = await rig.new_guest_session(expires_at=clock.now() - timedelta(hours=1))
    oldest = await rig.new_guest_session(expires_at=clock.now() - timedelta(hours=3))
    middle = await rig.new_guest_session(expires_at=clock.now() - timedelta(hours=2))

    adapter = SqlAlchemyExpiredGuestData(rig.session)
    candidates = await adapter.list_expired(as_of=clock.now(), limit=10)

    ours = [c.session_id for c in candidates if c.session_id in (oldest, middle, newest)]
    assert ours == [oldest, middle, newest]


# --- T32: the UNION ALL's derived key, at the adapter level -----------------------------------------


async def test_list_expired_returns_a_derived_key_for_a_rendering_export_job(
    settings: Settings, rig: _Rig, clock: FixedClock
) -> None:
    """T32 — the adapter-level version of AC-9. `list_expired` itself, not the whole purge, must
    hand back the derived `FileRef` for a job whose `file_key IS NULL`."""
    _assert_test_database(settings)
    owner = await rig.new_guest_session(expires_at=clock.now() - timedelta(hours=1))
    rendering_ref = await rig.add_rendering_export_job_with_bytes_on_disk(owner)

    adapter = SqlAlchemyExpiredGuestData(rig.session)
    candidates = await adapter.list_expired(as_of=clock.now(), limit=10)

    matching = next(c for c in candidates if c.session_id == owner)
    assert rendering_ref in matching.files


# --- T32: which_are_referenced ------------------------------------------------------------------


async def test_which_are_referenced_returns_only_the_referenced_subset(
    settings: Settings, rig: _Rig, clock: FixedClock
) -> None:
    """T32. Of a base-CV key, a `ready` export key and a key nothing points at, only the first two
    come back — the orphan sweep's whole cross-check rests on this being exact."""
    _assert_test_database(settings)
    owner = await rig.new_guest_session(expires_at=clock.now() + timedelta(hours=1))
    referenced_cv = await rig.add_base_cv(owner)
    referenced_export = await rig.add_ready_export_job(owner)
    unreferenced = _an_unreferenced_file_ref()

    adapter = SqlAlchemyExpiredGuestData(rig.session)
    result = await adapter.which_are_referenced([referenced_cv, referenced_export, unreferenced])

    assert result == frozenset({referenced_cv, referenced_export})


async def test_which_are_referenced_issues_no_statement_for_an_empty_input(
    settings: Settings, engine: AsyncEngine, rig: _Rig
) -> None:
    """T32. `WHERE file_key IN ()` is not valid SQL, and asking it anyway would be a round trip to
    learn what the caller already knew — the empty case must never reach Postgres at all."""
    _assert_test_database(settings)
    captured: list[str] = []

    def _capture(conn: object, cursor: object, statement: str, *_args: object) -> None:
        captured.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        adapter = SqlAlchemyExpiredGuestData(rig.session)
        result = await adapter.which_are_referenced([])
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert result == frozenset()
    assert captured == [], f"an empty input still reached Postgres: {captured}"


# --- T32: the inline-format filter is unreachable, and the test proves that rather than forcing it --


async def test_export_job_format_check_constraint_forbids_an_inline_row(
    settings: Settings, rig: _Rig, clock: FixedClock
) -> None:
    """T32's inline-format-filter bullet, resolved as the task instructs: check whether the state is
    reachable before testing it. `list_expired`'s `export_half` filters
    `export_job.format.in_(_QUEUED_FORMATS)` as a defence against a `md`/`txt` row contributing a key
    `FileRef.for_export` would refuse to build — but `ck_export_job_format_is_queued`
    (`format IN ('pdf','docx')`, `infrastructure/persistence/mapping/export/export_job.py`) already
    refuses that row at `INSERT`, before the filter would ever see it. There is therefore no reachable
    state in which the filter changes `list_expired`'s answer, and forcing an unreachable row past the
    database (bypassing the CHECK some other way) would test something the schema itself already
    makes impossible. This test proves the *unreachability* directly instead: a hand-written `INSERT`
    naming an inline format is refused by the constraint, exactly like the two `CHECK`-constraint
    probes in `test_export_job_repository.py`.
    """
    _assert_test_database(settings)
    owner = await rig.new_guest_session(expires_at=clock.now() - timedelta(hours=1))

    with pytest.raises(IntegrityError, match="ck_export_job_format_is_queued"):
        await rig.session.execute(
            text(
                "INSERT INTO export_job "
                "(id, guest_session_id, tailoring_run_id, document, format, run_version, "
                "status, requested_at, version) "
                "VALUES (:id, :guest_session_id, :run_id, 'cv', 'md', 1, 'queued', :requested_at, 1)"
            ),
            {
                "id": uuid4(),
                "guest_session_id": owner.value,
                "run_id": uuid4(),
                "requested_at": clock.now(),
            },
        )
    await rig.session.rollback()


# --- T32: whole-second fidelity on the heartbeat's `at` ----------------------------------------------


async def test_heartbeat_at_round_trips_with_whole_second_fidelity(
    settings: Settings, clock: FixedClock, clear_redis: None
) -> None:
    """T32. The `Clock` port is whole-second by contract (ADR-0007); this proves
    `RedisPurgeHeartbeat` does not quietly reintroduce sub-second drift on the round trip through
    Redis's hash and `datetime.fromisoformat` on the way back."""
    redis = create_redis(settings.redis_url)
    try:
        heartbeat = RedisPurgeHeartbeat(redis)
        await heartbeat.record(
            at=clock.now(),
            outcome="ok",
            sessions_deleted=3,
            files_unlinked=5,
            duration_ms=42,
        )
        read_back = await heartbeat.read()
    finally:
        await redis.aclose()

    assert read_back is not None
    assert read_back.at == clock.now()
