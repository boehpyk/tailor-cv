"""`SqlAlchemyTailoringHistoryQuery` — the `TailoringHistoryQuery` port (slice 2.3, ADR-0024).

A signed-in user's history, one keyset page at a time, in **one statement** (technical plan §0.5):

    SELECT r.id, r.status, r.failure_reason, r.requested_at, r.completed_at, r.version,
           (r.cv_edited_at IS NOT NULL OR r.cover_letter_edited_at IS NOT NULL) AS edited,
           r.base_cv_id,
           p.id, p.source, p.title, p.source_url, left(p.text, 140),
           c.id, c.label, c.original_filename
    FROM tailoring_run r
    LEFT JOIN posting_job_posting p ON p.id = r.job_posting_id AND p.user_id = r.user_id
    LEFT JOIN intake_base_cv     c ON c.id = r.base_cv_id     AND c.user_id = r.user_id
    WHERE r.user_id = :u
      AND (r.requested_at, r.id) < (:cursor_at, :cursor_id)        -- only when a cursor is given
    ORDER BY r.requested_at DESC, r.id DESC
    LIMIT :size + 1

**Core, every column named, and never a document.** No `tailored_cv`, `cover_letter`, `edited_cv`,
`edited_cover_letter`, `extracted_text` or posting `text` enters the result set (AC-55): the preview
is `left(p.text, 140)` computed in SQL, as 2.2's `char_length(extracted_text)` is, so the posting's
full text never leaves the database. Loading three aggregates per row to draw a list is the cost
2.2's T30 measured, and a query port is how this module avoids it without a repository reaching into
two other contexts' tables.

**Two `LEFT JOIN`s, on purpose.** The CV join is how "CV deleted" is *derived* (H-29, ADR-0023): no
row → `base_cv is None`, while the run's own `base_cv_id` is still reported. The posting join is
defensive: a missing posting should be impossible (plan §0.4), but an `INNER JOIN` would make such an
entry invisible to its owner — and therefore undeletable — so it is listed with `posting is None`
and a warning names the run (H-30). **Both joins carry the owner** (`… AND x.user_id = r.user_id`):
a dangling id that one day matches another owner's row can never lend its label to this history.

**Keyset, not `OFFSET`** (H-28): paging on the values `(requested_at, id)` is O(page) with
`ix_tailoring_run_user_id_requested_at` and neither skips nor repeats a row when an entry is added or
deleted between two page loads. `id` breaks ties because the `Clock` is whole-second. One extra row
is fetched to know whether a next page exists; it is never returned.

**Plain strings for title, label and filename.** Each is selected through `type_coerce(…, String)`,
which bypasses its column's `TypeDecorator`: the read model holds `str` (validated on the way in, by
the aggregate that stored it), and building a validating value object per row per page is exactly
the per-load cost 2.2's `ExtractedText` lesson was about. Ids and enums keep their decorators — they
are cheap, and a transposed UUID is still worth a type.

**Nothing here logs user text.** The one log line carries two ids.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog
from sqlalchemy import String, and_, func, literal, or_, select, tuple_, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tailoring.history import (
    HistoryBaseCv,
    HistoryCursor,
    HistoryPage,
    HistoryPageSize,
    HistoryPosting,
    TailoringHistoryEntry,
)
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)

if TYPE_CHECKING:
    from sqlalchemy import Row

    from tailorcraft.domain.tailoring.ports import TailoringHistoryQuery

log = structlog.get_logger(__name__)

# The preview's length in characters — `HistoryPosting.preview`'s bound (ADR-0024: "a preview is 140
# characters, not 30,000"). PostgreSQL's `left` counts characters, not bytes, like Python's slice.
_PREVIEW_CHARACTERS: Final = 140

_r = tailoring_run_table.alias("r")
_p = job_posting_table.alias("p")
_c = base_cv_table.alias("c")


class SqlAlchemyTailoringHistoryQuery:
    """History pages over one `AsyncSession`. Reads only; the unit of work is the caller's."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def page_for_user(
        self, user_id: UserId, after: HistoryCursor | None, size: HistoryPageSize
    ) -> HistoryPage:
        """The port's contract; the statement is the module docstring's."""
        statement = (
            select(
                _r.c.id,
                _r.c.status,
                _r.c.failure_reason,
                _r.c.requested_at,
                _r.c.completed_at,
                _r.c.version,
                or_(_r.c.cv_edited_at.is_not(None), _r.c.cover_letter_edited_at.is_not(None)).label(
                    "edited"
                ),
                _r.c.base_cv_id,
                _r.c.job_posting_id,
                _p.c.id.label("posting_id"),
                _p.c.source.label("posting_source"),
                type_coerce(_p.c.title, String).label("posting_title"),
                type_coerce(_p.c.source_url, String).label("posting_source_url"),
                func.left(type_coerce(_p.c.text, String), _PREVIEW_CHARACTERS).label(
                    "posting_preview"
                ),
                _c.c.id.label("cv_id"),
                type_coerce(_c.c.label, String).label("cv_label"),
                type_coerce(_c.c.original_filename, String).label("cv_original_filename"),
            )
            .select_from(
                _r.outerjoin(
                    _p, and_(_p.c.id == _r.c.job_posting_id, _p.c.user_id == _r.c.user_id)
                ).outerjoin(_c, and_(_c.c.id == _r.c.base_cv_id, _c.c.user_id == _r.c.user_id))
            )
            .where(_r.c.user_id == user_id)
            .order_by(_r.c.requested_at.desc(), _r.c.id.desc())
            .limit(size.value + 1)
        )
        if after is not None:
            # A row-value comparison, so PostgreSQL can use it as an index condition on
            # `(user_id, requested_at DESC, id DESC)` — two `OR`ed column comparisons would be a
            # filter. The bound values go through the columns' own types (`TailoringRunIdType`
            # unwraps the id), so the comparison is `timestamptz, uuid` against the same.
            statement = statement.where(
                tuple_(_r.c.requested_at, _r.c.id)
                < tuple_(
                    literal(after.requested_at, _r.c.requested_at.type),
                    literal(after.tailoring_run_id, _r.c.id.type),
                )
            )

        rows = (await self._session.execute(statement)).all()
        entries = tuple(_entry(row) for row in rows[: size.value])
        next_cursor = (
            HistoryCursor(
                requested_at=entries[-1].requested_at,
                tailoring_run_id=entries[-1].tailoring_run_id,
            )
            if len(rows) > size.value
            else None
        )
        return HistoryPage(entries=entries, next_cursor=next_cursor)


