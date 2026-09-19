"""`SqlAlchemyExpiredGuestData` — the `ExpiredGuestDataPort` adapter (ADR-0018, technical plan §3).

**No new table, no new mapping, no migration** (AC-2). Four questions asked of three tables that
already exist, in Core SQL, deliberately *not* through the ORM: the purge never loads a
`GuestSession`, a `BaseCv`, a `JobPosting`, a `TailoringRun` or an `ExportJob`, so hydrating one
would buy nothing but a chance to read a column this module must never read.

**Nothing here selects a document, and no `SELECT *` appears anywhere in this module.** That is the
one rule to carry away from the file, and it is mechanical rather than a matter of care: a `SELECT *`
on `intake_base_cv` puts `extracted_text` and `original_filename` in a result set, and on
`tailoring_run` it puts a tailored CV and a cover letter there. A result set is one `repr()` away
from a log line, a Sentry frame and a test failure message. Every column below is named, and the
list of them is four ids, one instant, one enum and two storage keys (AC-16).

**Nothing here logs a document either** — and in fact nothing here logs at all. There is no
`structlog` import: the purge's one line per run belongs to the entry point, built from the
`PurgeReport` (`application/retention/purge_expired_guest_sessions.py`'s "this layer does not log").
A future line added here would carry a session id, a count and an exception *type* — never a message,
never `exc_info`.

**The `FileRef` values bound into the statements below are the domain value objects, not bare
strings.** `intake_base_cv.file_key` and `export_job.file_key` are `FileRefType` columns, so the
decorator's `process_bind_param` is what turns a `FileRef` into the `VARCHAR` Postgres compares —
handing it a `str` skips the decorator and compares a value the column never stored in that form.
`tests/integration/persistence/test_schema.py` carries the same note about `GuestSessionIdType`,
which is the trap one table over.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import cast, func, null, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.export.value_objects import ExportDelivery, ExportFormat, ExportJobId
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.retention.value_objects import ExpiringGuestSession
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.types.export import ExportFormatType, ExportJobIdType
from tailorcraft.infrastructure.persistence.types.shared import FileRefType

if TYPE_CHECKING:
    from tailorcraft.domain.retention.ports import ExpiredGuestDataPort

# The formats a job can exist for, **asked of `ExportFormat` rather than written out**. A literal
# `("pdf", "docx")` here would be a fourth copy of a fact the enum already answers (`ExportFormat`'s
# `delivery` docstring: "no router, use case or React component re-derives this"), and the copy that
# a fifth format walks straight past — silently, because an unfiltered inline row does not fail, it
# raises out of `FileRef.for_export` in the middle of a purge. Derived once, at import.
_QUEUED_FORMATS: Final[tuple[ExportFormat, ...]] = tuple(
    fmt for fmt in ExportFormat if fmt.delivery is ExportDelivery.QUEUED
)


class SqlAlchemyExpiredGuestData:
    """Everything retention asks the store of record, over one `AsyncSession` it never hands out.

    The unit of work is the caller's, exactly as it is for the four repositories: nothing in here
    commits. `delete_session` opens a SAVEPOINT because a refused `DELETE` must not take the rest of
    the batch with it, but the *commit* that makes "rows first, committed, then files" a true
    statement about durability is `CommittingExpiredGuestDataAdapter`'s, in the composition roots
    (`infrastructure/tasks/container.py`). The use case names no transaction and may not (ADR-0002).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_expired(self, as_of: datetime) -> int:
        """`SELECT count(*) FROM identity_guest_session WHERE expires_at <= :as_of`.

        Served by `ix_identity_guest_session_expires_at`, which `mapping/identity/guest_session.py`
        built one slice early and said so. `count(*)`, never `len(await ...)`: this is
        `/health/ready`'s `overdue` field, polled every 15 seconds by every open tab, and a backlog
        count that materializes the backlog gets slower exactly as the number it reports gets worse.

        `<=`, not `<`, and the boundary is a decision rather than an accident: a session whose
        `expires_at` is exactly the cutoff *is* expired (R-18), matching `GuestSession.is_expired`'s
        `at >= expires_at`. It is also the same predicate `list_expired` selects on, which is the
        whole point of ADR-0018 decision 4 — a count of what is still overdue is the one signal a job
        that is not working cannot fake.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(guest_session_table)
            .where(guest_session_table.c.expires_at <= as_of)
        )
        return result.scalar_one()

    async def list_expired(self, as_of: datetime, limit: int) -> Sequence[ExpiringGuestSession]:
        """The next `limit` expired sessions, oldest first, each with the keys of its files.

        **Two statements, never N+1.** The first picks the batch; the second gathers every file key
        belonging to it in one `UNION ALL`. A per-session follow-up query would be `limit` round
        trips for a job whose whole purpose is to run unattended on a timer against a table that
        only grows.

        Statement 1 selects `id` and `expires_at` and nothing else — `token_hash` is not retention's
        business and is the one column on this table that could authenticate a request.

        Statement 2's two halves are deliberately asymmetric:

        - `intake_base_cv` gives up `file_key` **directly**. The column is `NOT NULL` (I-1: a base
          CV has a `FileRef` from the moment it exists), so the row is the authority.
        - `export_job` gives up `id` and `format`, and the key is **derived** — `FileRef.for_export`,
          not `file_key`. A `rendering` or `failed` job can have written bytes while carrying
          `file_key IS NULL` (X-32's orphan), so reading that column here would walk past a real
          file on the volume and leave it for the orphan sweep to find hours later. This is
          ADR-0016's determinism guarantee — same id and format, same key, every time — being
          consumed for the first time (AC-9), and it is the reason that guarantee was written down
          rather than merely observed.

        The halves line up column-for-column because a `UNION ALL` demands it, so each fills the
        other's pair with a typed `NULL`. The **first** select decides the compound's result types,
        which is why its `NULL`s are cast to `ExportJobIdType` / `ExportFormatType`: an untyped
        `null()` there would hand back a bare `UUID` and a bare `str` from the export half, and
        `FileRef.for_export` would then be called with two primitives that happen to compare equal to
        the value objects it asks for.

        Order comes from statement 1 and is preserved through the assembly: the batch is oldest
        `expires_at` first, because the people whose data has been overdue longest are cleared first
        (the port's docstring) and because an unordered `--limit` run makes "which two did it take?"
        a question with no right answer (AC-7). Statement 2 is unordered on purpose — it is a
        lookup, and its rows are bucketed by session id rather than read in sequence.

        A session with no files gets an empty tuple, which is the ordinary case for someone who
        opened the app and left.

        **`FOR UPDATE SKIP LOCKED` was considered and declined** (OQ-6). It would need the batch's
        row locks held across the whole run, which is the opposite of the per-session commit this
        adapter is wrapped to provide, and it buys protection against a race that is already benign:
        two concurrent runs (R-19) both see a candidate, both issue the `DELETE`, one affects zero
        rows and neither is an error. Locking would trade a harmless duplicate for a long-held
        transaction on the table `/health/ready` counts.
        """
        candidates = (
            await self._session.execute(
                select(guest_session_table.c.id, guest_session_table.c.expires_at)
                .where(guest_session_table.c.expires_at <= as_of)
                .order_by(guest_session_table.c.expires_at.asc())
                .limit(limit)
            )
        ).all()
        if not candidates:
            return ()

        session_ids: list[GuestSessionId] = [row.id for row in candidates]
        files_by_session: dict[GuestSessionId, list[FileRef]] = {sid: [] for sid in session_ids}

        # The first half's labels name the compound's result columns, and its column *types* are the
        # ones every row is processed through — hence the casts rather than a bare `null()`.
        base_cv_half = select(
            base_cv_table.c.guest_session_id.label("guest_session_id"),
            base_cv_table.c.file_key.label("file_key"),
            cast(null(), ExportJobIdType).label("export_job_id"),
            cast(null(), ExportFormatType).label("export_format"),
        ).where(base_cv_table.c.guest_session_id.in_(session_ids))
        export_half = select(
            export_job_table.c.guest_session_id,
            cast(null(), FileRefType),
            export_job_table.c.id,
            export_job_table.c.format,
        ).where(
            export_job_table.c.guest_session_id.in_(session_ids),
            # **The `ExportFormatNotQueued` decision, made in SQL rather than in a `try`.**
            #
            # `FileRef.for_export` raises for an inline format, and by ADR-0016 no `export_job` row
            # can hold one: `ExportJob.request` refuses it, `ck_export_job_format_is_queued` refuses a
            # hand-written `INSERT`, and this filter is the third lock. So the row being excluded
            # here is an impossible row.
            #
            # Filtering won over catching, for two reasons in that order. The first is that
            # excluding it is *correct*, not merely defensive: an inline format renders inside the
            # request with no worker and no file (ADR-0005), so such a row has no bytes on the
            # volume and there is nothing for the purge to unlink. It contributes no key because it
            # has no key. The second is the blast radius if the impossible ever happened — letting
            # the exception escape would abort the **entire** purge over one row that has nothing to
            # delete, and a purge that stops running is PII living past the window FR-6 promises.
            # A `try/except` around the derivation would have matched the first reason's outcome,
            # but it would place a guard *after* the row has already been carried out of the
            # database, where the next reader has to decide whether the skip was a bug.
            #
            # The counter-argument, recorded rather than waved away: an invisible row is a schema
            # violation nobody is told about. That is true, and it is the CHECK constraint's job to
            # be loud at the `INSERT` that causes it — not this job's, whose failure mode is the
            # expensive one.
            export_job_table.c.format.in_(_QUEUED_FORMATS),
        )

        for row in (await self._session.execute(union_all(base_cv_half, export_half))).all():
            owner: GuestSessionId = row.guest_session_id
            stored: FileRef | None = row.file_key
            if stored is not None:
                files_by_session[owner].append(stored)
                continue
            job_id: ExportJobId = row.export_job_id
            job_format: ExportFormat = row.export_format
            files_by_session[owner].append(FileRef.for_export(job_id, job_format))

        return tuple(
            ExpiringGuestSession(
                session_id=row.id,
                expires_at=row.expires_at,
                files=tuple(files_by_session[row.id]),
            )
            for row in candidates
        )

    async def delete_session(self, session_id: GuestSessionId) -> None:
        """`DELETE FROM identity_guest_session WHERE id = :id`, inside a SAVEPOINT.

        **One statement.** The four child tables — `intake_base_cv`, `posting_job_posting`,
        `tailoring_run`, `export_job` — go by their `ON DELETE CASCADE` foreign keys, which is why
        this slice adds no per-context delete and why AC-2 proves the cascades rather than trusting
        the comments that promised them.

        **Deleting an already-deleted session affects zero rows and is not an error** (AC-15, R-19).
        No `rowcount` is read, and none is returned: "how many rows went" is a question about the
        cascade, and the cascade is the database's mechanism rather than this port's promise. That
        silence is half of what makes the purge safe to retry, to redeliver and to run twice at once.

        **Why the SAVEPOINT, and why this method takes one id rather than a batch.** A failed flush
        rolls back to the nearest transaction boundary, and at the *root* boundary SQLAlchemy
        restores the snapshot by expiring every loaded instance — inside the flush, before any
        `except` of ours runs (`SessionTransaction._restore_snapshot(dirty_only=False)`, measured in
        1.4). A batch-wide delete would therefore turn one refused row into `MissingGreenlet` on
        every remaining candidate. Inside a SAVEPOINT the boundary is the nested transaction, the
        restore is `dirty_only=True`, the outer transaction stays usable, and
        `PurgeExpiredGuestSessions` counts one `sessions_failed` and carries on (R-3, AC-13).

        **`begin_nested()` flushes on entry, unconditionally**, to take that snapshot — which is why
        `CommittingTailoringRunRepository.save` has to `expunge` its dirty aggregate first. There is
        no equivalent here and there does not need to be: this adapter never adds an aggregate to the
        session, and the purge's composition root gives it a session nothing else writes through. So
        the entry flush has nothing to flush. If a future root ever shares this session with a
        repository, that stops being true, and the fix is that root's — not a second `expunge` here
        for an object this class does not know about.
        """
        async with self._session.begin_nested():
            await self._session.execute(
                guest_session_table.delete().where(guest_session_table.c.id == session_id)
            )

    async def which_are_referenced(self, keys: Sequence[FileRef]) -> frozenset[FileRef]:
        """Of these storage keys, the subset a live row still points at — one statement, always.

        A `UNION ALL` of two `IN` lookups over `intake_base_cv.file_key` and `export_job.file_key`,
        never `len(keys)` round trips: the orphan sweep asks this about every recognised file on the
        volume at once, so a per-key query is a sweep whose cost grows with the thing it exists to
        shrink.

        **An empty input never reaches the database.** `WHERE file_key IN ()` is not valid SQL, and
        SQLAlchemy's rewrite of an empty `IN` is a predicate that matches nothing — correct, and a
        round trip to learn what the caller already knew. `ReclaimOrphanedFiles` guards this too
        (`if keys:`); both guards stay, because this one is about the statement and that one is
        about not asking a question with no subject.

        Duplicates are collapsed before binding. `FileRef` is a frozen dataclass and the answer is a
        `frozenset`, so a repeated key changes nothing about the result — it only widens the bound
        parameter list, and the sweep can legitimately see the same key twice if a volume is ever
        restored over itself.

        `export_job.file_key` is nullable, and `IN` excludes `NULL` on its own — a `rendering` job's
        empty key is simply not a member, which is the right answer: it references nothing *yet*.
        That is the same row whose bytes `list_expired` derives a key for, and the asymmetry is
        intentional in both directions. There, the question is "what might this row have written?"
        and the answer must be generous. Here, the question is "does a row point at this file?" and
        the answer must be exact, because the caller deletes what is *not* in it.

        Returning the **referenced** set rather than the orphaned one is ADR-0018 decision 6, and it
        is what makes an adapter bug here cheap: a query that finds too little can only spare a file,
        never delete one. Nothing in this method is caught, either — if the read fails, it fails, and
        `ReclaimOrphanedFiles` turns that into `OrphanScanAborted` with nothing unlinked (R-33).
        """
        if not keys:
            return frozenset()
        # `dict.fromkeys`, not `set`, so the bound parameter order is the caller's and a statement
        # is reproducible when someone pastes it into `EXPLAIN`.
        unique: list[FileRef] = list(dict.fromkeys(keys))
        result = await self._session.execute(
            union_all(
                select(base_cv_table.c.file_key.label("file_key")).where(
                    base_cv_table.c.file_key.in_(unique)
                ),
                select(export_job_table.c.file_key).where(export_job_table.c.file_key.in_(unique)),
            )
        )
        return frozenset(result.scalars().all())


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance rather than trusting four signatures by eye — the
    # same assertion the four repositories carry, and what `domain/retention/ports.py` points at
    # when it explains why a `Protocol` gets no red-first cycle of its own. Never executed.
    def _assert_implements_expired_guest_data(adapter: SqlAlchemyExpiredGuestData) -> None:
        _: ExpiredGuestDataPort = adapter
