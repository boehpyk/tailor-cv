"""`TypeDecorator`s round-tripping the `intake` context's value objects (ADR-0007).

One class per value object, written out explicitly rather than behind a generic factory — a reader
should be able to see the column type and the round-trip logic without following an abstraction, and
each value object's validation is different enough (a nullable `Text`, a bounded `String`, a closed
enum) that a factory would just be a thin wrapper hiding the interesting part.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import String, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvStatus,
    CvContentType,
    ExtractedText,
    ExtractionFailureReason,
    OriginalFilename,
)


class BaseCvIdType(TypeDecorator[BaseCvId]):
    """`intake_base_cv.id` — a native Postgres `UUID` carrying a `BaseCvId` rather than a bare
    `UUID`, for the same reason `GuestSessionIdType` exists: a query result should hand back the
    typed id the domain works with, not a primitive every caller has to re-wrap."""

    impl = postgresql.UUID(as_uuid=True)
    cache_ok = True

    def process_bind_param(self, value: BaseCvId | None, dialect: Dialect) -> UUID | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> BaseCvId | None:
        if value is None:
            return None
        return BaseCvId(value)


class OriginalFilenameType(TypeDecorator[OriginalFilename]):
    """`intake_base_cv.original_filename` — `VARCHAR(255)`, never `NULL` (every `BaseCv` has one)."""

    impl = String(255)
    # No dialect-specific behaviour and no external state referenced by `process_bind_param` /
    # `process_result_value` beyond the value itself, so the compiled SQL is safe to cache across
    # invocations — this is the standard "yes" for a stateless `TypeDecorator` (SQLAlchemy docs).
    cache_ok = True

    def process_bind_param(self, value: OriginalFilename | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> OriginalFilename | None:
        if value is None:
            return None
        return OriginalFilename(value)


class ExtractedTextType(TypeDecorator[ExtractedText]):
    """`intake_base_cv.extracted_text` — `TEXT`, `NULL` until extraction succeeds (I-2)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: ExtractedText | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> ExtractedText | None:
        if value is None:
            return None
        return ExtractedText(value)


class BaseCvStatusType(TypeDecorator[BaseCvStatus]):
    """`intake_base_cv.status` — `VARCHAR(32)`, backed by a plain `String` rather than a native
    Postgres `ENUM`: adding a member to a native enum takes a lock, and this set (`uploaded`,
    `extracted`, `extraction_failed`, …) is expected to grow as extraction gets richer failure
    states. A `CHECK` or application-level validation is the enforcement point instead of the
    database type system."""

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: BaseCvStatus | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> BaseCvStatus | None:
        if value is None:
            return None
        return BaseCvStatus(value)


class CvContentTypeType(TypeDecorator[CvContentType]):
    """`intake_base_cv.content_type` — `VARCHAR(128)`, the **sniffed** MIME type.

    `CvContentType` is documented as "an enum, not a value object" (`domain/intake/value_objects.py`
    — the set is closed, decided entirely by `sniff_cv_content_type`), so it is not in T14's itemized
    value-object list. It still needs a `TypeDecorator`: without one, SQLAlchemy would bind the
    `StrEnum` member's raw string on the way in (which happens to work, since `StrEnum` members are
    themselves `str`) but hand back a bare `str` on the way out, leaving `BaseCv._content_type`
    holding something that is not actually a `CvContentType` — `.file_extension` would then raise
    `AttributeError` on a freshly loaded row. Kept in this module, next to `BaseCvStatusType`, because
    it is the same round-trip shape even though it guards an enum rather than a dataclass VO."""

    impl = String(128)
    cache_ok = True

    def process_bind_param(self, value: CvContentType | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(self, value: Any | None, dialect: Dialect) -> CvContentType | None:
        if value is None:
            return None
        return CvContentType(value)


class ExtractionFailureReasonType(TypeDecorator[ExtractionFailureReason]):
    """`intake_base_cv.extraction_failure_reason` — `VARCHAR(64)`, `NULL` unless `status ==
    EXTRACTION_FAILED` (I-2). Same reasoning as `BaseCvStatusType` for staying a plain `String`
    rather than a native enum."""

    impl = String(64)
    cache_ok = True

    def process_bind_param(
        self, value: ExtractionFailureReason | None, dialect: Dialect
    ) -> str | None:
        if value is None:
            return None
        return value.value

    def process_result_value(
        self, value: Any | None, dialect: Dialect
    ) -> ExtractionFailureReason | None:
        if value is None:
            return None
        return ExtractionFailureReason(value)
