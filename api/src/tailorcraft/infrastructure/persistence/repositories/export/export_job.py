"""`SqlAlchemyExportJobRepository` — the `ExportJobRepository` port (ADR-0007).

Filters below query `ExportJob._id` / `ExportJob._guest_session_id` / `ExportJob._status` and the
rest, the **private** attributes the imperative mapping in
`infrastructure/persistence/mapping/export/export_job.py` targets — never `ExportJob.id` /
`ExportJob.status`. Those short names are plain read-only `@property` objects on the domain class,
not `InstrumentedAttribute`s: `select(ExportJob).where(ExportJob.id == x)` would call the property,
get back an `ExportJobId`, evaluate a bare Python `==` against `x`, and build `select(...)
.where(True)` or `.where(False)` — a predicate that matches everything or nothing, silently, with no
error anywhere. It looks like a typo the first time you see it; it is not one. The identical note
sits on all three earlier repositories.

This is the codebase's fourth repository and the **widest**: nine methods where `TailoringRun`'s has
seven, because the `export` context has three entry points onto one job (poll, download, list) plus
an idempotency lookup the earlier contexts had no equivalent of. Two of the nine — `list_for_run`
and `find_latest_for_key` — exist for exactly one caller each, and their contracts are at the port,
not restated here.

**Nothing in this module logs a document.** That is easier here than anywhere else in the codebase
and worth naming rather than assuming: an `export_job` row holds ids, enums, integers, instants and
a storage key, and no column of it is PII (ADR-0016 §4). The one log line below carries a job id and
an exception *type*.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, cast

import structlog
from sqlalchemy import func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.orm.exc import StaleDataError

from tailorcraft.domain.export.errors import (
    ExportJobConcurrentlyModified,
    ExportJobNotFound,
)
from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportFormat, ExportJobId, ExportJobStatus
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.tailoring.value_objects import TailoredDocumentKind, TailoringRunId
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.types.export import ExportJobStatusType

if TYPE_CHECKING:
    from tailorcraft.domain.export.ports import ExportJobRepository

log = structlog.get_logger(__name__)

# `ExportJob._id` etc. are class-body annotations only (`domain/export/export_job.py` assigns no
# value — `map_imperatively`'s `properties=` installs the real `InstrumentedAttribute` at import
# time), so mypy sees them typed as the *domain* type (`ExportJobId`, `ExportJobStatus`,
# `datetime`, …) rather than as SQLAlchemy's mapped attribute. `ExportJob._id == job_id` then
# type-checks as `ExportJobId.__eq__` returning a plain `bool` — which is exactly the runtime trap
# the module docstring describes, just caught by mypy instead of by a silently-empty query. These
# `cast`s tell mypy what is actually there at runtime without touching behaviour; each is a `cast`,
# not an `Any`, so CLAUDE.md's ban on unjustified `Any` does not apply.
_EXPORT_JOB_ID: InstrumentedAttribute[ExportJobId] = cast(
    "InstrumentedAttribute[ExportJobId]", ExportJob._id
)
_EXPORT_JOB_GUEST_SESSION_ID: InstrumentedAttribute[GuestSessionId] = cast(
    "InstrumentedAttribute[GuestSessionId]", ExportJob._guest_session_id
)
_EXPORT_JOB_TAILORING_RUN_ID: InstrumentedAttribute[TailoringRunId] = cast(
    "InstrumentedAttribute[TailoringRunId]", ExportJob._tailoring_run_id
)
_EXPORT_JOB_DOCUMENT: InstrumentedAttribute[TailoredDocumentKind] = cast(
    "InstrumentedAttribute[TailoredDocumentKind]", ExportJob._document
)
_EXPORT_JOB_FORMAT: InstrumentedAttribute[ExportFormat] = cast(
    "InstrumentedAttribute[ExportFormat]", ExportJob._format
)
_EXPORT_JOB_STATUS: InstrumentedAttribute[ExportJobStatus] = cast(
    "InstrumentedAttribute[ExportJobStatus]", ExportJob._status
)
_EXPORT_JOB_REQUESTED_AT: InstrumentedAttribute[datetime] = cast(
    "InstrumentedAttribute[datetime]", ExportJob._requested_at
)
_EXPORT_JOB_STARTED_AT: InstrumentedAttribute[datetime | None] = cast(
    "InstrumentedAttribute[datetime | None]", ExportJob._started_at
)


class SqlAlchemyExportJobRepository:
    """Persistence for `ExportJob`, backed by `export_job`.

    Hides its `AsyncSession` completely — nothing outside this module ever sees it (ADR-0007). The
    unit of work (commit/rollback) is the caller's concern: one transaction per request in
    `infrastructure/api/deps.py::get_session`, and **two** per render in the worker, because
    `RenderExportJob` has to make `rendering` visible to a polling client while the render is still
    in flight (the port's `save` docstring).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def next_identity(self) -> ExportJobId:
        """Synchronous: application-assigned UUIDv7 needs no I/O (ADR-0007).

        It does two further jobs here that it does in no other repository. The id is the only
        argument the queued task ever receives (ADR-0014 §5), *and* it is the sole input to the
        job's storage key (`FileRef.for_export`, XJ-7) — so this call is what makes the rendered
        file's name known before the row is committed and before a byte is rendered.
        """
        return ExportJobId(uuid7())

    async def add(self, job: ExportJob) -> None:
        self._session.add(job)
        # `flush()`, not `commit()`: the transaction boundary belongs to the caller (a request or a
        # task), not to the repository.
        await self._session.flush()

    async def save(self, job: ExportJob) -> None:
        """Make the job's current state the state the next read hands back.

        A job reached this repository through `get`/`find`, so it is already persistent in this
        session's identity map and the flush alone emits the `UPDATE`; `add()` is a no-op for such
        an instance and is kept only so a job that has become detached (a session closed between the
        load and the save) is re-attached rather than silently not written.

        `flush`, not `commit`, for the same reason as `add` — the *caller's* two commits are the
        load-bearing part (port docstring, technical-plan "Transaction boundaries").

        Raises `ExportJobConcurrentlyModified` when the row moved on since this aggregate was loaded
        (XJ-6, ADR-0015 §3). The mapping declares `version` as `version_id_col`, so the flush's
        `UPDATE … WHERE id = :id AND version = :loaded` matches no row and SQLAlchemy raises
        `StaleDataError` — a vendor exception nothing outside this module may see, translated here
        into the domain's name for it. The session is left in the failed state a failed flush leaves
        it in: rolling back is the caller's boundary, exactly as committing is.

        **The id is read BEFORE the flush, and that line is load-bearing** (1.4's lesson, carried
        across from `SqlAlchemyTailoringRunRepository.save` because it is a trap and not a
        convention). A failed flush rolls its transaction back *on the way out*, and at the root
        boundary that rollback expires **every** instance the session holds — `job` included,
        because it is the dirty one. The session's transaction is then inactive until the caller
        rolls back, so touching `job.id` inside the `except` would try to re-load an expired
        attribute on a session that refuses to run a query: the `StaleDataError` this branch exists
        to translate would surface instead as a `PendingRollbackError`, a `SQLAlchemyError` nothing
        above this layer maps, rendered as a 503 for what is a 409. A plain `ExportJobId` value
        taken up front is immune to the expiry.

        That is also why `CommittingExportJobRepository` (the worker's wrapper, I14) puts this flush
        in a SAVEPOINT: the expiry then stops at the dirty job instead of reaching the whole
        identity map.
        """
        job_id = job.id
        self._session.add(job)
        try:
            await self._session.flush()
        except StaleDataError as exc:
            # The type only, never the message: SQLAlchemy's names the table and the primary key,
            # and `from None` makes the frame unreachable from any report of the domain error. The
            # PII argument that motivates both on `tailoring_run` does not apply here — no column of
            # `export_job` is PII (ADR-0016 §4) — but the *shape* is kept identical on purpose, so
            # that no future reader has to work out which repositories may log an exception message
            # and which may not. One rule, four adapters.
            log.warning(
                "export.concurrent_modification",
                export_job_id=str(job_id.value),
                error_type=type(exc).__name__,
            )
            raise ExportJobConcurrentlyModified(job_id) from None

    async def get(self, job_id: ExportJobId) -> ExportJob:
        """Raises `ExportJobNotFound` if no job with this id exists — **the API's read path.**"""
        # The column stays on the left of `==` below (silencing ruff's SIM300 "Yoda condition"):
        # see the module-level cast comment for why swapping it to satisfy that check would
        # silently re-break mypy.
        result = await self._session.execute(
            select(ExportJob).where(_EXPORT_JOB_ID == job_id)  # noqa: SIM300
        )
        found = result.scalar_one_or_none()
        if found is None:
            # **`from None` is load-bearing, not tidiness**, for the reason
            # `SqlAlchemyTailoringRunRepository.get` records: the API answers one shared 404 for
            # "no such job" and "not your job" (X-42), and the test that proves the second does not
            # leak through the first asserts that an `ExportJobNotFound` raised for a never-issued
            # id does **not** chain from `ExportJobNotOwnedBySession`. That assertion discriminates
            # only because the context is suppressed here. Let an exception chain through and the
            # test silently stops proving anything, and nothing fails.
            raise ExportJobNotFound(f"no ExportJob with id {job_id!r}") from None
        return found

    async def find(self, job_id: ExportJobId) -> ExportJob | None:
        """`None` where `get` raises — **this is the worker's lookup.**

        The asymmetry is deliberate and documented at the port: the task is handed an id that may
        legitimately have stopped existing between the enqueue and the pickup (a guest session
        purged at the 24-hour mark cascades its jobs away while a message for one of them is still
        in Redis, and `task_acks_late=True` makes redelivery of an already-purged id real). Absence
        is an ordinary branch there — the task returns `MISSING`, logs a line and stops.
        """
        result = await self._session.execute(
            select(ExportJob).where(_EXPORT_JOB_ID == job_id)  # noqa: SIM300
        )
        return result.scalar_one_or_none()

    async def list_for_run(self, run_id: TailoringRunId) -> Sequence[ExportJob]:
        """Every job requested for `run_id`, **newest first**, for the list endpoint.

        **The `id DESC` tiebreak is not decoration.** The `Clock` port is whole-second by contract
        and `requested_at` is stored at whole-second precision, so two jobs requested inside the
        same second are ordinary rather than exotic — and this is the context where they are
        *expected*: one click on "Download both" requests a CV and a cover letter in the same
        request-handling second. With `requested_at DESC` alone their relative order is whatever the
        plan happens to produce, which is to say undefined and free to change when the table grows a
        row or an index. `ExportJobId` is a UUIDv7, so its byte order is time order at a finer
        resolution than the column keeps: the tiebreak is the same "newest first", one resolution
        down.

        An empty sequence when the run has no exports — never an error (port docstring).
        """
        result = await self._session.execute(
            select(ExportJob)
            .where(_EXPORT_JOB_TAILORING_RUN_ID == run_id)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
            .order_by(_EXPORT_JOB_REQUESTED_AT.desc(), _EXPORT_JOB_ID.desc())
        )
        return result.scalars().all()

    async def find_latest_for_key(
        self, run_id: TailoringRunId, document: TailoredDocumentKind, format: ExportFormat
    ) -> ExportJob | None:
        """The most recently requested job for the (run, document, format) key, or `None`.

        The contract is the port's: the key is deliberately **not** unique, "latest" is
        `requested_at DESC, id DESC`, and the run version is not part of the key precisely so
        `RequestExport` can *see* the stale job and decide it is stale (ADR-0016 (b)).

        `LIMIT 1` over the ordered query, never `scalar_one_or_none()` — the key admits many rows by
        design, so anything that raises on a second one would turn the normal case (a user who
        exported, edited and exported again) into an error.

        Served by `ix_export_job_tailoring_run_id` plus a filter and a sort over the few dozen jobs a
        run can hold. The composite index that would serve it exactly is named in the mapping module
        as the change to make if that ever stops being true.
        """
        result = await self._session.execute(
            select(ExportJob)
            .where(_EXPORT_JOB_TAILORING_RUN_ID == run_id)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
            .where(_EXPORT_JOB_DOCUMENT == document)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
            .where(_EXPORT_JOB_FORMAT == format)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
            .order_by(_EXPORT_JOB_REQUESTED_AT.desc(), _EXPORT_JOB_ID.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def count_for_session(self, sid: GuestSessionId) -> int:
        """`SELECT count(*)`, not `len(await ...)` — the port docstring is explicit that the
        `TooManyExportJobs` cap must not materialize every row just to measure them. The saving is
        smaller here than on `tailoring_run`, whose rows each carry two documents, and the reason is
        the same anyway: the cap exists to bound a runaway loop, and a defence that costs more the
        worse the abuse gets is not a defence."""
        result = await self._session.execute(
            select(func.count()).select_from(ExportJob).where(_EXPORT_JOB_GUEST_SESSION_ID == sid)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
        )
        return result.scalar_one()

    async def list_stale_rendering(
        self, started_before: datetime, limit: int
    ) -> Sequence[ExportJob]:
        """The contract is `ExportJobRepository.list_stale_rendering`'s docstring: which jobs, the
        `NULL` fold, the total order, the bound, no locking. It is not restated here, so there is one
        copy to keep true. What follows is only what this adapter adds — and it is 1.3's
        `list_stale_running` line for line, because the two sweeps differ in their aggregate and not
        in their SQL.

        **The status reaches Postgres as a literal (`literal_execute=True`), not a bound
        parameter.** `ix_export_job_rendering_started_at` is a partial index, and the planner uses
        one only when it can prove the query's `WHERE` implies the index's. A generic prepared plan
        holding `status = $1` proves nothing, so on a table big enough to matter the sweep could fall
        back to a sequential scan with nothing reporting it — every minute, for ever. That was
        measured on `tailoring_run` at 1.3 (`EXPLAIN (GENERIC_PLAN)`: Seq Scan for `status = $3`,
        Index Scan for the literal), not reasoned about. The value is still rendered through
        `ExportJobStatusType`, so the literal is the decorator's spelling of the enum and not a
        second one.

        **`NULLS FIRST` is spelled out** because an ascending Postgres sort puts `NULL` *last*, the
        opposite of what the port promises. The `None` half of the filter is not a state this
        codebase writes — `mark_started` sets the status and the instant together — but a filter that
        left out a row the rule calls stale would hide it from the caller's re-check, and that row
        would stay `rendering` for ever.
        """
        rendering = literal(ExportJobStatus.RENDERING, ExportJobStatusType(), literal_execute=True)
        result = await self._session.execute(
            select(ExportJob)
            .where(_EXPORT_JOB_STATUS == rendering)  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
            .where(
                or_(
                    _EXPORT_JOB_STARTED_AT < started_before,  # noqa: SIM300 -- keep the InstrumentedAttribute on the left
                    _EXPORT_JOB_STARTED_AT.is_(None),
                )
            )
            .order_by(_EXPORT_JOB_STARTED_AT.asc().nulls_first(), _EXPORT_JOB_ID.asc())
            .limit(limit)
        )
        return result.scalars().all()


if TYPE_CHECKING:
    # Makes mypy prove `SqlAlchemyExportJobRepository` structurally satisfies `ExportJobRepository`
    # rather than trusting the shape by eye — nine methods is enough that "by eye" would eventually
    # miss one. Never executed (a `Protocol` needs no instance to check against, only a compatible
    # signature), so it costs nothing at runtime. This is what `domain/export/ports.py` points at
    # when it explains why a Protocol gets no red-first cycle of its own.
    def _assert_implements_export_job_repository(repo: SqlAlchemyExportJobRepository) -> None:
        _: ExportJobRepository = repo
