"""`TypeDecorator`s round-tripping the `posting` context's value objects (ADR-0007).

One class per value object, written out explicitly rather than behind a generic factory — the same
call `types/intake.py` makes and for the same reason: a reader should be able to see the column type
and the round-trip logic without following an abstraction, and the shapes differ enough (a nullable
`String(2048)`, a non-null `Text`, a closed enum) that a factory would be a thin wrapper hiding the
one interesting line in each.

**Every one of these guards both directions, and the `None` guard on the way out is not boilerplate.**
`source_url` and `title` are genuinely nullable — a pasted posting has no URL and a fetched page may
have no readable title — so `process_result_value` really does receive `None` on ordinary rows, and
a missing guard would call `SourceUrl(None)` and raise on a perfectly valid row.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import String, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from tailorcraft.domain.posting.value_objects import (
    JobPostingId,
    JobPostingText,
    PostingSource,
    PostingTitle,
    SourceUrl,
)


class JobPostingIdType(TypeDecorator[JobPostingId]):
    """`posting_job_posting.id` — a native Postgres `UUID` carrying a `JobPostingId` rather than a
    bare `UUID`, so a query result hands back the typed id the domain works with instead of a
    primitive every caller has to re-wrap (and could re-wrap as the wrong type)."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: JobPostingId | None, dialect: Dialect) -> UUID | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> JobPostingId | None:
        if value is None:
            return None
        return JobPostingId(value)


class SourceUrlType(TypeDecorator[SourceUrl]):
    """`posting_job_posting.source_url` — `VARCHAR(2048)`, `NULL` **iff** `source = 'pasted'` (J-2).

    The 2,048 matches `SourceUrl`'s own length rule rather than being chosen separately, so the
    column cannot refuse a value the type accepted. **This column is PII** (Constitution §8): it
    names a specific job at a specific company that a specific person is applying to, which is why
    nothing logs it and only `SourceUrl.host` is ever allowed near a log line.

    Reconstructing through `SourceUrl(...)` on the way out means a row loaded from the database is
    re-validated by the same rules that admitted it. That is deliberate: it costs a parse per row
    and it means a value hand-written into the table by a migration or a `psql` session cannot
    quietly become a `SourceUrl` that `__post_init__` would have refused.
    """

    impl = String(2048)
    cache_ok = True

    def process_bind_param(self, value: SourceUrl | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> SourceUrl | None:
        if value is None:
            return None
        return SourceUrl(value)


class PostingTitleType(TypeDecorator[PostingTitle]):
    """`posting_job_posting.title` — `VARCHAR(200)`, `NULL` for every pasted posting (J-4) and for a
    fetched page whose metadata carried no usable title."""

    impl = String(200)
    cache_ok = True

    def process_bind_param(self, value: PostingTitle | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> PostingTitle | None:
        if value is None:
            return None
        return PostingTitle(value)


class JobPostingTextType(TypeDecorator[JobPostingText]):
    """`posting_job_posting.text` — `TEXT`, never `NULL`: a `JobPosting` that exists always has
    usable text (J-1, ADR-0013), so there is no state this column can be null in.

    `TEXT` rather than a bounded `VARCHAR`, even though `JobPostingText` caps at 30,000 characters.
    On PostgreSQL the two are the same storage with the same performance, so a length in the column
    type would buy nothing and would be a *second* place the 30,000 lives — one that a change to the
    value object would silently fail to update, turning a rule the domain relaxed into a database
    error nobody predicted.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: JobPostingText | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> JobPostingText | None:
        if value is None:
            return None
        return JobPostingText(value)


class PostingSourceType(TypeDecorator[PostingSource]):
    """`posting_job_posting.source` — `VARCHAR(16)`, **not** a native Postgres `ENUM`.

    Same reasoning as `BaseCvStatusType`: adding a member to a native enum takes a lock, and the
    check constraint plus the value object are the real enforcement anyway. The `TypeDecorator` is
    what keeps a loaded row's `_source` an actual `PostingSource` rather than a bare `str` that
    happens to compare equal — `StrEnum` members bind correctly on the way in without one, which is
    exactly what makes the missing decorator on the way out easy to overlook (the trap
    `CvContentTypeType`'s docstring records).
    """

    impl = String(16)
    cache_ok = True

    def process_bind_param(self, value: PostingSource | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> PostingSource | None:
        if value is None:
            return None
        return PostingSource(value)
