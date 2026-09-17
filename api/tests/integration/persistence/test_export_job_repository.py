"""Persistence tests for `ExportJob` — the imperative mapping, its `TypeDecorator`s and
`SqlAlchemyExportJobRepository` against real PostgreSQL (I7, written **after**).

Mirrors `test_tailoring_run_repository.py`'s structure: mapping round-trip with
`session.expunge_all()` to force a genuine reload, whole-second timestamp fidelity, `NULL`
round-trips, raw-SQL `CHECK`-constraint probes, the cascade, the index, and the two-session
concurrency race. Three things are specific to `ExportJob` and have no analogue in that file:

- **Seven `CHECK`s, one per column** (1.3's AC-4 amendment, applied from the start here rather than
  learned the hard way): a conjunction-form constraint accepts a row holding exactly one of
  `file_key` / `byte_size` / `render_duration_ms` while `status != 'ready'` (`false = false`), so
  each of the three status-pairing tests below constructs exactly that half-row — the case the
  conjunction form would have admitted — rather than an all-or-nothing violation.
- **`file_key` is `UNIQUE`, and the only way to violate it is a raw `INSERT`**: it is a pure
  function of the primary key (`FileRef.for_export`, XJ-7), so two aggregate-built jobs can never
  collide. The test forces a collision by hand, the same way the `CHECK` probes reach a state the
  aggregate cannot build.
- **The two-session race exercises `mark_started`, not a revision** — `ExportJob` has no
  edit-in-place; the aggregate transition under test is `queued -> rendering`, and "the loser writes
  nothing" is checked by asserting the winner's version survives unchanged, exactly as
  `test_tailoring_run_repository.py`'s own race test does for `revise_cv`.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tailorcraft.domain.export.errors import ExportJobConcurrentlyModified
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import (
    ExportFailureReason,
    ExportFormat,
    ExportJobId,
    ExportJobStatus,
)
from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.clock import FixedClock

# Importing this module is also what runs `mapper_registry.map_imperatively(ExportJob, ...)` as an
# import side effect — without it `ExportJob._id` etc. do not exist yet and every repository call
# below fails at collection time with `AttributeError: type object 'ExportJob' has no attribute
# '_id'` (the identical note `test_tailoring_run_repository.py` carries for its own aggregate).
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.repositories.export.export_job import (
    SqlAlchemyExportJobRepository,
)
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.settings import Settings

# --- Test helpers --------------------------------------------------------------------------------


async def _persist_owner(
    session: AsyncSession, clock: FixedClock, *, token_hash: str
) -> GuestSession:
    """An `ExportJob` needs a real, persisted `GuestSession` to satisfy the `NOT NULL` FK
    (`export_job.guest_session_id`) — every test below builds one first."""
    repo = SqlAlchemyGuestSessionRepository(session)
    owner = GuestSession.start(
        id=repo.next_identity(), token_hash=token_hash, at=clock.now(), ttl_hours=24
    )
    await repo.add(owner)
    return owner


def _queued(
    jobs: SqlAlchemyExportJobRepository,
    owner_id: GuestSessionId,
    clock: FixedClock,
    *,
    document: TailoredDocumentKind = TailoredDocumentKind.CV,
    format: ExportFormat = ExportFormat.PDF,
    run_version: int = 1,
) -> ExportJob:
    return ExportJob.request(
        id=jobs.next_identity(),
        guest_session_id=owner_id,
        tailoring_run_id=TailoringRunId(value=uuid4()),
        document=document,
        format=format,
        run_version=run_version,
        requested_at=clock.now(),
    )


def _rendering(
    jobs: SqlAlchemyExportJobRepository, owner_id: GuestSessionId, clock: FixedClock
) -> ExportJob:
    job = _queued(jobs, owner_id, clock)
    job.mark_started(clock.now())
    return job


def _ready(
    jobs: SqlAlchemyExportJobRepository, owner_id: GuestSessionId, clock: FixedClock
) -> ExportJob:
    job = _rendering(jobs, owner_id, clock)
    job.mark_ready(byte_size=4096, render_duration_ms=850, at=clock.now())
    return job


def _failed(
    jobs: SqlAlchemyExportJobRepository,
    owner_id: GuestSessionId,
    clock: FixedClock,
    *,
    reason: ExportFailureReason = ExportFailureReason.NOT_QUEUED,
) -> ExportJob:
    """`queued -> failed`: the one path a job can fail without ever starting (`not_queued` is the
    only reason `mark_failed` accepts from `queued`, per the aggregate's own docstring table)."""
    job = _queued(jobs, owner_id, clock)
    job.mark_failed(reason, clock.now())
    return job


async def _raw_insert(
    session: AsyncSession, owner_id: GuestSessionId, clock: FixedClock, **overrides: object
) -> None:
    """Insert one `export_job` row by hand, bypassing the aggregate entirely — the only way to reach
    a state the seven `CHECK` constraints exist to refuse. `defaults` is an otherwise-legal `queued`
    row; each test overrides only the column(s) that pairing is about, so every OTHER constraint
    stays satisfied and the `IntegrityError` raised is unambiguously the one under test.
    """
    defaults: dict[str, object] = {
        "id": uuid4(),
        "guest_session_id": owner_id.value,
        "tailoring_run_id": uuid4(),
        "document": "cv",
        "format": "pdf",
        "run_version": 1,
        "status": "queued",
        "failure_reason": None,
        "file_key": None,
        "byte_size": None,
        "render_duration_ms": None,
        "requested_at": clock.now(),
        "started_at": None,
        "completed_at": None,
        "version": 1,
    }
    defaults.update(overrides)
    await session.execute(
        text(
            "INSERT INTO export_job "
            "(id, guest_session_id, tailoring_run_id, document, format, run_version, status, "
            "failure_reason, file_key, byte_size, render_duration_ms, requested_at, started_at, "
            "completed_at, version) "
            "VALUES (:id, :guest_session_id, :tailoring_run_id, :document, :format, :run_version, "
            ":status, :failure_reason, :file_key, :byte_size, :render_duration_ms, :requested_at, "
            ":started_at, :completed_at, :version)"
        ),
        defaults,
    )


# --- Mapping round-trip: every value object comes back as its own type, for ALL FOUR statuses ------


async def test_round_trip_of_a_queued_job_preserves_value_object_types_and_nulls(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="1" * 64)
    jobs = SqlAlchemyExportJobRepository(session)
    job = _queued(jobs, owner.id, clock)
    await jobs.add(job)

    session.expunge_all()
    reloaded = await jobs.get(job.id)

    assert isinstance(reloaded.id, ExportJobId)
    assert reloaded.id == job.id
    assert isinstance(reloaded.guest_session_id, GuestSessionId)
    assert reloaded.guest_session_id == owner.id
    assert isinstance(reloaded.tailoring_run_id, TailoringRunId)
    assert reloaded.tailoring_run_id == job.tailoring_run_id
    assert reloaded.document is TailoredDocumentKind.CV
    # An enum MEMBER, not a str that merely compares equal — `ExportJob`'s own transition guards
    # are written with `is`/`in` against enum members, so a loaded row that came back as a bare
    # `str` would refuse every legal transition while `==` elsewhere kept insisting it was fine.
    assert reloaded.status is ExportJobStatus.QUEUED
    assert reloaded.format is ExportFormat.PDF
    assert reloaded.run_version == 1
    assert reloaded.failure_reason is None
    assert reloaded.file_key is None
    assert reloaded.byte_size is None
    assert reloaded.render_duration_ms is None
    assert reloaded.started_at is None
    assert reloaded.completed_at is None
    assert reloaded.version == 1


async def test_round_trip_of_a_rendering_job_preserves_value_object_types(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="2" * 64)
    jobs = SqlAlchemyExportJobRepository(session)
    job = _rendering(jobs, owner.id, clock)
    await jobs.add(job)

    session.expunge_all()
    reloaded = await jobs.get(job.id)

    assert reloaded.status is ExportJobStatus.RENDERING
    assert reloaded.started_at is not None
    assert reloaded.started_at == job.started_at
    assert reloaded.file_key is None
    assert reloaded.byte_size is None
    assert reloaded.render_duration_ms is None
    assert reloaded.completed_at is None
    assert reloaded.version == 2


async def test_round_trip_of_a_ready_job_preserves_the_file_key_byte_size_and_duration(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="3" * 64)
    jobs = SqlAlchemyExportJobRepository(session)
    job = _ready(jobs, owner.id, clock)
    await jobs.add(job)

    session.expunge_all()
    reloaded = await jobs.get(job.id)

    assert reloaded.status is ExportJobStatus.READY
    assert isinstance(reloaded.file_key, FileRef)
    assert reloaded.file_key == job.storage_ref
    assert reloaded.byte_size == 4096
    assert reloaded.render_duration_ms == 850
    assert reloaded.completed_at is not None
    assert reloaded.completed_at == job.completed_at
    assert reloaded.failure_reason is None
    assert reloaded.version == 3


async def test_round_trip_of_a_failed_job_preserves_the_failure_reason_and_never_started(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="4" * 64)
    jobs = SqlAlchemyExportJobRepository(session)
    job = _failed(jobs, owner.id, clock, reason=ExportFailureReason.NOT_QUEUED)
    await jobs.add(job)

    session.expunge_all()
    reloaded = await jobs.get(job.id)

    assert reloaded.status is ExportJobStatus.FAILED
    assert reloaded.failure_reason is ExportFailureReason.NOT_QUEUED
    assert reloaded.file_key is None
    assert reloaded.byte_size is None
    assert reloaded.render_duration_ms is None
    assert reloaded.completed_at is not None
    assert reloaded.completed_at == job.completed_at
    # A `queued -> failed` job legitimately never started — the round trip must preserve that
    # `None` rather than inventing a `started_at` to satisfy some other reader.
    assert reloaded.started_at is None
    assert reloaded.version == 2


# --- Whole-second timestamp fidelity (ADR-0007), across all three timestamp columns ----------------


async def test_round_trip_preserves_whole_second_requested_started_and_completed_at(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="5" * 64)
    jobs = SqlAlchemyExportJobRepository(session)
    job = _ready(jobs, owner.id, clock)
    await jobs.add(job)

    session.expunge_all()
    reloaded = await jobs.get(job.id)

    assert reloaded.requested_at.microsecond == 0
    assert reloaded.requested_at == job.requested_at
    assert reloaded.started_at is not None
    assert reloaded.started_at.microsecond == 0
    assert reloaded.started_at == job.started_at
    assert reloaded.completed_at is not None
    assert reloaded.completed_at.microsecond == 0
    assert reloaded.completed_at == job.completed_at


# --- NULL round-trips for every optional column (a `queued` row is the ordinary case, not the -----
# --- edge case) --------------------------------------------------------------------------------


async def test_a_queued_job_round_trips_null_for_every_optional_column(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="6" * 64)
    jobs = SqlAlchemyExportJobRepository(session)
    job = _queued(jobs, owner.id, clock)
    await jobs.add(job)

    session.expunge_all()
    reloaded = await jobs.get(job.id)

    assert reloaded.failure_reason is None
    assert reloaded.file_key is None
    assert reloaded.byte_size is None
    assert reloaded.render_duration_ms is None
    assert reloaded.started_at is None
    assert reloaded.completed_at is None


# --- AC-4: the seven CHECK constraints, each rejecting its own bad row --------------------------


async def test_check_constraint_rejects_a_queued_row_with_an_inline_format(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`ck_export_job_format_is_queued` — the third lock on XJ-2, the only one that binds a
    hand-written `INSERT` (`ExportJob.request` and `FileRef.for_export` are the other two, and
    neither can be reached from raw SQL)."""
    owner = await _persist_owner(session, clock, token_hash="7" * 64)

    with pytest.raises(IntegrityError, match="ck_export_job_format_is_queued"):
        await _raw_insert(session, owner.id, clock, format="md")


async def test_check_constraint_rejects_a_run_version_of_zero(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`ck_export_job_run_version_positive` (XJ-8) — the rule where `ExportJob.request`'s own
    `InvalidRunVersion` guard cannot reach: a hand-written row."""
    owner = await _persist_owner(session, clock, token_hash="8" * 64)

    with pytest.raises(IntegrityError, match="ck_export_job_run_version_positive"):
        await _raw_insert(session, owner.id, clock, run_version=0)


async def test_check_constraint_rejects_a_ready_row_missing_only_the_file_key(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`ck_export_job_file_key_matches_status` — the half-row case a conjunction-form constraint
    would have admitted (1.3's AC-4 amendment): `byte_size` and `render_duration_ms` are both
    present, `file_key` alone is missing, and `status = 'ready'`."""
    owner = await _persist_owner(session, clock, token_hash="9" * 64)

    with pytest.raises(IntegrityError, match="ck_export_job_file_key_matches_status"):
        await _raw_insert(
            session,
            owner.id,
            clock,
            status="ready",
            file_key=None,
            byte_size=100,
            render_duration_ms=50,
            completed_at=clock.now(),
        )


async def test_check_constraint_rejects_a_ready_row_missing_only_the_byte_size(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`ck_export_job_byte_size_matches_status` — the mirror half-row: `file_key` and
    `render_duration_ms` present, `byte_size` alone missing."""
    owner = await _persist_owner(session, clock, token_hash="a" * 64)

    with pytest.raises(IntegrityError, match="ck_export_job_byte_size_matches_status"):
        await _raw_insert(
            session,
            owner.id,
            clock,
            status="ready",
            file_key="ab/cd/11111111-1111-7111-8111-111111111111.pdf",
            byte_size=None,
            render_duration_ms=50,
            completed_at=clock.now(),
        )


async def test_check_constraint_rejects_a_ready_row_missing_only_the_render_duration(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`ck_export_job_render_duration_matches_status` — the third half-row: `file_key` and
    `byte_size` present, `render_duration_ms` alone missing."""
    owner = await _persist_owner(session, clock, token_hash="b" * 64)

    with pytest.raises(IntegrityError, match="ck_export_job_render_duration_matches_status"):
        await _raw_insert(
            session,
            owner.id,
            clock,
            status="ready",
            file_key="ab/cd/22222222-2222-7222-8222-222222222222.pdf",
            byte_size=100,
            render_duration_ms=None,
            completed_at=clock.now(),
        )


async def test_check_constraint_rejects_a_failed_row_missing_the_failure_reason(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`ck_export_job_failure_reason_matches_status` (XJ-3)."""
    owner = await _persist_owner(session, clock, token_hash="c" * 64)

    with pytest.raises(IntegrityError, match="ck_export_job_failure_reason_matches_status"):
        await _raw_insert(
            session,
            owner.id,
            clock,
            status="failed",
            failure_reason=None,
            completed_at=clock.now(),
        )


async def test_check_constraint_rejects_a_terminal_row_missing_completed_at(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`ck_export_job_completed_at_matches_terminal_status` — a `failed` row (a terminal status)
    with `completed_at` unset, every other constraint satisfied."""
    owner = await _persist_owner(session, clock, token_hash="d" * 64)

    with pytest.raises(IntegrityError, match="ck_export_job_completed_at_matches_terminal_status"):
        await _raw_insert(
            session,
            owner.id,
            clock,
            status="failed",
            failure_reason="render_failed",
            completed_at=None,
        )


# --- UNIQUE on file_key: only a raw INSERT can force the collision a pure function forbids --------


async def test_unique_constraint_rejects_two_ready_rows_sharing_one_file_key(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`uq_export_job_file_key` — a corruption detector, not a business rule (the mapping module's
    own docstring): `file_key` is `FileRef.for_export(id, format).key`, a pure function of the
    primary key, so two rows built through the aggregate can never collide. The only way to reach
    the state this constraint refuses is to write a key neither row derived, by hand."""
    owner = await _persist_owner(session, clock, token_hash="e" * 64)
    shared_key = "ab/cd/33333333-3333-7333-8333-333333333333.pdf"

    await _raw_insert(
        session,
        owner.id,
        clock,
        status="ready",
        file_key=shared_key,
        byte_size=1,
        render_duration_ms=1,
        completed_at=clock.now(),
    )

    with pytest.raises(IntegrityError, match="uq_export_job_file_key"):
        await _raw_insert(
            session,
            owner.id,
            clock,
            status="ready",
            file_key=shared_key,
            byte_size=2,
            render_duration_ms=2,
            completed_at=clock.now(),
        )


# --- AC-22: deleting a guest session cascades to its export jobs ---------------------------------


async def test_deleting_a_guest_session_cascades_to_its_export_jobs(
    session: AsyncSession, clock: FixedClock
) -> None:
    """This is what lets the 1.6 purge reach `export_job` with **no new predicate**: it stays
    `identity_guest_session.expires_at < now()` and nothing else. Two jobs, one session, one delete
    — both rows must be gone in the same statement."""
    owner = await _persist_owner(session, clock, token_hash="f" * 64)
    jobs = SqlAlchemyExportJobRepository(session)
    first = _queued(jobs, owner.id, clock)
    await jobs.add(first)
    second = _ready(jobs, owner.id, clock)
    await jobs.add(second)

    await session.execute(guest_session_table.delete().where(guest_session_table.c.id == owner.id))
    await session.flush()

    # `export_job.id` is `TypeDecorator`-backed (`ExportJobIdType`), so the bound parameter is the
    # domain value object the decorator's `process_bind_param` expects — not the bare `UUID`
    # underneath it, the same lesson `test_tailoring_run_repository.py`'s cascade test needed.
    remaining = await session.execute(
        select(export_job_table.c.id).where(export_job_table.c.id.in_([first.id, second.id]))
    )
    assert remaining.scalars().all() == []


# --- AC-23: file_key is a pure function of (id, format), read straight from the row --------------


async def test_a_ready_jobs_file_key_column_equals_file_ref_for_export_of_its_id_and_format(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`row.file_key == FileRef.for_export(row.id, row.format).key` — the retention hook 1.6
    consumes together with AC-22's cascade: rows first, then files (from the keys read before the
    delete, or derived from the ids afterwards). Read with a raw `text()` query rather than through
    the mapping, so `row.file_key` is the column's literal `VARCHAR` value and the comparison is
    against the string the database actually holds, not a second decoded `FileRef`.
    """
    owner = await _persist_owner(session, clock, token_hash="10" * 32)
    jobs = SqlAlchemyExportJobRepository(session)
    job = _ready(jobs, owner.id, clock)
    await jobs.add(job)
    await session.flush()

    result = await session.execute(
        text("SELECT id, format, file_key FROM export_job WHERE id = :id"),
        {"id": job.id.value},
    )
    row = result.one()
    expected = FileRef.for_export(ExportJobId(row.id), ExportFormat(row.format))

    assert row.file_key == expected.key
    assert job.file_key is not None
    assert row.file_key == job.file_key.key


# --- The partial index exists, with its predicate ------------------------------------------------


async def test_ix_export_job_guest_session_id_exists(session: AsyncSession) -> None:
    """Serves `count_for_session` (the `TooManyExportJobs` cap) and the cascade above."""
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'export_job' AND indexname = 'ix_export_job_guest_session_id'"
        )
    )
    assert result.scalar_one_or_none() == "ix_export_job_guest_session_id"


async def test_ix_export_job_rendering_started_at_exists_with_its_partial_predicate(
    session: AsyncSession,
) -> None:
    """The stale-render sweep's index (`list_stale_rendering`). Postgres re-renders the predicate
    with a varchar cast, so the exact `pg_indexes.indexdef` text is
    `WHERE ((status)::text = 'rendering'::text)` — an assertion against the literal source
    (`WHERE status = 'rendering'`) would fail against a real database, correctly. Two columns,
    `(started_at, id)`, unlike 1.3's single-column twin — the `id` tiebreak lives inside this index
    too."""
    result = await session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'export_job' AND indexname = 'ix_export_job_rendering_started_at'"
        )
    )
    indexdef = result.scalar_one()
    assert "(started_at, id)" in indexdef
    assert "WHERE ((status)::text = 'rendering'::text)" in indexdef


# --- AC-6: two sessions racing to start one job — the mapper's version seam ----------------------


async def test_two_sessions_racing_to_start_one_job_the_second_save_raises_concurrently_modified(
    session: AsyncSession, connection: AsyncConnection, clock: FixedClock
) -> None:
    """The mapping's `version_id_col=export_job.c.version` / `version_id_generator=False` seam
    (AC-6), exercised directly against the repository — no HTTP, no use case, just two independent
    `AsyncSession`s on the same already-open connection, the identical technique
    `test_tailoring_run_repository.py`'s own race test uses. Two traps are real and were measured
    there, and both apply here unchanged: the identity map holds its entries *weakly*, so the second
    session's copy of the job must be kept in a local for the whole test or it is collected the
    moment nothing else references it; and the SAVEPOINT that load opens must be released —
    `session2.commit()` — before the first writer's save, or it encloses that write and a later
    rollback on `session2` would discard it.

    This is what makes **two simultaneous deliveries of one `queued` job render exactly once**
    (X-30): both read `queued`, both pass the in-memory `ExportAlreadyStarted` guard, and the
    loser's `save` after `mark_started` raises here — before either worker spends a second on
    WeasyPrint.
    """
    owner = await _persist_owner(session, clock, token_hash="45" * 32)
    jobs = SqlAlchemyExportJobRepository(session)
    job = _queued(jobs, owner.id, clock)
    await jobs.add(job)
    # Close the transaction the `add()` flush opened (autobegin), so it is not left dangling
    # *underneath* the nested SAVEPOINT `session2` is about to open on this same connection.
    await session.commit()

    second_factory = async_sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    session2 = second_factory()
    repo2 = SqlAlchemyExportJobRepository(session2)
    # Pre-loaded into session2's identity map NOW, before the first writer moves the row on, and
    # held in a local for the rest of the test (see the docstring above).
    stale_copy = await repo2.get(job.id)
    await session2.commit()  # releases the SAVEPOINT; expire_on_commit=False keeps the copy stale

    job.mark_started(clock.now())
    await jobs.save(job)

    stale_copy.mark_started(clock.now())
    with pytest.raises(ExportJobConcurrentlyModified) as exc_info:
        await repo2.save(stale_copy)
    assert exc_info.value.job_id == job.id

    # The caller's boundary (the repository's own `save` docstring): a failed flush leaves the
    # session unusable until rolled back.
    await session2.rollback()
    await session2.close()

    session.expunge_all()
    reloaded = await jobs.get(job.id)
    assert reloaded.status is ExportJobStatus.RENDERING
    # "The loser writes nothing": the version on the row is exactly the winner's, not bumped a
    # second time by the loser's (rejected) transition.
    assert reloaded.version == job.version
    del stale_copy  # held until here on purpose (see the docstring)


# --- The migration: upgrade -> downgrade -> upgrade, with recovery if downgrade raises -----------


def test_migration_fa1bef4468bf_up_down_up_recreates_export_job(settings: Settings) -> None:
    """Run as a plain `def test_...`, not `async def` — `alembic/env.py`'s
    `run_migrations_online` wraps every online migration in its own `asyncio.run(
    run_async_migrations())`, and `asyncio.run` refuses to start a second loop on top of one
    already running. pytest-asyncio's session-scoped loop is only ever *running* while an
    `async def` test's own body executes — idle here, which is where Alembic's own fresh
    `asyncio.run()` may open one (the identical note `test_tailoring_run_repository.py`'s own
    migration test carries).

    Downgrades to `274e4abc5d73` (the revision this one is built on), then restores head within
    this one test. This suite has ONE test database, migrated to head ONCE per session
    (`conftest.py`'s `_migrated`), so a schema left behind head would silently break every test
    that runs after this one. When the table is still physically present, recovery stamps before
    upgrading, so a downgrade that completes without dropping the table cannot leave
    `alembic_version` stuck. Requests no `session`/`connection` fixture: those bind to a SAVEPOINT
    held open on the same shared connection for the rest of the test, and Alembic's own DDL needs
    to run outside of any such transaction rather than risk lock contention with it.
    """
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.test_database_url)

    async def _table_exists() -> bool:
        verify_engine = create_async_engine(settings.test_database_url, poolclass=None)
        try:
            async with verify_engine.connect() as conn:
                result = await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE tablename = 'export_job'")
                )
                return result.scalar_one_or_none() is not None
        finally:
            await verify_engine.dispose()

    assert asyncio.run(_table_exists()) is True, (
        "export_job must exist at head before this test runs"
    )

    # `downgrade()` itself can raise rather than merely complete without dropping the table. A raise
    # partway through leaves the schema in whatever state PostgreSQL's DDL transaction rolled back
    # to, not necessarily matching `alembic_version`, so the same recovery the
    # completes-but-does-not-drop path below performs (stamp forward if the table is still
    # physically present, then upgrade to head) is run here too, before the original exception is
    # re-raised — chained onto a recovery failure exactly as `assertion_error` is below, so a broken
    # recovery is never mistaken for the regression that triggered it.
    downgrade_error: Exception | None = None
    try:
        command.downgrade(config, "274e4abc5d73")
    except Exception as exc:
        downgrade_error = exc

    if downgrade_error is not None:
        try:
            if asyncio.run(_table_exists()):
                command.stamp(config, "fa1bef4468bf")
            command.upgrade(config, "head")
            assert asyncio.run(_table_exists()) is True, (
                "the schema must be back at head before the next test in the session runs"
            )
        except Exception as recovery_exc:
            raise downgrade_error from recovery_exc
        raise downgrade_error

    # The assertion is captured rather than let propagate immediately, so that a failure recovering
    # the schema below can never stand in for it in the report. Suppose `downgrade()` regresses and
    # stops dropping the table — exactly the regression this test exists to catch: `alembic_version`
    # now claims `274e4abc5d73` while the table is still physically present. A plain
    # `upgrade(config, "head")` from there would try to `CREATE TABLE` a second time, fail with
    # "relation already exists", and roll back — leaving `alembic_version` stuck behind the physical
    # schema for every later session's `_migrated` fixture, and burying the real `AssertionError`
    # under that unrelated one.
    assertion_error: AssertionError | None = None
    try:
        assert asyncio.run(_table_exists()) is False, (
            "downgrade() must drop export_job — proven to discriminate by commenting out the "
            "op.drop_table() call in the migration's downgrade() locally"
        )
    except AssertionError as exc:
        assertion_error = exc

    try:
        if asyncio.run(_table_exists()):
            # downgrade() failed to drop the table (the regression above): stamp the recorded
            # revision forward to match what the schema actually holds before upgrading, so
            # upgrade(head) is a no-op rather than a duplicate CREATE TABLE that would fail and
            # strand alembic_version behind the schema.
            command.stamp(config, "fa1bef4468bf")
        command.upgrade(config, "head")
        assert asyncio.run(_table_exists()) is True, (
            "the schema must be back at head before the next test in the session runs"
        )
    except Exception as recovery_exc:
        # A failure recovering the schema must never replace the real assertion in the report — but
        # it must not be silently lost either, so it is chained as the cause.
        if assertion_error is not None:
            raise assertion_error from recovery_exc
        raise

    if assertion_error is not None:
        raise assertion_error
