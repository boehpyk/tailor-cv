"""`TypeDecorator`s round-tripping the `export` context's value objects (ADR-0007).

One class per value object, written out explicitly rather than behind a generic factory — the same
call `types/intake.py`, `types/posting.py` and `types/tailoring.py` all make. By now four modules
have chosen the explicit form over a `def enum_type(cls, length)` helper, and the reason has not
changed: each decorator's docstring is where the *column's* rule is recorded (why this length, why
`NULL` is ordinary here and impossible there, which `CHECK` backs it), and a factory would leave
those rules with nowhere to live but a comment beside the call.

**Two decorators this context needs are deliberately not here.**

- `FileRefType` lives in `types/shared.py`, where 1.1 put it, because `FileRef` lives in
  `domain/shared/files.py`. `export_job.file_key` imports it rather than declaring a second one:
  two decorators for one value object would be two chances for the storage grammar to be
  re-validated by different rules on the way out of two tables, which is exactly the failure the
  grammar exists to prevent.
- `TailoredDocumentKindType` lives in `types/tailoring.py`, because `TailoredDocumentKind` is the
  `tailoring` context's enum. `export_job.document` is its first *column*; 1.4 only ever used the
  kind as a URL path segment and a field on an event, so nothing needed a decorator until now. It
  is the one decorator two contexts share, and it is filed under the context that owns the meaning
  rather than the one that happened to need the column first.

**Three of the four below are enums, and none is a native PostgreSQL `ENUM`** — the same call every
other status and reason column in this schema makes, because adding a member to a native enum takes
a lock. The enforcement is the aggregate's transition table plus the seven `CHECK`s on
`export_job`, not the database's type system.

**The `None` guard runs in both directions on all four**, and on two of them that is load-bearing
rather than boilerplate: an `ExportJob` spends its whole `queued` / `rendering` life with
`failure_reason` `NULL`, and a `ready` job ends with it `NULL` for ever. Those are the ordinary
rows, not an edge case, so `process_result_value` really does receive `None` on most reads and an
unguarded `ExportFailureReason(None)` would raise on a row that is perfectly valid. The bind guard
matters for the same column for the same reason, and both guards are written on the two non-null
columns too, so that no reader has to work out which of the four is which.

**Nothing in this module carries PII, and that is a property of the table rather than of these
classes** (ADR-0016 §4): `export_job` holds ids, enums, integers, instants and a storage key. A
tailored CV is never copied onto a job row, so unlike `TailoredCvType` and `CoverLetterType` these
decorators guard nothing — they are silent anyway, for the reason those two are: a `TypeDecorator`
is exactly the low-level seam where a "just while I debug this" log line survives into production.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import String
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from tailorcraft.domain.export.value_objects import (
    ExportFailureReason,
    ExportFormat,
    ExportJobId,
    ExportJobStatus,
)


class ExportJobIdType(TypeDecorator[ExportJobId]):
    """`export_job.id` — a native Postgres `UUID` carrying an `ExportJobId`, not a bare `UUID`.

    The typed-id argument at its strongest for the second time: `export_job` holds **three** UUID
    columns, its own id plus two references, and two of them (`id` and `tailoring_run_id`) are
    handed to the download handler out of the same URL. With bare `UUID`s, passing the run id to
    `jobs.get(...)` is a lookup that quietly finds nothing; with a decorator per id type, each
    column hands back the type its attribute is annotated with and `mypy --strict` refuses the
    transposition before the code runs.

    The id is also the sole input to the job's storage key (`FileRef.for_export`, XJ-7), so this
    column and `file_key` are two spellings of one fact — which is what AC-23 asserts and what lets
    1.6 reconstruct every file's name from a row, or from an id alone.
    """

    impl = postgresql.UUID(as_uuid=True)
    # Stateless: both methods depend on nothing but the value passed in, so the compiled statement
    # is safe to cache across calls.
    cache_ok = True

    def process_bind_param(self, value: ExportJobId | None, dialect: Dialect) -> UUID | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> ExportJobId | None:
        if value is None:
            return None
        return ExportJobId(value)


class ExportFormatType(TypeDecorator[ExportFormat]):
    """`export_job.format` — `VARCHAR(8)`, never `NULL`, and never `'md'` or `'txt'`.

    8 rather than 16: the set is closed at four members and the longest is `docx`, so the width is
    chosen from the enum rather than rounded up out of habit. The value is the **wire spelling** —
    the same string appears in `?format=pdf`, in the job resource's JSON and in the storage key's
    extension — so this column needs no mapping table to agree with the router or the file store.

    **The column's own `CHECK` is `format IN ('pdf','docx')`, and this decorator is not it.** A job
    may not exist for an inline format (XJ-2), which is a rule about the delivery model and not
    about the string: `ExportFormat` itself admits all four members, because the *query parameter*
    on the inline route is one of them. The three locks on XJ-2 are `ExportJob.request`,
    `FileRef.for_export`, and that `CHECK` — the third being the one that binds a hand-written
    `INSERT`, which is the only reason it exists at all.

    Round-tripping through the enum on the way out is what keeps a loaded job's `_format` an actual
    `ExportFormat` rather than a `str` that compares equal: `format.delivery` and `format.media_type`
    are properties, and a bare string has neither.
    """

    impl = String(8)
    cache_ok = True

    def process_bind_param(self, value: ExportFormat | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> ExportFormat | None:
        if value is None:
            return None
        return ExportFormat(value)


class ExportJobStatusType(TypeDecorator[ExportJobStatus]):
    """`export_job.status` — `VARCHAR(16)`, never `NULL`.

    16 as every other status column in this schema is, for the same reason and with the same
    headroom; the longest member is `rendering` at nine characters.

    The decorator is what keeps a loaded job's `_status` an actual `ExportJobStatus`, and that
    distinction is not cosmetic: every transition guard in `ExportJob` is written with `is` / `in`
    against enum members, and `self._status is ExportJobStatus.RENDERING` is `False` for the string
    `"rendering"` — a loaded job would refuse every legal transition while a `==` comparison
    elsewhere kept insisting the status was right.

    It is also the value the stale sweep renders as a **literal** rather than a bound parameter
    (`SqlAlchemyExportJobRepository.list_stale_rendering`), so that the planner can prove the query's
    `WHERE` implies `ix_export_job_rendering_started_at`'s predicate. The literal is rendered *by
    this decorator*, so it is one spelling of the enum and not a second.
    """

    impl = String(16)
    cache_ok = True

    def process_bind_param(self, value: ExportJobStatus | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> ExportJobStatus | None:
        if value is None:
            return None
        return ExportJobStatus(value)


class ExportFailureReasonType(TypeDecorator[ExportFailureReason]):
    """`export_job.failure_reason` — `VARCHAR(32)`, `NULL` **iff** `status != 'failed'` (XJ-3, and
    the `ck_export_job_failure_reason_matches_status` constraint).

    32 matches `TailoringFailureReasonType`'s width, and the set it holds is wider than that one's
    in members (nine) while shorter in characters (`file_store_unavailable`, twenty-two). The width
    is therefore not a coincidence to preserve — it is headroom for a tenth reason, and the
    constraint that actually enforces the set is Python's, in `ExportFailureReason`.

    **`NULL` is the common case**, which is the whole reason the `None` guard is written out: every
    `queued`, `rendering` and `ready` job has this column empty, and a missing guard on the way out
    would raise on the majority of rows rather than on an exotic one.
    """

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: ExportFailureReason | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(
        self, value: Any | None, dialect: Dialect
    ) -> ExportFailureReason | None:
        if value is None:
            return None
        return ExportFailureReason(value)
