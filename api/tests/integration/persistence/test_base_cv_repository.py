"""Persistence tests for `BaseCv` — the imperative mapping, its `TypeDecorator`s and
`SqlAlchemyBaseCvRepository` against real PostgreSQL (T28, written **after**).

Every assertion states what technical-plan.md's persistence section and `domain/intake/base_cv.py`'s
invariants (I-1…I-5) say should happen, not what a first run of the code produced.

As in the `GuestSession` module, `session.expunge_all()` forces a genuine reload before every
round-trip assertion — otherwise the ORM's identity map would hand back the exact object `add()`
was given, proving nothing about the mapping or the `TypeDecorator`s.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tailorcraft.domain.identity.guest_session import GuestSession
from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import BaseCvNotFound
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvStatus,
    CvContentType,
    ExtractedText,
    ExtractionFailureReason,
    OriginalFilename,
)
from tailorcraft.domain.shared.files import FileRef
from tailorcraft.infrastructure.clock import FixedClock
from tailorcraft.infrastructure.identifiers import uuid7
from tailorcraft.infrastructure.persistence.mapping.intake.base_cv import base_cv_table
from tailorcraft.infrastructure.persistence.repositories.identity.guest_session import (
    SqlAlchemyGuestSessionRepository,
)
from tailorcraft.infrastructure.persistence.repositories.intake.base_cv import (
    SqlAlchemyBaseCvRepository,
)

# --- Test helpers --------------------------------------------------------------------------------


async def _persist_owner(
    session: AsyncSession, clock: FixedClock, *, token_hash: str
) -> GuestSession:
    """A `BaseCv` needs a real, persisted `GuestSession` to satisfy the `NOT NULL` FK
    (`intake_base_cv.guest_session_id`) — every test below builds one first."""
    repo = SqlAlchemyGuestSessionRepository(session)
    owner = GuestSession.start(
        id=repo.next_identity(), token_hash=token_hash, at=clock.now(), ttl_hours=24
    )
    await repo.add(owner)
    return owner


def _upload(
    cvs: SqlAlchemyBaseCvRepository,
    owner_id: GuestSessionId,
    clock: FixedClock,
    *,
    content_type: CvContentType = CvContentType.PDF,
    filename: str = "cv.pdf",
    size_bytes: int = 1234,
) -> BaseCv:
    cv_id = cvs.next_identity()
    return BaseCv.upload(
        id=cv_id,
        guest_session_id=owner_id,
        original_filename=OriginalFilename(filename),
        content_type=content_type,
        size_bytes=size_bytes,
        file=FileRef.for_base_cv(cv_id, content_type),
        uploaded_at=clock.now(),
    )


# --- Mapping round-trip: every value object comes back as its own type ---------------------------


async def test_round_trip_of_an_extracted_cv_preserves_value_object_types(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="1" * 64)
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _upload(cvs, owner.id, clock, size_bytes=1234)
    text = ExtractedText("word " * 200)
    cv.mark_extracted(text, clock.now())
    await cvs.add(cv)

    session.expunge_all()
    reloaded = await cvs.get(cv.id)

    assert isinstance(reloaded.id, BaseCvId)
    assert reloaded.id == cv.id
    assert isinstance(reloaded.guest_session_id, GuestSessionId)
    assert reloaded.guest_session_id == owner.id
    assert isinstance(reloaded.original_filename, OriginalFilename)
    assert reloaded.original_filename == cv.original_filename
    assert isinstance(reloaded.content_type, CvContentType)
    assert reloaded.content_type is CvContentType.PDF
    assert reloaded.size_bytes == 1234
    assert isinstance(reloaded.file, FileRef)
    assert reloaded.file == cv.file
    assert isinstance(reloaded.status, BaseCvStatus)
    assert reloaded.status is BaseCvStatus.EXTRACTED
    assert isinstance(reloaded.extracted_text, ExtractedText)
    assert reloaded.extracted_text == text
    assert reloaded.failure_reason is None


async def test_round_trip_of_a_failed_extraction_preserves_the_failure_reason_type(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="2" * 64)
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _upload(cvs, owner.id, clock)
    cv.mark_extraction_failed(ExtractionFailureReason.ENCRYPTED, clock.now())
    await cvs.add(cv)

    session.expunge_all()
    reloaded = await cvs.get(cv.id)

    assert reloaded.status is BaseCvStatus.EXTRACTION_FAILED
    assert reloaded.extracted_text is None
    assert isinstance(reloaded.failure_reason, ExtractionFailureReason)
    assert reloaded.failure_reason is ExtractionFailureReason.ENCRYPTED


# --- `TypeDecorator` round-trips including NULL (invariant I-2, both directions) -----------------


async def test_round_trip_of_a_freshly_uploaded_cv_leaves_both_extraction_columns_null(
    session: AsyncSession, clock: FixedClock
) -> None:
    """I-2's third state: `status == UPLOADED` means neither `extracted_text` nor
    `extraction_failure_reason` has been decided yet. Both `ExtractedTextType` and
    `ExtractionFailureReasonType` must round-trip `NULL` cleanly, and `extracted_at` (nullable
    `TIMESTAMP`) along with them."""
    owner = await _persist_owner(session, clock, token_hash="3" * 64)
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _upload(cvs, owner.id, clock, content_type=CvContentType.TXT, filename="cv.txt")
    await cvs.add(cv)

    session.expunge_all()
    reloaded = await cvs.get(cv.id)

    assert reloaded.status is BaseCvStatus.UPLOADED
    assert reloaded.extracted_text is None
    assert reloaded.failure_reason is None
    assert reloaded.extracted_at is None


# --- Whole-second timestamp fidelity ---------------------------------------------------------------


async def test_round_trip_preserves_whole_second_timestamps(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner = await _persist_owner(session, clock, token_hash="4" * 64)
    cvs = SqlAlchemyBaseCvRepository(session)
    cv = _upload(cvs, owner.id, clock)
    cv.mark_extracted(ExtractedText("word " * 200), clock.now())
    await cvs.add(cv)

    session.expunge_all()
    reloaded = await cvs.get(cv.id)

    assert reloaded.uploaded_at.microsecond == 0
    assert reloaded.extracted_at is not None
    assert reloaded.extracted_at.microsecond == 0


# --- Repository behaviour ---------------------------------------------------------------------------


async def test_get_raises_not_found_for_an_unknown_id(session: AsyncSession) -> None:
    cvs = SqlAlchemyBaseCvRepository(session)

    with pytest.raises(BaseCvNotFound):
        await cvs.get(cvs.next_identity())


async def test_count_for_session_counts_only_that_sessions_rows(
    session: AsyncSession, clock: FixedClock
) -> None:
    owner_a = await _persist_owner(session, clock, token_hash="5" * 64)
    owner_b = await _persist_owner(session, clock, token_hash="6" * 64)
    cvs = SqlAlchemyBaseCvRepository(session)

    for owner in (owner_a, owner_a, owner_b):
        await cvs.add(_upload(cvs, owner.id, clock))

    assert await cvs.count_for_session(owner_a.id) == 2
    assert await cvs.count_for_session(owner_b.id) == 1


async def test_list_for_session_is_scoped_and_ordered_newest_first(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`list_for_session`'s docstring is explicit: newest first, so a guest who has just uploaded a
    second CV sees it above the first. A second session's rows must never appear at all (F-20's
    authorization rule depends on this same scoping)."""
    owner_a = await _persist_owner(session, clock, token_hash="7" * 64)
    owner_b = await _persist_owner(session, clock, token_hash="8" * 64)
    cvs = SqlAlchemyBaseCvRepository(session)

    first = _upload(cvs, owner_a.id, clock, filename="first.pdf")
    await cvs.add(first)

    clock.advance(60)
    second = _upload(cvs, owner_a.id, clock, filename="second.pdf")
    await cvs.add(second)

    # a different session's CV must never appear in owner_a's list, regardless of ordering
    await cvs.add(_upload(cvs, owner_b.id, clock, filename="other.pdf"))

    result = await cvs.list_for_session(owner_a.id)

    assert [item.id for item in result] == [second.id, first.id]


