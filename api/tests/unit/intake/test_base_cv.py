"""The `BaseCv` aggregate: invariants I-1...I-10 from technical-plan.md.

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. Every assertion here comes from the
invariant table, not from running the (currently unimplemented) code and recording what it did — a
test written that way would have no source of truth independent of the code it is meant to guard.

**Slice 2.2 additions (AC-2...AC-5) hit a specific wall, named once here rather than at every test
that meets it.** `_assign_owner`'s `UserOwner` arm is `raise NotImplementedError` unconditionally
(T4's skeleton), so **no `UserOwner`-owned `BaseCv` can be constructed at all until T6 lands** — not
even as a precondition for a test of `rename` or `delete`. Every test below that needs a
saved (user-owned) source CV therefore goes red inside its own setup helper (`_saved_source`), on
`NotImplementedError`, before it ever reaches the method under test. That is still a legitimate red
per sdlc.md §2 ("or on the skeleton's `NotImplementedError` where the behaviour is unimplemented") —
it is not an `ImportError`, and it proves the exact thing T6 has to build. Where a test can exercise
its target method directly against a *guest*-owned CV instead (constructible today), it does, so the
red sits on the line the acceptance criterion is actually about.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import assert_never
from uuid import UUID

import pytest

from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.errors import ExtractionAlreadyDecided
from tailorcraft.domain.intake.events import BaseCvDeleted
from tailorcraft.domain.intake.value_objects import (
    BaseCvId,
    BaseCvLabel,
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

# --- Slice 2.2 fixtures ---------------------------------------------------------------------------

_USER_ID = UserId(value=UUID("22222222-2222-7222-8222-222222222222"))
_SOURCE_ID = BaseCvId(value=UUID("33333333-3333-7333-8333-333333333333"))


def _upload(*, size_bytes: int = 1024, at: datetime = _UPLOADED_AT) -> BaseCv:
    """A minimally valid upload, so every test below only names the one thing it is varying."""
    return BaseCv.upload(
        id=_CV_ID,
        owner=GuestOwner(_SESSION_ID),
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=size_bytes,
        file=FileRef.for_base_cv(_CV_ID, CvContentType.PDF),
        uploaded_at=at,
    )


def _saved_uploaded(*, at: datetime = _UPLOADED_AT) -> BaseCv:
    """A `UserOwner`-owned CV, freshly uploaded (`status == UPLOADED`, not yet extracted). Blocked
    today by `_assign_owner`'s `NotImplementedError` for the `UserOwner` arm — see the module
    docstring."""
    return BaseCv.upload(
        id=_SOURCE_ID,
        owner=UserOwner(_USER_ID),
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=2048,
        file=FileRef.for_base_cv(_SOURCE_ID, CvContentType.PDF),
        uploaded_at=at,
    )


def _saved_source(*, at: datetime = _UPLOADED_AT, text: str = "a" * 250) -> BaseCv:
    """A saved base CV: `UserOwner`-owned and `EXTRACTED`."""
    cv = _saved_uploaded(at=at)
    cv.mark_extracted(ExtractedText(text), at)
    cv.release_events()
    return cv


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


# ====================================================================================================
# AC-2: `upload` accepts either `Owner`; `owner` round-trips; `guest_session_id` is gone.
# ====================================================================================================


def test_upload_with_a_guest_owner_round_trips_through_the_owner_property() -> None:
    cv = _upload()

    assert cv.owner == GuestOwner(_SESSION_ID)


def test_upload_with_a_user_owner_round_trips_through_the_owner_property() -> None:
    """The `UserOwner` arm — blocked today by `_assign_owner`'s `NotImplementedError` (module
    docstring)."""
    cv = BaseCv.upload(
        id=_SOURCE_ID,
        owner=UserOwner(_USER_ID),
        original_filename=OriginalFilename("cv.pdf"),
        content_type=CvContentType.PDF,
        size_bytes=1024,
        file=FileRef.for_base_cv(_SOURCE_ID, CvContentType.PDF),
        uploaded_at=_UPLOADED_AT,
    )

    assert cv.owner == UserOwner(_USER_ID)


def test_base_cv_has_no_guest_session_id_property() -> None:
    """AC-2: the 1.1 `guest_session_id` property is removed outright, not merely deprecated —
    every caller reads `owner` instead. This is a structural fact about the class that already holds
    (T4 removed the property), so it is a legitimate green pin rather than a red awaiting GREEN."""
    assert not hasattr(BaseCv, "guest_session_id")
    assert not hasattr(_upload(), "guest_session_id")


def _owner_kind(cv: BaseCv) -> str:
    """AC-2's exhaustiveness mechanism: `match` over `cv.owner` with `assert_never` in the default
    arm. `mypy --strict`, not this function, is what proves a third `Owner` variant would be a
    compile-time error at this `match` — this only proves the two existing variants dispatch."""
    match cv.owner:
        case GuestOwner():
            return "guest"
        case UserOwner():
            return "user"
        case _:
            assert_never(cv.owner)


def test_owner_property_dispatches_exhaustively_for_a_guest_owned_cv() -> None:
    assert _owner_kind(_upload()) == "guest"


# ====================================================================================================
# AC-4: `rename` — only a `UserOwner` CV; `BaseCvLabel`'s own table lives in test_value_objects.py.
# ====================================================================================================


def test_rename_refuses_a_guest_owned_cv() -> None:
    """I-7. `label=None` on purpose (clearing needs no `BaseCvLabel` construction), so this red sits
    on `rename`'s own guard rather than on `BaseCvLabel.__post_init__`."""
    cv = _upload()

    with pytest.raises(InvariantViolated):
        cv.rename(None, _UPLOADED_AT)


