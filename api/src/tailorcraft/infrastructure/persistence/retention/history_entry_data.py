"""`SqlAlchemyHistoryEntryData` — the `HistoryEntryDataPort` adapter (slice 2.3, technical plan §0.6).

Deleting one entry of a signed-in user's history is retention's shape — *delete what an owner has for
X, rows committed, then files* — over three contexts' tables, so it is Core SQL here rather than a
`remove` on `TailoringRunRepository`. Five statements, one transaction, the owner in every `WHERE`:

    DELETE FROM tailoring_run WHERE id = :r AND user_id = :u RETURNING job_posting_id;  -- 0 rows → None
    DELETE FROM tracking_application WHERE tailoring_run_id = :r AND user_id = :u RETURNING id;
    DELETE FROM export_job WHERE tailoring_run_id = :r AND user_id = :u RETURNING id, format;
    SELECT id FROM posting_job_posting WHERE id = :p AND user_id = :u FOR UPDATE;
    DELETE FROM posting_job_posting
     WHERE id = :p AND user_id = :u
       AND NOT EXISTS (SELECT 1 FROM tailoring_run WHERE job_posting_id = :p);        -- rowcount

**`RETURNING` makes "what I deleted" and "what I must unlink" one set.** 2.2's erasure collects keys
first and locks the user row so nothing slips in between; here the export rows' `(id, format)` come
back from the `DELETE` itself, and each key is **derived** (`FileRef.for_export`) rather than read
from `file_key` — the purge's rule (ADR-0018 decision 3), so a `rendering` or `failed` job's
already-written bytes are not missed.

**The run goes first.** Zero rows means a concurrent deletion won (H-44): the loser returns `None`
before touching anything else, and unlinks nothing. Two deletions of one entry serialize on the run
row's lock; the second's `DELETE` matches nothing once the first commits.

**The posting survives while any run still references it** — whoever owns that run. The `NOT
EXISTS` names no owner on purpose: a reference is a reference, and a posting is cheaper to keep than
to explain the dangling id of. **The `FOR UPDATE` is what makes that true under concurrency**
(2.3 /verify, reviewer MINOR #1): `tailoring_run.job_posting_id` has no FK, and a run request for
the same posting holds it `FOR KEY SHARE` from its `INSERT` to its commit
(`SqlAlchemyTailoringRunRepository.add`). The lock waits that out, and the `DELETE` that follows is
a separate statement with a fresh READ COMMITTED snapshot, so it sees the new run and keeps the
posting. The run request, for its part, refuses with `JobPostingNotFound` if the posting is already
gone by the time it locks it. Between the two, no run can reference a deleted posting.

**Since slice 3.1 the entry's tracked application (its card) goes with it** (plan §0.7, AC-24) — a
card about documents that no longer exist is worth nothing, and refusing the deletion while the run
is tracked would put an unexpected 409 on a privacy action. Retention owns "delete everything an
owner has for X"; the card is one more table in X. **There is no FK `tracking_application.
tailoring_run_id → tailoring_run`** (ADR-0014/0016/0023 decline cross-context FKs), so the race a
track request could run against this deletion is closed by 2.3's two-lock pattern, copied rather than
invented. `SqlAlchemyTrackedApplicationRepository.add` INSERTs the card and **then** takes the run
`FOR KEY SHARE` (its INSERT's FK check already holds the user row, so account erasure — user row
first — meets it there and no lock cycle exists). This module's run `DELETE` conflicts with that
`KEY SHARE`:

- **track first:** the run `DELETE` waits for the track to commit; the card `DELETE` that follows is
  a **separate statement**, so under READ COMMITTED it takes a fresh snapshot after the wait and
  sees the committed card, and deletes it.
- **delete first:** the track's `KEY SHARE` waits for this transaction; once it commits the run row
  is gone, and the track refuses `TailoringRunNotFound` (its SAVEPOINT, card and all, rolled back).

Folding the card `DELETE` into the run's statement (a CTE, a `USING`) would read the pre-wait
snapshot and miss a card committed while the run's `DELETE` waited — CLAUDE.md's `NOT EXISTS`
footgun in another shape. An untracked entry's card `DELETE` matches nothing and changes nothing
else: its report reads `tracked_application_deleted=False`, as every 2.3 report implicitly did.

**Nothing here logs and nothing here commits.** `CommittingHistoryEntryData`
(`infrastructure/retention/data_access.py`) commits after this returns — which is what makes the
unlinks that follow in `EraseHistoryEntry` safe — and the entry point logs from the report.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.util import identity_key

from tailorcraft.domain.export.export_job import ExportJob
from tailorcraft.domain.export.value_objects import ExportDelivery, ExportFormat, ExportJobId
from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.posting.job_posting import JobPosting
from tailorcraft.domain.posting.value_objects import JobPostingId
from tailorcraft.domain.retention.value_objects import DeletedHistoryEntry
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.domain.tailoring.tailoring_run import TailoringRun
from tailorcraft.domain.tailoring.value_objects import TailoringRunId
from tailorcraft.domain.tracking.tracked_application import TrackedApplication
from tailorcraft.domain.tracking.value_objects import TrackedApplicationId, TrackedRunRef
from tailorcraft.infrastructure.persistence.mapping.export.export_job import export_job_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.mapping.tracking.tracked_application import (
    tracked_application_table,
)

if TYPE_CHECKING:
    from tailorcraft.domain.retention.ports import HistoryEntryDataPort

# Asked of `ExportFormat`, never written out — `expired_guest_data.py`'s `_QUEUED_FORMATS` and its
# reasoning. A job row for an inline format is impossible (`ExportJob.request`,
# `ck_export_job_format_is_queued`); if one ever existed it would have no file, so it contributes no
# key — and deriving one would raise out of `FileRef.for_export` mid-deletion.
_QUEUED_FORMATS: Final[frozenset[ExportFormat]] = frozenset(
    fmt for fmt in ExportFormat if fmt.delivery is ExportDelivery.QUEUED
)


class SqlAlchemyHistoryEntryData:
    """One history entry's rows, deleted over one `AsyncSession` it never hands out."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def delete_history_entry(
        self, user_id: UserId, run_id: UUID
    ) -> DeletedHistoryEntry | None:
        """The port's contract; the five statements are the module docstring's.

        **Loaded aggregates leave the identity map first**, by identity: `EraseHistoryEntry` loaded
        the run through `GetTailoringRun` to authorize it, and a Core `DELETE` does not tell the ORM
        the row went — a later flush of that instance would target a row that no longer exists (2.2's
        `delete_account` precedent). The run is expunged before its `DELETE`; the card, the jobs and
        the posting, which no caller loads today, are expunged by the identities the statements
        return, so the same holds the day one does.
        """
        typed_run_id = TailoringRunId(run_id)
        self._expunge(TailoringRun, typed_run_id)
        connection = await self._session.connection()

        posting_id: JobPostingId | None = (
            await connection.execute(
                delete(tailoring_run_table)
                .where(
                    tailoring_run_table.c.id == typed_run_id,
                    tailoring_run_table.c.user_id == user_id,
                )
                .returning(tailoring_run_table.c.job_posting_id)
            )
        ).scalar_one_or_none()
        if posting_id is None:
            return None

        # Slice 3.1 (plan §0.7): the entry's card, if the run was tracked — at most one, by
        # `uq_tracking_application_tailoring_run_id`. Its own statement, after the run's, never
        # folded into it: the run `DELETE` may have waited on a track request's `FOR KEY SHARE`,
        # and only a new statement's fresh READ COMMITTED snapshot sees the card that request
        # committed while we waited (module docstring).
        card_ids: list[TrackedApplicationId] = list(
            (
                await connection.execute(
                    delete(tracked_application_table)
                    .where(
                        tracked_application_table.c.tailoring_run_id == TrackedRunRef(run_id),
                        tracked_application_table.c.user_id == user_id,
                    )
                    .returning(tracked_application_table.c.id)
                )
            ).scalars()
        )
        for card_id in card_ids:
            self._expunge(TrackedApplication, card_id)

        jobs = (
            await connection.execute(
                delete(export_job_table)
                .where(
                    export_job_table.c.tailoring_run_id == typed_run_id,
                    export_job_table.c.user_id == user_id,
                )
                .returning(export_job_table.c.id, export_job_table.c.format)
            )
        ).all()
        export_files: list[FileRef] = []
        for row in jobs:
            job_id: ExportJobId = row.id
            job_format: ExportFormat = row.format
            self._expunge(ExportJob, job_id)
            if job_format in _QUEUED_FORMATS:
                export_files.append(FileRef.for_export(job_id, job_format))

        # Lock the posting, THEN decide in a separate statement (2.3 /verify, reviewer MINOR #1).
        # A run request that authorized this posting holds it `FOR KEY SHARE` from its `INSERT`
        # until its commit (`SqlAlchemyTailoringRunRepository.add`); `FOR UPDATE` waits behind that.
        # The wait is the point: the `DELETE` below is its own statement, so under READ COMMITTED
        # it takes a fresh snapshot *after* the lock is granted and sees a run committed while we
        # waited — the posting is kept. Folded into one statement, the `NOT EXISTS` would read the
        # snapshot from before the wait and delete a posting the new run references.
        # No row here (a foreign or already-gone posting) needs no special case: the `DELETE`
        # below matches nothing either.
        await connection.execute(
            select(job_posting_table.c.id)
            .where(
                job_posting_table.c.id == posting_id,
                job_posting_table.c.user_id == user_id,
            )
            .with_for_update()
        )
        still_referenced = exists(
            select(tailoring_run_table.c.id).where(
                tailoring_run_table.c.job_posting_id == posting_id
            )
        )
        posting = await connection.execute(
            delete(job_posting_table).where(
                job_posting_table.c.id == posting_id,
                job_posting_table.c.user_id == user_id,
                ~still_referenced,
            )
        )
        posting_deleted = posting.rowcount > 0
        if posting_deleted:
            self._expunge(JobPosting, posting_id)

        return DeletedHistoryEntry(
            export_files=tuple(export_files),
            export_jobs=len(jobs),
            posting_deleted=posting_deleted,
            tracked_application_deleted=bool(card_ids),
        )

    def _expunge(self, cls: type[object], ident: object) -> None:
        """Drop `cls(ident)` from the identity map if it is there — never a load."""
        stale = self._session.identity_map.get(identity_key(cls, ident))
        if stale is not None:
            self._session.expunge(stale)


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance. Never executed.
    def _assert_implements_history_entry_data(adapter: SqlAlchemyHistoryEntryData) -> None:
        _: HistoryEntryDataPort = adapter
