"""`TypeDecorator`s round-tripping the `tailoring` context's value objects (ADR-0007).

One class per value object, written out explicitly rather than behind a generic factory — the same
call `types/intake.py` and `types/posting.py` make, and for the same reason: a reader should see the
column type and the round-trip logic without following an abstraction, and the shapes differ enough
(a `UUID`, two closed enums, two large nullable `Text`s, two short nullable provenance strings) that
a factory would be a thin wrapper hiding the one interesting line in each.

**Six of the seven are nullable, and the `None` guard on the way out is load-bearing here rather
than boilerplate.** A `TailoringRun` spends its whole `queued`/`running` life with `failure_reason`,
both documents and both provenance columns `NULL` — those are the ordinary rows, not the edge case —
so `process_result_value` really does receive `None` on most reads, and a missing guard would call
`TailoredCv(None)` and raise on a row that is perfectly valid.

**`TailoredCvType` and `CoverLetterType` carry PII** (Constitution §8): a tailored CV is a person's
employment history rewritten and a cover letter names where they want to work. Nothing logs either
value — only `character_count` — and these two decorators are deliberately silent: no debug log of
what they bound or loaded, because a `TypeDecorator` is exactly the sort of low-level seam where a
"just while I debug this" log line survives into production.

Note what is **not** here: there is no decorator for `LlmCallMetrics` or `TailoredDocuments`. Both
are multi-column value objects, ADR-0007 forbids SQLAlchemy composites, and the resolution (OQ-5) is
that neither is a mapped attribute at all — the aggregate stores seven private scalars and assembles
the two composites on read. `mapping/tailoring/tailoring_run.py` carries the full reasoning at the
seam; the three plain `INTEGER` metric columns therefore need no decorator, because an `int` is
already an `int`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import String, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from tailorcraft.domain.tailoring.value_objects import (
    CoverLetter,
    ModelName,
    PromptVersion,
    TailoredCv,
    TailoringFailureReason,
    TailoringRunId,
    TailoringRunStatus,
)


class TailoringRunIdType(TypeDecorator[TailoringRunId]):
    """`tailoring_run.id` — a native Postgres `UUID` carrying a `TailoringRunId` rather than a bare
    `UUID`.

    The typed-id argument is at its strongest in this table, which holds **four** UUID columns —
    its own id plus three references. With bare `UUID`s a query that transposed two of them would
    load a run pointing at the wrong person's CV with no error anywhere; with a decorator per id
    type, each column hands back the type its attribute is annotated with and `mypy --strict`
    refuses the transposition before the code runs.
    """

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: TailoringRunId | None, dialect: Dialect) -> UUID | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> TailoringRunId | None:
        if value is None:
            return None
        return TailoringRunId(value)


class TailoringRunStatusType(TypeDecorator[TailoringRunStatus]):
    """`tailoring_run.status` — `VARCHAR(16)`, never `NULL`, and **not** a native Postgres `ENUM`.

    Same reasoning `BaseCvStatusType` and `PostingSourceType` carry: adding a member to a native
    enum takes a lock, and the real enforcement is the aggregate's transition table plus the three
    `CHECK` constraints on the table, not the database's type system.

    The decorator is what keeps a loaded row's `_status` an actual `TailoringRunStatus` rather than
    a bare `str` that happens to compare equal. That distinction matters more here than anywhere
    else in the codebase: every transition guard in `TailoringRun` is written with `is` / `in`
    against enum members, and `self._status is TailoringRunStatus.RUNNING` is `False` for the string
    `"running"` — a loaded run would refuse every legal transition while a `==` comparison elsewhere
    kept insisting the status was right.
    """

    impl = String(16)
    cache_ok = True

    def process_bind_param(self, value: TailoringRunStatus | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(
        self, value: Any | None, dialect: Dialect
    ) -> TailoringRunStatus | None:
        if value is None:
            return None
        return TailoringRunStatus(value)


class TailoringFailureReasonType(TypeDecorator[TailoringFailureReason]):
    """`tailoring_run.failure_reason` — `VARCHAR(32)`, `NULL` **iff** `status != 'failed'` (TR-2,
    and the `ck_tailoring_run_failure_reason_matches_status` constraint).

    32 rather than `BaseCvStatusType`'s 64 because this set is closed and its longest member is
    `llm_output_invalid` at eighteen characters; `String` rather than a native enum for the reason
    above.
    """

    impl = String(32)
    cache_ok = True

    def process_bind_param(
        self, value: TailoringFailureReason | None, dialect: Dialect
    ) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(
        self, value: Any | None, dialect: Dialect
    ) -> TailoringFailureReason | None:
        if value is None:
            return None
        return TailoringFailureReason(value)


class TailoredCvType(TypeDecorator[TailoredCv]):
    """`tailoring_run.tailored_cv` — `TEXT`, `NULL` until the run succeeds (TR-2). **PII.**

    `TEXT` rather than a bounded `VARCHAR(20000)` matching `TailoredCv`'s own ceiling, for the
    reason `JobPostingTextType` records: on PostgreSQL the two are the same storage with the same
    performance, so a length here would buy nothing and would put the 20,000 in a *second* place —
    one that a change to the value object would silently fail to update, turning a rule the domain
    relaxed into a database error nobody predicted.

    Reconstructing through `TailoredCv(...)` on the way out re-validates a stored document by the
    same rules that admitted it, which is deliberate: a body hand-written into the table by a
    migration or a `psql` session cannot quietly become a `TailoredCv` that `__post_init__` would
    have refused. It also means the normalization runs again on read — which is free, because
    normalizing an already-normalized document is the identity.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: TailoredCv | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> TailoredCv | None:
        if value is None:
            return None
        return TailoredCv(value)


class CoverLetterType(TypeDecorator[CoverLetter]):
    """`tailoring_run.cover_letter` — `TEXT`, `NULL` until the run succeeds (TR-2). **PII.**

    A separate class from `TailoredCvType` rather than one decorator parameterized by the value
    object, mirroring the domain's decision to keep `TailoredCv` and `CoverLetter` two types with
    two sets of bounds. The two files agree on the same rule: shared shape is not shared behaviour.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: CoverLetter | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> CoverLetter | None:
        if value is None:
            return None
        return CoverLetter(value)


class ModelNameType(TypeDecorator[ModelName]):
    """`tailoring_run.model_name` — `VARCHAR(64)`, `NULL` on every run that has not succeeded.

    The 64 matches `ModelName`'s own length rule rather than being chosen separately, so the column
    cannot refuse a value the type accepted. This is **provenance**: which model wrote the documents
    in this row, recorded after the fact. The domain has no opinion about which model to call — that
    is configuration an adapter reads — so nothing ever writes this column except `mark_succeeded`.
    """

    impl = String(64)
    cache_ok = True

    def process_bind_param(self, value: ModelName | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> ModelName | None:
        if value is None:
            return None
        return ModelName(value)


class PromptVersionType(TypeDecorator[PromptVersion]):
    """`tailoring_run.prompt_version` — `VARCHAR(16)`, `NULL` on every run that has not succeeded.

    The other half of provenance: which prompt wrote the documents in this row. 16 matches
    `PromptVersion`'s own bound, and the value object's closed grammar (`[A-Za-z0-9._-]`) is
    re-applied on the way out — which is the point of round-tripping through the type rather than
    handing back a `str`, since this value is a *key* that a future regression comparison will group
    by, and a key with a space in it groups differently in two of the three places it appears.
    """

    impl = String(16)
    cache_ok = True

    def process_bind_param(self, value: PromptVersion | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> PromptVersion | None:
        if value is None:
            return None
        return PromptVersion(value)