def test_rename_sets_the_label_on_a_user_owned_cv() -> None:
    cv = _saved_source()
    label = BaseCvLabel("Senior Backend Role")

    cv.rename(label, _UPLOADED_AT)

    assert cv.label == label


def test_rename_with_none_clears_an_existing_label() -> None:
    cv = _saved_source()
    cv.rename(BaseCvLabel("Senior Backend Role"), _UPLOADED_AT)

    cv.rename(None, _UPLOADED_AT)

    assert cv.label is None


def test_rename_records_no_event() -> None:
    """A label is user text; events carry ids only (AC-4, AC-6)."""
    cv = _saved_source()

    cv.rename(BaseCvLabel("Senior Backend Role"), _UPLOADED_AT)
    events = cv.release_events()

    assert events == ()


# ====================================================================================================
# AC-5: `delete` — only a `UserOwner` CV; records `BaseCvDeleted`.
# ====================================================================================================


def test_delete_refuses_a_guest_owned_cv() -> None:
    """I-10: a guest CV is deleted by the purge, never by a request."""
    cv = _upload()

    with pytest.raises(InvariantViolated):
        cv.delete(_UPLOADED_AT)


def test_delete_records_exactly_one_base_cv_deleted_event_for_a_user_owned_cv() -> None:
    cv = _saved_source()
    at = datetime(2026, 9, 21, 9, 0, 0, tzinfo=UTC)

    cv.delete(at)
    events = cv.release_events()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, BaseCvDeleted)
    assert event.base_cv_id == cv.id
    assert event.owner == UserOwner(_USER_ID)
    assert event.occurred_at == at


def test_delete_does_not_mutate_the_aggregates_own_state() -> None:
    """The docstring is explicit: `delete` records the event; the repository removes the row. A
    `BaseCv` that has been `delete()`-d still reads back its own fields unchanged."""
    cv = _saved_source()

    cv.delete(_UPLOADED_AT)

    assert cv.status is BaseCvStatus.EXTRACTED
    assert cv.owner == UserOwner(_USER_ID)

    assert cv.extracted_at == _UPLOADED_AT
