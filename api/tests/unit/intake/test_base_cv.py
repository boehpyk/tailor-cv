"""The `BaseCv` aggregate: invariants I-1...I-5 from technical-plan.md.

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. Every assertion here comes from the
invariant table, not from running the (currently unimplemented) code and recording what it did — a
test written that way would have no source of truth independent of the code it is meant to guard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from tailorcraft.domain.identity.value_objects import GuestSessionId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import ExtractionAlreadyDecided
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvStatus,
    CvContentType,
    ExtractedText,
    ExtractionFailureReason,
    OriginalFilename,
)
from tailorcraft.domain.shared.errors import InvariantViolated
from tailorcraft.domain.shared.files import FileRef

_CV_ID = BaseCvId(value=UUID("0192f0a1-89ab-7cde-8123-456789abcdef"))
_SESSION_ID = GuestSessionId(value=UUID("11111111-1111-7111-8111-111111111111"))
_UPLOADED_AT = datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC)


def _upload(*, size_bytes: int = 1024, at: datetime = _UPLOADED_AT) -> BaseCv:
    """A minimally valid upload, so every test below only names the one thing it is varying."""
    return BaseCv.upload(
        id=_CV_ID,
        guest_session_id=_SESSION_ID,
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=size_bytes,
        file=FileRef.for_base_cv(_CV_ID, CvContentType.PDF),
        uploaded_at=at,
    )


# --- I-1: size_bytes > 0 ----------------------------------------------------------------------


@pytest.mark.parametrize("size_bytes", [0, -1], ids=["zero", "negative"])
def test_upload_rejects_non_positive_size_bytes(size_bytes: int) -> None:
    """I-1: `size_bytes > 0` is the only thing `upload()` itself validates about the file — the 10
    MB upper bound is a boundary/config rule the domain never sees."""
    with pytest.raises(InvariantViolated):
        _upload(size_bytes=size_bytes)


def test_freshly_uploaded_cv_has_no_extraction_outcome_yet() -> None:
    """The state right after `upload()`, before either `mark_*` method has run."""
    cv = _upload()

    assert cv.status is BaseCvStatus.UPLOADED
    assert cv.extracted_text is None
    assert cv.failure_reason is None
    assert cv.extracted_at is None


# --- I-3: extraction is decided exactly once, in either direction ----------------------------


def test_mark_extracted_twice_raises_extraction_already_decided() -> None:
    cv = _upload()
    cv.mark_extracted(ExtractedText("a" * 200), _UPLOADED_AT)

    with pytest.raises(ExtractionAlreadyDecided):
        cv.mark_extracted(ExtractedText("a" * 200), _UPLOADED_AT)


def test_mark_extraction_failed_twice_raises_extraction_already_decided() -> None:
    cv = _upload()
    cv.mark_extraction_failed(ExtractionFailureReason.CORRUPT, _UPLOADED_AT)

    with pytest.raises(ExtractionAlreadyDecided):
        cv.mark_extraction_failed(ExtractionFailureReason.CORRUPT, _UPLOADED_AT)


def test_mark_extraction_failed_after_mark_extracted_raises_extraction_already_decided() -> None:
    """The guard has to hold in both directions — a success cannot be overwritten by a later
    failure any more than a failure can be overwritten by a later success."""
    cv = _upload()
    cv.mark_extracted(ExtractedText("a" * 200), _UPLOADED_AT)

    with pytest.raises(ExtractionAlreadyDecided):
        cv.mark_extraction_failed(ExtractionFailureReason.CORRUPT, _UPLOADED_AT)


def test_mark_extracted_after_mark_extraction_failed_raises_extraction_already_decided() -> None:
    cv = _upload()
    cv.mark_extraction_failed(ExtractionFailureReason.CORRUPT, _UPLOADED_AT)

    with pytest.raises(ExtractionAlreadyDecided):
        cv.mark_extracted(ExtractedText("a" * 200), _UPLOADED_AT)


# --- I-2: status/extracted_text/failure_reason move together, and never both -----------------


def test_mark_extracted_sets_status_and_text_and_clears_failure_reason() -> None:
    cv = _upload()
    text = ExtractedText("a" * 250)

    cv.mark_extracted(text, _UPLOADED_AT)

    assert cv.status is BaseCvStatus.EXTRACTED
    assert cv.extracted_text == text
    assert cv.failure_reason is None


def test_mark_extraction_failed_sets_status_and_reason_and_leaves_text_none() -> None:
    cv = _upload()

    cv.mark_extraction_failed(ExtractionFailureReason.NO_TEXT_LAYER, _UPLOADED_AT)

    assert cv.status is BaseCvStatus.EXTRACTION_FAILED
    assert cv.failure_reason is ExtractionFailureReason.NO_TEXT_LAYER
    assert cv.extracted_text is None


# --- I-4: extracted_at >= uploaded_at ----------------------------------------------------------


def test_mark_extracted_earlier_than_uploaded_at_raises_invariant_violated() -> None:
    cv = _upload(at=_UPLOADED_AT)
    earlier = _UPLOADED_AT - timedelta(seconds=1)

    with pytest.raises(InvariantViolated):
        cv.mark_extracted(ExtractedText("a" * 200), earlier)


def test_mark_extraction_failed_earlier_than_uploaded_at_raises_invariant_violated() -> None:
    cv = _upload(at=_UPLOADED_AT)
    earlier = _UPLOADED_AT - timedelta(seconds=1)

    with pytest.raises(InvariantViolated):
        cv.mark_extraction_failed(ExtractionFailureReason.CORRUPT, earlier)


def test_mark_extracted_at_the_same_instant_as_uploaded_at_is_allowed() -> None:
    """I-4 is `extracted_at >= uploaded_at` — the equal case is explicitly not a violation, only
    strictly earlier is. An extractor fast enough to finish within the same whole second as the
    upload must not be punished for its speed."""
    cv = _upload(at=_UPLOADED_AT)

    cv.mark_extracted(ExtractedText("a" * 200), _UPLOADED_AT)

    assert cv.extracted_at == _UPLOADED_AT


def test_mark_extraction_failed_at_the_same_instant_as_uploaded_at_is_allowed() -> None:
    cv = _upload(at=_UPLOADED_AT)

    cv.mark_extraction_failed(ExtractionFailureReason.CORRUPT, _UPLOADED_AT)

    assert cv.extracted_at == _UPLOADED_AT
