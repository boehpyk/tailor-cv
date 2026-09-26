"""Domain events for `intake`: what `BaseCv` records, and what it must never carry (AC-13, amended
by slice 2.2's AC-6).

Pure domain tests: no I/O, no fixtures, no event loop, no mocks. The field-set assertions are the
guard AC-13 promises — they must fail if a future edit adds a text or filename field to any of
these events, not merely if someone changes the value of an existing field.

**Amended for AC-6 (slice 2.2), in this RED commit rather than the GREEN one that will make T6's
new events real** (CLAUDE.md: "a test edited in the commit that made it pass is the failure this
cycle exists to prevent" — this edit precedes GREEN, so it is not that failure). Two changes:
`BaseCvCopied` and `BaseCvDeleted` join the parametrized field-set sweep, and `"label"` joins the
forbidden-name set — AC-6 is explicit that "no event carries a filename, a label, extracted text or
bytes," and the pre-2.2 list only ever forbade a filename. Both new events are pure data that
shipped complete in T4, so this addition is legitimately green already, exactly like the three
existing rows in the same parametrize — it still earns its place, because without it a future edit
adding a `label` field to any of these five events would pass silently.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID

import pytest

from tailorcraft.domain.identity.ownership import GuestOwner, UserOwner
from tailorcraft.domain.identity.value_objects import GuestSessionId, UserId
from tailorcraft.domain.intake.base_cv import BaseCv
from tailorcraft.domain.intake.events import (
    BaseCvCopied,
    BaseCvDeleted,
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
        owner=GuestOwner(_SESSION_ID),
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
    assert event.owner == GuestOwner(_SESSION_ID)
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


# --- AC-6: BaseCvCopied / BaseCvDeleted are pinned by exact field set --------------------------
#
# Direct dataclass construction, not via `BaseCv.copy_from`/`delete` — both events shipped complete
# in T4 (pure data, nothing deferred to GREEN), so this pins the exact shape the other contexts'
# `test_events.py` files pin for their own new events (`field_names == {...}`), independent of
# whether `copy_from`/`delete` themselves are implemented yet (they are not — see test_base_cv.py).


def test_base_cv_copied_field_set_is_pinned() -> None:
    field_names = {field.name for field in dataclasses.fields(BaseCvCopied)}

    assert field_names == {"base_cv_id", "source_base_cv_id", "owner", "occurred_at"}


def test_base_cv_copied_owner_is_typed_guest_owner_not_the_general_union() -> None:
    """The copy's owner is always a guest workspace (AC-3): `BaseCvCopied` narrows to `GuestOwner`
    rather than accepting the general `Owner` union, so a `UserOwner`-owned copy is unconstructable
    as an event, not merely disallowed by convention."""
    event = BaseCvCopied(
        base_cv_id=_CV_ID,
        source_base_cv_id=_CV_ID,
        owner=GuestOwner(_SESSION_ID),
        occurred_at=_UPLOADED_AT,
    )

    assert event.owner == GuestOwner(_SESSION_ID)


def test_base_cv_deleted_field_set_is_pinned() -> None:
    field_names = {field.name for field in dataclasses.fields(BaseCvDeleted)}

    assert field_names == {"base_cv_id", "owner", "occurred_at"}


def test_base_cv_deleted_owner_is_typed_user_owner_not_the_general_union() -> None:
    """I-10: a guest CV is deleted by the purge, never by a request, so `BaseCvDeleted.owner` is
    narrowed to `UserOwner` — the event that would say otherwise cannot be constructed."""
    user_id = UserId(value=UUID("99999999-9999-7999-8999-999999999999"))

    event = BaseCvDeleted(base_cv_id=_CV_ID, owner=UserOwner(user_id), occurred_at=_UPLOADED_AT)

    assert event.owner == UserOwner(user_id)


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
    [BaseCvUploaded, BaseCvTextExtracted, BaseCvExtractionFailed, BaseCvCopied, BaseCvDeleted],
    ids=[
        "BaseCvUploaded",
        "BaseCvTextExtracted",
        "BaseCvExtractionFailed",
        "BaseCvCopied",
        "BaseCvDeleted",
    ],
)
def test_event_field_set_contains_no_text_filename_or_label_field(
    event_type: type[DomainEvent],
) -> None:
    """AC-13, amended for AC-6: checked structurally rather than against a fixed list of known-good
    names, so a test that only asserted `field_names == {"base_cv_id", ...}` would keep passing after
    a future edit *added* a field such as `original_filename` alongside the existing ones. Asserting
    that the forbidden names are absent from whatever the field set turns out to be is what makes
    this test fail the moment a text, filename or label field is added, rather than only when one is
    removed. `"label"` is new in 2.2 — AC-6's own wording: "no event carries a filename, a label,
    extracted text or bytes.\""""
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
        "label",
    }

    assert field_names.isdisjoint(forbidden_names)
