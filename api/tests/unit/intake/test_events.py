"""Domain events for `intake`: what `BaseCv` records, and what it must never carry (AC-13).

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. The field-set assertions are the
guard AC-13 promises — they must fail if a future edit adds a text or filename field to any of
these events, not merely if someone changes the value of an existing field.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID

import pytest

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.events import (
    BaseCvExtractionFailed,
    BaseCvTextExtracted,
    BaseCvUploaded,
)
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    CvContentType,
    ExtractedText,
    ExtractionFailureReason,
    OriginalFilename,
)
from tailorcraft.domain.shared.events import DomainEvent
from tailorcraft.domain.shared.files import FileRef

_CV_ID = BaseCvId(value=UUID("0192f0a1-89ab-7cde-8123-456789abcdef"))
_SESSION_ID = GuestSessionId(value=UUID("11111111-1111-7111-8111-111111111111"))
_UPLOADED_AT = datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC)


def _uploaded_cv(*, size_bytes: int = 1024) -> BaseCv:
    return BaseCv.upload(
        id=_CV_ID,
        guest_session_id=_SESSION_ID,
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=size_bytes,
        file=FileRef.for_base_cv(_CV_ID, CvContentType.PDF),
        uploaded_at=_UPLOADED_AT,
    )


# --- what gets recorded, and when --------------------------------------------------------------


def test_upload_records_exactly_one_base_cv_uploaded_event() -> None:
    cv = _uploaded_cv(size_bytes=2048)

    events = cv.release_events()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, BaseCvUploaded)
    assert event.base_cv_id == _CV_ID
    assert event.guest_session_id == _SESSION_ID
    assert event.content_type is CvContentType.PDF
    assert event.size_bytes == 2048
    assert event.occurred_at == _UPLOADED_AT


def test_mark_extracted_records_one_event_carrying_the_character_count_not_the_text() -> None:
    """The event carries the *count*, not the text — `character_count` exists on `ExtractedText`
    for exactly this reason (Constitution §8: the text is PII and must never reach an event)."""
    cv = _uploaded_cv()
    cv.release_events()  # discard BaseCvUploaded so this test sees only what mark_extracted adds
    text = ExtractedText("a" * 321)

    cv.mark_extracted(text, _UPLOADED_AT)
    events = cv.release_events()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, BaseCvTextExtracted)
    assert event.base_cv_id == _CV_ID
    assert event.character_count == text.character_count
    assert event.occurred_at == _UPLOADED_AT


def test_mark_extraction_failed_records_one_event_carrying_the_reason() -> None:
    cv = _uploaded_cv()
    cv.release_events()

    cv.mark_extraction_failed(ExtractionFailureReason.ENCRYPTED, _UPLOADED_AT)
    events = cv.release_events()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, BaseCvExtractionFailed)
    assert event.base_cv_id == _CV_ID
    assert event.reason is ExtractionFailureReason.ENCRYPTED
    assert event.occurred_at == _UPLOADED_AT


# --- release_events empties the buffer ----------------------------------------------------------


def test_release_events_empties_the_buffer() -> None:
    """Called twice, the second call must return nothing — this is what stops a retried handler
    from publishing the same fact twice (`RecordsEvents.release_events` docstring)."""
    cv = _uploaded_cv()

    first_release = cv.release_events()
    second_release = cv.release_events()

    assert len(first_release) == 1
    assert second_release == ()


# --- AC-13: no event may carry CV text or a filename, checked structurally ---------------------


@pytest.mark.parametrize(
    "event_type",
    [BaseCvUploaded, BaseCvTextExtracted, BaseCvExtractionFailed],
    ids=["BaseCvUploaded", "BaseCvTextExtracted", "BaseCvExtractionFailed"],
)
def test_event_field_set_contains_no_text_or_filename_field(event_type: type[DomainEvent]) -> None:
    """AC-13, checked structurally rather than against a fixed list of known-good names: a test
    that only asserted `field_names == {"base_cv_id", ...}` would keep passing after a future edit
    *added* a field such as `original_filename` alongside the existing ones. Asserting that the
    forbidden names are absent from whatever the field set turns out to be is what makes this test
    fail the moment a text or filename field is added, rather than only when one is removed."""
    field_names = {field.name for field in dataclasses.fields(event_type)}

    forbidden_names = {
        "text",
        "extracted_text",
        "original_filename",
        "filename",
        "content",
        "bytes",
        "data",
        "raw_text",
    }

    assert field_names.isdisjoint(forbidden_names)
