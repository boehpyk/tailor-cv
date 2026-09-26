"""The `intake_base_cv` table and the imperative mapping for `BaseCv` (ADR-0007).

As with `identity_guest_session`, every column collides with a read-only `@property` of the same
short name on `BaseCv` (`id`, `status`, `file`, …), so the `properties=` dict below must target the
private attributes (`_id`, `_status`, `_file`, …) that `BaseCv.upload` / `mark_extracted` /
`mark_extraction_failed` actually write. `BaseCv` also composes `RecordsEvents`, which gives every
instance a `_recorded_events` buffer (`domain/shared/events.py`) — that attribute is deliberately
**absent** from both the table and `properties=` here: it is not a persisted fact about the
aggregate, it is an in-memory outbox that `UploadBaseCv` drains via `release_events()` before the
transaction commits. Naming only the fourteen mapped attributes below is what keeps SQLAlchemy from
ever trying to instrument it.

**Slice 2.2 (ADR-0022): one aggregate, two owner shapes, one table.** The domain's owner is a sum
type, `GuestOwner | UserOwner`, exactly one. A foreign key has exactly one target table, so the
table stores a *product* — `guest_session_id NULL`, `user_id NULL` — and
`ck_intake_base_cv_exactly_one_owner` restores "exactly one". The CHECK is not the model; it is the
second lock, behind `BaseCv._assign_owner`, for the writes that do not go through the domain (a
hand-written `INSERT`, a backfill, a future adapter bug). The translation between the two shapes
happens in exactly one place, `BaseCv.owner` / `_assign_owner`, and this module only maps the two
private attributes those read and write.
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
from tailorcraft.infrastructure.persistence.mapping.identity.user import user_table
from tailorcraft.infrastructure.persistence.registry import mapper_registry, metadata
from tailorcraft.infrastructure.persistence.types.identity import GuestSessionIdType, UserIdType
from tailorcraft.infrastructure.persistence.types.intake import (
    BaseCvIdType,
    BaseCvLabelType,
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
    # The guest half of the owner. `ondelete="CASCADE"` executes ADR-0006's retention rule at the
    # database level: purging an expired `identity_guest_session` row removes every `BaseCv` it owns
    # without a second delete statement from the purge job. Indexed because the same column serves
    # the cascade, the `GET /api/base-cvs` list query and `count_for_session` — the naming convention
    # turns this into `ix_intake_base_cv_guest_session_id`.
    #
    # **Nullable since 2.2**, and that is what spares a saved CV from the purge *by schema* rather
    # than by a `WHERE` (S-52): a user-owned row has `guest_session_id IS NULL`, so the cascade from
    # `identity_guest_session` has no path to it, and `list_expired`'s `IN (…)` never matches it.
    Column(
        "guest_session_id",
        GuestSessionIdType,
        ForeignKey(guest_session_table.c.id, ondelete="CASCADE"),
        nullable=True,
        index=True,
    ),
    # The user half of the owner (2.2). `ON DELETE CASCADE` so erasing an account takes its saved
    # CVs' rows in the same statement (`SqlAlchemyAccountData.delete_account`); the files are
    # collected beforehand under a row lock on the user (technical plan §0.4). The FK's name,
    # `fk_intake_base_cv_user_id_identity_user`, is load-bearing: `SqlAlchemyBaseCvRepository.add`
    # recognises an upload racing an erasure by it (S-12, AC-32). Indexed
    # (`ix_intake_base_cv_user_id`) because Postgres does not index a referencing column by itself,
    # and `list_for_user`, `count_for_user`, `files_of_account` and the cascade all seek on it.
    Column(
        "user_id",
        UserIdType,
        ForeignKey(user_table.c.id, ondelete="CASCADE"),
        nullable=True,
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
    # `VARCHAR(80)` via `BaseCvLabelType` — `BaseCvLabel`'s own bound, in characters. User text: never
    # logged, never in an event (AC-6).
    Column("label", BaseCvLabelType, nullable=True),
    # **No foreign key, on purpose** (ADR-0022, technical plan §5). A working copy is guest data and
    # its source is user data; an FK would be the one edge in the graph that crosses owners. With
    # `SET NULL`, deleting a saved CV would write to a stranger-lifetime row; with `RESTRICT`, a
    # working copy would block its owner from deleting their own CV. This is provenance — "this was
    # copied from that" — and history does not hold a lock on its subject. It feeds the wire's
    # derived `origin` and is what 2.4's claim reads to decide what to do with copies. No index
    # either: nothing seeks on it.
    Column("copied_from_base_cv_id", BaseCvIdType, nullable=True),
    # Mind registry.py's naming convention: `"ck": "ck_%(table_name)s_%(constraint_name)s"` — the
    # `name=` given here is the `constraint_name` slot, so this becomes
    # `ck_intake_base_cv_size_bytes_positive`, matching the plan exactly.
    CheckConstraint("size_bytes > 0", name="size_bytes_positive"),
    # I-6's second lock: the product of two nullable owner columns has four states and the domain's
    # sum type two. `num_nonnulls` refuses both others — two owners, and none — in one expression
    # that stays correct if a third owner column is ever added to its argument list.
    # `ck_intake_base_cv_exactly_one_owner`.
    CheckConstraint("num_nonnulls(guest_session_id, user_id) = 1", name="exactly_one_owner"),
    # I-7's second lock: a label only on a saved CV. Deliberately **no** "copied ⇒ guest-owned"
    # CHECK beside it — 2.4's claim may re-key a working copy to its user, and 2.2 must not make that
    # choice illegal (technical plan §6). `ck_intake_base_cv_label_only_when_user_owned`.
    CheckConstraint("label IS NULL OR user_id IS NOT NULL", name="label_only_when_user_owned"),
)

mapper_registry.map_imperatively(
    BaseCv,
    base_cv_table,
    properties={
        "_id": base_cv_table.c.id,
        # The two halves of the owner (ADR-0022): two attributes, one fact. `BaseCv._assign_owner`
        # is their only writer and `BaseCv.owner` their only reader — see the class comment there.
        "_owner_guest_session_id": base_cv_table.c.guest_session_id,
        "_owner_user_id": base_cv_table.c.user_id,
        "_label": base_cv_table.c.label,
        "_copied_from": base_cv_table.c.copied_from_base_cv_id,
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