def _entry(row: Row[tuple[object, ...]]) -> TailoringHistoryEntry:
    """One result row as an entry. A `None` join side is the read model's `None`, not an error."""
    posting: HistoryPosting | None = None
    if row.posting_id is not None:
        posting = HistoryPosting(
            job_posting_id=row.posting_id,
            source=row.posting_source,
            title=row.posting_title,
            source_url=row.posting_source_url,
            preview=row.posting_preview,
        )
    else:
        # H-30: should be impossible (plan §0.4), and listed anyway so its owner can delete it. A
        # warning because it means a bug somewhere. Ids only — never a title, a label or a preview.
        log.warning(
            "tailoring.history_posting_missing",
            tailoring_run_id=str(row.id.value),
            job_posting_id=str(row.job_posting_id.value),
        )

    base_cv = (
        HistoryBaseCv(
            base_cv_id=row.cv_id,
            label=row.cv_label,
            original_filename=row.cv_original_filename,
        )
        if row.cv_id is not None
        else None
    )
    return TailoringHistoryEntry(
        tailoring_run_id=row.id,
        status=row.status,
        failure_reason=row.failure_reason,
        requested_at=row.requested_at,
        completed_at=row.completed_at,
        version=row.version,
        edited=row.edited,
        base_cv_id=row.base_cv_id,
        base_cv=base_cv,
        posting=posting,
    )


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance. Never executed.
    def _assert_implements_tailoring_history_query(
        adapter: SqlAlchemyTailoringHistoryQuery,
    ) -> None:
        _: TailoringHistoryQuery = adapter
