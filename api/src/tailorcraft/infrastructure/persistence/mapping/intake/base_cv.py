"""The `intake_base_cv` table and the imperative mapping for `BaseCv` (ADR-0007).

As with `identity_guest_session`, every column collides with a read-only `@property` of the same
short name on `BaseCv` (`id`, `status`, `file`, …), so the `properties=` dict below must target the
private attributes (`_id`, `_status`, `_file`, …) that `BaseCv.upload` / `mark_extracted` /
`mark_extraction_failed` actually write. `BaseCv` also composes `RecordsEvents`, which gives every
instance a `_recorded_events` buffer (`domain/shared/events.py`) — that attribute is deliberately
**absent** from both the table and `properties=` here: it is not a persisted fact about the
aggregate, it is an in-memory outbox that `UploadBaseCv` drains via `release_events()` before the
transaction commits. Naming only the eleven mapped attributes below is what keeps SQLAlchemy from
ever trying to instrument it.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    Table,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP

from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.infrastructure.persistence.mapping.identity.guest_session import (
    guest_session_table,
)
from tailorcraft.infrastructure.persistence.registry import mapper_registry, metadata
from tailorcraft.infrastructure.persistence.types.identity import GuestSessionIdType
from tailorcraft.infrastructure.persistence.types.intake import (
    BaseCvIdType,
    BaseCvStatusType,
    CvContentTypeType,
    ExtractedTextType,
    ExtractionFailureReasonType,
    OriginalFilenameType,
)
from tailorcraft.infrastructure.persistence.types.shared import FileRefType

base_cv_table = Table(
    "intake_base_cv",
    metadata,
    Column("id", BaseCvIdType, primary_key=True),
    # `ondelete="CASCADE"` executes ADR-0006's retention rule at the database level: purging an
    # expired `identity_guest_session` row removes every `BaseCv` it owns without a second delete
    # statement from the purge job. Indexed because the same column serves the cascade, the
    # `GET /api/base-cvs` list query and `count_for_session` — the naming convention turns this into
    # `ix_intake_base_cv_guest_session_id`.
    Column(
        "guest_session_id",
        GuestSessionIdType,
        ForeignKey(guest_session_table.c.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    Column("original_filename", OriginalFilenameType, nullable=False),
    Column("content_type", CvContentTypeType, nullable=False),
    Column("size_bytes", Integer, nullable=False),
    # Unique because a `FileRef` addresses exactly one stored file (ADR-0011); two rows pointing at
    # the same key would make "which row owns this file" unanswerable. The naming convention turns
    # this into `uq_intake_base_cv_file_key`.
    Column("file_key", FileRefType, nullable=False, unique=True),
    # `VARCHAR(32)` + `BaseCvStatusType`, deliberately **not** a native Postgres `ENUM`: adding a
    # member to a native enum takes a lock, and this status set is expected to grow as extraction
    # gains richer states (technical-plan.md's persistence section says the same for
    # `extraction_failure_reason` below).
    Column("status", BaseCvStatusType, nullable=False),
    Column("extracted_text", ExtractedTextType, nullable=True),
    Column("extraction_failure_reason", ExtractionFailureReasonType, nullable=True),
    Column("uploaded_at", TIMESTAMP(timezone=True, precision=0), nullable=False),
    Column("extracted_at", TIMESTAMP(timezone=True, precision=0), nullable=True),
    # Mind registry.py's naming convention: `"ck": "ck_%(table_name)s_%(constraint_name)s"` — the
    # `name=` given here is the `constraint_name` slot, so this becomes
    # `ck_intake_base_cv_size_bytes_positive`, matching the plan exactly.
    CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
)

mapper_registry.map_imperatively(
    BaseCv,
    base_cv_table,
    properties={
        "_id": base_cv_table.c.id,
        "_guest_session_id": base_cv_table.c.guest_session_id,
        "_original_filename": base_cv_table.c.original_filename,
        "_content_type": base_cv_table.c.content_type,
        "_size_bytes": base_cv_table.c.size_bytes,
        "_file": base_cv_table.c.file_key,
        "_status": base_cv_table.c.status,
        "_extracted_text": base_cv_table.c.extracted_text,
        "_failure_reason": base_cv_table.c.extraction_failure_reason,
        "_uploaded_at": base_cv_table.c.uploaded_at,
        "_extracted_at": base_cv_table.c.extracted_at,
    },
)