async def test_file_key_is_unique(session: AsyncSession, clock: FixedClock) -> None:
    """`uq_intake_base_cv_file_key` — makes "two rows, one file" unrepresentable at the database
    level (technical-plan.md's persistence table)."""
    owner = await _persist_owner(session, clock, token_hash="9" * 64)
    cvs = SqlAlchemyBaseCvRepository(session)
    first = _upload(cvs, owner.id, clock)
    await cvs.add(first)

    # a second aggregate, deliberately given the first one's exact `FileRef` — the scenario the
    # unique constraint exists to make impossible, since ordinarily `FileRef.for_base_cv` derives a
    # distinct key from a distinct id.
    duplicate_id = cvs.next_identity()
    duplicate = BaseCv.upload(
        id=duplicate_id,
        guest_session_id=owner.id,
        original_filename=OriginalFilename("dup.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=1,
        file=first.file,
        uploaded_at=clock.now(),
    )
    session.add(duplicate)

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_size_bytes_positive_check_constraint(
    session: AsyncSession, clock: FixedClock
) -> None:
    """`ck_intake_base_cv_size_bytes_positive`. `BaseCv.upload()` already refuses `size_bytes <= 0`
    at the domain level (I-1), so hitting this constraint means going around the aggregate entirely
    and inserting the row by hand — proving the database itself, not merely the domain, refuses it
    (defence in depth, matching `LocalFileStore`'s containment check).

    Every column below is `TypeDecorator`-backed, so the values handed to `.values(...)` are the
    domain types each decorator's `process_bind_param` expects (`BaseCvId`, `CvContentType`, …) —
    not the bare primitives underneath them, the same lesson `test_schema.py`'s cascade test
    needed."""
    owner = await _persist_owner(session, clock, token_hash="0" * 64)
    bad_cv_id = BaseCvId(uuid7())

    with pytest.raises(IntegrityError):
        await session.execute(
            base_cv_table.insert().values(
                id=bad_cv_id,
                guest_session_id=owner.id,
                original_filename=OriginalFilename("cv.pdf"),
                content_type=CvContentType.PDF,
                size_bytes=0,
                file_key=FileRef.for_base_cv(bad_cv_id, CvContentType.PDF),
                status=BaseCvStatus.UPLOADED,
                extracted_text=None,
                extraction_failure_reason=None,
                uploaded_at=clock.now(),
                extracted_at=None,
            )
        )
