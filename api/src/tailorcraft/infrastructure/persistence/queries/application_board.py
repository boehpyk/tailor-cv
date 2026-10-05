"""`SqlAlchemyApplicationBoardQuery` — the `ApplicationBoardQuery` port (slice 3.1, ADR-0024 and its
amendment (a)).

A signed-in user's whole board in **one statement** (technical plan §0.8):

    SELECT a.id, a.tailoring_run_id, a.stage, a.title, a.tracked_at, a.stage_changed_at, a.version,
           r.id AS run_id, r.requested_at,
           (r.cv_edited_at IS NOT NULL OR r.cover_letter_edited_at IS NOT NULL) AS edited,
           p.id, p.source, p.title, p.source_url, left(p.text, 140) AS posting_preview,
           c.id, c.label, c.original_filename
    FROM tracking_application a
    LEFT JOIN tailoring_run       r ON r.id = a.tailoring_run_id AND r.user_id = a.user_id
    LEFT JOIN posting_job_posting p ON p.id = r.job_posting_id   AND p.user_id = a.user_id
    LEFT JOIN intake_base_cv      c ON c.id = r.base_cv_id       AND c.user_id = a.user_id
    WHERE a.user_id = :u
    ORDER BY a.stage_changed_at DESC, a.id DESC

**2.3's history query, one table further out** (`queries/tailoring_history.py`), and the same rules:

**Core, every column named, and never a document** (AC-31). No `tailored_cv`, `cover_letter`,
`edited_cv`, `edited_cover_letter`, `extracted_text` or posting `text` enters the result set: the
preview is `left(p.text, 140)` computed in SQL, so the posting's full text never leaves the database.

**Three `LEFT JOIN`s, each with the owner in its condition** (ADR-0024 decision 3). The CV join is how
"CV deleted" is *derived* — no row, `base_cv is None`, the card still listed. The run join is
defensive: a card whose run is gone should be impossible (the two locks of plan §0.7), but an `INNER
JOIN` would make such a card invisible and therefore unremovable, so it is listed with `run` and
`posting` both `None` and a warning names it (T-36). The owner is `a.user_id` in every condition, so
a dangling id that one day matches another owner's row can never lend its title, label or dates to
this board.

**Unpaginated, deliberately** (ADR-0024 amendment (a)): a Kanban shows every column at once, and the
board is bounded at write time by `MAX_TRACKED_APPLICATIONS_PER_USER`. The order `(stage_changed_at
DESC, id DESC)` is `ix_tracking_application_user_id_stage_changed_at`'s, so the plan reads the index
in order and sorts nothing; `id` breaks ties because the `Clock` is whole-second.

**Plain values for everything owned by another context.** `domain/tracking` imports no sibling
context (ADR-0029 decision 1), so the read model holds bare `UUID`s for the posting and CV ids and a
`str` for the posting source. Each is selected through `type_coerce(…)`, which bypasses its column's
`TypeDecorator` — the same device 2.3 uses for titles, labels and filenames, which also stay `str`
here (validated on the way in; re-validating per card per load is 2.2's `ExtractedText` lesson). The
card's own columns keep their decorators: they are tracking's types.

**Nothing here logs user text.** The one log line carries one id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog
from sqlalchemy import String, and_, func, or_, select, type_coerce
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.value_objects import UserId
from tailorcraft.domain.tracking.board import (
    ApplicationBoard,
    BoardBaseCv,
    BoardCard,
    BoardPosting,
    BoardRun,
)
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.mapping.posting.job_posting import job_posting_table
from tailorcraft.infrastructure.persistence.mapping.tailoring.tailoring_run import (
    tailoring_run_table,
)
from tailorcraft.infrastructure.persistence.mapping.tracking.tracked_application import (
    tracked_application_table,
)

if TYPE_CHECKING:
    from sqlalchemy import Row

    from tailorcraft.domain.tracking.ports import ApplicationBoardQuery

log = structlog.get_logger(__name__)

# The preview's length in characters — `BoardPosting.preview`'s bound, and history's (ADR-0024).
# PostgreSQL's `left` counts characters, not bytes, like Python's slice.
_PREVIEW_CHARACTERS: Final = 140

# A bare UUID out of another context's id column, bypassing its `TypeDecorator` (module docstring).
_UUID: Final = postgresql.UUID(as_uuid=True)

_a = tracked_application_table.alias("a")
_r = tailoring_run_table.alias("r")
_p = job_posting_table.alias("p")
_c = base_cv_table.alias("c")


class SqlAlchemyApplicationBoardQuery:
    """The board over one `AsyncSession`. Reads only; the unit of work is the caller's."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def board_for_user(self, user_id: UserId) -> ApplicationBoard:
        """The port's contract; the statement is the module docstring's."""
        statement = (
            select(
                _a.c.id,
                _a.c.tailoring_run_id,
                _a.c.stage,
                type_coerce(_a.c.title, String).label("title"),
                _a.c.tracked_at,
                _a.c.stage_changed_at,
                _a.c.version,
                type_coerce(_r.c.id, _UUID).label("run_id"),
                _r.c.requested_at,
                or_(_r.c.cv_edited_at.is_not(None), _r.c.cover_letter_edited_at.is_not(None)).label(
                    "edited"
                ),
                type_coerce(_p.c.id, _UUID).label("posting_id"),
                type_coerce(_p.c.source, String).label("posting_source"),
                type_coerce(_p.c.title, String).label("posting_title"),
                type_coerce(_p.c.source_url, String).label("posting_source_url"),
                func.left(type_coerce(_p.c.text, String), _PREVIEW_CHARACTERS).label(
                    "posting_preview"
                ),
                type_coerce(_c.c.id, _UUID).label("cv_id"),
                type_coerce(_c.c.label, String).label("cv_label"),
                type_coerce(_c.c.original_filename, String).label("cv_original_filename"),
            )
            .select_from(
                _a.outerjoin(
                    _r, and_(_r.c.id == _a.c.tailoring_run_id, _r.c.user_id == _a.c.user_id)
                )
                .outerjoin(_p, and_(_p.c.id == _r.c.job_posting_id, _p.c.user_id == _a.c.user_id))
                .outerjoin(_c, and_(_c.c.id == _r.c.base_cv_id, _c.c.user_id == _a.c.user_id))
            )
            .where(_a.c.user_id == user_id)
            .order_by(_a.c.stage_changed_at.desc(), _a.c.id.desc())
        )

        rows = (await self._session.execute(statement)).all()
        return ApplicationBoard(cards=tuple(_card(row) for row in rows))


def _card(row: Row[tuple[object, ...]]) -> BoardCard:
    """One result row as a card. A `None` join side is the read model's `None`, not an error."""
    run: BoardRun | None = None
    if row.run_id is not None:
        run = BoardRun(
            tailoring_run_id=row.tailoring_run_id,
            requested_at=row.requested_at,
            edited=row.edited,
        )
    else:
        # T-36: prevented by the two locks of plan §0.7, and listed anyway so its owner can remove
        # it. A warning because it means a bug somewhere. The card's id only — never a title.
        log.warning("tracking.board_run_missing", tracked_application_id=str(row.id.value))

    posting = (
        BoardPosting(
            job_posting_id=row.posting_id,
            source=row.posting_source,
            title=row.posting_title,
            source_url=row.posting_source_url,
            preview=row.posting_preview,
        )
        if row.posting_id is not None
        else None
    )
    base_cv = (
        BoardBaseCv(
            base_cv_id=row.cv_id,
            label=row.cv_label,
            original_filename=row.cv_original_filename,
        )
        if row.cv_id is not None
        else None
    )
    return BoardCard(
        id=row.id,
        tailoring_run_id=row.tailoring_run_id,
        stage=row.stage,
        title=row.title,
        tracked_at=row.tracked_at,
        stage_changed_at=row.stage_changed_at,
        version=row.version,
        run=run,
        posting=posting,
        base_cv=base_cv,
    )


if TYPE_CHECKING:
    # Makes mypy prove the structural conformance. Never executed.
    def _assert_implements_application_board_query(
        adapter: SqlAlchemyApplicationBoardQuery,
    ) -> None:
        _: ApplicationBoardQuery = adapter
